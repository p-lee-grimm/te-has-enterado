"""Загрузка конфигов. Единственное место, где читаются YAML и переменные окружения."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    """Подставляет ${VAR} из окружения. Отсутствующая переменная -> пустая строка."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_dotenv(path: Path | None = None) -> None:
    """Минимальный .env loader — не тянем зависимость ради пятнадцати строк."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        os.environ.setdefault(key, val)


class Settings(dict):
    """dict с точечным доступом по пути: settings.get_path('cluster.sim_threshold')."""

    def get_path(self, dotted: str, default: Any = None) -> Any:
        node: Any = self
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        sentinel = object()
        value = self.get_path(dotted, sentinel)
        if value is sentinel:
            raise KeyError(f"settings.yaml: отсутствует ключ {dotted}")
        return value


@dataclass(frozen=True, slots=True)
class Source:
    id: str
    name: str
    feed_url: str
    type: str  # press | agency | official
    lean: str  # left | center-left | center | center-right | right
    weight: float
    lang: str
    status: str  # active | disabled

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@lru_cache(maxsize=1)
def get_settings(path: str | None = None) -> Settings:
    load_dotenv()
    p = Path(path) if path else CONFIG_DIR / "settings.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Settings(_expand_env(data))


def load_sources(path: str | None = None, include_disabled: bool = False) -> list[Source]:
    """Читает sources.yaml. blocked_sources из settings отключает источник немедленно."""
    p = Path(path) if path else CONFIG_DIR / "sources.yaml"
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    blocked = set(get_settings().get_path("blocked_sources", []) or [])

    out: list[Source] = []
    for item in raw.get("sources", []):
        status = item.get("status", "active")
        if item["id"] in blocked:
            status = "disabled"
        src = Source(
            id=item["id"],
            name=item["name"],
            feed_url=item.get("feed_url", ""),
            type=item.get("type", "press"),
            lean=item.get("lean", "center"),
            weight=float(item.get("weight", 1.0)),
            lang=item.get("lang", "es"),
            status=status,
        )
        if src.is_active or include_disabled:
            out.append(src)
    return out


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def env(key: str, default: str | None = None, required: bool = False) -> str:
    load_dotenv()
    value = os.environ.get(key, default)
    if required and not value:
        raise RuntimeError(f"Не задана переменная окружения {key}")
    return value or ""
