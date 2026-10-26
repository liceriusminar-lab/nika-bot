#!/data/data/com.termux/files/usr/bin/bash
# Установка бота на Android-телефон через Termux.
#
# Запуск (в Termux, одной строкой):
#   curl -fsSL https://raw.githubusercontent.com/liceriusminar-lab/nika-bot/main/tools/android/setup.sh | bash
#
# Перед запуском положить в папку «Загрузки» (Download) телефона:
#   nika.env — файл настроек (.env) с компьютера, обязательно
#   nika.db  — база с памятью (data/nika.db), если переносишь существующую Нику
#
# Скрипт: ставит Ubuntu внутри Termux (proot-distro), Python и бота из репозитория,
# забирает настройки и базу из «Загрузок», создаёт скрипты запуска и автозапуск
# после перезагрузки (нужно приложение Termux:Boot) и запускает бота.
# Повторный запуск безопасен: обновит бота и перезапустит его.
set -euo pipefail

REPO="https://github.com/liceriusminar-lab/nika-bot.git"
ROOTFS="$PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu"
APP="$ROOTFS/root/nika"
DL="$HOME/storage/downloads"

say() { printf '\n\033[1;35m== %s\033[0m\n' "$1"; }

say "Не даём телефону усыпить Termux"
termux-wake-lock || true

say "Доступ к папке «Загрузки»"
if [ ! -d "$DL" ]; then
  termux-setup-storage || true
  echo "Разреши доступ к файлам во всплывшем окне…"
  for _ in $(seq 1 60); do [ -d "$DL" ] && break; sleep 1; done
fi
[ -d "$DL" ] || { echo "Нет доступа к «Загрузкам». Запусти скрипт ещё раз и нажми «Разрешить»."; exit 1; }

if [ ! -f "$DL/nika.env" ] && [ ! -f "$APP/.env" ]; then
  echo "В «Загрузках» нет файла nika.env (настройки бота). Скопируй его с компьютера и запусти скрипт снова."
  exit 1
fi

say "Пакеты Termux"
pkg update -y
pkg install -y proot-distro procps

if [ ! -d "$ROOTFS" ]; then
  say "Ставлю Ubuntu (один раз, несколько минут)"
  proot-distro install ubuntu
fi

say "Python и git внутри Ubuntu"
proot-distro login ubuntu -- bash -c \
  "export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y python3 python3-venv git ca-certificates tzdata"

say "Код бота"
proot-distro login ubuntu -- bash -c \
  "if [ -d /root/nika/.git ]; then git -C /root/nika pull --ff-only; else git clone $REPO /root/nika; fi"

say "Настройки и база"
mkdir -p "$APP/data"
if [ -f "$DL/nika.env" ]; then
  cp "$DL/nika.env" "$APP/.env" && rm "$DL/nika.env"   # ключи не оставляем в общей папке
  echo "nika.env → .env"
fi
if [ -f "$DL/nika.db" ]; then
  [ -f "$APP/data/nika.db" ] && cp "$APP/data/nika.db" "$APP/data/nika.db.before-import"
  cp "$DL/nika.db" "$APP/data/nika.db" && rm "$DL/nika.db"
  echo "nika.db → data/nika.db"
fi

say "Проверяю файл настроек"
ENV_FILE="$APP/.env"
[ -f "$ENV_FILE" ] || { echo "Файла настроек нет. Положи nika.env в «Загрузки» и запусти скрипт снова."; exit 1; }
# Блокнот в Windows мог сохранить метку кодировки (BOM) и переводы строк CRLF — убираем
sed -i '1s/^\xEF\xBB\xBF//; s/\r$//' "$ENV_FILE"
size=$(stat -c%s "$ENV_FILE")
keys=$(grep -cE '^[A-Z_]+=' "$ENV_FILE" || true)
echo "Файл настроек: $size байт, строк с настройками: $keys"
if ! grep -qE '^TELEGRAM_BOT_TOKEN=[0-9]+:.+' "$ENV_FILE" || ! grep -qE '^OWNER_TELEGRAM_ID=[0-9]+' "$ENV_FILE"; then
  echo "В файле нет TELEGRAM_BOT_TOKEN или OWNER_TELEGRAM_ID — похоже, nika.env скопировался не целиком."
  echo "Скопируй nika.env в «Загрузки» заново (дождись конца копирования) и запусти скрипт ещё раз."
  rm -f "$ENV_FILE"
  exit 1
fi

say "Библиотеки Python (первый раз — несколько минут)"
proot-distro login ubuntu -- bash -c \
  "cd /root/nika && { [ -x .venv/bin/python ] || python3 -m venv .venv; } && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt"

say "Скрипты запуска"
cat > "$HOME/nika-run.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Держит бота запущенным: если он упал — перезапускает через 10 секунд.
termux-wake-lock
LOG="$HOME/nika.log"
while true; do
  # лог не даём разрастись больше 5 МБ
  [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 5000000 ] && : > "$LOG"
  proot-distro login ubuntu -- bash -c "cd /root/nika && TZ=Asia/Vladivostok .venv/bin/python -m bot.main" >> "$LOG" 2>&1
  echo "$(date '+%F %T') бот остановился, перезапуск через 10 с" >> "$LOG"
  sleep 10
done
EOF

cat > "$HOME/nika-start.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Запустить бота в фоне (если ещё не запущен)
if pgrep -f nika-run.sh > /dev/null; then echo "Ника уже работает"; exit 0; fi
nohup "$HOME/nika-run.sh" > /dev/null 2>&1 &
echo "Ника запущена"
EOF

cat > "$HOME/nika-stop.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Остановить бота
pkill -f nika-run.sh || true
pkill -f "bot.main" || true
echo "Ника остановлена"
EOF

cat > "$HOME/nika-log.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Последние строки лога бота (выход — Ctrl+C)
tail -n 40 -f "$PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu/root/nika/data/logs/bot.log"
EOF

cat > "$HOME/nika-update.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Обновить бота из GitHub и перезапустить
proot-distro login ubuntu -- bash -c "cd /root/nika && git pull --ff-only && .venv/bin/pip install -q -r requirements.txt"
"$HOME/nika-stop.sh"; sleep 2; "$HOME/nika-start.sh"
EOF
chmod +x "$HOME"/nika-*.sh

say "Автозапуск после перезагрузки (приложение Termux:Boot)"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-nika.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
"$HOME/nika-start.sh"
EOF
chmod +x "$HOME/.termux/boot/start-nika.sh"

say "Проверяю связь с Telegram и агрегатором"
proot-distro login ubuntu -- bash -c "cd /root/nika && .venv/bin/python -m tools.check_connectivity" || {
  echo "Связь не прошла — бот не запущен. Пришли этот текст тому, кто помогает с настройкой."
  exit 1
}

say "Запускаю Нику"
"$HOME/nika-stop.sh" > /dev/null
sleep 2
"$HOME/nika-start.sh"
cat <<'EOF'

Готово! Ника работает на этом телефоне.
  ~/nika-log.sh     — смотреть лог (выход Ctrl+C)
  ~/nika-stop.sh    — остановить
  ~/nika-start.sh   — запустить
  ~/nika-update.sh  — обновить из GitHub
Termux можно свернуть, но не смахивать из списка приложений.
EOF
