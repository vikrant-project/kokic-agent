#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
runtime="${KOKIC_HOME:-$HOME/Kokic Agent}"
if [[ ! -x "$runtime/.venv/bin/python" ]]; then
  python3 -m venv "$runtime/.venv"
  "$runtime/.venv/bin/pip" install -r requirements.lock
fi
exec "$runtime/.venv/bin/python" run.py "$@"
