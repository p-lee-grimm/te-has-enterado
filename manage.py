#!/usr/bin/env python
"""Ручное управление реестром сущностей и связями (§7.3, §4.2).

Автосоздание сущностей запрещено: ошибка матчинга даёт дубликат, дубликаты
расходятся в фактах, а разгребать их дороже, чем завести руками по три штуки
в неделю. Поэтому всё, что не сматчилось, копится в очереди, а решение
принимает человек — здесь.

    python manage.py entity seed                  # залить стартовый набор
    python manage.py entity list
    python manage.py entity add <id> --name-es … --card …
    python manage.py entity alias <id> <алиас> [<алиас> …]
    python manage.py entity approve <id>
    python manage.py entity merge <из> <в>
    python manage.py entity never-explain <id>
    python manage.py unresolved [--limit 20]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import yaml  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from quepasa.config import CONFIG_DIR, load_dotenv  # noqa: E402
from quepasa.db import connect  # noqa: E402
from quepasa.entities import normalize  # noqa: E402

console = Console(width=int(os.environ.get("QP_TABLE_WIDTH", "120")))

MAX_CARD = 200


def _add_aliases(conn, entity_id: str, aliases) -> int:
    n = 0
    for alias in aliases:
        key = normalize(alias)
        if not key:
            continue
        conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (entity_id, key),
        )
        n += 1
    return n


def cmd_seed(args) -> int:
    path = Path(args.file or CONFIG_DIR / "entities_seed.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    added = 0
    with connect() as conn:
        for e in raw.get("entities", []):
            card = (e.get("card") or "").strip()
            if len(card) > MAX_CARD:
                console.print(f"[red]{e['id']}: карточка {len(card)} символов, "
                              f"максимум {MAX_CARD} — пропущена[/]")
                continue
            conn.execute(
                """
                INSERT INTO entities (id, type, name_es, name_ru, card, card_status,
                                      wiki_url_es, wiki_url_ru)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    type=EXCLUDED.type, name_es=EXCLUDED.name_es,
                    name_ru=EXCLUDED.name_ru, card=EXCLUDED.card,
                    wiki_url_es=EXCLUDED.wiki_url_es, card_updated_at=now()
                """,
                (e["id"], e.get("type", "other"), e["name_es"], e.get("name_ru", ""),
                 card, e.get("card_status", "approved"),
                 e.get("wiki_url_es"), e.get("wiki_url_ru")),
            )
            _add_aliases(conn, e["id"],
                         list(e.get("aliases", [])) + [e["name_es"], e.get("name_ru", "")])
            added += 1
    console.print(f"Залито сущностей: [bold]{added}[/] из {path}")
    return 0


def cmd_list(args) -> int:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT e.*, (SELECT count(*) FROM entity_aliases a WHERE a.entity_id = e.id) AS n_alias
            FROM entities e ORDER BY e.mentions_count DESC, e.id
            """
        ).fetchall()
    t = Table(title=f"Сущности ({len(rows)})")
    for col in ("id", "тип", "имя", "статус", "алиасов", "упом.", "карточка"):
        t.add_column(col, overflow="fold")
    for r in rows:
        colour = {"approved": "green", "draft": "yellow", "stale": "red"}[r["card_status"]]
        t.add_row(r["id"], r["type"], r["name_es"],
                  f"[{colour}]{r['card_status']}[/]", str(r["n_alias"]),
                  str(r["mentions_count"]), (r["card"] or "")[:70])
    console.print(t)
    return 0


