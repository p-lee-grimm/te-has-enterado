#!/usr/bin/env bash
# Сбор и обработка. Каждая стадия отдельно: упавшая не роняет остальные.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
exec >>logs/cycle.log 2>&1
lock cycle
echo "=== $(date -Is) ==="
run_step $PY run.py --stage ingest  --commit
run_step $PY run.py --stage embed   --commit
run_step $PY run.py --stage cluster --commit
run_step $PY run.py --stage rank    --commit
# Имена из очереди заводятся сами и доезжают до вышедших постов. Раз в час,
# а не каждые полчаса: каждое имя — это лестница источников и сборка
# пояснения под каждый пост, то есть вызовы модели.
run_step $PY run.py --adopt-queue   --commit
run_step $PY run.py --purge-bodies
run_step $PY run.py --purge-embeddings --commit
trim_log logs/cycle.log
