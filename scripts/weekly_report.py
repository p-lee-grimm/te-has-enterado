#!/usr/bin/env python
"""Недельная сводка владельцу (§6).

Без неё через месяц будет невозможно понять, почему упало качество.

    python scripts/weekly_report.py            # напечатать
    python scripts/weekly_report.py --send     # отправить в личку владельцу
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quepasa.config import env, load_dotenv  # noqa: E402
from quepasa.db import connect  # noqa: E402

DAYS = 7


def collect(days: int = DAYS) -> dict:
    with connect() as conn:
        by_day = conn.execute(
            """
            SELECT to_char(date_trunc('day', fetched_at), 'DD.MM') AS d,
                   count(*) AS articles,
                   count(DISTINCT cluster_id) AS clusters
            FROM articles
            WHERE fetched_at >= now() - make_interval(days => %s)
            GROUP BY 1 ORDER BY min(fetched_at)
            """,
            (days,),
        ).fetchall()

        feeds = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'active') AS active,
                   count(*) FILTER (
                       WHERE status = 'active'
                         AND (last_ok_at IS NULL OR last_ok_at < now() - interval '48 hours')
                   ) AS stale
            FROM sources
            """
        ).fetchone()

        top_sources = conn.execute(
            """
            SELECT s.name, count(*) AS hits
            FROM digest_items di
            JOIN digests d      ON d.id = di.digest_id AND d.status = 'published'
            JOIN articles a     ON a.cluster_id = di.cluster_id
            JOIN sources s      ON s.id = a.source_id
            WHERE d.published_at >= now() - make_interval(days => %s)
            GROUP BY s.name ORDER BY hits DESC LIMIT 5
            """,
            (days,),
        ).fetchall()

        runs = conn.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status = 'ok')     AS ok,
                   count(*) FILTER (WHERE status = 'gated')  AS gated,
                   count(*) FILTER (WHERE status = 'failed') AS failed,
                   COALESCE(sum(cost_usd), 0)                AS cost,
                   COALESCE(sum(llm_tokens_in), 0)           AS tin,
                   COALESCE(sum(llm_tokens_out), 0)          AS tout
            FROM runs WHERE started_at >= now() - make_interval(days => %s)
            """,
            (days,),
        ).fetchone()

        digests = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'published') AS published,
                   count(*) FILTER (WHERE status = 'skipped')   AS skipped,
                   COALESCE(avg(item_count) FILTER (WHERE status = 'published'), 0) AS avg_items
            FROM digests WHERE created_at >= now() - make_interval(days => %s)
            """,
            (days,),
        ).fetchone()

        facts = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'active') AS active,
                   count(*) FILTER (WHERE status = 'candidate') AS candidate,
                   count(*) FILTER (WHERE status = 'retired') AS retired,
                   count(*) FILTER (WHERE created_at >= now() - make_interval(days => %s))
                       AS added,
                   count(*) FILTER (WHERE kind = 'legal' AND status = 'stale')
                       AS legal_expired
            FROM entity_facts
            """,
            (days,),
        ).fetchone()
        # Очередь работы владельца, а не порог понимания для читателя:
        # читатель уже защищён ролью в теле поста, а пул углубляет контекст
        # для повторяющихся фигур.
        no_pool = conn.execute(
            """
            SELECT e.name_es, e.mentions_count FROM entities e
            WHERE NOT e.never_explain
              AND NOT EXISTS (SELECT 1 FROM entity_facts f
                              WHERE f.entity_id = e.id AND f.status = 'active')
            ORDER BY e.mentions_count DESC LIMIT 5
            """
        ).fetchall()
        unresolved = conn.execute(
            """
            SELECT surface, count FROM entity_unresolved
            WHERE last_seen >= now() - make_interval(days => %s)
            ORDER BY count DESC LIMIT 10
            """,
            (days,),
        ).fetchall()
        taps = conn.execute(
            """
            SELECT count(*) AS total, count(DISTINCT user_id) AS people,
                   count(DISTINCT entity_id) AS entities
            FROM context_taps WHERE tapped_at >= now() - make_interval(days => %s)
            """,
            (days,),
        ).fetchone()

    return {
        "by_day": by_day, "feeds": feeds, "top_sources": top_sources,
        "runs": runs, "digests": digests, "facts": facts, "no_pool": no_pool,
        "unresolved": unresolved, "taps": taps,
    }


def subscribers() -> int | None:
    """Число подписчиков канала. Не критично — при ошибке просто не показываем."""
    from quepasa.telegram import TelegramError, _call

    chat = env("TELEGRAM_CHANNEL_ID")
    if not chat:
        return None
    try:
        return _call("getChatMemberCount", {"chat_id": chat})
    except (TelegramError, RuntimeError):
        return None


def render_report(data: dict, subs: int | None) -> str:
    lines = [f"<b>Сводка за {DAYS} дней</b>", ""]

    lines.append("<b>Статьи и сюжеты по дням</b>")
    for row in data["by_day"]:
        lines.append(f"  {row['d']}: {row['articles']} статей, {row['clusters']} сюжетов")
    if not data["by_day"]:
        lines.append("  нет данных")

    f = data["feeds"]
    lines += ["", "<b>Источники</b>",
              f"  активных: {f['active']}, молчат больше 48 ч: {f['stale']}"]

    lines += ["", "<b>Топ-5 источников в выпусках</b>"]
    for row in data["top_sources"]:
        lines.append(f"  {row['name']}: {row['hits']}")
    if not data["top_sources"]:
        lines.append("  выпусков пока не было")

    d, r = data["digests"], data["runs"]
    lines += [
        "", "<b>Выпуски</b>",
        f"  опубликовано: {d['published']}, пропущено: {d['skipped']}, "
        f"пунктов в среднем: {float(d['avg_items']):.1f}",
        "", "<b>Прогоны</b>",
        f"  всего {r['total']}: ok {r['ok']}, не прошли ворота {r['gated']}, упали {r['failed']}",
        "", "<b>Стоимость LLM</b>",
        f"  ${float(r['cost']):.2f} за неделю ({r['tin']} вх. / {r['tout']} исх. токенов)",
    ]

    if float(r["cost"]) > 10:
        lines.append("  ⚠️ на порядок выше ожидаемого — проверь, не зациклилось ли что-то")

    e = data["facts"]
    lines += ["", "<b>Пул фактов</b>",
              f"  показываются: {e['active']}, добавлено за неделю: {e['added']}, "
              f"ждут второго полюса: {e['candidate']}, сняты: {e['retired']}"]
    if int(e["legal_expired"]):
        lines.append(f"  ⚠️ просрочен процессуальный статус: {e['legal_expired']} — "
                     f"перепроверка: python run.py --refresh-facts --commit")

    if data["no_pool"]:
        lines += ["", "<b>Чаще всего упоминаются, а пула нет</b>"]
        for r in data["no_pool"]:
            lines.append(f"  {r['name_es']} ×{r['mentions_count']}")
        lines.append("  <i>Собрать: python manage.py fact extract &lt;id&gt; --commit</i>")

    if data["unresolved"]:
        lines += ["", "<b>Очередь неразрешённых сущностей</b>"]
        for r in data["unresolved"]:
            lines.append(f"  {r['surface']} ×{r['count']}")
        lines.append("  <i>Завести: python manage.py entity add …</i>")

    t = data["taps"]
    if t and int(t["total"]) > 0:
        lines += ["", "<b>Замерный режим</b>",
                  f"  тапов: {t['total']}, людей: {t['people']}, "
                  f"сущностей: {t['entities']}",
                  "  <i>Пересланный пост кнопок не сохраняет — охват занижен.</i>"]

    if subs is not None:
        lines += ["", f"<b>Подписчиков:</b> {subs}"]

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--send", action="store_true", help="отправить владельцу в личку")
    ap.add_argument("--days", type=int, default=DAYS)
    args = ap.parse_args()

    load_dotenv()
    report = render_report(collect(args.days), subscribers() if args.send else None)

    if args.send:
        from quepasa.telegram import notify_owner
        notify_owner(report)
        print("Отправлено владельцу.")
    else:
        import re
        print(re.sub(r"</?b>", "", report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
