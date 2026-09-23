#!/usr/bin/env bash
# HackerFab Stepper – Linux / macOS launcher
# Usage: ./run.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  HackerFab Stepper"
echo "  ─────────────────"
echo ""

# ── 1. Find a compatible system Python ──────────────────────────────────
# Prefer Python 3.13 because Pillow publishes macOS and Windows wheels for it.
PYTHON_BIN="${PYTHON_BIN:-python3.13}"
if ! command -v "$PYTHON_BIN" &>/dev/null; then
    PYTHON_BIN="python3"
fi

if ! command -v "$PYTHON_BIN" &>/dev/null; then
    echo "  Python 3.10-3.13 is required but was not found."
    echo "  Install Python 3.13, then run this script again."
    exit 1
fi

PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
case "$PYTHON_VERSION" in
    3.10|3.11|3.12|3.13) ;;
    *)
        echo "  Python 3.10-3.13 is required (found $PYTHON_VERSION)."
        echo "  Install Python 3.13, then run this script again."
        exit 1
        ;;
esac

# ── 2. Create the project environment and install dependencies ──────────
# Version the directory so a failed Python 3.14 environment is never reused.
VENV_DIR=".venv-$PYTHON_VERSION"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "  Creating Python environment..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# On first run this downloads PyTorch and its dependencies (~1-2 GB).
echo "  Checking dependencies..."
"$VENV_DIR/bin/python" -m pip install --prefer-binary --requirement requirements.txt
echo "  Dependencies OK"
echo ""

# ── 3. Launch ────────────────────────────────────────────────────────────
echo "  Starting..."
echo ""
exec "$VENV_DIR/bin/python" src/gui.py
