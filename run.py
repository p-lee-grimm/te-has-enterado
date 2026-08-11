#!/usr/bin/env python
"""Единственная точка входа.

    python run.py --migrate                       # применить схему
    python run.py --stage ingest --dry-run        # один этап, ничего не пишем
    python run.py --stage ingest --commit         # один этап, пишем в БД
    python run.py --all --commit                  # полный прогон
    python run.py --purge-bodies                  # затереть просроченные тексты (§5.4)

По умолчанию режим --dry-run: ничего не публикуется и не пишется (§8.4).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from quepasa.config import load_dotenv  # noqa: E402

STAGES = ["ingest", "embed", "cluster", "rank", "select", "summarize", "render", "gate", "publish"]


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("trafilatura").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=STAGES, help="прогнать один этап")
    ap.add_argument("--all", action="store_true", help="полный прогон пайплайна")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--commit", dest="dry_run", action="store_false",
                    help="писать в БД и публиковать (иначе только печать)")
    ap.add_argument("--migrate", action="store_true", help="применить миграции")
    ap.add_argument("--build-vector-index", action="store_true")
    ap.add_argument("--purge-bodies", action="store_true", help="затереть просроченные тексты")
    ap.add_argument("--status", action="store_true",
                    help="живо ли всё: свежесть сбора, фиды, очереди")
    ap.add_argument("--refresh-cards", action="store_true",
                    help="пометить устаревшие карточки и перегенерировать")
    ap.add_argument("--check-facts", action="store_true",
                    help="сверить опубликованные посты с текущими источниками")
    ap.add_argument("--process-callbacks", action="store_true",
                    help="разобрать нажатия кнопок в чате ревью (один раз)")
    ap.add_argument("--serve-callbacks", action="store_true",
                    help="слушать нажатия постоянно: отвечать надо за секунды")
    ap.add_argument("--poll-timeout", type=int, default=25,
                    help="сколько секунд ждать обновления при --serve-callbacks")
    ap.add_argument("--purge-embeddings", action="store_true",
                    help="затереть векторы у старых статей (база перестаёт расти)")
    ap.add_argument("--reembed", action="store_true",
                    help="сбросить векторы, посчитанные другой моделью, и пересчитать")
    ap.add_argument("--digest-post", action="store_true",
                    help="собрать и опубликовать дайджест по категориям")
    ap.add_argument("--autopost", action="store_true",
                    help="отобрать сюжеты по правилу и опубликовать (по умолчанию сухой прогон)")
    ap.add_argument("--sync-posts", action="store_true",
                    help="дополнить опубликованные посты новыми изданиями")
    ap.add_argument("--check-schedule", action="store_true",
                    help="код 0, если сейчас время запуска по Мадриду (для cron в UTC)")
    ap.add_argument("--limit-sources", type=int, help="взять только N источников (отладка)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    setup_logging(args.verbose)
    log = logging.getLogger("run")

    if args.migrate:
        from quepasa.db import migrate
        applied = migrate()
        log.info("Миграции применены: %s", ", ".join(applied))
        return 0

    if args.build_vector_index:
        from quepasa.db import build_vector_index
        build_vector_index()
        return 0

    if args.check_schedule:
        from quepasa.schedule import should_run_now
        ok, why = should_run_now()
        log.info("Расписание: %s", why)
        return 0 if ok else 3

    if args.status:
        from quepasa.status import checks, collect
        data = collect()
        bad = 0
        for name, ok, detail in checks(data):
            mark = "OK  " if ok else "СТОП"
            if not ok:
                bad += 1
            print(f"  [{mark}] {name:26} {detail}")
        print()
        print("Всё в порядке." if bad == 0 else f"Требует внимания: {bad}.")
        return 0 if bad == 0 else 1

    if args.refresh_cards:
        from quepasa.cards import refresh_stale
        log.info("Карточки: %s", refresh_stale(dry_run=args.dry_run))
        return 0

    if args.check_facts:
        from quepasa.edits import run as check_facts
        st = check_facts(dry_run=args.dry_run)
        log.info("Сверка фактов: %s", st)
        return 0

    if args.process_callbacks:
        from quepasa.cards import process_callbacks
        st = process_callbacks()
        log.info("Ревью: утверждено %s, удалено %s, правок %s",
                 st["approved"], st["deleted"], st["edited"])
        return 0

    if args.serve_callbacks:
        # Долгое ожидание вместо опроса по расписанию: ответ на нажатие
        # Telegram принимает считаные секунды, и разбор раз в две минуты
        # опаздывал всегда — владелец видел мёртвую кнопку.
        import time as _time

        from quepasa.cards import process_callbacks
        log.info("Слушаю нажатия (долгое ожидание %s с)", args.poll_timeout)
        while True:
            try:
                st = process_callbacks(timeout=args.poll_timeout)
                if st["approved"] or st["deleted"] or st["edited"] or st.get("taps"):
                    log.info("Ревью: утверждено %s, удалено %s, правок %s",
                             st["approved"], st["deleted"], st["edited"])
            except KeyboardInterrupt:
                return 0
            except Exception:  # noqa: BLE001 — служба не должна умирать от сбоя
                log.exception("Разбор нажатий сорвался, продолжаю через 15 с")
                _time.sleep(15)

    if args.purge_embeddings:
        from quepasa.config import get_settings as _gs
        from quepasa.db import connect, purge_old_embeddings
        days = int(_gs().get_path("embed.keep_embeddings_days", 14))
        if args.dry_run:
            with connect() as conn:
                n = conn.execute(
                    "SELECT count(*) AS n FROM articles WHERE embedding IS NOT NULL "
                    "AND published_at < now() - make_interval(days => %s)", (days,)
                ).fetchone()["n"]
            log.info("DRY-RUN: затёрли бы векторов старше %s дн.: %s", days, n)
            return 0
        with connect() as conn:
            n = purge_old_embeddings(conn, days)
        log.info("Затёрто векторов старше %s дн.: %s", days, n)
        return 0

    if args.reembed:
        from quepasa.config import get_settings as _gs
        from quepasa.db import connect, drop_embeddings, embedding_models
        tag = f"{_gs().require('embed.provider')}/{_gs().require('embed.model')}"
        if args.dry_run:
            with connect() as conn:
                log.info("Сейчас в базе: %s", [dict(m) for m in embedding_models(conn)])
            log.info("DRY-RUN: сбросили бы всё, что не %s. Для запуска добавь --commit", tag)
            return 0
        with connect() as conn:
            n = drop_embeddings(conn, keep_model=tag)
        log.info("Сброшено векторов чужой модели: %s. Теперь: python run.py --stage embed --commit", n)
        return 0

    if args.digest_post:
        from quepasa.digest import build
        stats = build(dry_run=args.dry_run)
        log.info("Дайджест: кандидатов %s, пунктов %s, ошибок %s",
                 stats.get("candidates"), stats.get("items"), stats.get("errors"))
        return 0

    if args.autopost:
        from quepasa.posts import autopost
        stats = autopost(dry_run=args.dry_run)
        # Публикация уже произошла; это только печать итога. Ключи берём
        # через get: расхождение с posts.autopost стоило падения после каждой
        # публикации — пост уходил, а прогон завершался ошибкой.
        for it in stats["items"]:
            log.info("  сюжет %s · %s ист. · %s · %s",
                     it.get("cluster_id"), it.get("n_sources"),
                     ",".join(it.get("buckets") or []), (it.get("headline") or "")[:70])
        if args.dry_run:
            log.info("DRY-RUN: ничего не опубликовано. Для запуска — --commit")
        return 0

    if args.sync_posts:
        from quepasa.posts import sync_all
        stats = sync_all(dry_run=args.dry_run)
        log.info("Синхронизация постов: %s", stats)
        return 0

    if args.purge_bodies:
        from quepasa.db import connect, purge_expired_bodies
        with connect() as conn:
            n = purge_expired_bodies(conn)
        log.info("Затёрто просроченных текстов: %s", n)
        return 0

    if not args.stage and not args.all:
        ap.print_help()
        return 1

    if args.dry_run:
        log.info("Режим DRY-RUN: в БД не пишем, ничего не публикуем.")

    stages = STAGES if args.all else [args.stage]
    return run_pipeline(stages, args, log)


def run_pipeline(stages: list[str], args, log) -> int:
    from quepasa.pipeline import Context, STAGE_FUNCS

    ctx = Context(dry_run=args.dry_run, limit_sources=args.limit_sources)
    ctx.open_run()

    try:
        for name in stages:
            fn = STAGE_FUNCS.get(name)
            if fn is None:
                log.warning("Этап %s ещё не реализован — пропускаем", name)
                continue
            t0 = time.monotonic()
            log.info("──── стадия %s ────", name)
            fn(ctx)
            ctx.stage_stats.setdefault(name, {})["elapsed_s"] = round(time.monotonic() - t0, 1)
            if ctx.halted:
                log.warning("Прогон остановлен на стадии %s: %s", name, ctx.halt_reason)
                break
    except Exception as exc:
        log.exception("Прогон упал")
        ctx.close_run(status="failed", error=f"{type(exc).__name__}: {exc}")
        return 1

    ctx.close_run(status="gated" if ctx.halted else "ok")
    return 2 if ctx.halted else 0


if __name__ == "__main__":
    raise SystemExit(main())
