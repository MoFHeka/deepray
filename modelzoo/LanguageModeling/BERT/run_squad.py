# Copyright (c) 2021, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Run BERT on SQuAD 1.1 and SQuAD 2.0 in tf2.0."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import os
import shutil
import subprocess
import sys
import time

import numpy as np
import tensorflow as tf
from absl import app, flags, logging
from dllogger import Verbosity

from deepray.utils import export
from deepray.core.base_trainer import Trainer
from deepray.core.common import distribution_utils
from deepray.datasets import tokenization
from deepray.datasets.squad import Squad
# word-piece tokenizer based squad_lib
# sentence-piece tokenizer based squad_lib
from deepray.datasets.squad import squad_lib_sp, squad_lib as squad_lib_wp
from deepray.layers.nlp import bert_models, bert_modeling as modeling
from deepray.utils.flags import common_flags
from deepray.utils.horovod_utils import is_main_process
# from optimization import create_optimizer

FLAGS = flags.FLAGS

MODEL_CLASSES = {
    'bert': (modeling.BertConfig, squad_lib_wp, tokenization.FullTokenizer),
    'albert': (modeling.AlbertConfig, squad_lib_sp, tokenization.FullSentencePieceTokenizer),
}

DTYPE_MAP = {
    "fp16": tf.float16,
    "bf16": tf.bfloat16,
    "fp32": tf.float32,
}


def get_raw_results(predictions):
  """Converts multi-replica predictions to RawResult."""
  squad_lib = MODEL_CLASSES[FLAGS.model_name][1]
  for unique_ids, start_logits, end_logits in zip(
      predictions['unique_ids'], predictions['start_positions'], predictions['end_positions']
  ):
    for values in zip(unique_ids.numpy(), start_logits.numpy(), end_logits.numpy()):
      yield squad_lib.RawResult(unique_id=values[0], start_logits=values[1].tolist(), end_logits=values[2].tolist())


