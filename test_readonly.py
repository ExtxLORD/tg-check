#!/usr/bin/env python3
"""Static verification: bot.py must contain no mail-mutating or sending code paths."""
import re
import sys
import warnings

warnings.simplefilter("error", SyntaxWarning)
sys.path.insert(0, ".")
import bot as b  # noqa: E402  (import must be clean: no SyntaxWarning)

src = open("bot.py", encoding="utf-8").read()
# Look only at real code: strip docstrings and comments
code = re.sub(r'r?"""[\s\S]*?"""', '""', src)
code = re.sub(r"#.*", "", code)

fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + f" {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        fails.append(name)


FORBIDDEN = {
    "smtplib": r"\bsmtplib\b",
    "SMTP()": r"\bSMTP\s*\(",
    "DELETE": r"['\"]DELETE['\"]|\bDELETE\s*\(",
    "EXPUNGE": r"['\"]EXPUNGE['\"]|\bEXPUNGE\s*\(",
    "MOVE": r"['\"]MOVE['\"]",
    "-FLAGS": r"-FLAGS",
    "APPEND": r"['\"]APPEND['\"]",
    "RENAME": r"['\"]RENAME['\"]",
}
for name, pat in FORBIDDEN.items():
    check(f"code has no {name}", not re.search(pat, code))

# select должен быть read-write: иначе сервер запретит STORE +\Seen после доставки
check("no readonly select (would block \\Seen)", "readonly=True" not in code)
check("BODY.PEEK used", "BODY.PEEK[]" in code)
n_store = len(re.findall(r"uid\(\s*['\"]store['\"]", code))
check("exactly 1 uid(store) path", n_store == 1, f"found {n_store}")
check(
    "that store is +FLAGS (\\Seen) only",
    bool(re.search(
        r"uid\(\s*['\"]store['\"],\s*uid,\s*['\"]\+FLAGS['\"],\s*r?['\"]\(\\Seen\)['\"]",
        code,
    )),
)

tg_posts = set(re.findall(r'_post\(\s*["\'](\w+)["\']', code))
tg_posts.add("getUpdates")  # called via self.tg.get_updates -> requests.get
allowed_tg = {"sendMessage", "sendDocument", "getMe", "getUpdates"}
check("Telegram methods limited to read-out", tg_posts <= allowed_tg, str(tg_posts))

# no other chat allowed: sendMessage/sendDocument always use cfg.tg_chat_id
check("all TG sends use configured chat_id",
      code.count("self.cfg.tg_chat_id") >= 3 and "chat_id\": msg" not in code)

# parsing smoke (regression of earlier suite)
import base64
raw = "\r\n".join([
    "From: a@b.c", "Subject: =?UTF-8?B?" + base64.b64encode("Привет".encode()).decode() + "?=",
    "MIME-Version: 1.0", 'Content-Type: multipart/mixed; boundary="X"', "",
    "--X", "Content-Type: text/plain; charset=utf-8", "", "текст письма", "",
    "--X", 'Content-Type: application/pdf; name="f.pdf"',
    'Content-Disposition: attachment; filename="f.pdf"',
    "Content-Transfer-Encoding: base64", "",
    base64.b64encode(b"%PDF").decode(), "--X--",
]).encode()
m = b.parse_message(raw, "9")
check("parse: body", "текст письма" in m.body)
check("parse: subject", "Привет" in m.subject)
check("parse: attachment", len(m.attachments) == 1 and m.attachments[0].data == b"%PDF")

chunks = b.split_text("A" * 9000 + "\n" + "Б" * 9000, 4000)
check("split_text lossless", "".join(chunks) == "A" * 9000 + "\n" + "Б" * 9000)
check("split_text within limit", all(len(c) <= 4000 for c in chunks))

# Регрессия: однострочное письмо длиннее лимита давало ПУСТОЙ первый чанк.
# Telegram отвечает на пустой текст 400-й -> доставка падает -> письмо не
# помечается \Seen и пересылается каждую проверку (спам-петля).
for name, sample in {
    "одна строка 12000": "X" * 12000,
    "одна строка ровно 2 лимита": "X" * 8000,
    "текст + длинная строка": "Привет\n" + "Y" * 9000,
    "строка ровно в лимит": "Z" * 4000,
}.items():
    parts = b.split_text(sample, 4000)
    check(f"split_text: нет пустых чанков ({name})", all(p != "" for p in parts), str([len(p) for p in parts]))
    check(f"split_text: lossless ({name})", "".join(parts) == sample)

