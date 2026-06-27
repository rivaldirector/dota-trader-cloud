#!/bin/bash
cd "$(dirname "$0")"
echo "=== Starting Enhanced Backtest ==="
echo "Working directory: $(pwd)"
python3 scripts/enhanced_backtest.py 2>&1 | tee /tmp/backtest_output.txt
echo ""
echo "=== Done. Results saved to /tmp/backtest_output.txt ==="
echo "Press any key to close..."
read -n 1
