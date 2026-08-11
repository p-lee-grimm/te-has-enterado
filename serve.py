#!/usr/bin/env python
"""Локальная консоль оператора. Только для владельца, только на localhost.

Нужна ровно для того, что нельзя сделать автоматом: посмотреть глазами
и разметить руками. Разметка пар копится в БД и превращает «покрутить порог
на глаз» в повторяемое число.

    python serve.py            # http://127.0.0.1:8765
    python serve.py --port 9000

Это инструмент владельца, а не веб-фронт продукта (§0 non-goals):
наружу не смотрит, читателям не показывается, в проде не крутится.
"""

from __future__ import annotations

import argparse
import html
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from quepasa.config import get_settings, load_dotenv  # noqa: E402

e = html.escape

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e8ee;
     font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:#79b8ff;text-decoration:none} a:hover{text-decoration:underline}
header{background:#161922;border-bottom:1px solid #262b36;padding:0 20px;
       position:sticky;top:0;z-index:10}
nav{display:flex;gap:4px;max-width:1100px;margin:0 auto;flex-wrap:wrap}
nav a{padding:14px 14px;color:#9aa4b8;border-bottom:2px solid transparent;font-weight:500}
nav a.on{color:#fff;border-bottom-color:#4c8dff}
main{max-width:1100px;margin:0 auto;padding:24px 20px 80px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 10px;color:#c7cede}
.sub{color:#7a8496;font-size:13px;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:#161922;border:1px solid #262b36;border-radius:10px;padding:14px 16px}
.card .n{font-size:26px;font-weight:600;letter-spacing:-.5px}
.card .l{color:#7a8496;font-size:12px;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13.5px;
      background:#161922;border:1px solid #262b36;border-radius:10px;overflow:hidden}
th{text-align:left;padding:9px 12px;background:#1b1f2a;color:#9aa4b8;
   font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px}
td{padding:9px 12px;border-top:1px solid #222735;vertical-align:top}
tr.hi td{background:#1a2333}
.ok{color:#5ac47d} .warn{color:#e0b34a} .bad{color:#f0736a} .dim{color:#7a8496}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;
      background:#222735;color:#9aa4b8;white-space:nowrap}
.lean-far-left{background:#2e1a3d;color:#d9a3f5}
.lean-left{background:#2a1f33;color:#c98fe0}
.lean-center-left{background:#1f2a33;color:#7fc4e0}
.lean-center{background:#232a22;color:#93c98f}
.lean-center-right{background:#33291f;color:#e0b98f}
.lean-right{background:#331f1f;color:#e08f8f}
.lean-far-right{background:#3d1a1a;color:#f5a3a3}
.pair{background:#161922;border:1px solid #262b36;border-radius:10px;
      padding:16px;margin-bottom:14px}
.pair .sim{font-variant-numeric:tabular-nums;color:#9aa4b8;font-size:12.5px;
           margin-bottom:10px}
.art{padding:9px 0;border-top:1px solid #222735}
.art:first-of-type{border-top:none}
.art .t{font-weight:500}
.btns{display:flex;gap:8px;margin-top:12px}
button{font:inherit;font-size:13.5px;font-weight:500;padding:8px 16px;border-radius:8px;
       border:1px solid #2e3543;background:#1e2430;color:#e6e8ee;cursor:pointer}
button:hover{background:#28303e}
button.yes{border-color:#2c5c3e;background:#1b3626;color:#9ae0b4}
button.yes:hover{background:#245030}
button.no{border-color:#5c2c2c;background:#361b1b;color:#e09a9a}
button.no:hover{background:#502424}
form{display:inline}
.post{background:#161922;border:1px solid #262b36;border-radius:10px;padding:20px;
      white-space:pre-wrap;line-height:1.6}
.post b{color:#fff} .post i{color:#9aa4b8}
.empty{background:#161922;border:1px dashed #2e3543;border-radius:10px;
       padding:28px;text-align:center;color:#7a8496}
code{background:#1b1f2a;padding:2px 6px;border-radius:5px;font-size:12.5px;
     font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.note{background:#1a2333;border-left:3px solid #4c8dff;padding:11px 14px;
      border-radius:0 8px 8px 0;margin:14px 0;font-size:13.5px;color:#c7cede}
.note.warn{background:#2a2419;border-left-color:#e0b34a}
textarea{width:100%;min-height:150px;background:#12151c;color:#e6e8ee;
  border:1px solid #2e3543;border-radius:8px;padding:12px;font:14px/1.6 inherit;resize:vertical}
textarea:focus{outline:none;border-color:#4c8dff}
select,input[type=text]{background:#12151c;color:#e6e8ee;border:1px solid #2e3543;
  border-radius:8px;padding:8px 10px;font:inherit;font-size:14px}
label{display:block;font-size:12px;color:#7a8496;text-transform:uppercase;
  letter-spacing:.4px;margin:16px 0 6px;font-weight:600}
.tg{background:#17212b;border-radius:10px;padding:14px 16px;line-height:1.5;
  max-width:560px;font-size:14.5px}
.tg a{color:#6ab3f3}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:start}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
button.go{border-color:#2b4a7d;background:#1c3358;color:#cfe2ff}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:14px}
"""

TABS = [
    ("/", "Обзор"),
    ("/clusters", "Сюжеты"),
    ("/posts", "Посты"),
    ("/label", "Разметка пар"),
    ("/threshold", "Порог"),
    ("/sources", "Источники"),
]


def page(title: str, path: str, body: str) -> bytes:
    nav = "".join(
        f'<a href="{h}" class="{"on" if h == path else ""}">{e(t)}</a>' for h, t in TABS
    )
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} — Qué Pasa</title><style>{CSS}</style></head>
<body><header><nav>{nav}</nav></header><main>{body}</main></body></html>""".encode()


def lean_pill(lean: str) -> str:
    from quepasa.posts import LEAN_EMOJI

    arrow = LEAN_EMOJI.get(lean, "")
    return f'<span class="pill lean-{e(lean)}">{arrow} {e(lean)}</span>'


def card(n, label) -> str:
    return f'<div class="card"><div class="n">{n}</div><div class="l">{e(label)}</div></div>'


# ----------------------------------------------------------------- страницы


def view_overview() -> str:
    from quepasa.db import connect

    with connect() as conn:
        a = conn.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE embedding IS NOT NULL) AS embedded,
                   count(*) FILTER (WHERE body IS NOT NULL)      AS with_body,
                   count(*) FILTER (WHERE fetched_at >= now() - interval '24 hours') AS day
            FROM articles
            """
        ).fetchone()
        c = conn.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status = 'open') AS open,
                   count(*) FILTER (WHERE n_sources >= 3)  AS eligible
            FROM clusters
            """
        ).fetchone()
        src = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status='active') AS active,
                   count(*) FILTER (WHERE status='active' AND (last_ok_at IS NULL
                        OR last_ok_at < now() - interval '48 hours')) AS stale
            FROM sources
            """
        ).fetchone()
        runs = conn.execute(
            "SELECT id, started_at, status, cost_usd FROM runs ORDER BY id DESC LIMIT 8"
        ).fetchall()
        labels_n = conn.execute("SELECT count(*) AS n FROM pair_labels").fetchone()["n"]

    s = get_settings()
    provider = s.require("embed.provider")

    from quepasa.status import checks as health_checks
    from quepasa.status import collect as health_collect

    rows_health = "".join(
        f'<tr><td class="{"ok" if ok else "bad"}">{"работает" if ok else "СТОП"}</td>'
        f"<td>{e(name)}</td><td class=dim>{e(detail)}</td></tr>"
        for name, ok, detail in health_checks(health_collect())
    )
    health = (f'<h2>Живо ли всё</h2><table><tr><th>статус</th><th>что</th>'
              f'<th>подробности</th></tr>{rows_health}</table>')

    warn = ""
    if provider == "local":
        warn = (
            '<div class="note warn"><b>embed.provider = local</b> — офлайн-заглушка '
            "без семантики. Косинус живёт в другом диапазоне, чем у настоящей модели: "
            "разметка и порог, полученные сейчас, к боевому провайдеру не переносятся. "
            "Перед продом поставь <code>voyage</code> и ключ.</div>"
        )

    rows = "".join(
        f"<tr><td>{r['id']}</td>"
        f"<td class=dim>{r['started_at']:%d.%m %H:%M}</td>"
        f"<td class='{ {'ok':'ok','gated':'warn','failed':'bad'}.get(r['status'],'dim') }'>"
        f"{e(r['status'])}</td>"
        f"<td>${float(r['cost_usd']):.4f}</td></tr>"
        for r in runs
    ) or '<tr><td colspan=4 class=dim>прогонов ещё не было</td></tr>'

    run_at = (f"{int(s.require('publish.run_hour_local')):02d}:"
              f"{int(s.require('publish.run_minute_local')):02d}")
    return f"""<h1>Обзор</h1>
<div class=sub>Порог {s.require('cluster.sim_threshold')} · провайдер {e(provider)}
 ({e(s.require('embed.model'))}) · выпуск в {run_at} по Мадриду</div>
{warn}
<div class=cards>
  {card(a['total'], 'статей всего')}
  {card(a['day'], 'за сутки')}
  {card(a['with_body'], 'с полным текстом')}
  {card(c['open'], 'открытых сюжетов')}
  {card(c['eligible'], 'сюжетов ≥3 источников')}
  {card(f"{src['active']}", 'активных фидов')}
  {card(labels_n, 'размечено пар')}
</div>
{health}
<h2>Последние прогоны</h2>
<table><tr><th>#</th><th>начало</th><th>статус</th><th>LLM</th></tr>{rows}</table>
<div class=note>Кнопок «запустить прогон» здесь нет специально: запуск — дело cron,
а консоль нужна, чтобы смотреть и размечать.
Прогон руками — <code>python run.py --all --commit</code>.</div>"""


def view_label(qs: dict) -> str:
    from quepasa import calibrate

    s = get_settings()
    window = int(s.require("cluster.window_hours"))
    current = float(s.require("cluster.sim_threshold"))

    at = float(qs.get("at", [current])[0])
    band = float(qs.get("band", [0.12])[0])
    n = int(qs.get("n", [12])[0])

    rows = calibrate.load_corpus()
    if len(rows) < 50:
        return (
            "<h1>Разметка пар</h1>"
            f'<div class="empty">В корпусе {len(rows)} статей с эмбеддингами. '
            "Размечать рано — дай ingest поработать пару дней.</div>"
        )

    done = calibrate.labelled_keys()
    pairs = calibrate.border_pairs(
        rows, at, window, n, band, cross_source_only=True, exclude=done, seed=None
    )
    stat = calibrate.recommend_threshold(calibrate.labels())

    head = """<h1>Разметка пар</h1>
<div class=sub>Один вопрос на пару: это один и тот же сюжет или разные?
Размеченное копится в базе и сразу пересчитывает рекомендованный порог.</div>"""

    if stat and stat.get("threshold"):
        head += (
            f'<div class=note>Размечено <b>{stat["n"]}</b> пар '
            f'({stat["n_same"]} «одна», {stat["n_diff"]} «разные»). '
            f'Рекомендованный порог: <b>{stat["threshold"]}</b> '
            f'(согласие {stat["accuracy"]:.0%}). '
            f'Сейчас в конфиге {current}. <a href="/threshold">Подробнее →</a></div>'
        )
    elif stat:
        head += f'<div class=note>Размечено {stat["n"]}. {e(stat.get("note", ""))}</div>'

    if not pairs:
        return head + (
            '<div class="empty">В этой полосе неразмеченных пар не осталось.<br>'
            f'Расширь полосу: <a href="/label?at={at}&band={band + 0.1:.2f}">'
            f"band={band + 0.1:.2f}</a> или сдвинь центр.</div>"
        )

    blocks = []
    for sim, i, j in pairs:
        a, b = rows[i], rows[j]
        arts = "".join(
            f'<div class=art><div class=t>{e(r["title"])}</div>'
            f'<div class=dim style="margin-top:3px">{e(r["source_name"])} '
            f'{lean_pill(r["lean"])} · {r["published_at"]:%d.%m %H:%M} · '
            f'<a href="{e(r["url"] or "", quote=True)}" target=_blank rel=noopener>'
            f"открыть</a></div></div>"
            for r in (a, b)
        )
        blocks.append(f"""<div class=pair>
<div class=sim>близость <b>{sim:.3f}</b></div>
{arts}
<div class=btns>
  <form method=post action=/label>
    <input type=hidden name=a value="{a['id']}"><input type=hidden name=b value="{b['id']}">
    <input type=hidden name=sim value="{sim}"><input type=hidden name=same value="1">
    <input type=hidden name=at value="{at}"><input type=hidden name=band value="{band}">
    <button class=yes type=submit>Один сюжет</button>
  </form>
  <form method=post action=/label>
    <input type=hidden name=a value="{a['id']}"><input type=hidden name=b value="{b['id']}">
    <input type=hidden name=sim value="{sim}"><input type=hidden name=same value="0">
    <input type=hidden name=at value="{at}"><input type=hidden name=band value="{band}">
    <button class=no type=submit>Разные</button>
  </form>
</div></div>""")

    controls = (
        '<div class=sub style="margin-top:18px">Полоса: '
        + " · ".join(
            f'<a href="/label?at={at}&band={bb}">±{bb}</a>'
            for bb in (0.04, 0.08, 0.12, 0.2)
        )
        + f" · центр {at:.2f}</div>"
    )
    return head + "".join(blocks) + controls


def view_threshold(qs: dict) -> str:
    from quepasa import calibrate

    s = get_settings()
    window = int(s.require("cluster.window_hours"))
    current = float(s.require("cluster.sim_threshold"))

    rows = calibrate.load_corpus()
    if len(rows) < 50:
        return (
            "<h1>Порог</h1>"
            f'<div class="empty">В корпусе {len(rows)} статей. Считать нечего.</div>'
        )

    lo = float(qs.get("from", [0.30])[0])
    hi = float(qs.get("to", [0.90])[0])
    step = float(qs.get("step", [0.05])[0])

    grid = calibrate.sweep(rows, lo, hi, step, window)
    stat = calibrate.recommend_threshold(calibrate.labels())

    rec = ""
    if stat and stat.get("threshold"):
        overlap = (
            "<br>Классы пересекаются: есть «разные» пары с близостью выше, чем у "
            "некоторых «одинаковых». Идеального порога не существует, "
            "выбирай компромисс."
            if stat.get("overlap") else ""
        )
        rec = f"""<div class=note>
По <b>{stat['n']}</b> размеченным парам лучший порог — <b>{stat['threshold']}</b>,
согласие <b>{stat['accuracy']:.0%}</b>.<br>
«Одна история» опускается до {stat['same_min']}, «разные» доходят до {stat['diff_max']}.
{overlap}<br><br>
Правится вручную в <code>config/settings.yaml</code> →
<code>cluster.sim_threshold</code> (сейчас {current}).</div>"""
    else:
        rec = ('<div class=note>Размеченных пар пока мало. '
               '<a href="/label">Разметить →</a></div>')

    trs = "".join(
        f'<tr class="{"hi" if abs(g["threshold"] - current) < 1e-9 else ""}">'
        f'<td><b>{g["threshold"]:.2f}</b></td><td>{g["clusters"]}</td>'
        f'<td>{g["singletons"]}</td><td>{g["top1"]}</td>'
        f'<td class=dim>{", ".join(map(str, g["top5"]))}</td>'
        f'<td><b>{g["eligible"]}</b></td>'
        f'<td><a href="/label?at={g["threshold"]:.2f}&band=0.04">разметить</a></td></tr>'
        for g in grid
    )

    return f"""<h1>Порог кластеризации</h1>
<div class=sub>Корпус {len(rows)} статей · окно {window} ч ·
провайдер {e(s.require('embed.provider'))}</div>
{rec}
<h2>Сетка порогов</h2>
<table>
<tr><th>порог</th><th>сюжетов</th><th>одиночек</th><th>крупнейший</th>
    <th>топ-5 размеров</th><th>годных ≥3 ист.</th><th></th></tr>
{trs}</table>
<div class=sub style="margin-top:14px">Шаг:
<a href="/threshold?from={lo}&to={hi}&step=0.02">0.02</a> ·
<a href="/threshold?from={lo}&to={hi}&step=0.05">0.05</a> ·
диапазон
<a href="/threshold?from=0.30&to=0.90&step={step}">0.30–0.90</a> ·
<a href="/threshold?from=0.70&to=0.92&step={step}">0.70–0.92</a></div>"""


def view_clusters() -> str:
    from quepasa.db import connect

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.n_articles, c.n_sources, c.score, c.last_seen_at,
                   count(DISTINCT s.lean) AS lean_spread,
                   (array_agg(a.title ORDER BY a.published_at DESC))[1] AS sample,
                   string_agg(DISTINCT s.name, ', ') AS outlets
            FROM clusters c
            JOIN articles a ON a.cluster_id = c.id
            JOIN sources s  ON s.id = a.source_id
            WHERE c.status = 'open'
            GROUP BY c.id
            HAVING count(DISTINCT a.source_id) >= 2
            ORDER BY c.n_sources DESC, c.score DESC
            LIMIT 60
            """
        ).fetchall()

    if not rows:
        return ('<h1>Сюжеты</h1><div class="empty">Сюжетов с несколькими '
                "источниками пока нет. Прогони ingest и cluster.</div>")

    trs = "".join(
        f'<tr class="{"hi" if r["n_sources"] >= 3 else ""}">'
        f'<td><b>{r["n_sources"]}</b></td><td>{r["n_articles"]}</td>'
        f'<td>{r["lean_spread"]}</td><td>{float(r["score"]):.1f}</td>'
        f'<td>{e(r["sample"] or "")}<div class=dim style="margin-top:3px">'
        f'{e(r["outlets"])}</div></td>'
        f'<td><a href="/post?cluster={r["id"]}">пост →</a></td></tr>'
        for r in rows
    )
    return f"""<h1>Открытые сюжеты</h1>
<div class=sub>Подсвечены те, что проходят порог «≥3 уникальных источника» —
только они могут попасть в выпуск.</div>
<table><tr><th>ист.</th><th>статей</th><th>полюсов</th><th>скор</th>
<th>сюжет</th><th></th></tr>{trs}</table>"""


def view_digest() -> str:
    from quepasa.db import connect

    with connect() as conn:
        d = conn.execute(
            "SELECT * FROM digests ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if d and d.get("body_html"):
        gate = d.get("gate_report") or {}
        checks = ""
        if isinstance(gate, dict) and gate.get("checks"):
            checks = "".join(
                f'<tr><td class="{"ok" if c["passed"] else "bad"}">'
                f'{"прошла" if c["passed"] else "СТОП"}</td>'
                f'<td>{e(c["name"])}</td><td class=dim>{e(c["detail"])}</td></tr>'
                for c in gate["checks"]
            )
            checks = f"<h2>Ворота качества</h2><table>{checks}</table>"
        return (f'<h1>Выпуск #{d["id"]}</h1>'
                f'<div class=sub>{e(d["status"])} · {d["item_count"]} пунктов</div>'
                f'<div class=post>{d["body_html"]}</div>{checks}')

    # выпусков ещё не было — показываем, что попало бы в него сейчас
    from quepasa.stages import rank, select

    _, ranked = rank.run(dry_run=True)
    _, chosen = select.run(ranked, dry_run=True)

    if not chosen:
        return ('<h1>Выпуск</h1><div class="empty">Ни один сюжет не набрал '
                "3 уникальных источника — публиковать нечего.</div>")

    items = "".join(
        f'<div class=pair><div class=sim>сюжет {c["cluster_id"]} · '
        f'{c["n_sources"]} источников · {c["lean_spread"]} полюсов</div>'
        + "".join(
            f'<div class=art><div class=t>{e(a["title"])}</div>'
            f'<div class=dim style="margin-top:3px">{e(a["source_name"])} '
            f'{lean_pill(a["lean"])}</div></div>'
            for a in c["articles"]
        )
        + "</div>"
        for c in chosen
    )
    return f"""<h1>Что попало бы в выпуск</h1>
<div class=sub>Опубликованных выпусков ещё нет. Ниже — отбор на сейчас,
без пересказа: он требует ключа к LLM.</div>{items}"""


def view_post(qs: dict) -> str:
    """Редактор поста: markdown слева, превью как в Telegram справа."""
    from quepasa import posts
    from quepasa.db import connect
    from quepasa.markup import html_to_preview

    cid = int(qs.get("cluster", ["0"])[0])
    if not cid:
        return '<h1>Пост</h1><div class="empty">Не указан сюжет.</div>'

    with connect() as conn:
        cluster = conn.execute(
            "SELECT * FROM clusters WHERE id = %s", (cid,)
        ).fetchone()
        if cluster is None:
            return f'<h1>Пост</h1><div class="empty">Сюжета {cid} нет.</div>'
        post = posts.get_post(conn, cid)
        articles = posts.cluster_articles(conn, cid)

    header_md = (post or {}).get("header_md") or posts.default_header_md(articles)
    category = (post or {}).get("category") or ""
    status = (post or {}).get("status") or "draft"

    full_md = posts.compose_md(header_md, articles, category)
    preview = html_to_preview(posts.compose_html(header_md, articles, category))

    cats = "".join(
        f'<option value="{e(c)}"{" selected" if c == category else ""}>{e(c)}</option>'
        for c in [""] + list(posts.TOPIC_HASHTAG)
    )

    flash = ""
    if qs.get("err"):
        flash = f'<div class="note warn"><b>Не получилось:</b> {e(qs["err"][0])}</div>'
    elif qs.get("ok"):
        msg = {"published": "Опубликовано в канал.",
               "published_loud": "Опубликовано со звуком.",
               "generated": "Черновик сгенерирован — прочитай и поправь.",
               "edited": "Пост дополнен новыми изданиями.",
               "unchanged": "Новых изданий нет — правка не потребовалась.",
               "skip": "Пост ещё не опубликован."}.get(qs["ok"][0], qs["ok"][0])
        flash = f'<div class=note>{e(msg)}</div>'

    published_note = ""
    if status == "published" and post.get("message_id"):
        from quepasa.config import env
        chan = env("TELEGRAM_CHANNEL_ID", "").lstrip("@")
        link = f"https://t.me/{chan}/{post['message_id']}" if chan else "#"
        known = set(post.get("posted_source_ids") or [])
        fresh = sorted({a["source_id"] for a in articles} - known)
        extra = (
            f"<br>С момента публикации добавились: <b>{e(', '.join(fresh))}</b>. "
            "Нажми «Обновить пост» — бот поправит сообщение, ручной текст не тронется."
            if fresh else "<br>Новых изданий с момента публикации нет."
        )
        published_note = (
            f'<div class=note>Опубликован '
            f'<a href="{e(link, quote=True)}" target=_blank rel=noopener>в канале</a>'
            f' · правок: {post.get("edit_count", 0)}{extra}</div>'
        )

    srcs = "".join(
        f'<div class=art><div class=t>{e(a["title"])}</div>'
        f'<div class=dim style="margin-top:3px">{e(a["source_name"])} '
        f'{lean_pill(a["lean"])} · {a["published_at"]:%d.%m %H:%M} · '
        f'<a href="{e(a.get("url") or a["url_canonical"], quote=True)}" '
        f'target=_blank rel=noopener>открыть</a></div></div>'
        for a in articles
    )

    from quepasa.db import connect as _conn
    with _conn() as _c:
        quota = posts.sound_quota_left(_c)
    big = int(cluster["n_sources"]) >= int(
        get_settings().get_path("autopost.sound.min_sources", 5))
    sound_checked = " checked" if (big and quota > 0) else ""
    sound_hint = (
        f'<span class=dim>— осталось на сегодня: {quota}</span>' if quota
        else '<span class=warn>— дневная квота выбрана</span>'
    )

    publish_btn = (
        '<button class=go name=action value=publish type=submit>Опубликовать в канал</button>'
        if status != "published" else
        '<button class=go name=action value=sync type=submit>Обновить пост</button>'
    )

    return f"""<h1>Сюжет {cid}</h1>
<div class=sub>{cluster["n_sources"]} источников · {cluster["n_articles"]} статей ·
статус: <b>{e(status)}</b></div>
{flash}{published_note}
<form method=post action="/post">
<input type=hidden name=cluster value="{cid}">
<div class=grid2>
  <div>
    <label>Заголовок и текст (markdown)</label>
    <textarea name=header_md
      placeholder="**Нейтральный заголовок по-русски**">{e(header_md)}</textarea>
    <div class=sub style="margin-top:6px">
      <code>**жирный**</code> · <code>_курсив_</code> ·
      <code>[текст](ссылка)</code>. Блок ссылок и хэштег добавятся сами.
    </div>
    <label>Категория (хэштег)</label>
    <select name=category>{cats}</select>
    <label>Уведомление</label>
    <label style="text-transform:none;letter-spacing:0;font-size:14px;color:#e6e8ee">
      <input type=checkbox name=with_sound value=1{sound_checked}>
      со звуком {sound_hint}
    </label>
    <div class=row>
      <button name=action value=generate type=submit>✨ Сгенерировать</button>
      <button name=action value=save type=submit>Сохранить черновик</button>
      {publish_btn}
    </div>
    <div class=sub style="margin-top:6px">«Сгенерировать» просит модель
    сформулировать событие по-русски нейтрально. Это заготовка — читай и правь
    перед публикацией.</div>
  </div>
  <div>
    <label>Как будет выглядеть в Telegram</label>
    <div class=tg>{preview}</div>
    <label>Итоговый markdown</label>
    <div class=post style="font-size:13px">{e(full_md)}</div>
  </div>
</div>
</form>
<h2>Источники сюжета ({len(articles)})</h2>
<div class=pair>{srcs}</div>"""


def view_posts() -> str:
    from quepasa.config import env
    from quepasa.db import connect

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.*, c.n_sources, c.n_articles,
                   (SELECT count(DISTINCT a.source_id) FROM articles a
                    WHERE a.cluster_id = p.cluster_id) AS current_sources
            FROM posts p JOIN clusters c ON c.id = p.cluster_id
            ORDER BY p.published_at DESC NULLS FIRST, p.id DESC
            """
        ).fetchall()

    if not rows:
        return ('<h1>Посты</h1><div class="empty">Постов пока нет.<br>'
                'Открой <a href="/clusters">Сюжеты</a> и подготовь первый.</div>')

    chan = env("TELEGRAM_CHANNEL_ID", "").lstrip("@")
    trs = ""
    for r in rows:
        known = len(r["posted_source_ids"] or [])
        fresh = int(r["current_sources"]) - known
        if r["status"] == "published":
            link = (f'<a href="https://t.me/{chan}/{r["message_id"]}" target=_blank '
                    f'rel=noopener>в канале</a>') if chan else "опубликован"
            state = f'<span class=ok>{link}</span>'
        else:
            state = f'<span class=dim>{e(r["status"])}</span>'
        pending = (f'<span class=warn>+{fresh}</span>'
                   if r["status"] == "published" and fresh > 0 else '<span class=dim>—</span>')
        head = (r["header_md"] or "").strip().replace("**", "")[:80]
        trs += (
            f'<tr><td>{state}</td>'
            f'<td><a href="/post?cluster={r["cluster_id"]}">{e(head) or "(без заголовка)"}</a></td>'
            f'<td>{e(r["category"] or "—")}</td>'
            f'<td>{r["current_sources"]}</td><td>{pending}</td>'
            f'<td>{r["edit_count"]}</td></tr>'
        )

    return f"""<h1>Посты</h1>
<div class=sub>Колонка «новых» — издания, появившиеся после публикации.
Обновить всё сразу: <code>python run.py --sync-posts --commit</code>.</div>
<table><tr><th>статус</th><th>заголовок</th><th>категория</th>
<th>источников</th><th>новых</th><th>правок</th></tr>{trs}</table>"""


def view_sources() -> str:
    from quepasa.db import connect

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, count(a.id) AS articles,
                   count(a.body) AS with_body,
                   max(a.published_at) AS newest
            FROM sources s LEFT JOIN articles a ON a.source_id = s.id
            GROUP BY s.id ORDER BY s.status, count(a.id) DESC
            """
        ).fetchall()

    # издание, у которого текст перестал доставаться, — тихая поломка:
    # статьи идут, но пересказ работает по одному анонсу
    starved = [
        r for r in rows
        if r["status"] == "active" and r["articles"] >= 10
        and r["with_body"] * 10 < r["articles"] and r.get("body_fetch", True)
    ]
    alarm = ""
    if starved:
        names = ", ".join(e(r["name"]) for r in starved)
        alarm = (
            f'<div class="note warn"><b>Полный текст не достаётся: {names}.</b><br>'
            "Обычно это антибот-защита издания (403 на загрузку страницы). "
            "Статьи не теряются — пересказ пойдёт по анонсу из фида, — но если "
            "так навсегда, выключи загрузку тела для этого источника: "
            "<code>body_fetch: false</code> в <code>config/sources.yaml</code>, "
            "чтобы не жечь запросы впустую.</div>"
        )

    trs = ""
    for r in rows:
        if r["status"] != "active":
            state = '<span class=dim>отключён</span>'
        elif r["last_ok_at"] is None:
            state = '<span class=warn>не опрашивался</span>'
        else:
            state = '<span class=ok>ок</span>'
        pct = (
            f'{100 * r["with_body"] // r["articles"]}%' if r["articles"] else "—"
        )
        if not r.get("body_fetch", True):
            pct = '<span class=dim>выкл</span>'
        elif r["articles"] >= 10 and r["with_body"] * 10 < r["articles"]:
            pct = f'<span class=bad>{pct}</span>'
        trs += (
            f"<tr><td>{state}</td><td><b>{e(r['name'])}</b>"
            f"<div class=dim>{e(r['id'])}</div></td>"
            f"<td>{lean_pill(r['lean'])}</td><td class=dim>{e(r['type'])}</td>"
            f"<td>{r['articles']}</td><td>{pct}</td>"
            f"<td class=dim>{r['newest']:%d.%m %H:%M}</td></tr>"
            if r["newest"] else
            f"<tr><td>{state}</td><td><b>{e(r['name'])}</b>"
            f"<div class=dim>{e(r['id'])}</div></td>"
            f"<td>{lean_pill(r['lean'])}</td><td class=dim>{e(r['type'])}</td>"
            f"<td>0</td><td>—</td><td class=dim>—</td></tr>"
        )

    return f"""<h1>Источники</h1>
<div class=sub>Правится в <code>config/sources.yaml</code>. Проверка —
<code>python scripts/validate_feeds.py --check-current</code>.</div>
{alarm}
<table><tr><th>статус</th><th>издание</th><th>полюс</th><th>тип</th>
<th>статей</th><th>с текстом</th><th>свежайшая</th></tr>{trs}</table>"""


# ----------------------------------------------------------------- сервер

ROUTES = {
    "/": lambda qs: view_overview(),
    "/label": view_label,
    "/threshold": view_threshold,
    "/clusters": lambda qs: view_clusters(),
    "/posts": lambda qs: view_posts(),
    "/post": view_post,
    "/digest": lambda qs: view_digest(),
    "/sources": lambda qs: view_sources(),
}

TITLES = dict(TABS)
TITLES["/post"] = "Пост"
TITLES["/digest"] = "Выпуск"


class Handler(BaseHTTPRequestHandler):
    server_version = "QuePasaConsole/0"

    def log_message(self, fmt, *args):  # тише в консоли
        pass

    def _send(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        view = ROUTES.get(parsed.path)
        if view is None:
            self._send(page("404", parsed.path, "<h1>Нет такой страницы</h1>"), 404)
            return
        try:
            body = view(parse_qs(parsed.query))
        except Exception:
            body = (
                "<h1>Ошибка</h1><div class=note>Скорее всего не поднята база "
                "или не применены миграции: <code>python run.py --migrate</code>.</div>"
                f"<div class=post>{e(traceback.format_exc())}</div>"
            )
        self._send(page(TITLES.get(parsed.path, "Консоль"), parsed.path, body))

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _handle_post_editor(self, form):
        """Сохранение черновика, публикация и дополнение поста."""
        from quepasa import posts
        from quepasa.db import connect

        cid = int(form.get("cluster", ["0"])[0])
        action = form.get("action", ["save"])[0]
        header_md = form.get("header_md", [""])[0]
        category = form.get("category", [""])[0]

        with connect() as conn:
            posts.upsert_draft(conn, cid, header_md, category)

        note = ""
        try:
            if action == "publish":
                loud = form.get("with_sound", [""])[0] == "1"
                res = posts.publish(cid, dry_run=False, silent=not loud)
                note = ("?ok=published" if res.get("silent") else "?ok=published_loud")
            elif action == "generate":
                from quepasa.db import connect as _c
                header, topic, meta = posts.generate_header(cid)
                with _c() as conn:
                    posts.upsert_draft(conn, cid, header, topic or category)
                note = f"?ok=generated&cost={meta['cost_usd']}"
            elif action == "sync":
                res = posts.sync_post(cid, dry_run=False)
                note = f"?ok={res['status']}"
        except Exception as exc:  # noqa: BLE001 — ошибку показываем, а не роняем консоль
            traceback.print_exc()
            note = "?err=" + quote(str(exc)[:200])

        sep = "&" if note else "?"
        self._redirect(f"/post{note}{sep}cluster={cid}")

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)

        if path == "/post":
            self._handle_post_editor(form)
            return
        if path != "/label":
            self._send(b"not found", 404, "text/plain")
            return

        from quepasa import calibrate

        try:
            calibrate.save_label(
                int(form["a"][0]), int(form["b"][0]),
                form["same"][0] == "1", float(form["sim"][0]),
            )
        except Exception:
            traceback.print_exc()

        at = form.get("at", ["0.5"])[0]
        band = form.get("band", ["0.12"])[0]
        self.send_response(303)
        self.send_header("Location", f"/label?at={at}&band={band}")
        self.end_headers()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="только localhost по умолчанию")
    args = ap.parse_args()

    load_dotenv()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Консоль оператора: http://{args.host}:{args.port}")
    print("Ctrl+C — остановить")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
