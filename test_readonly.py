#!/usr/bin/env python3
"""Static verification: bot.py must contain no mail-mutating or sending code paths."""
import re
import sys
import time
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
        self.levels = []

    def emit(self, rec):
        self.msgs.append(rec.getMessage())
        self.levels.append(rec.levelname)


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

# 409 Conflict: разбираем два случая — наложение при деплое и реальный второй экземпляр
_orig_get, _orig_sleep = b.requests.get, b.time.sleep
b.requests.get = lambda *a, **k: _FakeResp(409, '{"ok":false,"error_code":409,"description":'
                                               '"Conflict: terminated by other getUpdates request; '
                                               'make sure that only one bot instance is running"}')
b.time.sleep = lambda s: None
try:
    # случай 1: конфликт случился сразу после старта -> это наложение при деплое
    _cap.msgs.clear(); _cap.levels.clear()
    _sec.tg._started = time.time()
    _sec.tg._last_conflict_warn = 0.0
    _sec.tg.conflict_count = 0
    _sec.tg.get_updates(timeout=1)
    _sec.tg.get_updates(timeout=1)      # внутри окна троттлинга — второго лога быть не должно
    _s409 = [m for m in _cap.msgs if "409" in m]
    check("409 сразу после старта: warning про наложение при деплое",
          len(_s409) == 1 and _cap.levels[0] == "WARNING" and "наложение" in _s409[0].lower(),
          f"{_cap.levels[:1]} {(_s409[:1] or [''])[0][:60]}")

    # случай 2: конфликт при большом аптайме -> это настоящий второй экземпляр
    _cap.msgs.clear(); _cap.levels.clear()
    _sec.tg._started = time.time() - 400
    _sec.tg._last_conflict_warn = 0.0
    _sec.tg.get_updates(timeout=1)
    _s409 = [m for m in _cap.msgs if "ДРУГОЙ экземпляр" in m]
    check("409 при долгом аптайме: ERROR про второй экземпляр",
          len(_s409) == 1 and _cap.levels[0] == "ERROR", _cap.levels[:1])
    check("409: счётчик конфликтов растёт", _sec.tg.conflict_count > 0, str(_sec.tg.conflict_count))
finally:
    b.requests.get, b.time.sleep = _orig_get, _orig_sleep

check("логи без токена", all(_TOKEN not in m for m in _cap.msgs), str([m[:60] for m in _cap.msgs]))

# предупреждение о конфликте уходит в чат только при настоящей проблеме
_alerts = []
_sec.tg.send_text = lambda text, html=False: _alerts.append(text)

_sec.tg.conflict_count = 9
_sec.started_at = time.time()          # свежий старт — не пугаем пользователя
_sec._last_conflict_alert = 0.0
_sec._maybe_alert_conflicts()
check("409 в первые минуты: чат не пугаем", not _alerts)

_sec.started_at = time.time() - 400    # аптайм > 3 минут — конфликт систематический
_sec._maybe_alert_conflicts()
_sec._maybe_alert_conflicts()          # повторно — не раньше чем через 6 часов
check("409 при долгом аптайме: одно предупреждение в чат", len(_alerts) == 1, f"сообщений: {len(_alerts)}")
check("409-предупреждение объясняет причину",
      bool(_alerts) and "второй экземпляр" in _alerts[0].lower() and "409" in _alerts[0])

# /status: экземпляр, аптайм, счётчик конфликтов
_st = _sec.status_text()
check("status: показывает экземпляр и аптайм", _sec.instance in _st and "аптайм" in _st, _st.splitlines()[2][:70])
check("status: показывает счётчик 409 и предупреждение",
      "409-конфликтов: <b>9</b>" in _st and "второй экземпляр" in _st.lower())
check("format_uptime", b.format_uptime(59) == "59 с" and "мин" in b.format_uptime(120)
      and "ч" in b.format_uptime(7300) and "д" in b.format_uptime(90000))

b.log.removeHandler(_cap)

# ---------------------------------------------------------------------------
# /diag — проверка доступности IMAP с самого хостинга (диагностика на PaaS)
import os as _os
import urllib.request as _url

_os.environ["PORT"] = "18099"
_diag_cfg = b.Config(imap_host="localhost", imap_port=1, mail_login="l", mail_password="p",
                     tg_token="t", tg_chat_id="1", check_interval=300, imap_folder="INBOX",
                     max_per_check=25, max_attachment_mb=50)
try:
    b.start_health_server(_diag_cfg)
    _root_body = _url.urlopen("http://127.0.0.1:18099/", timeout=10).read().decode()
    _diag_body = _url.urlopen("http://127.0.0.1:18099/diag", timeout=30).read().decode()
finally:
    _os.environ.pop("PORT", None)

check("health: / отвечает ok", _root_body == "ok", _root_body[:30])
check("diag: показывает DNS и результат TCP", "DNS:" in _diag_body and "TCP" in _diag_body)
check("diag: закрытый порт виден как FAIL", "FAIL" in _diag_body, _diag_body.replace("\n", " | ")[:110])

print()
print("ALL PASSED" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
