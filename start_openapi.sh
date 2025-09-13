#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8812}"
HOST="${HOST:-0.0.0.0}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv-openapi}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure venv module exists
if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
  echo "[!] Python venv module missing. On Debian/Ubuntu: apt install -y python3-venv"
  exit 1
fi

# Create venv if needed
if [ ! -d "$VENV_DIR" ]; then
  echo "[+] Creating virtualenv at $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# Activate venv
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

# Upgrade pip (best effort)
python -m pip install --upgrade pip || true

# Install wrapper deps
if [ -f "requirements-openapi.txt" ]; then
  pip install -r requirements-openapi.txt
else
  pip install fastapi uvicorn pydantic "mcp[cli]>=1.14" pihole6api
fi

# Install project deps if present (NO editable install; your repo is flat)
if [ -f "requirements.txt" ]; then
  echo "[+] Installing project requirements.txt"
  pip install -r requirements.txt || {
    echo "[!] Failed to install project requirements.txt"
    exit 1
  }
fi

echo "[+] Starting OpenAPI wrapper on ${HOST}:${PORT}"
exec uvicorn api_wrapper:app --host "${HOST}" --port "${PORT}"
