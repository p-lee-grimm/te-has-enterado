#!/usr/bin/env bash
# Вечерний пост. Час сверяется в коде по таймзоне Europe/Madrid,
# поэтому cron можно ставить шире и не думать про переход на летнее время.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
exec >>logs/evening.log 2>&1
lock evening
echo "=== $(date -Is) ==="
$PY run.py --check-schedule && $PY run.py --digest-post --commit
trim_log logs/evening.log
