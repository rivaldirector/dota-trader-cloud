#!/bin/bash
cd "$(dirname "$0")"
echo "=== Git Fix & Push ==="

# Remove ALL stale locks
rm -f .git/index.lock .git/HEAD.lock .git/MERGE_HEAD .git/rebase-merge 2>/dev/null
echo "Locks cleared"

# Stage everything (including new .command files, grid_results_v2.json, etc.)
git add -A
echo "All changes staged:"
git status --short

# Commit if there's anything new to commit
if ! git diff --cached --quiet; then
    git commit -m "chore: add grid search V2 files and launcher scripts

- scripts/grid_search_v2.py — 960-combo grid search
- scripts/grid_results_v2.json — top-50 results
- git helper .command files"
    echo "Commit created"
fi

echo ""
echo "--- Pulling remote changes... ---"
git fetch origin main
git rebase origin/main
echo ""
echo "--- Pushing... ---"
git push origin main
echo ""
echo "=== Done ==="
echo "Press any key to close..."
read -n 1
