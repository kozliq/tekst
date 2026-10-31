#!/bin/bash
# Ставит или обновляет Текст на ketrix. Запускать с Мака из папки tekst: deploy/install.sh
# Первый раз: пользователь tekstbot, пакеты Python в /opt/tekst/venv, модели (~700 МБ, со сверкой sha256).
# Потом — просто обновляет код и перезапускает службу. Токен кладёт deploy/token.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
ssh ketrix 'id tekstbot >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin tekstbot
mkdir -p /opt/tekst/tools && install -d -o tekstbot -m 700 /var/lib/tekst-bot'
scp -q tekst.py speech.py requirements.txt ketrix:/opt/tekst/
scp -q tools/models.sh ketrix:/opt/tekst/tools/
scp -q brand/avatar-640.jpg ketrix:/opt/tekst/avatar-640.jpg
scp -q deploy/tekst-bot.service deploy/tekst-tgapi.service ketrix:/etc/systemd/system/
ssh ketrix 'set -e; cd /opt/tekst
sed -i "s/shasum -a 256/sha256sum/g" tools/models.sh
[ -x venv/bin/python ] || python3 -m venv venv
nice -n 19 venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
nice -n 19 bash tools/models.sh /opt/tekst/models
chown -R root:root /opt/tekst && chmod -R go-w /opt/tekst
systemctl daemon-reload && systemctl enable tekst-bot tekst-tgapi >/dev/null 2>&1
systemctl restart tekst-tgapi
if [ -f /var/lib/tekst-bot/bot.token ]; then systemctl restart tekst-bot; sleep 8; journalctl -u tekst-bot -n 5 --no-pager -o cat; else echo "Код на месте. Теперь токен: deploy/token.sh"; fi'
