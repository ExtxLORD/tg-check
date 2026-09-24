#!/usr/bin/env python3
r"""
Read-only mail → Telegram bot.

GUARANTEES (read-only by construction):
  * Mail access uses IMAP only: UID SEARCH + UID FETCH with BODY.PEEK[]
    (BODY.PEEK does NOT set the \Seen flag as a side effect).
  * The ONLY write operation in the entire program is:
        UID STORE <uid> +FLAGS (\\Seen)
    executed AFTER the message was successfully delivered to Telegram.
  * There is no SMTP code path, no DELETE, no EXPUNGE, no MOVE,
    no flag removal, no message modification of any other kind.
  * The bot's only output channel is Telegram messages to TG_CHAT_ID.
    Updates from any other chat are ignored.
"""

from __future__ import annotations

import email
import html as html_mod
import imaplib
import io
import logging
import os
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

import requests
from dotenv import load_dotenv

log = logging.getLogger("mailbot")

TG_MSG_LIMIT = 4000          # Telegram hard limit is 4096; keep margin
TG_DOC_LIMIT_BYTES = 50 * 1024 * 1024   # Bot API limit for sendDocument


# ----------------------------------------------------------------- config

@dataclass(frozen=True)
class Config:
    imap_host: str
    imap_port: int
    mail_login: str
    mail_password: str
    tg_token: str
    tg_chat_id: str
    check_interval: int      # seconds between automatic checks
    imap_folder: str
    max_per_check: int       # safety cap of mails processed in one run
    max_attachment_mb: int


def load_config() -> Config:
    load_dotenv()

    def req(key: str) -> str:
        val = os.getenv(key, "").strip()
        if not val:
            raise SystemExit(f"[config] Не задана обязательная переменная: {key} (см. .env.example)")
        return val

    def opt(key: str, default: str) -> str:
        return os.getenv(key, "").strip() or default

    return Config(
        imap_host=req("IMAP_HOST"),
        imap_port=int(opt("IMAP_PORT", "993")),
        mail_login=req("MAIL_LOGIN"),
        mail_password=req("MAIL_PASSWORD"),
        tg_token=req("TG_BOT_TOKEN"),
        tg_chat_id=req("TG_CHAT_ID"),
        check_interval=max(30, int(opt("CHECK_INTERVAL", "300"))),
        imap_folder=opt("IMAP_FOLDER", "INBOX"),
        max_per_check=max(1, int(opt("MAX_PER_CHECK", "25"))),
        max_attachment_mb=max(1, int(opt("MAX_ATTACHMENT_MB", "50"))),
    )


# ------------------------------------------------------- e-mail parsing

class _HTMLTextExtractor(HTMLParser):
    _BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "hr", "table", "section", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t ]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_text(source: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        return html_mod.unescape(re.sub(r"<[^>]+>", " ", source)).strip()
    return parser.text()


@dataclass
class Attachment:
    filename: str
    mime: str
    data: bytes


@dataclass
class ParsedMail:
    uid: str
    subject: str
    sender: str
    date: str
    body: str
    attachments: list[Attachment] = field(default_factory=list)
    html_only: bool = False


def _safe_filename(name: str, idx: int) -> str:
    name = (name or "").strip() or f"attachment_{idx}"
    name = re.sub(r"[\\/\0\r\n\t]", "_", name)
    return name[:150]


def parse_message(raw: bytes, uid: str) -> ParsedMail:
    msg: Message = email_from_bytes(raw)

    subject = str(msg.get("Subject") or "(без темы)")
    sender = str(msg.get("From") or "(нет отправителя)")
    date = str(msg.get("Date") or "")

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []
    att_idx = 0

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = str(part.get_content_disposition() or "")
        filename = part.get_filename()
        content_id = part.get("Content-ID")

        is_attachment = disp == "attachment" or (
            bool(filename) and not content_id  # inline-with-name, but not signature images
        )

        if is_attachment:
            att_idx += 1
            data = part.get_payload(decode=True)
            if data is None:
                payload = part.get_payload()
                data = payload.encode("utf-8", "replace") if isinstance(payload, str) else b""
            attachments.append(Attachment(
                filename=_safe_filename(filename, att_idx),
                mime=ctype or "application/octet-stream",
                data=data,
            ))
            continue

        if ctype == "text/plain":
            plain_parts.append(_part_text(part))
        elif ctype == "text/html":
            html_parts.append(_part_text(part))

    body = "\n".join(p for p in plain_parts if p.strip()).strip()
    html_only = False
    if not body and html_parts:
        body = html_to_text("\n".join(html_parts))
        html_only = True

    return ParsedMail(
        uid=uid, subject=subject, sender=sender, date=date,
        body=body, attachments=attachments, html_only=html_only,
    )


def email_from_bytes(raw: bytes) -> Message:
    try:
        return email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        return email.message_from_bytes(raw)


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except Exception:
        pass
    data = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, "replace")
    except Exception:
        return data.decode("utf-8", "replace")