def predict_squad_customized(input_meta_data, bert_config, predict_tfrecord_path, num_steps):
  """Make predictions using a Bert-based squad model."""
  data_pipe = Squad(max_seq_length=input_meta_data['max_seq_length'], dataset_type="squad")
  predict_dataset = data_pipe(predict_tfrecord_path, FLAGS.predict_batch_size, is_training=False)

  strategy = distribution_utils.get_distribution_strategy()
  predict_iterator = distribution_utils.make_distributed_iterator(strategy, predict_dataset)

  if FLAGS.mode == 'trt_predict':
    squad_model = export.TFTRTModel(FLAGS.savedmodel_dir, "amp" if common_flags.use_float16() else "fp32")

  elif FLAGS.mode == 'sm_predict':
    squad_model = export.SavedModel(FLAGS.savedmodel_dir, "amp" if common_flags.use_float16() else "fp32")

  else:
    with distribution_utils.get_strategy_scope(strategy):
      squad_model, _ = bert_models.squad_model(
          bert_config, input_meta_data['max_seq_length'], float_type=DTYPE_MAP[FLAGS.dtype]
      )

    if FLAGS.init_checkpoint:
      checkpoint = tf.train.Checkpoint(model=squad_model)
      checkpoint.restore(FLAGS.init_checkpoint).expect_partial()

    checkpoint_path = tf.train.latest_checkpoint(FLAGS.model_dir)
    logging.info('Restoring checkpoints from %s', checkpoint_path)
    checkpoint = tf.train.Checkpoint(model=squad_model)
    checkpoint.restore(checkpoint_path).expect_partial()

  @tf.function
  def predict_step(iterator):
    """Predicts on distributed devices."""

    def _replicated_step(inputs):
      """Replicated prediction calculation."""
      x, _ = inputs
      unique_ids = x.pop('unique_ids')
      if FLAGS.benchmark:
        t0 = tf.timestamp()
        unique_ids = t0
      logits_dict = squad_model(x, training=False)
      logits_dict['unique_ids'] = unique_ids
      logits_dict.update(unique_ids=unique_ids)
      return logits_dict

    def tuple_fun(x):
      return x,

    if strategy:
      outputs = strategy.run(_replicated_step, args=(next(iterator),))
      map_func = strategy.experimental_local_results
    else:
      outputs = _replicated_step(next(iterator),)
      map_func = tuple_fun
    return tf.nest.map_structure(map_func, outputs)

  all_results = []
  time_list = []
  eval_start_time = time.time()
  elapsed_secs = 0

  for _ in range(num_steps):
    predictions = predict_step(predict_iterator)
    if FLAGS.benchmark:
      # transfer tensor to CPU for synchronization
      t0 = predictions['unique_ids'][0]
      start_logits = predictions['start_positions'][0]
      start_logits.numpy()
      elapsed_secs = time.time() - t0.numpy()
      # Removing first 4 (arbitrary) number of startup iterations from perf evaluations
      if _ > 3:
        time_list.append(elapsed_secs)
      continue

    for result in get_raw_results(predictions):
      all_results.append(result)

    if len(all_results) % 100 == 0:
      logging.info('Made predictions for %d records.', len(all_results))

  eval_time_elapsed = time.time() - eval_start_time
  logging.info("-----------------------------")
  logging.info("Summary Inference Statistics")
  logging.info("Batch size = %d", FLAGS.predict_batch_size)
  logging.info("Sequence Length = %d", input_meta_data['max_seq_length'])
  logging.info("Precision = %s", FLAGS.dtype)
  logging.info(
      "Total Inference Time = %0.2f for Sentences = %d", eval_time_elapsed, num_steps * FLAGS.predict_batch_size
  )

  if FLAGS.benchmark:
    eval_time_wo_overhead = sum(time_list)
    time_list.sort()
    num_sentences = (num_steps - 4) * FLAGS.predict_batch_size

    avg = np.mean(time_list)
    cf_50 = max(time_list[:int(len(time_list) * 0.50)])
    cf_90 = max(time_list[:int(len(time_list) * 0.90)])
    cf_95 = max(time_list[:int(len(time_list) * 0.95)])
    cf_99 = max(time_list[:int(len(time_list) * 0.99)])
    cf_100 = max(time_list[:int(len(time_list) * 1)])
    ss_sentences_per_second = num_sentences * 1.0 / eval_time_wo_overhead

    logging.info(
        "Total Inference Time W/O Overhead = %0.2f for Sequences = %d", eval_time_wo_overhead,
        (num_steps - 4) * FLAGS.predict_batch_size
    )
    logging.info("Latency Confidence Level 50 (ms) = %0.2f", cf_50 * 1000)
    logging.info("Latency Confidence Level 90 (ms) = %0.2f", cf_90 * 1000)
    logging.info("Latency Confidence Level 95 (ms) = %0.2f", cf_95 * 1000)
    logging.info("Latency Confidence Level 99 (ms) = %0.2f", cf_99 * 1000)
    logging.info("Latency Confidence Level 100 (ms) = %0.2f", cf_100 * 1000)
    logging.info("Latency Average (ms) = %0.2f", avg * 1000)
    logging.info("Throughput Average (sequences/sec) = %0.2f", ss_sentences_per_second)

    dllogging = input_meta_data['dllogging']
    dllogging.logger.log(step=(), data={"throughput_val": ss_sentences_per_second}, verbosity=Verbosity.DEFAULT)

  logging.info("-----------------------------")

  return all_results


def train_squad(input_meta_data,):
  """Run bert squad training."""

  bert_config = MODEL_CLASSES[FLAGS.model_name][0].from_json_file(FLAGS.config_file)
  max_seq_length = input_meta_data['max_seq_length']

  # The original BERT model does not scale the loss by
  # 1/num_replicas_in_sync. It could be an accident. So, in order to use
  # the same hyper parameter, we do the same thing here by keeping each
  # replica loss as it is.
  strategy = distribution_utils.get_distribution_strategy()

  with distribution_utils.get_strategy_scope(strategy):
    """Get Squad model and optimizer."""
    squad_model, core_model = bert_models.squad_model(
        bert_config, max_seq_length, float_type=DTYPE_MAP[FLAGS.dtype], hub_module_url=FLAGS.hub_module_url
    )

  data_pipe = Squad(
      max_seq_length=max_seq_length,
      dataset_type="squad",
  )
  train_input = data_pipe(
      FLAGS.train_data,
      FLAGS.batch_size,
      is_training=True,
  )

  trainer = Trainer(
      model={
          "main": squad_model,
          "sub_model": core_model
      },
      # optimizer= create_optimizer(
      #   learning_rate, steps_per_epoch * epochs, warmup_steps, FLAGS.optimizer_type),
      optimizer=tf.keras.optimizers.Adam(learning_rate=FLAGS.learning_rate, amsgrad=False),
      loss={
          "start_positions": tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
          "end_positions": tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True)
      },
      loss_weights={
          "start_positions": 0.5,
          "end_positions": 0.5
      },
  )
  trainer.fit(train_input=train_input,)
  export.export_to_savedmodel(model=trainer.models)


