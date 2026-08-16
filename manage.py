#!/usr/bin/env python
"""Ручное управление реестром сущностей, пулом фактов и связями (§7.3, §4.2).

Автосоздание сущностей запрещено: ошибка матчинга даёт дубликат, дубликаты
расходятся в фактах, а разгребать их дороже, чем завести руками по три штуки
в неделю. Поэтому всё, что не сматчилось, копится в очереди, а решение
принимает человек — здесь.

    python manage.py entity seed                  # залить стартовый набор
    python manage.py entity list
    python manage.py entity add <id> --name-es …
    python manage.py entity alias <id> <алиас> [<алиас> …]
    python manage.py entity merge <из> <в>
    python manage.py entity never-explain <id>
    python manage.py unresolved [--limit 20]

    python manage.py fact list <id>               # пул одной сущности
    python manage.py fact extract <id> [--commit] # собрать по лестнице источников
    python manage.py fact extract --all [--commit]
    python manage.py fact fix <номер> "новый текст"
    python manage.py fact retire <номер>
    python manage.py fact audit [--send]          # выборка недели владельцу
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
            conn.execute(
                """
                INSERT INTO entities (id, type, name_es, name_ru,
                                      wiki_url_es, wiki_url_ru, official_url)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    type=EXCLUDED.type, name_es=EXCLUDED.name_es,
                    name_ru=EXCLUDED.name_ru, wiki_url_es=EXCLUDED.wiki_url_es
                """,
                (e["id"], e.get("type", "other"), e["name_es"], e.get("name_ru", ""),
                 e.get("wiki_url_es"), e.get("wiki_url_ru"), e.get("official_url")),
            )
            _add_aliases(conn, e["id"],
                         list(e.get("aliases", [])) + [e["name_es"], e.get("name_ru", "")])
            added += 1
    console.print(f"Залито сущностей: [bold]{added}[/] из {path}")
    console.print("[dim]Пул фактов собирается отдельно: "
                  "python manage.py fact extract --all --commit[/]")
    return 0


def cmd_list(args) -> int:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT e.*,
                   (SELECT count(*) FROM entity_aliases a WHERE a.entity_id = e.id)
                       AS n_alias,
                   (SELECT count(*) FROM entity_facts f
                     WHERE f.entity_id = e.id AND f.status = 'active'
                       AND (f.expires_at IS NULL OR f.expires_at > now())) AS n_facts
            FROM entities e ORDER BY e.mentions_count DESC, e.id
            """
        ).fetchall()
    t = Table(title=f"Сущности ({len(rows)})")
    for col in ("id", "тип", "имя", "фактов", "алиасов", "упом.", "пул обновлён"):
        t.add_column(col, overflow="fold")
    for r in rows:
        colour = "green" if r["n_facts"] else "red"
        t.add_row(r["id"], r["type"], r["name_es"],
                  f"[{colour}]{r['n_facts']}[/]", str(r["n_alias"]),
                  str(r["mentions_count"]),
                  f"{r['facts_updated_at']:%d.%m.%Y}" if r["facts_updated_at"] else "—")
    console.print(t)
    return 0