# ------------------------------------------------------------ IMAP layer
# READ-ONLY: search + peek-fetch. The single write op is mark_seen().

def imap_connect(cfg: Config) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    client = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ctx, timeout=30)
    client.login(cfg.mail_login, cfg.mail_password)
    client.select(cfg.imap_folder, readonly=True)   # EXAMINE-like: no accidental changes
    return client


def imap_fetch_unseen(cfg: Config) -> list[ParsedMail]:
    client = imap_connect(cfg)
    try:
        typ, data = client.uid("search", None, "UNSEEN")
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        uids = [u.decode() for u in (data[0] or b"").split() if u]
        log.info("UNSEEN: %d писем (uids=%s)", len(uids), ",".join(uids[:20]) + ("…" if len(uids) > 20 else ""))

        mails: list[ParsedMail] = []
        for uid in uids:
            # BODY.PEEK[] — чтение без побочного эффекта \Seen
            typ, data = client.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not data or data[0] is None:
                log.warning("Не удалось скачать uid=%s", uid)
                continue
            raw = data[0][1] if isinstance(data[0], tuple) else b""
            if not raw:
                # смешанные форматы ответа
                for item in data:
                    if isinstance(item, tuple) and len(item) > 1:
                        raw = item[1]
                        break
            if not raw:
                continue
            try:
                mails.append(parse_message(raw, uid))
            except Exception:
                log.exception("Ошибка разбора письма uid=%s", uid)
        return mails
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass


def imap_mark_seen(cfg: Config, uid: str) -> bool:
    """The ONLY write operation of the whole bot. Called only after successful delivery."""
    client = imap_connect(cfg)
    try:
        typ, _ = client.uid("store", uid, "+FLAGS", r"(\Seen)")
        return typ == "OK"
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass


# --------------------------------------------------------- Telegram layer

class Telegram:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.base = f"https://api.telegram.org/bot{cfg.tg_token}/"
        self._offset = 0

    def _post(self, method: str, **kwargs) -> dict:
        resp = requests.post(self.base + method, timeout=120, **kwargs)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API {method} error: {payload}")
        return payload["result"]

    def send_text(self, text: str, html: bool = False) -> None:
        # HTML-сообщения не режем (можно разорвать тег) — только если влезают целиком
        if html and len(text) <= TG_MSG_LIMIT:
            self._post("sendMessage", data={
                "chat_id": self.cfg.tg_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            })
            return
        for chunk in split_text(text, TG_MSG_LIMIT):
            self._post("sendMessage", data={
                "chat_id": self.cfg.tg_chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            })

    def send_document(self, filename: str, data: bytes, mime: str) -> None:
        buf = io.BytesIO(data)
        self._post("sendDocument", data={"chat_id": self.cfg.tg_chat_id}, files={
            "document": (filename, buf, mime),
        })

    def get_updates(self, timeout: int = 30) -> list[dict]:
        try:
            resp = requests.get(self.base + "getUpdates", params={
                "offset": self._offset,
                "timeout": timeout,
                "allowed_updates": ["message"],
            }, timeout=timeout + 15)
            resp.raise_for_status()
            payload = resp.json()
            if not payload.get("ok"):
                return []
            updates = payload["result"]
            for u in updates:
                self._offset = u["update_id"] + 1
            return updates
        except Exception as exc:
            log.warning("getUpdates: %s", exc)
            time.sleep(3)
            return []


_RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")


def h(text: str) -> str:
    """Экранирование для Telegram HTML."""
    return html_mod.escape(str(text), quote=False)


def format_date(raw: str) -> str:
    """'Thu, 24 Sep 2026 13:59:21 +0300' -> '24 сентября 2026, 13:59'."""
    raw = (raw or "").strip()
    if not raw:
        return "—"
    try:
        dt = parsedate_to_datetime(raw)
        return f"{dt.day} {_RU_MONTHS[dt.month - 1]} {dt.year}, {dt.strftime('%H:%M')}"
    except Exception:
        return raw


