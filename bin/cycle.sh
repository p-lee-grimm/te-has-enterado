#!/usr/bin/env bash
# Сбор и обработка. Каждая стадия отдельно: упавшая не роняет остальные.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
exec >>logs/cycle.log 2>&1
lock cycle
echo "=== $(date -Is) ==="
$PY run.py --stage ingest  --commit
$PY run.py --stage embed   --commit
$PY run.py --stage cluster --commit
$PY run.py --stage rank    --commit
$PY run.py --purge-bodies
$PY run.py --purge-embeddings --commit
trim_log logs/cycle.log
