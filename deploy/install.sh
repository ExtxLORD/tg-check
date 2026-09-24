#!/usr/bin/env bash
# Установка бота на VPS (Ubuntu/Debian). Запускать от root:
#   bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/mail-read-bot
REPO=https://github.com/ExtxLORD/tg-check.git
UNIT=mailbot

echo "==> Проверки"
command -v git >/dev/null || { echo "нет git"; exit 1; }
command -v python3 >/dev/null || { echo "нет python3"; exit 1; }
python3 -c 'import venv' 2>/dev/null || { echo "нет модуля venv (apt install python3-venv)"; exit 1; }

echo "==> Пользователь mailbot (изолирован от root и от VPN)"
id -u mailbot >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin mailbot

echo "==> Код"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi

echo "==> Python-окружение"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> Конфиг"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chown mailbot:mailbot "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "!!!" 
  echo "!!!  Создан шаблон $APP_DIR/.env"
  echo "!!!  Заполните MAIL_LOGIN, MAIL_PASSWORD, TG_BOT_TOKEN, TG_CHAT_ID"
  echo "!!!  и уменьшите MAX_ATTACHMENT_MB до 25 (VPS на 512 МБ)."
  echo "!!!  Затем: systemctl restart $UNIT"
  echo "!!!"
else
  echo ".env уже есть — не трогаю"
fi
chown -R mailbot:mailbot "$APP_DIR"

echo "==> Systemd-юнит"
cp "$APP_DIR/deploy/mailbot.service" "/etc/systemd/system/$UNIT.service"
systemctl daemon-reload
systemctl enable --now "$UNIT"
sleep 2
systemctl --no-pager -l status "$UNIT" | head -12 || true

echo
echo "Готово. Логи: journalctl -u $UNIT -f"
