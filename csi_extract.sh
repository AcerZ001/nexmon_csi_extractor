#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON_EXECUTABLE:-/home/pi/.pyenv/versions/nexmon-csi/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
    echo "Python not found: $PYTHON. Set PYTHON_EXECUTABLE to your nexmon-csi venv Python." >&2
    exit 1
fi
export PYTHONUNBUFFERED=1
exec "$PYTHON" "$PROJECT_DIR/src/application/csi_extractor.py" "$@"
