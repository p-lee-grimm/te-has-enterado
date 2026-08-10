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
