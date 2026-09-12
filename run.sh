#!/usr/bin/env bash
# Linux/macOS counterpart of run.ps1
set -euo pipefail
cd "$(dirname "$0")"

SOURCE=synthetic
MODE=fusion
PORT=8765
OPEN=1
BENCHMARK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-browser) OPEN=0; shift ;;
    --benchmark) BENCHMARK=1; shift ;;
    -h|--help)
      echo "Usage: ./run.sh [--source synthetic|camera|auto] [--mode fusion|naive] [--port N] [--no-browser] [--benchmark]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

PYTHON=".venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Virtual environment missing. See README.md (Linux/macOS)." >&2
  exit 1
fi

if [[ "$BENCHMARK" -eq 1 ]]; then
  exec "$PYTHON" -m nebula_mvp.benchmark --verify
fi

ARGS=(-m nebula_mvp --source "$SOURCE" --mode "$MODE" --port "$PORT")
if [[ "$OPEN" -eq 1 ]]; then
  ARGS+=(--open)
fi
exec "$PYTHON" "${ARGS[@]}"