def split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit:
            chunks.append("".join(current))
            current, size = [], 0
        # very long single line — hard-cut
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks or [""]


# ------------------------------------------------------------- bot logic

class Bot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.tg = Telegram(cfg)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._processed: set[str] = set()      # uids already delivered this run
        self.last_check: str = "ещё не было"
        self.last_result: str = "—"
        self.total_delivered = 0
        self._last_error_notify = 0.0

    # ---- main check ----------------------------------------------------
    def check_mail(self, reason: str) -> None:
        if not self._lock.acquire(blocking=False):
            log.info("Проверка уже идёт, пропуск (%s)", reason)
            return
        try:
            log.info("Проверка почты (%s)…", reason)
            mails = imap_fetch_unseen(self.cfg)
            self.last_check = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            pending = [m for m in mails if m.uid not in self._processed]
            skipped_cap = max(0, len(pending) - self.cfg.max_per_check)
            if skipped_cap:
                pending = pending[: self.cfg.max_per_check]

            delivered = 0
            for mail in pending:
                try:
                    self._deliver(mail)
                    # единственная запись на почте — строго ПОСЛЕ успешной отправки
                    if imap_mark_seen(self.cfg, mail.uid):
                        self._processed.add(mail.uid)
                    else:
                        log.warning("Не удалось пометить прочитанным uid=%s (повтор в след. цикле)", mail.uid)
                    delivered += 1
                    self.total_delivered += 1
                except Exception:
                    log.exception("Не удалось доставить uid=%s — остаётся непрочитанным", mail.uid)

            self.last_result = (
                f"непрочитанных найдено: {len(mails)}, отправлено: {delivered}"
                + (f", отложено (лимит {self.cfg.max_per_check}/проверку): {skipped_cap}" if skipped_cap else "")
            )
            log.info(self.last_result)

            if reason == "manual":
                text = (
                    "✅ <b>Проверка завершена</b>\n\n"
                    f"📬 Непрочитанных найдено: <b>{len(mails)}</b>\n"
                    f"📤 Отправлено вам: <b>{delivered}</b>"
                )
                if skipped_cap:
                    text += f"\n⏳ Отложено до следующих проверок: {skipped_cap}"
                text += f"\n\n🔁 Следующая автоматическая проверка: через {self.cfg.check_interval} с"
                self.tg.send_text(text, html=True)
        except imaplib.IMAP4.error as exc:
            self._handle_error(f"IMAP ошибка: {exc}")
        except Exception as exc:
            self._handle_error(f"Ошибка проверки: {exc}")
        finally:
            self._lock.release()

    def _deliver(self, mail: ParsedMail) -> None:
        name, addr = parseaddr(mail.sender or "")
        name = (name or addr or "(нет имени)").strip()
        addr = (addr or "").strip()
        subj = mail.subject or "(без темы)"
        date = format_date(mail.date)

        att_names = [a.filename for a in mail.attachments]
        att_line_html = ""
        att_line_plain = ""
        if att_names:
            shown = ", ".join(att_names[:8])
            more = f" …и ещё {len(att_names) - 8}" if len(att_names) > 8 else ""
            att_line_html = f"\n\n📎 <b>Вложения ({len(att_names)}):</b> {h(shown + more)}"
            att_line_plain = f"\n\n📎 Вложения ({len(att_names)}): {shown}{more}"

        sender_html = h(name) + (f" &lt;{h(addr)}&gt;" if addr else "")
        header = (
            "📬 <b>Новое непрочитанное письмо</b>\n\n"
            f"<b>👤 От:</b> {sender_html}\n"
            f"<b>📝 Тема:</b> {h(subj)}\n"
            f"<b>📅 Дата:</b> {h(date)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
        )
        body = mail.body or "<i>(письмо без текстовой части)</i>"
        combined = header + h(body) + att_line_html

        if len(combined) <= TG_MSG_LIMIT - 200:
            # короткое письмо — одним красивым HTML-сообщением
            self.tg.send_text(combined, html=True)
        else:
            # длинное — шапка HTML, затем текст обычными частями (без разрыва тегов)
            self.tg.send_text(header + "⬇️ <i>Текст длинный — читайте ниже</i>", html=True)
            plain_body = mail.body or "(письмо без текстовой части)"
            for chunk in split_text(plain_body, TG_MSG_LIMIT):
                self.tg.send_text(chunk)
            if att_line_plain:
                self.tg.send_text(att_line_plain.strip())

        max_bytes = self.cfg.max_attachment_mb * 1024 * 1024
        for att in mail.attachments:
            if len(att.data) > max_bytes:
                self.tg.send_text(
                    f"⚠️ Вложение «{h(att.filename)}» "
                    f"({len(att.data) / 1048576:.1f} МБ) не отправлено — "
                    f"лимит {self.cfg.max_attachment_mb} МБ."
                )
                continue
            self.tg.send_document(att.filename, att.data, att.mime)

    def _handle_error(self, msg: str) -> None:
        log.error(msg)
        self.last_result = msg
        now = time.time()
        if now - self._last_error_notify > 1800:   # не спамим в чат чаще раза в 30 мин
            self._last_error_notify = now
            try:
                self.tg.send_text(f"⚠️ Проблема с проверкой почты:\n{msg}")
            except Exception:
                log.exception("Не удалось уведомить о проблеме")

    # ---- threads ------------------------------------ще раза в 30 мин
            self._last_error_notify = now
            try:
                self.tg.send_text(f"⚠️ Проблема с проверкой почты:\n{msg}")
            except Exception:
                log.exception("Не удалось уведомить о проблеме")

    # ---- threads -------------------------------------------------------
    def timer_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.cfg.check_interval)
            if self._stop.is_set():
                break
            self.check_mail("timer")

    def telegram_loop(self) -> None:
        help_text = (
            "🤖 <b>Mail Read Bot</b> <i>(read-only)</i>\n\n"
            "Бот только читает непрочитанные письма\n"
            "и пересылает их в этот чат. Ничего не отправляет\n"
            "и не удаляет на почте.\n\n"
            "🧩 <b>Команды:</b>\n"
            "/check — проверить почту сейчас\n"
            "/status — статус последней проверки\n"
            "/help — эта справка"
        )
        log.info("Бот запущен. Интервал: %s с. Ожидание команд…", self.cfg.check_interval)
        while not self._stop.is_set():
            for upd in self.tg.get_updates(timeout=30):
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if chat_id != self.cfg.tg_chat_id:
                    log.info("Игнорирую сообщение из чата %s (не мой)", chat_id)
                    continue
                if text in ("/start", "/help"):
                    self.tg.send_text(help_text, html=True)
                elif text == "/check":
                    self.tg.send_text("🔍 <b>Проверяю почту…</b>", html=True)
                    self.check_mail("manual")
                elif text == "/status":
                    self.tg.send_text(
                        "📊 <b>Статус бота</b>\n\n"
                        f"⏱ Последняя проверка: <i>{self.last_check}</i>\n"
                        f"📬 Результат: {h(str(self.last_result))}\n"
                        f"📤 Всего отправлено писем: <b>{self.total_delivered}</b>\n"
                        f"🔁 Интервал проверки: <b>{self.cfg.check_interval}</b> с",
                        html=True,
                    )
                elif text:
                    self.tg.send_text("❓ Неизвестная команда. Нажмите /help — список команд.")

    def run(self) -> None:
        # smoke-check Telegram before anything else
        try:
            me = self.tg._post("getMe")
            log.info("Telegram-бот: @%s", me.get("username"))
        except Exception as exc:
            raise SystemExit(f"[tg] Не удалось подключиться к Telegram: {exc}")

        threading.Thread(target=self.timer_loop, name="timer", daemon=True).start()
        # первая проверка сразу при старте
        threading.Thread(target=self.check_mail, args=("startup",), daemon=True).start()
        try:
            self.telegram_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()


def start_health_server() -> None:
    """Мини HTTP-сервер для health-check'ов PaaS (Render/Koyeb выставляют $PORT).
    Боту он не нужен — включается только если платформа задала PORT."""
    port = os.environ.get("PORT", "").strip()
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # тишина в логах
            pass

    server = ThreadingHTTPServer(("0.0.0.0", int(port)), _HealthHandler)
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    log.info("Health-check сервер слушает :%s", port)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config()
    if not re.fullmatch(r"-?\d+", cfg.tg_chat_id):
        log.warning(
            "TG_CHAT_ID='%s' не похож на числовой id — Telegram будет отвечать 400. "
            "Нужно число из getUpdates (не имя, не @username и не id самого бота).",
            cfg.tg_chat_id,
        )
    start_health_server()
    Bot(cfg).run()


if __name__ == "__main__":
    main()
