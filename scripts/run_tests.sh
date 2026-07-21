#!/usr/bin/env bash
# Run the test suite with a clean PYTHONPATH.
#
# Some dev machines (e.g. ones with ROS sourced) export a PYTHONPATH that leaks
# unrelated pytest plugins into the venv and breaks collection. Clearing it here
# keeps the run hermetic. Qt UI tests run headless via the offscreen platform.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
fi

env PYTHONPATH= QT_QPA_PLATFORM=offscreen "$PYTHON" -m pytest "$@"