def cmd_add(args) -> int:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO entities (id, type, name_es, name_ru, wiki_url_es, official_url)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET name_es=EXCLUDED.name_es,
                wiki_url_es=COALESCE(EXCLUDED.wiki_url_es, entities.wiki_url_es)
            """,
            (args.id, args.type, args.name_es, args.name_ru or "", args.wiki,
             args.official),
        )
        _add_aliases(conn, args.id, [args.name_es, args.name_ru or ""] + (args.alias or []))

    console.print(f"Заведена [bold]{args.id}[/]. Собираю пул фактов…")
    args.all = False
    args.no_press = False
    return cmd_fact_extract(args)


def cmd_refresh(args) -> int:
    """Пересобирает вышедшие посты под текущие правила рендера."""
    from quepasa.posts import refresh_published

    res = refresh_published(dry_run=not args.commit, window_hours=args.hours)
    for it in res["items"]:
        marks = []
        if it["dropped_significance"]:
            marks.append("убрана строка «зачем»")
        if it["geo_tag"]:
            marks.append(f"тег {it['geo_tag']}")
        console.print(f"  пост {it['post_id']} (msg {it['message_id']}): "
                      + (", ".join(marks) or "пересборка"))
    console.print(
        f"\nПроверено: {res['checked']}. "
        f"{'Изменено' if args.commit else 'Будет изменено'}: {res['edited']}. "
        f"Без изменений: {res['unchanged']}. Ошибок: {res['errors']}."
    )
    if not args.commit:
        console.print("[dim]Сухой прогон. Чтобы применить — --commit[/]")
    return 0


def cmd_backfill(args) -> int:
    """Разносит пояснения по уже вышедшим постам.

    Порядок — от редких имён к частым: пояснений на пост не больше двух, и
    занимать их должно то, чего читатель скорее не знает.
    """
    from quepasa.posts import backfill_entity_context

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.name_es, e.mentions_count FROM entities e
            WHERE NOT e.never_explain
              AND EXISTS (SELECT 1 FROM entity_facts f
                          WHERE f.entity_id = e.id AND f.status = 'active'
                            AND (f.expires_at IS NULL OR f.expires_at > now()))
              -- приведение обязательно: без него Postgres не выводит тип
              -- параметра в проверке на NULL и падает на IndeterminateDatatype.
              -- Плейсхолдер в комментарии тоже считается — писать его нельзя.
              AND (%s::text IS NULL OR e.id = %s::text)
            ORDER BY e.mentions_count ASC, e.id
            """,
            (args.id, args.id),
        ).fetchall()

    if not rows:
        console.print("[yellow]Нечего разносить: ни у кого нет пула фактов[/]")
        return 1

    total_edited = total_checked = 0
    for r in rows:
        res = backfill_entity_context(r["id"], dry_run=not args.commit)
        total_checked += res.get("checked", 0)
        total_edited += res.get("edited", 0)
        if res.get("checked"):
            console.print(
                f"  [bold]{r['name_es']}[/] — постов {res['checked']}, "
                f"правок {res['edited']}"
                + (f", пропущено (два пояснения уже есть): {res['skipped_full']}"
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


# ------------------------------------------------------------- пул фактов


def cmd_fact_list(args) -> int:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM entity_facts WHERE entity_id = %s ORDER BY status, id",
            (args.id,),
        ).fetchall()
    if not rows:
        console.print(f"У {args.id} пула нет. "
                      f"Собрать: python manage.py fact extract {args.id} --commit")
        return 1

    t = Table(title=f"Факты {args.id} ({len(rows)})")
    for col in ("#", "тип", "факт", "темы", "источник", "статус", "до"):
        t.add_column(col, overflow="fold")
    colours = {"active": "green", "candidate": "yellow",
               "stale": "red", "retired": "dim"}
    for r in rows:
        t.add_row(str(r["id"]), r["kind"], r["fact"],
                  ", ".join(r["topics"] or []), r["source_tier"],
                  f"[{colours.get(r['status'], '')}]{r['status']}[/]",
                  f"{r['expires_at']:%d.%m}" if r["expires_at"] else "—")
    console.print(t)
    return 0


def cmd_fact_extract(args) -> int:
    """Собирает пул по лестнице источников: wiki → wiki_org → wikidata → official → press."""
    from quepasa.factops import refresh_entity

    with connect() as conn:
        if getattr(args, "all", False):
            rows = conn.execute(
                "SELECT id, name_es FROM entities ORDER BY mentions_count DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name_es FROM entities WHERE id = %s", (args.id,)
            ).fetchall()

    if not rows:
        console.print("[red]Сущности нет. Сначала: entity add[/]")
        return 1

    total = 0.0
    for r in rows:
        res = refresh_entity(r["id"], dry_run=not args.commit,
                             with_press=not getattr(args, "no_press", False))
        total += float(res.get("cost_usd", 0))
        console.print(f"[bold]{r['name_es']}[/] — источников "
                      f"{res.get('sources', 0)}, принято {res.get('kept', 0)}, "
                      f"отклонено {res.get('rejected', 0)}")
        for f in res.get("facts", []):
            console.print(f"   • {f['fact']}  [dim]({f['kind']})[/]")
        if res.get("reason"):
            console.print(f"   [yellow]{res['reason']}[/]")

    console.print(f"\nСущностей: {len(rows)}. Потрачено: ${total:.4f}.")
    if not args.commit:
        console.print("[dim]Это сухой прогон, в базу ничего не записано. "
                      "Чтобы применить — --commit[/]")
    return 0


def cmd_fact_fix(args) -> int:
    from quepasa.factops import fix

    console.print(fix(args.fact_id, args.text))
    return 0


def cmd_fact_retire(args) -> int:
    from quepasa.factops import retire

    ok = retire(args.fact_id)
    console.print(f"Факт #{args.fact_id} снят." if ok
                  else f"[yellow]Факт #{args.fact_id} уже снят или его нет.[/]")
    return 0 if ok else 1


def cmd_fact_audit(args) -> int:
    from quepasa.factops import audit

    res = audit(dry_run=not args.send)
    console.print(f"Отправлено фактов на сверку: {res['sent']}"
                  if args.send else
                  "[dim]Сухой прогон: смотри лог. Отправить владельцу — --send[/]")
    return 0


def cmd_callbacks(args) -> int:
    from quepasa.review import process_callbacks

    st = process_callbacks(timeout=args.wait)
    console.print(f"правок {st['edited']}, снято фактов {st.get('retired', 0)}, "
                  f"нажатий {st.get('taps', 0)}")
    return 0


def cmd_alias(args) -> int:
    with connect() as conn:
        n = _add_aliases(conn, args.id, args.alias)
    console.print(f"Добавлено алиасов: {n}")
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
    p.add_argument("--type", default="other",
                   choices=["person", "company", "party", "institution", "media", "other"])
    p.add_argument("--wiki", help="точная ссылка на статью Википедии")
    p.add_argument("--official", help="сайт органа, партии или издания")
    p.add_argument("--alias", nargs="*")
    p.add_argument("--commit", action="store_true",
                   help="сразу собрать пул фактов, а не показать сухой прогон")
    p.set_defaults(fn=cmd_add)

    p = ent.add_parser("callbacks"); p.add_argument("--wait", type=int, default=0)
    p.set_defaults(fn=cmd_callbacks)

    p = ent.add_parser("refresh",
                       help="пересобрать вышедшие посты: гео-теги, significance")
    p.add_argument("--commit", action="store_true")
    p.add_argument("--hours", type=float, default=None,
                   help="окно в часах; по умолчанию из конфига")
    p.set_defaults(fn=cmd_refresh)

    p = ent.add_parser("backfill",
                       help="разнести пояснения по вышедшим постам")
    p.add_argument("id", nargs="?", help="одна сущность; без аргумента — все")
    p.add_argument("--commit", action="store_true")
    p.set_defaults(fn=cmd_backfill)

    p = ent.add_parser("alias"); p.add_argument("id"); p.add_argument("alias", nargs="+")
    p.set_defaults(fn=cmd_alias)
    p = ent.add_parser("never-explain"); p.add_argument("id")
    p.add_argument("--off", action="store_true"); p.set_defaults(fn=cmd_never_explain)
    p = ent.add_parser("merge"); p.add_argument("src"); p.add_argument("to")
    p.set_defaults(fn=cmd_merge)

    fact = sub.add_parser("fact").add_subparsers(dest="cmd", required=True)

    p = fact.add_parser("list", help="пул одной сущности"); p.add_argument("id")
    p.set_defaults(fn=cmd_fact_list)

    p = fact.add_parser("extract", help="собрать пул по лестнице источников")
    p.add_argument("id", nargs="?")
    p.add_argument("--all", action="store_true", help="все сущности реестра")
    p.add_argument("--no-press", action="store_true",
                   help="только энциклопедия и официальные источники")
    p.add_argument("--commit", action="store_true")
    p.set_defaults(fn=cmd_fact_extract)

    p = fact.add_parser("fix", help="заменить текст факта")
    p.add_argument("fact_id", type=int); p.add_argument("text")
    p.set_defaults(fn=cmd_fact_fix)

    p = fact.add_parser("retire", help="убрать факт из пула навсегда")
    p.add_argument("fact_id", type=int); p.set_defaults(fn=cmd_fact_retire)

    p = fact.add_parser("audit", help="выборка недели владельцу на сверку")
    p.add_argument("--send", action="store_true")
    p.set_defaults(fn=cmd_fact_audit)

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
