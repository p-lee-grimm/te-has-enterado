#!/usr/bin/env bash
# Недельное обслуживание: пул фактов и сводка.
#
# Порядок важен. Сначала снимаем просроченные процессуальные статусы и
# перепроверяем очередь — иначе на сверку владельцу уйдёт факт, который
# система через минуту сама и снимет. Аудит идёт вторым, сводка последней:
# в неё попадают итоги обеих операций.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
exec >>logs/weekly.log 2>&1
lock weekly
echo "=== $(date -Is) ==="
$PY run.py --refresh-facts --commit
$PY run.py --audit-facts --commit
$PY scripts/weekly_report.py --send
trim_log logs/weekly.log
