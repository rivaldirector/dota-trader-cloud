#!/bin/bash
# BetsAPI 24-hour Maximum Harvest Launcher
# Run from project root: bash scripts/start_harvest.sh

set -e
cd "$(dirname "$0")/.."

mkdir -p logs

echo "========================================"
echo "BetsAPI 24-Hour Harvest Starting"
echo "$(date)"
echo "========================================"

# ── 1. Start LIVE COLLECTOR in background ────────────────────────────────────
echo ""
echo "[1/2] Starting live odds collector in background..."
nohup python3 scripts/betsapi_live_collector.py >> logs/live_collector.log 2>&1 &
LIVE_PID=$!
echo "  Live collector PID: $LIVE_PID"
echo "  Log: logs/live_collector.log"
echo "  Stop with: kill $LIVE_PID"
sleep 2

# ── 2. Start MAIN HARVEST in foreground ──────────────────────────────────────
echo ""
echo "[2/2] Starting main harvest (foreground — Ctrl+C to pause safely)..."
echo "  All data → storage/betsapi_harvest.db"
echo "  Resumable — run again if interrupted"
echo ""
python3 scripts/betsapi_harvest.py

echo ""
echo "========================================"
echo "Harvest complete. Live collector still running (PID: $LIVE_PID)"
echo "Monitor live: tail -f logs/live_collector.log"
echo "Stop live:    kill $LIVE_PID"
echo "========================================"
