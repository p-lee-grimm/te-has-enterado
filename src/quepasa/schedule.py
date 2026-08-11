"""Проверка расписания с учётом перехода на летнее время (§3.11).

GitHub Actions умеет только UTC. Испания переходит на летнее время, поэтому
19:30 по Мадриду — это 17:30 UTC летом и 18:30 UTC зимой. Хардкодить UTC-час
нельзя: полгода выпуск выходил бы на час не вовремя.

Решение: cron срабатывает в оба часа, а нужный отбирается здесь по таймзоне
Europe/Madrid.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import get_settings


def should_run_now(now: datetime | None = None) -> tuple[bool, str]:
    s = get_settings()
    tz = ZoneInfo(s.require("render.timezone"))
    hour = int(s.get_path("digest.run_hour_local", s.require("publish.run_hour_local")))
    minute = int(s.get_path("digest.run_minute_local", s.require("publish.run_minute_local")))
    tolerance = int(s.require("publish.schedule_tolerance_minutes"))

    now = now.astimezone(tz) if now else datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta_min = (now - target).total_seconds() / 60

    local = now.strftime("%Y-%m-%d %H:%M %Z")
    if 0 <= delta_min <= tolerance:
        return True, f"{local} — попадает в окно {hour:02d}:{minute:02d} (+{tolerance} мин)"
    return False, (
        f"{local} — не время запуска (цель {hour:02d}:{minute:02d} по Мадриду, "
        f"расхождение {delta_min:+.0f} мин)"
    )
