#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="${1:-/tmp/traceh-demo}"
DATA_DIR="${2:-/tmp/traceh-data}"

rm -rf "$WORKSPACE" "$DATA_DIR"
cp -R "$ROOT/examples/demo_bug" "$WORKSPACE"

PYTHONPATH="$ROOT/src" python -m traceh.cli.main run \
  "$WORKSPACE" \
  "Fix the addition bug and run the tests" \
  --script "$ROOT/examples/demo_script.json" \
  --verify-command "python -m unittest -v" \
  --data-dir "$DATA_DIR"
