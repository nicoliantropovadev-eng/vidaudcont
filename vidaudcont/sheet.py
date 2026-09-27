"""Google Sheet connection through a small Apps Script web app (assets/VidAudCont_connector.gs).

The script runs inside the user's spreadsheet under their Google account, so the program needs no
Google Cloud project or sign-in: it only knows the web app URL and a shared key.
"""
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from . import resources

PLACEHOLDER = "__VIDAUDCONT_KEY__"


class SheetError(RuntimeError):
    pass


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
    def __init__(self, url, key, sheet="", link_col="C", cuts_col="D", note_col="E", timeout=90):
        if not url or not url.strip().startswith(("https://", "http://127.0.0.1")):  # 127.0.0.1: tests only
            raise SheetError("не указан адрес веб-приложения (https://script.google.com/…/exec)")
        self.url, self.key, self.sheet, self.timeout = url.strip(), key, sheet.strip(), timeout
        self.cols = {"link_col": col_number(link_col), "cuts_col": col_number(cuts_col),
                     "note_col": col_number(note_col)}

    @classmethod
    def from_config(cls, cfg):
        return cls(cfg.get("url", ""), cfg.get("key", ""), cfg.get("sheet", ""), cfg.get("link_col", "C"),
                   cfg.get("cuts_col", "D"), cfg.get("note_col", "E"))

    def _call(self, payload, post=False):
        payload = dict(payload, key=self.key, **self.cols)
        if self.sheet:
            payload["sheet"] = self.sheet
        if post:
            req = urllib.request.Request(self.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(self.url + ("&" if "?" in self.url else "?") + urllib.parse.urlencode(payload))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # Google answers via a redirect
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise SheetError(f"Google ответил ошибкой {e.code}. Проверьте адрес веб-приложения.") from e
        except urllib.error.URLError as e:
            raise SheetError(f"нет связи с Google: {e.reason}") from e
        try:
            data = json.loads(raw)
        except ValueError:
            raise SheetError("вместо ответа скрипта пришла страница Google. Проверьте, что при развёртывании "
                             "выбран доступ «Все» и что адрес заканчивается на /exec.") from None
        if not data.get("ok"):
            err = data.get("error", "неизвестная ошибка")
            if err == "wrong key":
                err = "ключ не совпадает: скопируйте код скрипта из программы заново и сделайте новое развёртывание"
            raise SheetError(err)
        return data

    def ping(self):
        return self._call({"action": "ping"})

    def rows(self):
        """[{row, link, id, cuts, note}] for every row whose link column holds a YouTube link."""
        return self._call({"action": "rows"})["rows"]

    def write(self, updates, batch=100):
        """updates: [{row, cuts?, note?}] -> number written."""
        n = 0
        for i in range(0, len(updates), batch):
            n += self._call({"action": "write", "updates": updates[i:i + batch]}, post=True)["written"]
        return n
