#!/usr/bin/env bash
# Общая часть для всех cron-скриптов.
#
# PATH задаём явно: cron даёт минимальный (/usr/bin:/bin), и установленный
# в ~/.local/bin claude в нём не виден. Симптом — «claude не найден в PATH»
# при том, что из шелла всё работает.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
PY=./.venv/bin/python
mkdir -p logs

# Один прогон за раз: сбор занимает три минуты, и если крон запустит второй
# поверх первого, они подерутся за фиды и за квоту.
lock() {
  exec 9>"logs/$1.lock"
  flock -n 9 || { echo "$(date -Is) пропуск: предыдущий прогон ещё идёт"; exit 0; }
}

trim_log() { tail -n 5000 "$1" > "$1.tmp" 2>/dev/null && mv "$1.tmp" "$1"; }

# Шаг с потолком по времени. Без него зависший вызов держит лок вечно:
# 25 августа --check-facts повис на сетевом чтении, и автопостинг не
# запускался трое суток — каждый следующий прогон видел лок и уходил.
# Лучше оборвать шаг, чем остановить канал.
STEP_TIMEOUT="${STEP_TIMEOUT:-900}"
run_step() {
  timeout --kill-after=30 "$STEP_TIMEOUT" "$@" || {
    code=$?
    if [ $code -eq 124 ] || [ $code -eq 137 ]; then
      echo "$(date -Is) ШАГ ОБОРВАН по таймауту ${STEP_TIMEOUT}с: $*"
    fi
    return 0   # один шаг не должен ронять остальные
  }
}