# Пустой/пробельный текст вообще не должен уходить в Telegram (иначе 400 + повтор письма)
_sent = []
_bot = b.Bot(b.Config(imap_host="h", imap_port=993, mail_login="l", mail_password="p",
                      tg_token="t", tg_chat_id="1", check_interval=300, imap_folder="INBOX",
                      max_per_check=25, max_attachment_mb=50))
_bot.tg._post = lambda *a, **k: _sent.append(a) or {"ok": True}
_bot.tg.send_text("")
_bot.tg.send_text("   \n  ")
check("send_text: пустой/пробельный текст не отправляется", not _sent)
_bot.tg.send_text("ок")
check("send_text: обычный текст отправляется", len(_sent) == 1)

# Регрессия: задублированный блок в _handle_error слал ДВА уведомления об одной ошибке
_sent.clear()
_bot.tg.send_text = lambda text, html=False: _sent.append(text)
_bot._handle_error("сбой")
check("_handle_error: ровно одно уведомление на ошибку", len(_sent) == 1, f"отправлено {len(_sent)}")

# ---------------------------------------------------------------------------
# Регрессия: токен бота не должен попадать в логи.
# requests подставляет полный URL (bot<TOKEN>/method) в текст своих исключений —
# из-за этого токен уже утекал в логи Render.
import logging as _logging
import requests as _rq

_TOKEN = "8704709555:AAEJd6T_SUPERSECRET"
_sec = b.Bot(b.Config(imap_host="h", imap_port=993, mail_login="l", mail_password="p",
                      tg_token=_TOKEN, tg_chat_id="1", check_interval=300, imap_folder="INBOX",
                      max_per_check=25, max_attachment_mb=50))

check("_redact: токен вычищается",
      _TOKEN not in _sec.tg._redact(f"url: https://api.telegram.org/bot{_TOKEN}/getUpdates"))


class _FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self.text = body

    def json(self):
        import json as _j
        return _j.loads(self.text)


class _Capture(_logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, rec):
        self.msgs.append(rec.getMessage())


_cap = _Capture()
_cap.setLevel(_logging.DEBUG)
b.log.addHandler(_cap)

# сетевая ошибка: requests поднимает ConnectionError с URL, содержащим токен
_orig_post = b.requests.post
b.requests.post = lambda *a, **k: (_ for _ in ()).throw(
    _rq.ConnectionError(f"Max retries exceeded with url: /bot{_TOKEN}/sendMessage"))
try:
    _sec.tg._post("sendMessage")
    check("_post: сетевая ошибка -> RuntimeError", False)
except RuntimeError as exc:
    check("_post: токен не утекает в исключение", _TOKEN not in str(exc), str(exc)[:70])
finally:
    b.requests.post = _orig_post

# 409 Conflict: понятное сообщение про второй экземпляр, без токена, с троттлингом
_orig_get, _orig_sleep = b.requests.get, b.time.sleep
b.requests.get = lambda *a, **k: _FakeResp(409, '{"ok":false,"error_code":409,"description":'
                                               '"Conflict: terminated by other getUpdates request; '
                                               'make sure that only one bot instance is running"}')
b.time.sleep = lambda s: None
try:
    _cap.msgs.clear()
    _sec.tg.get_updates(timeout=1)
    _sec.tg.get_updates(timeout=1)      # внутри окна троттлинга — второго лога быть не должно
finally:
    b.requests.get, b.time.sleep = _orig_get, _orig_sleep
b.log.removeHandler(_cap)

_conflict_logs = [m for m in _cap.msgs if "ДРУГОЙ экземпляр" in m]
check("409: понятное сообщение про второй экземпляр", len(_conflict_logs) >= 1)
check("409: троттлинг (одно сообщение на окно)", len(_conflict_logs) == 1, f"сообщений: {len(_conflict_logs)}")
check("логи без токена", all(_TOKEN not in m for m in _cap.msgs), str([m[:60] for m in _cap.msgs]))

print()
print("ALL PASSED" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