def predict_squad(input_meta_data):
  """Makes predictions for a squad dataset."""

  config_cls, squad_lib, tokenizer_cls = MODEL_CLASSES[FLAGS.model_name]
  bert_config = config_cls.from_json_file(FLAGS.config_file)
  if tokenizer_cls == tokenization.FullTokenizer:
    tokenizer = tokenizer_cls(vocab_file=FLAGS.vocab_file, do_lower_case=FLAGS.do_lower_case)
  else:
    assert tokenizer_cls == tokenization.FullSentencePieceTokenizer
    tokenizer = tokenizer_cls(sp_model_file=FLAGS.sp_model_file)
  # Whether data should be in Ver 2.0 format.
  version_2_with_negative = input_meta_data.get('version_2_with_negative', False)
  eval_examples = squad_lib.read_squad_examples(
      input_file=FLAGS.predict_file, is_training=False, version_2_with_negative=version_2_with_negative
  )

  eval_writer = squad_lib.FeatureWriter(filename=os.path.join(FLAGS.model_dir, 'eval.tf_record'), is_training=False)
  eval_features = []

  def _append_feature(feature, is_padding):
    if not is_padding:
      eval_features.append(feature)
    eval_writer.process_feature(feature)

  # TPU requires a fixed batch size for all batches, therefore the number
  # of examples must be a multiple of the batch size, or else examples
  # will get dropped. So we pad with fake examples which are ignored
  # later on.
  kwargs = dict(
      examples=eval_examples,
      tokenizer=tokenizer,
      max_seq_length=input_meta_data['max_seq_length'],
      doc_stride=input_meta_data['doc_stride'],
      max_query_length=input_meta_data['max_query_length'],
      is_training=False,
      output_fn=_append_feature,
      batch_size=FLAGS.predict_batch_size
  )

  # squad_lib_sp requires one more argument 'do_lower_case'.
  if squad_lib == squad_lib_sp:
    kwargs['do_lower_case'] = FLAGS.do_lower_case
  dataset_size = squad_lib.convert_examples_to_features(**kwargs)
  eval_writer.close()

  logging.info('***** Running predictions *****')
  logging.info('  Num orig examples = %d', len(eval_examples))
  logging.info('  Num split examples = %d', len(eval_features))
  logging.info('  Batch size = %d', FLAGS.predict_batch_size)

  num_steps = int(dataset_size / FLAGS.predict_batch_size)
  if FLAGS.benchmark and num_steps > 1000:
    num_steps = 1000
  all_results = predict_squad_customized(input_meta_data, bert_config, eval_writer.filename, num_steps)

  if FLAGS.benchmark:
    return

  output_prediction_file = os.path.join(FLAGS.model_dir, 'predictions.json')
  output_nbest_file = os.path.join(FLAGS.model_dir, 'nbest_predictions.json')
  output_null_log_odds_file = os.path.join(FLAGS.model_dir, 'null_odds.json')

  squad_lib.write_predictions(
      eval_examples,
      eval_features,
      all_results,
      FLAGS.n_best_size,
      FLAGS.max_answer_length,
      FLAGS.do_lower_case,
      output_prediction_file,
      output_nbest_file,
      output_null_log_odds_file,
      verbose=FLAGS.verbose_logging
  )

  if FLAGS.eval_script:
    eval_out = subprocess.check_output([sys.executable, FLAGS.eval_script, FLAGS.predict_file, output_prediction_file])
    scores = str(eval_out).strip()
    exact_match = float(scores.split(":")[1].split(",")[0])
    if version_2_with_negative:
      f1 = float(scores.split(":")[2].split(",")[0])
    else:
      f1 = float(scores.split(":")[2].split("}")[0])
    dllogging = input_meta_data['dllogging']
    dllogging.logger.log(step=(), data={"f1": f1}, verbosity=Verbosity.DEFAULT)
    dllogging.logger.log(step=(), data={"exact_match": exact_match}, verbosity=Verbosity.DEFAULT)
    print(str(eval_out))


