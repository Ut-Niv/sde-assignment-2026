#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> starts the controller and runs until the scenario finishes.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export SIM_URL
STATE="${CONTROLLER_STATE:-$ROOT/controller_state.db}"
exec python -m controller.main --url "$SIM_URL" --state "$STATE"
