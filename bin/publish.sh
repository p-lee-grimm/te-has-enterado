#!/usr/bin/env bash
# Публикация и разбор нажатий. Отдельно от сбора: у них разный ритм.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
exec >>logs/publish.log 2>&1
lock publish
echo "=== $(date -Is) ==="
# кнопки разбирает callbacks.sh, и только он: getUpdates двигает общий offset
# в bot_state, и два процесса поделили бы обновления между собой — часть
# нажатий досталась бы одному, часть другому, и оба сочли бы, что всё разобрали
$PY run.py --autopost --commit
$PY run.py --sync-posts --commit
$PY run.py --check-facts --commit
trim_log logs/publish.log
