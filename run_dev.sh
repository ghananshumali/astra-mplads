#!/usr/bin/env bash
# ASTRA — start the API and the React frontend together (development).
#   bash run_dev.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f data/astra.db ]; then
  echo "[ASTRA] No database found. Run these first:"
  echo "        python scripts/fetch_data.py"
  echo "        python scripts/run_pipeline.py"
  exit 1
fi
[ -d frontend/node_modules ] || (echo "[ASTRA] Installing frontend deps..." && cd frontend && npm install)

echo "[ASTRA] Starting API on :8000 ..."
python -m uvicorn astra.api.main:app --host 127.0.0.1 --port 8000 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT
sleep 3
echo "[ASTRA] Starting frontend on :5173 ..."
cd frontend && npm run dev