def export_squad(model_export_path, input_meta_data):
  """Exports a trained model as a `SavedModel` for inference.

  Args:
    model_export_path: a string specifying the path to the SavedModel directory.
    input_meta_data: dictionary containing meta data about input and model.

  Raises:
    Export path is not specified, got an empty string or None.
  """
  if not model_export_path:
    raise ValueError('Export path is not specified: %s' % model_export_path)
  bert_config = MODEL_CLASSES[FLAGS.model_name][0].from_json_file(FLAGS.config_file)
  squad_model, _ = bert_models.squad_model(bert_config, input_meta_data['max_seq_length'], float_type=tf.float32)
  export.export_to_savedmodel(model_export_path + '/savedmodel', model=squad_model, checkpoint_dir=FLAGS.model_dir)

  model_name = FLAGS.triton_model_name

  model_folder = model_export_path + "/triton_models/" + model_name
  version_folder = model_folder + "/" + str(FLAGS.triton_model_version)
  final_model_folder = version_folder + "/model.savedmodel"

  if not os.path.exists(version_folder):
    os.makedirs(version_folder)
  if not os.path.exists(final_model_folder):
    os.rename(model_export_path + '/savedmodel', final_model_folder)
    print("Model saved to dir", final_model_folder)
  else:
    if FLAGS.triton_model_overwrite:
      shutil.rmtree(final_model_folder)
      os.rename(model_export_path + '/savedmodel', final_model_folder)
      print("WARNING: Existing model was overwritten. Model dir: {}".format(final_model_folder))
    else:
      print(
          "ERROR: Could not save Triton model. Folder already exists. Use '--triton_model_overwrite=True' if you would like to overwrite an existing model. Model dir: {}"
          .format(final_model_folder)
      )
      return

  config_filename = os.path.join(model_folder, "config.pbtxt")
  if os.path.exists(config_filename) and not FLAGS.triton_model_overwrite:
    print(
        "ERROR: Could not save Triton model config. Config file already exists. Use '--triton_model_overwrite=True' if you would like to overwrite an existing model config. Model config: {}"
        .format(config_filename)
    )
    return

  config_template = r"""
name: "{model_name}"
platform: "tensorflow_savedmodel"
max_batch_size: {max_batch_size}
input [
    {{
        name: "input_mask"
        data_type: TYPE_INT32
        dims: {seq_length}
    }},
    {{
        name: "input_type_ids"
        data_type: TYPE_INT32
        dims: {seq_length}
    }},
    {{
        name: "input_word_ids"
        data_type: TYPE_INT32
        dims: {seq_length}
    }}
    ]
    output [
    {{
        name: "end_positions"
        data_type: TYPE_FP32
        dims: {seq_length}
    }},
    {{
        name: "start_positions"
        data_type: TYPE_FP32
        dims: {seq_length}
    }}
]
{dynamic_batching}
instance_group [
    {{
        count: {engine_count}
        kind: KIND_GPU
        gpus: [{gpu_list}]
    }}
]"""

  batching_str = ""
  max_batch_size = FLAGS.triton_max_batch_size

  if FLAGS.triton_dyn_batching_delay > 0:
    # Use only full and half full batches
    pref_batch_size = [int(max_batch_size / 2.0), max_batch_size]

    batching_str = r"""
dynamic_batching {{
    preferred_batch_size: [{0}]
    max_queue_delay_microseconds: {1}
}}""".format(", ".join([str(x) for x in pref_batch_size]), int(FLAGS.triton_dyn_batching_delay * 1000.0))

  config_values = {
      "model_name": model_name,
      "max_batch_size": max_batch_size,
      "seq_length": input_meta_data['max_seq_length'],
      "dynamic_batching": batching_str,
      "gpu_list": ", ".join([x.name.split(":")[-1] for x in tf.config.list_physical_devices('GPU')]),
      "engine_count": FLAGS.triton_engine_count
  }

  with open(model_folder + "/config.pbtxt", "w") as file:
    final_config_str = config_template.format_map(config_values)
    file.write(final_config_str)


def main(_):
  with tf.io.gfile.GFile(FLAGS.input_meta_data_path, 'rb') as reader:
    input_meta_data = json.loads(reader.read().decode('utf-8'))
  #  Get the value of 'train_data_size' from input_meta_data and set FLAGS.num_train_examples
  FLAGS([sys.argv[0], f"--num_train_examples={input_meta_data['train_data_size']}"])

  if FLAGS.mode == 'export_only':
    export_squad(FLAGS.model_dir, input_meta_data)
    return

  os.makedirs(FLAGS.model_dir, exist_ok=True)

  if FLAGS.mode in ('train', 'train_and_predict'):
    train_squad(input_meta_data)
  if FLAGS.mode in ('predict', 'sm_predict', 'trt_predict', 'train_and_predict') and is_main_process():
    predict_squad(input_meta_data)


if __name__ == '__main__':
  flags.mark_flag_as_required('config_file')
  flags.mark_flag_as_required('model_dir')
  app.run(main)
