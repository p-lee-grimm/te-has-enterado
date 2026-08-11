"""Markdown -> HTML-разметка Telegram.

Почему не MarkdownV2: там нужно экранировать полтора десятка символов, включая
точку, минус и скобки. Испанские и русские тексты полны и тем и другим, одна
пропущенная точка — и Telegram отвергает всё сообщение. Поэтому редактируем
в markdown, а отправляем HTML: экранируем ВСЁ, а потом вставляем ровно те теги,
которые разрешены.

Поддерживается подмножество, которое понимает Telegram:
    **жирный**  __курсив__ или _курсив_  `код`  ~~зачёркнутый~~
    [текст](https://ссылка)
    ||спойлер||
Всё остальное остаётся текстом.
"""

from __future__ import annotations

import html
import re

# Разрешённые схемы ссылок. tg:// нужен для упоминаний, остальное отсекаем:
# javascript: в ссылке Telegram не выполнит, но и пропускать его незачем.
_SAFE_SCHEMES = ("http://", "https://", "tg://")

_TOKEN = "\x00{}\x00"  # плейсхолдер, которого не бывает в пользовательском тексте


def _placeholder(i: int) -> str:
    return _TOKEN.format(i)


def markdown_to_telegram_html(text: str) -> str:
    """Переводит markdown в HTML, пригодный для parse_mode=HTML.

    Порядок важен: сначала вынимаем ссылки и код в плейсхолдеры, потом
    экранируем весь текст, потом возвращаем готовые теги на место. Так
    угловые скобки и амперсанды в тексте не могут превратиться в разметку.
    """
    if not text:
        return ""

    parts: list[str] = []

    def stash(rendered: str) -> str:
        parts.append(rendered)
        return _placeholder(len(parts) - 1)

    out = text

    # `код` — раньше всего: внутри него markdown не работает
    def repl_code(m: re.Match) -> str:
        return stash(f"<code>{html.escape(m.group(1))}</code>")

    out = re.sub(r"`([^`\n]+)`", repl_code, out)

    # [текст](url)
    def repl_link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2).strip()
        if not url.lower().startswith(_SAFE_SCHEMES):
            # не ссылка — оставляем как обычный текст, чтобы не потерять смысл
            return stash(html.escape(f"[{label}]({url})"))
        return stash(
            f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
        )

    out = re.sub(r"\[([^\]\n]*)\]\(([^)\s]+)\)", repl_link, out)

    # остальной текст экранируем целиком
    out = html.escape(out)

    # и только теперь — простые парные маркеры
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"~~(.+?)~~", r"<s>\1</s>", out, flags=re.S)
    out = re.sub(r"\|\|(.+?)\|\|", r'<span class="tg-spoiler">\1</span>', out, flags=re.S)
    out = re.sub(r"__(.+?)__", r"<i>\1</i>", out, flags=re.S)
    # одиночное подчёркивание — курсив, но не внутри слова (some_var_name)
    out = re.sub(r"(?<![\w\\])_([^_\n]+)_(?!\w)", r"<i>\1</i>", out)

    for i, rendered in enumerate(parts):
        out = out.replace(_placeholder(i), rendered)
    return out


def strip_markdown(text: str) -> str:
    """Голый текст — для оценки длины и превью."""
    out = re.sub(r"\[([^\]\n]*)\]\([^)\s]+\)", r"\1", text or "")
    out = re.sub(r"(\*\*|~~|\|\||__)", "", out)
    out = re.sub(r"`([^`\n]+)`", r"\1", out)
    return out


def html_to_preview(rendered_html: str) -> str:
    """HTML Telegram -> HTML для страницы превью.

    Теги Telegram (b/i/s/code/a) браузер понимает как есть; экранировать заново
    ничего не надо — вход уже безопасен, его собрал markdown_to_telegram_html.
    Меняем только перевод строки на <br>, чтобы совпадало с видом в мессенджере.
    """
    return (rendered_html or "").replace("\n", "<br>")
