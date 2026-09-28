"""Google Sheet connection through a small Apps Script web app (assets/VidAudCont_connector.gs).

The script runs inside the user's spreadsheet under their Google account, so the program needs no
Google Cloud project or sign-in: it only knows the web app URL and a shared key.
"""
import base64
import colorsys
import http.client
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, resources
from .applog import log_event

PLACEHOLDER = "__VIDAUDCONT_KEY__"
# Google's web apps now and then answer 404, 5xx or one of its own pages to a request that works a moment later
TRANSIENT_HTTP = {404, 408, 429, 500, 502, 503, 504}
RETRY_PAUSE = 2.0  # seconds before the first repeat, doubled for each next one: 2, 4, 8, 16
USER_AGENT = f"VidAudCont/{__version__}"


class SheetError(RuntimeError):
    def __init__(self, msg, transient=False):
        super().__init__(msg)
        self.transient = transient


SCRIPT_VERSION = 2  # the connector that reports row colours
CONNECTION_PREFIX = "VIDAUDCONT-CONNECTION:"
CONNECTION_FIELDS = ("url", "key", "sheet", "link_col", "cuts_col", "note_col")


def export_connection(cfg):
    """The table connection as one line of text, to set up the program on another computer. The script in
    the table accepts one key: every computer has to use the same address and key (never a new script)."""
    data = {k: cfg.get(k) for k in CONNECTION_FIELDS if cfg.get(k)}
    return CONNECTION_PREFIX + base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).decode("ascii")


def import_connection(text):
    """The fields of a line made by export_connection (spaces and line breaks added by messengers are ignored)."""
    text = "".join((text or "").split())
    if not text.startswith(CONNECTION_PREFIX):
        raise ValueError("это не подключение VidAudCont: на другом компьютере нажмите «Скопировать подключение»")
    try:
        data = json.loads(base64.urlsafe_b64decode(text[len(CONNECTION_PREFIX):].encode("ascii")))
    except ValueError:
        raise ValueError("строка подключения повреждена — скопируйте её ещё раз целиком") from None
    if not (isinstance(data, dict) and data.get("url") and data.get("key")):
        raise ValueError("в строке подключения нет адреса или ключа")
    return {k: str(data[k]) for k in CONNECTION_FIELDS if data.get(k)}


def is_red(color):
    """Red, dark or light red, red berry: a fill or text colour the user marks rows to skip with."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (color or "").strip())
    if not m:
        return False
    r, g, b = (int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return (h <= 20 / 360 or h >= 340 / 360) and s >= 0.15 and v >= 0.25


def row_marked_red(row):
    return any(is_red(c) for c in row.get("bg") or []) or is_red(row.get("fg"))


def new_key():
    return secrets.token_urlsafe(18)


def script_code(key):
    with open(resources.asset("VidAudCont_connector.gs"), encoding="utf-8") as f:
        return f.read().replace(PLACEHOLDER, key)


def col_number(letter_or_number):
    s = str(letter_or_number).strip().upper()
    if s.isdigit():
        return int(s)
    n = 0
    for ch in s:
        if not "A" <= ch <= "Z":
            raise ValueError(f"не понимаю колонку «{letter_or_number}»")
        n = n * 26 + ord(ch) - 64
    return n


class SheetClient:
    def __init__(self, url, key, sheet="", link_col="C", cuts_col="D", note_col="E", timeout=90, retries=4,
                 cancelled=None):
        if not url or not url.strip().startswith(("https://", "http://127.0.0.1")):  # 127.0.0.1: tests only
            raise SheetError("не указан адрес веб-приложения (https://script.google.com/…/exec)")
        self.url, self.key, self.sheet, self.timeout = url.strip(), key, sheet.strip(), timeout
        self.retries, self.cancelled = retries, cancelled
        self.version = None
        self.cols = {"link_col": col_number(link_col), "cuts_col": col_number(cuts_col),
                     "note_col": col_number(note_col)}

    @classmethod
    def from_config(cls, cfg, **kw):
        return cls(cfg.get("url", ""), cfg.get("key", ""), cfg.get("sheet", ""), cfg.get("link_col", "C"),
                   cfg.get("cuts_col", "D"), cfg.get("note_col", "E"), **kw)

    def _call(self, payload, post=False):
        """Passing failures are repeated with growing pauses: every action is safe to repeat (a write only sets
        the same cells again, even if the first attempt reached the table and just its answer got lost)."""
        for attempt in range(self.retries + 1):
            try:
                return self._call_once(payload, post)
            except SheetError as e:
                if not e.transient:
                    raise
                if attempt == self.retries:
                    if attempt:
                        raise SheetError(f"{e} Попыток: {attempt + 1}.", transient=True) from e
                    raise
                pause = RETRY_PAUSE * 2 ** attempt
                log_event(f"таблица ({payload.get('action')}): {e} — повтор через {pause:g} с")
                end = time.monotonic() + pause
                while time.monotonic() < end:
                    if self.cancelled and self.cancelled():
                        raise InterruptedError("остановлено") from e
                    time.sleep(min(0.25, max(0.0, end - time.monotonic())))

    def _call_once(self, payload, post):
        payload = dict(payload, key=self.key, **self.cols)
        if self.sheet:
            payload["sheet"] = self.sheet
        if post:
            req = urllib.request.Request(self.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        else:
            req = urllib.request.Request(self.url + ("&" if "?" in self.url else "?") + urllib.parse.urlencode(payload),
                                         headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # Google answers via a redirect
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in TRANSIENT_HTTP:
                raise SheetError(f"Google не ответил (ошибка {e.code}). Обычно это временный сбой Google — "
                                 "попробуйте ещё раз через минуту. Если ошибка не проходит, проверьте адрес "
                                 "веб-приложения в «Таблица…».", transient=True) from e
            raise SheetError(f"Google ответил ошибкой {e.code}. Проверьте адрес веб-приложения.") from e
        except (OSError, http.client.HTTPException) as e:  # no network, timeout, dropped connection
            raise SheetError(f"нет связи с Google: {getattr(e, 'reason', None) or e}", transient=True) from e
        try:
            data = json.loads(raw)
        except ValueError:  # Google's error page, or its sign-in page if the access is not "Anyone"
            raise SheetError("вместо ответа скрипта пришла страница Google. Проверьте, что при развёртывании "
                             "выбран доступ «Все» и что адрес заканчивается на /exec.", transient=True) from None
        if not data.get("ok"):
            err = data.get("error", "неизвестная ошибка")
            if err == "wrong key":
                err = ("ключ не совпадает: код в таблице поставлен с другого компьютера. Там откройте «Таблица…» → "
                       "«Скопировать подключение», а здесь — «Вставить подключение». Код в Apps Script не меняйте")
            raise SheetError(err)
        return data

    def ping(self):
        return self._call({"action": "ping"})

    def rows(self):
        """[{row, link, id, cuts, note, bg, fg}] for every row whose link column holds a YouTube link.
        self.version tells whether the script in the table reports row colours (2) or not (1)."""
        data = self._call({"action": "rows"})
        self.version = data.get("version", 1)
        return data["rows"]

    def write(self, updates, batch=100):
        """updates: [{row, cuts?, note?}] -> number written."""
        n = 0
        for i in range(0, len(updates), batch):
            n += self._call({"action": "write", "updates": updates[i:i + batch]}, post=True)["written"]
        return n
