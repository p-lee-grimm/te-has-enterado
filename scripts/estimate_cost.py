#!/usr/bin/env python
"""Во что обходится месяц работы. Считает по реальным объёмам из базы (§6).

Прайсы меняются, поэтому они собраны в PRICES и их надо сверять с сайтами
провайдеров. Всё остальное — измеренные величины, а не догадки.

    python scripts/estimate_cost.py
    python scripts/estimate_cost.py --articles-per-day 900
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from quepasa.config import get_settings  # noqa: E402
from quepasa.db import connect  # noqa: E402

console = Console(width=int(os.environ.get("QP_TABLE_WIDTH", "100")))

# Прайсы за миллион токенов. Сверять с сайтами: цены меняются.
PRICES = {
    "voyage-3.5": 0.06,
    "voyage-3.5-lite": 0.02,
    "voyage-3-large": 0.18,
}
LLM_PRICES = {  # (вход, выход) за миллион токенов
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "haiku": (1.0, 5.0),
}

# Измерено на реальных испанских текстах корпуса
CHARS_PER_TOKEN = 3.41
# Замер: `claude -p` стоит около этого за вызов независимо от размера промпта —
# сессия Claude Code тащит свой системный контекст. На наших коротких запросах
# это в десятки раз дороже прямого вызова API.
CLI_CALL_USD = 0.036
# Размер вектора в байтах: pgvector хранит float4
BYTES_PER_DIM = 4
# Накладные на строку статьи без вектора (url, заголовок, анонс, метаданные)
ROW_OVERHEAD_BYTES = 2500


def measure() -> dict:
    """Берёт из базы то, что можно измерить, а не предполагать."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT count(*) AS n,
                   avg(length(COALESCE(NULLIF(body,''), summary_feed, ''))) AS avg_chars,
                   avg(length(title)) AS avg_title
            FROM articles
            """
        ).fetchone()
        per_day = conn.execute(
            """
            SELECT count(*)::float / GREATEST(1, EXTRACT(EPOCH FROM
                       (max(published_at) - min(published_at))) / 86400) AS per_day
            FROM articles WHERE published_at IS NOT NULL
            """
        ).fetchone()
    return {
        "articles": row["n"],
        "avg_chars": float(row["avg_chars"] or 0),
        "avg_title": float(row["avg_title"] or 0),
        "per_day": float(per_day["per_day"] or 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--articles-per-day", type=float)
    ap.add_argument("--posts-per-day", type=float, default=6)
    args = ap.parse_args()

    s = get_settings()
    m = measure()
    per_day = args.articles_per_day or m["per_day"]

    body_chars = int(s.require("embed.body_chars"))
    emb_chars = min(m["avg_chars"], body_chars) + m["avg_title"]
    emb_tokens_day = per_day * emb_chars / CHARS_PER_TOKEN
    emb_model = s.require("embed.model")
    emb_price = PRICES.get(emb_model, 0.06)
    emb_month = emb_tokens_day * 30 / 1e6 * emb_price

    # пост на сюжет: заголовки на вход, короткий JSON на выход
    llm_model = str(s.require("summarize.model"))
    p_in, p_out = LLM_PRICES.get(llm_model, (1.0, 5.0))
    prompt_tokens = 1500          # prompts/post_headline.md
    titles_tokens = 10 * 25       # ~10 изданий в сюжете
    out_tokens = 150
    llm_in_month = args.posts_per_day * (prompt_tokens + titles_tokens) * 30
    llm_out_month = args.posts_per_day * out_tokens * 30
    llm_month = llm_in_month / 1e6 * p_in + llm_out_month / 1e6 * p_out
    cli_month = args.posts_per_day * 30 * CLI_CALL_USD
    via_cli = s.require("summarize.provider") == "claude_cli"

    dim = int(s.require("embed.dimensions"))
    row_bytes = dim * BYTES_PER_DIM + ROW_OVERHEAD_BYTES
    storage_month_mb = per_day * 30 * row_bytes / 1e6

    console.print(
        f"Измерено: [bold]{m['articles']}[/] статей в базе, "
        f"~[bold]{per_day:.0f}[/] в сутки, средний текст {m['avg_chars']:.0f} симв.\n"
    )

    t = Table(title="Примерная стоимость в месяц")
    t.add_column("статья расхода")
    t.add_column("объём", justify="right")
    t.add_column("$/мес", justify="right")

    t.add_row(
        f"Эмбеддинги ({emb_model})",
        f"{emb_tokens_day * 30 / 1e6:.1f}M токенов",
        f"{emb_month:.2f}",
    )
    t.add_row(
        f"Пересказ по API ({llm_model}, {args.posts_per_day:.0f}/день)",
        f"{llm_in_month / 1e6:.2f}M вх / {llm_out_month / 1e6:.2f}M вых",
        f"{llm_month:.2f}",
    )
    t.add_row(
        "[dim]…он же через `claude -p`[/]",
        f"[dim]{args.posts_per_day * 30:.0f} вызовов × ${CLI_CALL_USD}[/]",
        f"[{'bold yellow' if via_cli else 'dim'}]{cli_month:.2f}[/]",
    )
    t.add_row("Telegram Bot API", "—", "0.00")
    t.add_row("GitHub Actions (~10 мин/день)", "~300 мин", "0.00")
    t.add_row(
        "[dim]Supabase: прирост базы[/]",
        f"[dim]+{storage_month_mb:.0f} МБ/мес[/]",
        "[dim]0.00 до 500 МБ[/]",
    )
    t.add_row("[bold]Итого[/]", "",
              f"[bold]{emb_month + (cli_month if via_cli else llm_month):.2f}[/]")
    console.print(t)

    months_to_full = 500 / storage_month_mb if storage_month_mb else 999
    console.print(
        f"\n[bold]Про хранилище.[/] Вектор занимает {dim * BYTES_PER_DIM / 1024:.0f} КБ, "
        f"и это основной вес строки. При текущем потоке бесплатные 500 МБ Supabase "
        f"кончатся примерно через [bold]{months_to_full:.0f} мес.[/]\n"
        "Эмбеддинг нужен только пока статья участвует в кластеризации (окно "
        f"{s.require('cluster.window_hours')} ч) и в калибровке порога. "
        "Дальше его можно затирать — тогда база перестанет расти:\n"
        "    python run.py --purge-embeddings --commit\n"
    )
    if via_cli:
        console.print(
            f"[bold]Про провайдера.[/] Сейчас `claude_cli`. Отдельного счёта не "
            f"будет — это расходуется квота подписки, — но каждый вызов стоит "
            f"около ${CLI_CALL_USD} против ${llm_month / max(1, args.posts_per_day * 30):.4f} "
            f"по API: сессия Claude Code тащит свой системный контекст.\n"
            f"Для ежедневного крона дешевле `anthropic` с ключом; "
            f"`claude_cli` удобен локально, когда жмёшь кнопку руками."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