def cmd_add(args) -> int:
    card = (args.card or "").strip()
    autogen = not card
    if len(card) > MAX_CARD:
        console.print(f"[red]Карточка {len(card)} символов, максимум {MAX_CARD}[/]")
        return 1
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO entities (id, type, name_es, name_ru, card, card_status, wiki_url_es)
            VALUES (%s,%s,%s,%s,%s,'draft',%s)
            ON CONFLICT (id) DO UPDATE SET name_es=EXCLUDED.name_es, card=EXCLUDED.card,
                card_updated_at=now()
            """,
            (args.id, args.type, args.name_es, args.name_ru or "", card, args.wiki),
        )
        _add_aliases(conn, args.id, [args.name_es, args.name_ru or ""] + (args.alias or []))
    if autogen:
        console.print(f"Заведена [bold]{args.id}[/], карточка генерируется из Википедии…")
        args.no_review = False
        return cmd_generate(args)
    console.print(f"Заведена [bold]{args.id}[/] со статусом draft. "
                  f"Утвердить: python manage.py entity approve {args.id}")
    return 0


def cmd_backfill(args) -> int:
    """Разносит утверждённые карточки по уже вышедшим постам.

    Порядок — от редких имён к частым: карточек на пост не больше двух, и
    занимать их должно то, чего читатель скорее не знает.
    """
    from quepasa.posts import backfill_entity_card

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name_es, mentions_count FROM entities
            WHERE card_status = 'approved' AND NOT never_explain
              AND btrim(card) <> ''
              -- приведение обязательно: без него Postgres не выводит тип
              -- параметра в «%s IS NULL» и падает на IndeterminateDatatype
              AND (%s::text IS NULL OR id = %s::text)
            ORDER BY mentions_count ASC, id
            """,
            (args.id, args.id),
        ).fetchall()

    if not rows:
        console.print("[yellow]Нечего разносить: утверждённых карточек нет[/]")
        return 1

    total_edited = total_checked = 0
    for r in rows:
        res = backfill_entity_card(r["id"], dry_run=not args.commit)
        total_checked += res.get("checked", 0)
        total_edited += res.get("edited", 0)
        if res.get("checked"):
            console.print(
                f"  [bold]{r['name_es']}[/] — постов {res['checked']}, "
                f"правок {res['edited']}"
                + (f", пропущено (две карточки уже есть): {res['skipped_full']}"
                   if res.get("skipped_full") else "")
            )

    console.print(
        f"\nСущностей просмотрено: {len(rows)}. "
        f"Подходящих постов: {total_checked}. "
        f"{'Исправлено' if args.commit else 'Будет исправлено'}: {total_edited}."
    )
    if not args.commit:
        console.print("[dim]Это сухой прогон. Чтобы применить — --commit[/]")
    return 0


def cmd_generate(args) -> int:
    """Черновик карточки из Википедии -> в чат ревью с кнопками."""
    from quepasa.cards import CardError, generate, send_for_review

    with connect() as conn:
        e = conn.execute("SELECT * FROM entities WHERE id=%s", (args.id,)).fetchone()
    if e is None:
        console.print(f"[red]Сущности {args.id} нет. Сначала: entity add[/]")
        return 1

    try:
        draft = generate(e["name_es"], e["wiki_url_es"], args.context or "")
    except CardError as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    with connect() as conn:
        conn.execute(
            "UPDATE entities SET card=%s, card_status='draft', wiki_url_es=%s, "
            "card_updated_at=now() WHERE id=%s",
            (draft["card"], draft["wiki_url"], args.id),
        )
    console.print(f"[bold]{e['name_es']}[/]  (${draft['cost_usd']})")
    console.print(f"  {draft['card']}")
    console.print(f"  [dim]{len(draft['card'])}/200 символов · {draft['wiki_url']}[/]")
    for p in draft["problems"]:
        console.print(f"  [red]проблема: {p}[/]")

    if not args.no_review:
        send_for_review(dict(e), draft)
        console.print("\nОтправлено в чат ревью. "
                      + ("Кнопок нет — сначала поправь текст."
                         if draft["problems"] else "Нажми «Ок», чтобы утвердить."))
    return 0


def cmd_callbacks(args) -> int:
    from quepasa.cards import process_callbacks

    st = process_callbacks(timeout=args.wait)
    console.print(f"утверждено {st['approved']}, удалено {st['deleted']}, "
                  f"правок {st['edited']}")
    return 0


def cmd_alias(args) -> int:
    with connect() as conn:
        n = _add_aliases(conn, args.id, args.alias)
    console.print(f"Добавлено алиасов: {n}")
    return 0


def cmd_approve(args) -> int:
    with connect() as conn:
        conn.execute(
            "UPDATE entities SET card_status='approved', card_updated_at=now() WHERE id=%s",
            (args.id,),
        )
    console.print(f"[green]{args.id}: карточка утверждена[/]")
    return 0


def cmd_never_explain(args) -> int:
    with connect() as conn:
        conn.execute("UPDATE entities SET never_explain = %s WHERE id = %s",
                     (not args.off, args.id))
    console.print(f"{args.id}: never_explain = {not args.off}")
    return 0


