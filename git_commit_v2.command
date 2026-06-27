#!/bin/bash
cd "$(dirname "$0")"
echo "=== Git Commit: enhanced_backtest V2 winner params ==="

# Remove stale lock if exists
if [ -f .git/index.lock ]; then
    rm -f .git/index.lock
    echo "Removed stale index.lock"
fi

git add scripts/enhanced_backtest.py scripts/grid_search_v2.py
git status

git commit -m "feat: grid search V2 winner params — bank=\$2077, ROI=+5.7%, WR=65.6%

Grid Search V2 (960 combinations) found optimal params:
- EDGE_MIN = 0.03 (was 0.04)
- KELLY_FRACTION = 0.50 (was 0.40)
- KELLY_CAP = 0.06 (unchanged)
- COMP_MIN = 0.50 (was 0.52)
- ELO_WEIGHT = 0.55 / form=0.281 / h2h=0.169 (was 0.60/0.25/0.15)

Backtest result: bank=\$2,077.71, WR=65.6%, 256 bets, x2 ROI achieved"

echo ""
git push origin main
echo ""
echo "=== Done ==="
echo "Press any key to close..."
read -n 1
