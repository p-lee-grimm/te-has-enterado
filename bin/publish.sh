#!/usr/bin/env bash
# Публикация и разбор нажатий. Отдельно от сбора: у них разный ритм.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
exec >>logs/publish.log 2>&1
lock publish
echo "=== $(date -Is) ==="
$PY run.py --process-callbacks
$PY run.py --autopost --commit
$PY run.py --sync-posts --commit
$PY run.py --check-facts --commit
trim_log logs/publish.log
