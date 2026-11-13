#!/usr/bin/env bash
# Regenerate the hash-locked server requirements for Linux, inside the same Python
# image the containers use. Run from the repository root:  deploy/lock-requirements.sh
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${PYTHON_IMAGE:-python:3.14-slim}"
for name in api bot; do
  docker run --rm -v "$PWD/requirements:/req" -w /req "$IMAGE" sh -c \
    "pip install --quiet --root-user-action=ignore pip-tools && \
     pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras -o $name.txt $name.in"
  echo "requirements/$name.txt updated"
done
