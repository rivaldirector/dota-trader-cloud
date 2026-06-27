#!/bin/bash
cd "$(dirname "$0")"
echo "=== Git Pull --rebase + Push ==="

# Remove stale locks if any
rm -f .git/index.lock .git/HEAD.lock 2>/dev/null

git pull --rebase origin main
echo ""
echo "--- Rebase done, pushing... ---"
git push origin main
echo ""
echo "=== Done ==="
echo "Press any key to close..."
read -n 1
