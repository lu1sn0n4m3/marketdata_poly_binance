#!/bin/bash
# Run the uploader locally for testing
# Usage: ./run_uploader.sh

set -e

cd "$(dirname "$0")"

# Load environment from prod.env
if [ -f deploy/prod.env ]; then
    export $(grep -v '^#' deploy/prod.env | xargs)
fi

# Override DATA_DIR for local testing
export DATA_DIR=./data

# Run uploader
PYTHONPATH=./uploader/src:$PYTHONPATH ./venv/bin/python -m uploader.main
