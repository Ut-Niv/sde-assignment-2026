#!/usr/bin/env bash
# Demonstrates crash recovery: start controller, kill mid-run, restart, finish cleanly.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
PORT=8710
STATE="$ROOT/crash_state.db"
RUN="$ROOT/runs/crash_demo"
rm -rf "$RUN" "$STATE"
mkdir -p "$RUN"

echo "== starting simulator =="
python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir "$RUN" --port "$PORT" > /tmp/crash_sim.log 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
sleep 12
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null

echo "== starting controller (will kill after ~8s) =="
CONTROLLER_STATE="$STATE" ./run.sh "http://127.0.0.1:$PORT" > /tmp/crash_ctl1.log 2>&1 &
CTL_PID=$!
sleep 8
echo "== killing controller PID $CTL_PID =="
kill -9 "$CTL_PID" 2>/dev/null || true
wait "$CTL_PID" 2>/dev/null || true
echo "state after kill:"
python -c "
from controller.store import Store
s=Store('$STATE')
print(' last_chunk', s.get_meta('last_chunk'))
print(' pending', s.all_pending())
print(' decisions', len(s.query_decisions(limit=100)))
"

echo "== restarting controller =="
CONTROLLER_STATE="$STATE" ./run.sh "http://127.0.0.1:$PORT" > /tmp/crash_ctl2.log 2>&1
echo "== second run finished =="
tail -5 /tmp/crash_ctl2.log
curl -s "http://127.0.0.1:$PORT/sim/state" | python -c "import sys,json; d=json.load(sys.stdin); print('finished', d.get('finished'), 'ts', d.get('ts'))"
echo "OK crash recovery demo complete"
