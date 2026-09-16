#!/usr/bin/env bash
# ASTRA — start the poller, the API and the React frontend together (development).
#   bash run_dev.sh                    # everything
#   ASTRA_NO_POLLER=1 bash run_dev.sh  # site only
#
# Ctrl+C stops all three. To keep the data updating while the site is closed,
# run the poller on its own instead:  python -m astra.ingestion.poller
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f data/astra.db ]; then
  echo "[ASTRA] No database found. Run these first:"
  echo "        python scripts/fetch_data.py"
  echo "        python scripts/run_pipeline.py"
  exit 1
fi
[ -d frontend/node_modules ] || (echo "[ASTRA] Installing frontend deps..." && cd frontend && npm install)

POLLER_PID=""
if [ "${ASTRA_NO_POLLER:-0}" = "1" ]; then
  echo "[ASTRA] ASTRA_NO_POLLER=1: the site will show the data as of the last portal check."
else
  # If another poller already holds the database, this one prints why and
  # exits with code 3; the site then uses the one already running.
  echo "[ASTRA] Starting poller (checks the portal every minute) ..."
  python -m astra.ingestion.poller &
  POLLER_PID=$!
fi

echo "[ASTRA] Starting API on :8000 ..."
python -m uvicorn astra.api.main:app --host 127.0.0.1 --port 8000 &
API_PID=$!
trap 'kill $API_PID $POLLER_PID 2>/dev/null || true' EXIT
sleep 3
echo "[ASTRA] Starting frontend on :5173 ..."
cd frontend && npm run dev
