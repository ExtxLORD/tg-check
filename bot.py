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
import json
import logging
import os
import random
import re
import socket
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

import requests
from dotenv import load_dotenv

log = logging.getLogger("mailbot")

TG_MSG_LIMIT = 4000          # Telegram hard limit is 4096; keep margin
TG_DOC_LIMIT_BYTES = 50 * 1024 * 1024   # Bot API limit for sendDocument

# Главные кнопки (обычная reply-клавиатура) — всегда видны под полем ввода.
# Нажатие присылает текст кнопки как обычное сообщение, поэтому текст кнопки
# обязан распознаваться наравне с командой (см. CMD_ALIASES ниже).
MAIN_KEYBOARD: dict = {
    "keyboard": [
        [{"text": "🔍 Проверить сейчас"}, {"text": "📊 Статус"}],
        [{"text": "❓ Помощь"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Проверить почту / статус / помощь",
}

# Меню по кнопке «/» рядом с полем ввода (setMyCommands)
BOT_COMMANDS = [
    {"command": "check", "description": "Проверить почту сейчас"},
    {"command": "status", "description": "Статус, счётчики, пауза"},
    {"command": "help", "description": "Справка и кнопки"},
]

_ALIASES = {
    "/start": "help", "/help": "help", "help": "help",
    "❓ помощь": "help", "помощь": "help",
    "/check": "check", "check": "check",
    "🔍 проверить сейчас": "check", "🔍 проверить": "check", "проверить": "check",
    "/status": "status", "status": "status",
    "📊 статус": "status", "статус": "status",
}


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


CMD_ALIASES = {_norm(k): v for k, v in _ALIASES.items()}


def resolve_command(text: str) -> str | None:
    """Распознать команду: "/check", "🔍 Проверить сейчас", "статус" -> действие."""
    return CMD_ALIASES.get(_norm(text))


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

    def _attachment_mb() -> int:
        raw = int(opt("MAX_ATTACHMENT_MB", "50"))
        limit = TG_DOC_LIMIT_BYTES // (1024 * 1024)      # 50 — потолок Bot API
        if raw > limit:
            print(f"[config] MAX_ATTACHMENT_MB={raw} > {limit} МБ (лимит Telegram) — "
                  f"использую {limit}")
            return limit
        return max(1, raw)

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
        max_attachment_mb=_attachment_mb(),
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

_PASSWORD_OK: dict[str, str] = {}   # login -> вариант пароля, который сервер принял

_APP_PW_HINT = (
    "нужен «пароль для внешних приложений» Mail.ru — обычный пароль от ящика "
    "IMAP не принимает. Создайте его: почта Mail.ru → Настройки → Все настройки → "
    "Безопасность → «Пароли для внешних приложений» → Создать (к аккаунту должен "
    "быть привязан телефон; выберите доступ к Почте для почтовой программы). "
    "Полученный пароль вставьте в MAIL_PASSWORD и перезапустите сервис. "
    "Инструкция Mail.ru: https://help.mail.ru/mail/security/protection/external"
)


def human_imap_error(exc: Exception | str | None) -> str:
    """Читаемое объяснение вместо сырого b'[AUTHENTICATIONFAILED] NEOBHODIM ...'."""
    raw = str(exc)
    m = re.search(r"b'([^']*)'", raw)
    if m:
        raw = m.group(1)
    raw = raw.replace("\\n", " ").replace("\\r", " ").strip()
    up = raw.upper()
    if ("AUTHENTICATIONFAILED" in up or "NEOBHODIM PAROL" in up
            or "APPLICATION PASSWORD IS REQUIRED" in up or "LOGIN FAILED" in up):
        return "сервер отклонил логин: " + _APP_PW_HINT
    if "TIMED OUT" in up or "TIMEOUT" in up:
        return ("сервер почты не отвечает (таймаут соединения) — вероятна блокировка "
                "облачного IP почтовым сервисом; проверьте доступность через /diag")
    if "GETADDRINFO" in up or "LOOKUP" in up or "NO ADDRESS" in up:
        return f"не удалось разрешить имя сервера почты: {raw[:200]}"
    return (raw or str(exc))[:300]


def _password_variants(cfg: Config) -> list[str]:
    """Варианты пароля для попытки логина.

    Mail.ru показывает «пароль для внешних приложений» группами с пробелами;
    если скопировать его вместе с ними (или с кавычками), сервер отвечает
    AUTHENTICATIONFAILED. Пробуем исходный вариант и очищенный, а если один из
    них уже срабатывал в этом процессе — начинаем с него.
    """
    base = cfg.mail_password
    variants: list[str] = []
    for candidate in (base, base.replace(" ", ""), base.strip().strip('"').strip("'")):
        if candidate and candidate not in variants:
            variants.append(candidate)
    known = _PASSWORD_OK.get(cfg.mail_login)
    if known and known in variants:
        variants.remove(known)
        variants.insert(0, known)
    return variants

def imap_connect(cfg: Config, attempts: int = 2) -> imaplib.IMAP4_SSL:
    """Одно соединение на проверку: fetch + mark_seen на одном клиенте.

    SELECT открывается на запись — иначе сервер запретит ЕДИНСТВЕННУЮ
    операцию записи в боте (STORE +FLAGS \\Seen после доставки).
    Чтение при этом остаётся без побочных эффектов: BODY.PEEK[] не ставит \\Seen.

    Отказ авторизации не повторяем (сервер уже сказал, что пароль не тот —
    лишние попытки только злят антиспам почтовика), сетевые сбои — повторяем.
    """
    ctx = ssl.create_default_context()
    last_exc: Exception | None = None
    for attempt in range(attempts):
        auth_rejected = False
        for password in _password_variants(cfg):
            try:
                client = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ctx, timeout=30)
                client.login(cfg.mail_login, password)
                client.select(cfg.imap_folder)
                _PASSWORD_OK[cfg.mail_login] = password
                return client
            except imaplib.IMAP4.error as exc:
                auth_rejected = True
                last_exc = exc
                log.warning("IMAP: логин отклонён — %s", human_imap_error(exc))
            except Exception as exc:  # noqa: BLE001 — сеть/SSL: есть смысл повторить
                last_exc = exc
                break
        if auth_rejected:
            raise last_exc if last_exc else RuntimeError("IMAP login failed")
        if attempt + 1 < attempts:
            log.warning("IMAP connect failed (%s), повтор через 4 с…", human_imap_error(last_exc))
            time.sleep(4)
    raise last_exc if last_exc else RuntimeError("IMAP connect failed")


def imap_release(client: imaplib.IMAP4_SSL | None) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception:
        pass
    try:
        client.logout()
    except Exception:
        pass


def imap_fetch_unseen(client: imaplib.IMAP4_SSL) -> list[ParsedMail]:
    typ, data = client.uid("search", None, "UNSEEN")
    if typ != "OK":
        raise RuntimeError(f"IMAP search failed: {typ}")
    uids = [u.decode() for u in (data[0] or b"").split() if u]
    log.info("UNSEEN: %d писем (uids=%s)", len(uids),
             ",".join(uids[:20]) + ("…" if len(uids) > 20 else ""))

    mails: list[ParsedMail] = []
    for uid in uids:
        # BODY.PEEK[] — чтение без побочного эффекта \Seen
        typ, data = client.uid("fetch", uid, "(BODY.PEEK[])")
        if typ != "OK" or not data or data[0] is None:
            log.warning("Не удалось скачать uid=%s", uid)
            continue
        raw = data[0][1] if isinstance(data[0], tuple) else b""
        if not raw:
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


def imap_mark_seen(client: imaplib.IMAP4_SSL, uid: str) -> bool:
    """Единственная операция записи во всём боте — только после доставки."""
    typ, _ = client.uid("store", uid, "+FLAGS", r"(\Seen)")
    return typ == "OK"


# --------------------------------------------------------- Telegram layer

class Telegram:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.base = f"https://api.telegram.org/bot{cfg.tg_token}/"
        self._offset = 0
        self._last_conflict_warn = 0.0
        self._last_net_warn = 0.0
        self._started = time.time()
        self.conflict_count = 0

    def _redact(self, text: str) -> str:
        """Вычистить токен из текста перед записью в лог.

        requests подставляет полный URL (а в нём лежит токен) в текст своих
        исключений — из-за этого токен уже утекал в логи PaaS. Любую строку,
        которая может содержать URL Bot API, пропускаем через этот фильтр.
        """
        token = self.cfg.tg_token
        return text.replace(token, "***") if token else text

    def _post(self, method: str, **kwargs) -> dict:
        try:
            resp = requests.post(self.base + method, timeout=120, **kwargs)
        except requests.RequestException as exc:
            # from None: исходное исключение в traceback снова содержало бы URL с токеном
            raise RuntimeError(
                f"Telegram API {method}: сеть недоступна ({self._redact(str(exc))})"
            ) from None
        if resp.status_code != 200:
            # Тело ответа Telegram — единственное место с причиной ошибки (400/413/…)
            raise RuntimeError(
                f"Telegram API {method} HTTP {resp.status_code}: {resp.text[:500]}"
            )
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API {method} error: {payload}")
        return payload["result"]

    def send_text(self, text: str, html: bool = False, keyboard: bool = False) -> None:
        """keyboard=True прикрепляет кнопки меню к сообщению (только к первому,
        если текст разбивается на части)."""
        if not text or not text.strip():
            # Telegram отвечает 400 на пустой текст. Такой вызов сорвал бы
            # доставку письма целиком (и письмо ушло бы повторно) — пропускаем.
            log.warning("send_text: пустой текст, отправка пропущена")
            return
        markup = (
            {"reply_markup": json.dumps(MAIN_KEYBOARD, ensure_ascii=False)}
            if keyboard else {}
        )
        # HTML-сообщения не режем (можно разорвать тег) — только если влезают целиком
        if html and len(text) <= TG_MSG_LIMIT:
            self._post("sendMessage", data={
                "chat_id": self.cfg.tg_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                **markup,
            })
            return
        for i, chunk in enumerate(split_text(text, TG_MSG_LIMIT)):
            self._post("sendMessage", data={
                "chat_id": self.cfg.tg_chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
                **(markup if i == 0 else {}),
            })

    def set_commands(self) -> None:
        """Зарегистрировать меню команд (кнопка «/» рядом с полем ввода)."""
        self._post("setMyCommands", data={
            "commands": json.dumps(BOT_COMMANDS, ensure_ascii=False),
            "scope": json.dumps({"type": "all_private_chats"}),
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
                # Bot API ожидает JSON-строку, а не повторяющийся параметр
                "allowed_updates": json.dumps(["message"]),
            }, timeout=timeout + 15)
        except requests.RequestException as exc:
            # ВАЖНО: str(exc) содержит URL с токеном — только через _redact
            now = time.time()
            if now - self._last_net_warn >= 60:      # не спамим лог каждые 3 секунды
                self._last_net_warn = now
                log.warning("getUpdates: сеть недоступна (%s)", self._redact(str(exc)))
            time.sleep(3)
            return []
        if resp.status_code == 409:
            # «terminated by other getUpdates request» = прямо сейчас обновления
            # забирает кто-то ещё с этим токеном. Два типичных случая:
            #  1) деплой на Render: новый контейнер поднялся, старый ещё ~10 с жив
            #     и продолжает long-poll — само пройдёт, когда Render погасит старый;
            #  2) настоящий второй экземпляр (VPS/Koyeb/локальный запуск) — тогда
            #     это будет повторяться и через несколько минут аптайма.
            self.conflict_count += 1
            now = time.time()
            uptime = now - self._started
            if now - self._last_conflict_warn >= 600:
                self._last_conflict_warn = now
                if uptime < 180:
                    log.warning(
                        "getUpdates 409: похоже на наложение старого и нового контейнера "
                        "при деплое (аптайм %.0f с). Если конфликт повторится позже — "
                        "с этим токеном работает второй экземпляр",
                        uptime,
                    )
                else:
                    log.error(
                        "getUpdates 409 Conflict: обновления забирает ДРУГОЙ экземпляр бота с этим "
                        "токеном (VPS / второй сервис / локальный запуск). Команды работать не будут, "
                        "пока работают оба. Остановите лишний экземпляр, например: systemctl stop mailbot"
                    )
            time.sleep(5)
            return []
        if resp.status_code != 200:
            log.warning("getUpdates HTTP %s: %s", resp.status_code, resp.text[:300])
            time.sleep(3)
            return []
        try:
            payload = resp.json()
            if not payload.get("ok"):
                log.warning("getUpdates: ok=false: %s", resp.text[:300])
                return []
            updates = payload["result"]
        except Exception as exc:
            log.warning("getUpdates: не разобрал ответ (%s)", self._redact(str(exc)))
            return []
        for u in updates:
            self._offset = u["update_id"] + 1
        return updates


_RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")

# Москва, UTC+3 (без перехода на летнее время)
_MSK = timezone(timedelta(hours=3))


def h(text: str) -> str:
    """Экранирование для Telegram HTML."""
    return html_mod.escape(str(text), quote=False)


def format_date(raw: str) -> str:
    """'Thu, 24 Sep 2026 10:00:00 +0000' -> '24 сентября 2026, 13:00' (МСК)."""
    raw = (raw or "").strip()
    if not raw:
        return "—"
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(_MSK)
        return f"{dt.day} {_RU_MONTHS[dt.month - 1]} {dt.year}, {dt.strftime('%H:%M')} (МСК)"
    except Exception:
        return raw


def format_uptime(seconds: float) -> str:
    total = int(max(0, seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {secs} с"
    return f"{secs} с"


def split_text(text: str, limit: int) -> list[str]:
    """Резать текст на части ≤ limit БЕЗ потери символов и без пустых частей.

    Пустой чанк — не мелочь: Telegram отвечает на sendMessage с пустым текстом
    «400 Bad Request: message text is empty», доставка падает, письмо не
    помечается \\Seen и уходит повторно при каждой проверке (спам-петля).
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit:
            # flush только непустой буфер — иначе получится пустое сообщение
            if current:
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
        # uid, уже доставленные в этом запуске (если STORE \Seen не прошёл —
        # чтобы письмо не ушло повторно). История ограничена, иначе set
        # растёт бесконечно все недели работы процесса.
        self._processed: set[str] = set()
        self._processed_order: deque[str] = deque()
        self._processed_max = 5000
        self.last_check: str = "ещё не было"
        self.last_result: str = "—"
        self.total_delivered = 0
        self._last_error_notify = 0.0
        self.started_at = time.time()
        self.instance = f"{socket.gethostname()}:{os.getpid()}"
        self._last_conflict_alert = 0.0
        self._consecutive_failures = 0
        self._auto_pause_until = 0.0
        self._last_auth_notify = 0.0

    # ---- main check ----------------------------------------------------
    def _mark_processed(self, uid: str) -> None:
        """Запомнить доставленный uid, вытесняя самые старые (кап 5000)."""
        if uid in self._processed:
            return
        if len(self._processed_order) >= self._processed_max:
            self._processed.discard(self._processed_order.popleft())
        self._processed.add(uid)
        self._processed_order.append(uid)

    def check_mail(self, reason: str) -> None:
        if not self._lock.acquire(blocking=False):
            log.info("Проверка уже идёт, пропуск (%s)", reason)
            if reason == "manual":
                # Пользователь ждёт ответа на /check после «Проверяю почту…»
                try:
                    self.tg.send_text("⏳ Проверка уже идёт — результат придёт сюда.")
                except Exception:
                    log.exception("Не удалось ответить на /check")
            return
        try:
            log.info("Проверка почты (%s)…", reason)
            client = None
            try:
                client = imap_connect(self.cfg)
                mails = imap_fetch_unseen(client)
                # соединение и чтение прошли — сбрасываем автопаузу после сбоев
                self._consecutive_failures = 0
                self._auto_pause_until = 0.0
                self.last_check = datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S")

                pending = [m for m in mails if m.uid not in self._processed]
                skipped_cap = max(0, len(pending) - self.cfg.max_per_check)
                if skipped_cap:
                    pending = pending[: self.cfg.max_per_check]

                delivered = 0
                for mail in pending:
                    try:
                        self._deliver(mail)
                        # ВСЕГДА после успешной отправки — иначе при сбое STORE
                        # письмо ушло бы повторно каждую проверку (спам)
                        self._mark_processed(mail.uid)
                        if not imap_mark_seen(client, mail.uid):
                            log.warning(
                                "STORE \\Seen не прошёл uid=%s — повторно отправлено не будет",
                                mail.uid,
                            )
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
                    self.tg.send_text(text, html=True, keyboard=True)
            finally:
                imap_release(client)
        except imaplib.IMAP4.error as exc:
            self._handle_error(f"Почта: {human_imap_error(exc)}")
        except Exception as exc:
            self._handle_error(f"Ошибка проверки: {human_imap_error(exc)}")
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
                    f"⚠️ Вложение «{att.filename}» "
                    f"({len(att.data) / 1048576:.1f} МБ) не отправлено — "
                    f"лимит {self.cfg.max_attachment_mb} МБ."
                )
                continue
            self.tg.send_document(att.filename, att.data, att.mime)

    def status_text(self) -> str:
        conflicts = self.tg.conflict_count
        pause = ""
        if not self._auto_check_allowed():
            left = int(max(0.0, self._auto_pause_until - time.time()) / 60) + 1
            pause = (
                f"\n⏸ Автопроверки на паузе ещё ~{left} мин "
                f"(сбоев подряд: {self._consecutive_failures}) — жмите /check вручную"
            )
        return (
            "📊 <b>Статус бота</b>\n\n"
            f"🖥 Экземпляр: <code>{h(self.instance)}</code>, "
            f"аптайм {format_uptime(time.time() - self.started_at)}\n"
            f"⏱ Последняя проверка (МСК): <i>{self.last_check}</i>\n"
            f"📬 Результат: {h(str(self.last_result))}\n"
            f"📤 Всего отправлено писем: <b>{self.total_delivered}</b>\n"
            f"⚠️ 409-конфликтов: <b>{conflicts}</b>"
            + (" — обновления забирает второй экземпляр!" if conflicts else "")
            + pause
            + f"\n🔁 Интервал проверки: <b>{self.cfg.check_interval}</b> с"
        )

    def _maybe_alert_conflicts(self) -> None:
        """Повторяющиеся 409 — значит, с этим токеном реально работает второй
        экземпляр. Предупреждаем в чат, но не чаще раза в 6 часов и только если
        конфликты идут уже дольше трёх минут аптайма (наложение при деплое
        укладывается в первые секунды и сообщение не заслуживает)."""
        n = self.tg.conflict_count
        if n < 3:
            return
        now = time.time()
        if now - self.started_at < 180:
            return
        if self._last_conflict_alert and now - self._last_conflict_alert < 21600:
            return
        self._last_conflict_alert = now
        try:
            self.tg.send_text(
                f"⚠️ <b>Конфликт обновлений (409)</b>: уже {n} раз.\n\n"
                "С этим токеном одновременно опрашивает Telegram второй экземпляр бота "
                "(VPS, другой хостинг или локальный запуск) — команды и письма будут "
                "приходить через раз или дублироваться.\n\n"
                "Остановите лишний экземпляр, например: <code>systemctl stop mailbot</code> "
                "на VPS, либо удалите второй сервис в панели хостинга.",
                html=True,
            )
        except Exception:
            log.exception(" Не удалось предупредить о конфликте 409")

    def _handle_error(self, msg: str) -> None:
        log.error(msg)
        self.last_result = msg
        # Повторяющиеся сбои IMAP — почти всегда «сервер почты нас не пускает»
        # (блокировка облачного IP антиспамом Mail.ru и т.п.). Продолжать долбить
        # его каждые CHECK_INTERVAL секунд бессмысленно и только продлевает
        # блокировку, поэтому автопроверки постепенно уходят в паузу до 30 минут.
        # Ручной /check паузу игнорирует — пользователь решает сам.
        self._consecutive_failures += 1
        if self._consecutive_failures >= 2:
            extra = min(120.0 * (2 ** (self._consecutive_failures - 2)), 1800.0)
            self._auto_pause_until = time.time() + extra
            log.warning(
                "Сбоев подряд: %d — автопроверки на паузе %.0f с (ручной /check работает)",
                self._consecutive_failures, extra,
            )
        now = time.time()
        # Отказ авторизации требует действий человека (создать пароль для
        # приложений), поэтому напоминаем о нём раз в 6 часов, а не каждые
        # полчаса. Сетевые сбои — как раньше, раз в 30 минут.
        auth_problem = ("внешних приложений" in msg) or ("AUTHENTICATIONFAILED" in msg.upper())
        window = 21600 if auth_problem else 1800
        stamp = self._last_auth_notify if auth_problem else self._last_error_notify
        if now - stamp > window:
            if auth_problem:
                self._last_auth_notify = now
            else:
                self._last_error_notify = now
            try:
                self.tg.send_text(f"⚠️ Проблема с проверкой почты:\n{msg}")
            except Exception:
                log.exception("Не удалось уведомить о проблеме")

    # ---- threads -------------------------------------------------------
    def _auto_check_allowed(self) -> bool:
        """Не запускать таймерную проверку, пока идёт автопауза после сбоев."""
        return time.time() >= self._auto_pause_until

    def timer_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.cfg.check_interval)
            if self._stop.is_set():
                break
            if not self._auto_check_allowed():
                left = self._auto_pause_until - time.time()
                log.info(
                    "Автопроверка на паузе ещё %.0f с (сбоев подряд: %d)",
                    max(0.0, left), self._consecutive_failures,
                )
                continue
            self.check_mail("timer")

    def help_text(self) -> str:
        return (
            "🤖 <b>Mail Read Bot</b> <i>(read-only)</i>\n\n"
            "Бот читает непрочитанные письма и пересылает их в этот чат.\n"
            "На почте ничего не отправляет и не удаляет — только помечает\n"
            "прочитанным письмо, которое уже доставлено вам.\n\n"
            "🧩 <b>Кнопки под полем ввода:</b>\n"
            "🔍 <b>Проверить сейчас</b> — проверить почту немедленно\n"
            "📊 <b>Статус</b> — когда была проверка, счётчики, автопауза\n"
            "❓ <b>Помощь</b> — эта справка\n\n"
            "Команды <code>/check</code>, <code>/status</code>, <code>/help</code> "
            "работают так же, как кнопки."
        )

    def telegram_loop(self) -> None:
        log.info("Бот запущен. Интервал: %s с. Ожидание команд…", self.cfg.check_interval)
        while not self._stop.is_set():
            updates = self.tg.get_updates(timeout=30)
            self._maybe_alert_conflicts()
            for upd in updates:
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if chat_id != self.cfg.tg_chat_id:
                    log.info("Игнорирую сообщение из чата %s (не мой)", chat_id)
                    continue
                action = resolve_command(text)
                if action == "help":
                    self.tg.send_text(self.help_text(), html=True, keyboard=True)
                elif action == "check":
                    self.tg.send_text("🔍 <b>Проверяю почту…</b>", html=True)
                    self.check_mail("manual")
                elif action == "status":
                    self.tg.send_text(self.status_text(), html=True, keyboard=True)
                elif text:
                    self.tg.send_text(
                        "❓ Не понял. Нажмите кнопку на клавиатуре ниже или отправьте /help.",
                        keyboard=True,
                    )

    def run(self) -> None:
        # smoke-check Telegram before anything else
        try:
            me = self.tg._post("getMe")
            log.info("Telegram-бот: @%s", me.get("username"))
        except Exception as exc:
            raise SystemExit(f"[tg] Не удалось подключиться к Telegram: {exc}")

        try:
            self.tg.set_commands()
            log.info("Меню команд зарегистрировано (setMyCommands)")
        except Exception as exc:
            log.warning("Не удалось обновить меню команд: %s", exc)

        # При деплое Render поднимает новый контейнер, пока старый ещё ~10 с жив и
        # продолжает опрашивать Telegram и почту. Если начать сразу: экземпляры пинают
        # друг друга (409), а письмо, пришедшее в эти секунды, может уйти дважды.
        # Небольшая пауза даёт старому контейнеру умереть до нашего первого действия
        # (health-сервер уже слушает, так что Render не считает деплой зависшим).
        delay = random.uniform(8.0, 15.0)
        log.info("Первый опрос Telegram и проверка почты — через %.0f с", delay)
        time.sleep(delay)

        threading.Thread(target=self.timer_loop, name="timer", daemon=True).start()
        threading.Thread(target=self.check_mail, args=("startup",), daemon=True).start()
        try:
            self.telegram_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()


def start_health_server(cfg: Config | None = None) -> None:
    """Мини HTTP-сервер для health-check'ов PaaS (Render/Koyeb выставляют $PORT).
    Боту он не нужен — включается только если платформа задала PORT.

    Дополнительно отдаёт /diag: проверка DNS/TCP/TLS до IMAP-сервера С ЭТОГО
    хостинга — единственный честный способ узнать, дотягивается ли PaaS до
    корпоративной почты, вместо гадания по таймаутам в логах.
    Если задана переменная DIAG_KEY, /diag требует ?key=<значение>.
    """
    port = os.environ.get("PORT", "").strip()
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit

    diag_key = os.environ.get("DIAG_KEY", "").strip()

    def imap_reachability() -> str:
        host = cfg.imap_host if cfg else "(IMAP_HOST не задан)"
        imap_port = cfg.imap_port if cfg else 993
        lines = [f"Хост: {host}:{imap_port}"]
        t0 = time.time()
        try:
            infos = socket.getaddrinfo(host, imap_port, type=socket.SOCK_STREAM)
        except Exception as exc:
            lines.append(f"DNS: FAIL за {time.time() - t0:.1f} с — {type(exc).__name__}: {exc}")
            return "\n".join(lines)
        addrs = sorted({info[4][0] for info in infos})
        lines.append(f"DNS: OK за {time.time() - t0:.1f} с -> {', '.join(addrs)}")
        for addr in addrs:
            t0 = time.time()
            try:
                with socket.create_connection((addr, imap_port), timeout=10) as sock:
                    try:
                        ctx = ssl.create_default_context()
                        with ctx.wrap_socket(sock, server_hostname=host) as tls:
                            lines.append(
                                f"TCP+TLS {addr}:{imap_port}: OK за {time.time() - t0:.1f} с "
                                f"({tls.version()}) — почта доступна с этого хостинга"
                            )
                    except Exception as exc:
                        lines.append(f"TCP {addr}:{imap_port}: порт открыт, TLS FAIL — {exc}")
            except Exception as exc:
                lines.append(
                    f"TCP {addr}:{imap_port}: FAIL за {time.time() - t0:.1f} с — "
                    f"{type(exc).__name__}: {exc}"
                )
        return "\n".join(lines)

    def imap_login_test() -> str:
        """Проверка логина/пароля изнутри контейнера (без Shell на хостинге).
        Тратит одну попытку логина — вызывается только по /diag?login=1."""
        if cfg is None:
            return "IMAP не настроен (нет конфигурации)"
        try:
            client = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port,
                                       ssl_context=ssl.create_default_context(), timeout=20)
        except Exception as exc:
            return f"Соединение: FAIL — {human_imap_error(exc)}"
        try:
            client.login(cfg.mail_login, cfg.mail_password)
            typ, _ = client.select(cfg.imap_folder)
            return f"LOGIN {cfg.mail_login}: OK — папка {cfg.imap_folder} доступна ({typ})"
        except Exception as exc:
            return f"LOGIN {cfg.mail_login}: FAIL — {human_imap_error(exc)}"
        finally:
            imap_release(client)

    class _HealthHandler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            parts = urlsplit(self.path)
            if parts.path.rstrip("/") == "/diag":
                qs = parse_qs(parts.query)
                if diag_key and qs.get("key", [""])[0] != diag_key:
                    self._send(403, b"forbidden: otkroite /diag?key=DIAG_KEY")
                    return
                report = imap_reachability()
                if qs.get("login", [""])[0] == "1":
                    report += "\n\nПроверка входа (тратит одну попытку логина):\n" + imap_login_test()
                self._send(200, report.encode("utf-8"))
                return
            self._send(200, b"ok")

        def log_message(self, *args) -> None:  # тишина в логах
            pass

    server = ThreadingHTTPServer(("0.0.0.0", int(port)), _HealthHandler)
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    log.info("Health-check сервер слушает :%s (диагностика почты: /diag)", port)


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
    start_health_server(cfg)
    Bot(cfg).run()


if __name__ == "__main__":
    main()
