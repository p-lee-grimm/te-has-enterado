#!/usr/bin/env bash
# Разбор нажатий, часто и дёшево.
#
# Кнопки разбирал publish.sh раз в полчаса, и нажатие висело без ответа до
# следующего прогона — неотличимо от сломанной кнопки. Это один вызов
# getUpdates, его можно делать хоть каждую минуту.
#
# Лок общий с publish: оба двигают один и тот же offset в bot_state, и если
# они пересекутся, часть обновлений достанется одному, часть другому. Когда
# publish идёт, этот прогон просто пропускается — publish разбирает кнопки сам.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
exec >>logs/callbacks.log 2>&1
lock publish
$PY run.py --process-callbacks
trim_log logs/callbacks.log
