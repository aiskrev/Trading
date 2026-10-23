#!/bin/sh
# Starts the control room on http://127.0.0.1:8765/ (this computer only). Ctrl+C stops it.
# Use the project's virtual environment, e.g.:  ~/venvs/trading/bin/python -m ui
cd "$(dirname "$0")/.." || exit 1
exec "${PYTHON:-python3}" -m ui "$@"
