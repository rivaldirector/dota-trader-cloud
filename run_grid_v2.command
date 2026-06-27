#!/bin/bash
cd "$(dirname "$0")"
echo "=== Grid Search V2: ищем bank >= \$2000 ==="
echo "Параметров: 960 комбинаций, ~5-8 минут"
echo ""
python3 scripts/grid_search_v2.py 2>&1 | tee /tmp/grid_v2_output.txt
echo ""
echo "=== Done. Results saved to /tmp/grid_v2_output.txt ==="
echo "Press any key to close..."
read -n 1