def cmd_merge(args) -> int:
    """Переносит алиасы и упоминания и удаляет дубликат."""
    with connect() as conn:
        conn.execute(
            "UPDATE entity_aliases SET entity_id=%s WHERE entity_id=%s "
            "AND alias NOT IN (SELECT alias FROM entity_aliases WHERE entity_id=%s)",
            (args.to, args.src, args.to),
        )
        conn.execute("UPDATE entity_mentions SET entity_id=%s WHERE entity_id=%s",
                     (args.to, args.src))
        conn.execute(
            "UPDATE entities SET mentions_count = mentions_count + "
            "(SELECT mentions_count FROM entities WHERE id=%s) WHERE id=%s",
            (args.src, args.to),
        )
        conn.execute("DELETE FROM entities WHERE id=%s", (args.src,))
    console.print(f"{args.src} → {args.to}: алиасы и упоминания перенесены")
    return 0


def cmd_link(args) -> int:
    from quepasa.related import link

    with connect() as conn:
        link(conn, args.a, args.b)
    console.print(f"Связь {args.a} ↔ {args.b} проставлена вручную")
    return 0


def cmd_unlink(args) -> int:
    """Отклонённая пара больше не предлагается никогда."""
    from quepasa.related import block

    with connect() as conn:
        block(conn, args.a, args.b)
    console.print(f"Пара {args.a} ↔ {args.b} заблокирована навсегда")
    return 0


def cmd_unresolved(args) -> int:
    """Рабочая очередь: что встречается часто, но в реестре не заведено."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM entity_unresolved ORDER BY count DESC, last_seen DESC LIMIT %s",
            (args.limit,),
        ).fetchall()
    if not rows:
        console.print("Очередь пуста.")
        return 0
    t = Table(title=f"Неразрешённые сущности (топ {len(rows)})")
    for col in ("встречалось", "строка", "похоже на", "последний раз"):
        t.add_column(col, overflow="fold")
    for r in rows:
        t.add_row(str(r["count"]), r["surface"], r["candidate_id"] or "—",
                  f"{r['last_seen']:%d.%m %H:%M}")
    console.print(t)
    console.print("\nЗавести: [dim]python manage.py entity add <id> --name-es … --card …[/]\n"
                  "Или добавить алиас существующей: "
                  "[dim]python manage.py entity alias <id> «строка»[/]")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="group", required=True)

    ent = sub.add_parser("entity").add_subparsers(dest="cmd", required=True)

    p = ent.add_parser("seed"); p.add_argument("--file"); p.set_defaults(fn=cmd_seed)
    p = ent.add_parser("list"); p.set_defaults(fn=cmd_list)

    p = ent.add_parser("add")
    p.add_argument("id")
    p.add_argument("--name-es", required=True)
    p.add_argument("--name-ru", default="")
    p.add_argument("--card", help="если не задать — сгенерируем из Википедии")
    p.add_argument("--type", default="other",
                   choices=["person", "company", "party", "institution", "media", "other"])
    p.add_argument("--wiki")
    p.add_argument("--alias", nargs="*")
    p.add_argument("--context", default="", help="уточнение для поиска в Википедии")
    p.set_defaults(fn=cmd_add)

    p = ent.add_parser("generate"); p.add_argument("id")
    p.add_argument("--context", default="", help="уточнение для поиска в Википедии")
    p.add_argument("--no-review", action="store_true")
    p.set_defaults(fn=cmd_generate)

    p = ent.add_parser("callbacks"); p.add_argument("--wait", type=int, default=0)
    p.set_defaults(fn=cmd_callbacks)

    p = ent.add_parser("backfill",
                       help="разнести утверждённые карточки по вышедшим постам")
    p.add_argument("id", nargs="?", help="одна сущность; без аргумента — все")
    p.add_argument("--commit", action="store_true")
    p.set_defaults(fn=cmd_backfill)

    p = ent.add_parser("alias"); p.add_argument("id"); p.add_argument("alias", nargs="+")
    p.set_defaults(fn=cmd_alias)
    p = ent.add_parser("approve"); p.add_argument("id"); p.set_defaults(fn=cmd_approve)
    p = ent.add_parser("never-explain"); p.add_argument("id")
    p.add_argument("--off", action="store_true"); p.set_defaults(fn=cmd_never_explain)
    p = ent.add_parser("merge"); p.add_argument("src"); p.add_argument("to")
    p.set_defaults(fn=cmd_merge)

    p = sub.add_parser("link"); p.add_argument("a", type=int); p.add_argument("b", type=int)
    p.set_defaults(fn=cmd_link)
    p = sub.add_parser("unlink"); p.add_argument("a", type=int); p.add_argument("b", type=int)
    p.set_defaults(fn=cmd_unlink)

    p = sub.add_parser("unresolved"); p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_unresolved)

    args = ap.parse_args()
    load_dotenv()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
