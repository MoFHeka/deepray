#!/usr/bin/env bash

set -x -e

docker build \
    -f tools/docker/dev_container.Dockerfile \
    --build-arg TF_VERSION=2.9.3 \
    --build-arg TF_PACKAGE=tensorflow \
    --build-arg PY_VERSION=$PY_VERSION \
    --no-cache \
    --target dev_container \
    -t hailinfufu/deepray:latest-py${PY_VERSION}-tf${TF_VERSION}-cu118-ubuntu20.04 ./