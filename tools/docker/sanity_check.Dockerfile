#syntax=docker/dockerfile:1.1.5-experimental

FROM python:3.9 as yapf-test

COPY tools/install_deps/yapf.txt ./
RUN pip install -r yapf.txt
COPY ./ /deepray
WORKDIR /deepray

RUN python tools/format.py
RUN touch /ok.txt

# -------------------------------
FROM python:3.9 as valid_build_files

COPY tools/install_deps/tensorflow-cpu.txt ./
RUN pip install --default-timeout=1000 -r tensorflow-cpu.txt

RUN apt-get update && apt-get install sudo
COPY tools/install_deps/install_bazelisk.sh .bazelversion ./
RUN bash install_bazelisk.sh

COPY ./ /deepray
WORKDIR /deepray
RUN printf '\n\nn' | bash ./configure || true
RUN --mount=type=cache,id=cache_bazel,target=/root/.cache/bazel \
    bazel build --nobuild -- //deepray/...
RUN touch /ok.txt

# -------------------------------
FROM python:3.9-alpine as clang-format

RUN apk update && apk add git
RUN git clone https://github.com/DoozyX/clang-format-lint-action.git
WORKDIR ./clang-format-lint-action
RUN git checkout f85c199

COPY ./ /deepray
RUN python run-clang-format.py \
               -r \
               --style=google \
               --clang-format-executable ./clang-format/clang-format9 \
               /deepray
RUN touch /ok.txt

# -------------------------------
# Bazel code format
FROM alpine:3.11 as check-bazel-format

COPY ./tools/install_deps/buildifier.sh ./
RUN sh buildifier.sh

COPY ./ /deepray
RUN buildifier -mode=check -r /deepray
RUN touch /ok.txt

# -------------------------------
# docs tests
FROM python:3.9 as docs_tests

COPY tools/install_deps/tensorflow-cpu.txt ./
RUN pip install --default-timeout=1000 -r tensorflow-cpu.txt
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY tools/install_deps/doc_requirements.txt ./
RUN pip install -r doc_requirements.txt

RUN apt-get update && apt-get install -y rsync

COPY ./ /deepray
WORKDIR /deepray
RUN pip install --no-deps -e .
RUN python tools/docs/build_docs.py
RUN touch /ok.txt

# -------------------------------
# test the editable mode
FROM python:3.9 as test_editable_mode

COPY tools/install_deps/tensorflow-cpu.txt ./
RUN pip install --default-timeout=1000 -r tensorflow-cpu.txt
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY tools/install_deps/pytest.txt ./
RUN pip install -r pytest.txt

RUN apt-get update && apt-get install -y sudo rsync
COPY tools/install_deps/install_bazelisk.sh .bazelversion ./
RUN bash install_bazelisk.sh

COPY ./ /deepray
WORKDIR /deepray
RUN python configure.py
RUN --mount=type=cache,id=cache_bazel,target=/root/.cache/bazel \
    bash tools/install_so_files.sh
RUN pip install --no-deps -e .
RUN pytest -v -n auto ./deepray/activations
RUN touch /ok.txt

# -------------------------------
# ensure that all checks were successful
# this is necessary if using docker buildkit
# with "export DOCKER_BUILDKIT=1"
# otherwise dead branch elimination doesn't
# run all tests
FROM scratch

COPY --from=0 /ok.txt /ok0.txt
COPY --from=1 /ok.txt /ok1.txt
COPY --from=2 /ok.txt /ok2.txt
COPY --from=3 /ok.txt /ok3.txt
COPY --from=4 /ok.txt /ok4.txt
COPY --from=5 /ok.txt /ok5.txt
