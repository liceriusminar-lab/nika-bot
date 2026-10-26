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
#
# Всё, что внутри Ubuntu, делается через `proot-distro login`, а не через путь к её папке:
# у разных версий proot-distro этот путь разный.
set -euo pipefail
# Ctrl+C прерывает установку целиком, а не только текущий шаг
trap 'echo; echo "Установка прервана."; exit 130' INT

REPO="https://github.com/liceriusminar-lab/nika-bot.git"
DL="$HOME/storage/downloads"
# Сюда старая версия скрипта по ошибке складывала настройки — заберём их оттуда
OLD="$PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu"

say() { printf '\n\033[1;35m== %s\033[0m\n' "$1"; }
# Команда внутри Ubuntu. </dev/null — чтобы она не съела сам скрипт, который идёт через curl | bash
ubuntu() { proot-distro login ubuntu -- bash -c "$1" < /dev/null; }
# Положить файл с телефона внутрь Ubuntu: ubuntu_put <файл> <путь внутри Ubuntu>
ubuntu_put() { proot-distro login ubuntu -- bash -c "mkdir -p \"\$(dirname '$2')\" && cat > '$2'" < "$1"; }

say "Не даём телефону усыпить Termux"
termux-wake-lock || true

say "Доступ к папке «Загрузки»"
if [ ! -d "$DL" ]; then
  termux-setup-storage || true
  echo "Разреши доступ к файлам во всплывшем окне…"
  for _ in $(seq 1 60); do [ -d "$DL" ] && break; sleep 1; done
fi
[ -d "$DL" ] || { echo "Нет доступа к «Загрузкам». Запусти скрипт ещё раз и нажми «Разрешить»."; exit 1; }

# Откуда брать настройки и базу: «Загрузки» или папка, куда их положила старая версия скрипта
ENV_SRC=""; DB_SRC=""
[ -f "$DL/nika.env" ] && ENV_SRC="$DL/nika.env"
[ -f "$DL/nika.db" ] && DB_SRC="$DL/nika.db"
if [ -d "$OLD/root/nika" ] && [ ! -e "$OLD/etc/os-release" ]; then
  [ -z "$ENV_SRC" ] && [ -f "$OLD/root/nika/.env" ] && ENV_SRC="$OLD/root/nika/.env"
  [ -z "$DB_SRC" ] && [ -f "$OLD/root/nika/data/nika.db" ] && DB_SRC="$OLD/root/nika/data/nika.db"
fi

say "Пакеты Termux"
pkg update -y < /dev/null
pkg install -y proot-distro procps < /dev/null

if ! proot-distro login ubuntu -- true < /dev/null 2>/dev/null; then
  # Папка, оставшаяся от старой версии скрипта, мешает установке — сначала уберём её
  if [ -d "$OLD" ] && [ ! -e "$OLD/etc/os-release" ]; then
    TMP="$HOME/.nika-rescue"; mkdir -p "$TMP"
    [ -n "$ENV_SRC" ] && [ "$ENV_SRC" != "$DL/nika.env" ] && cp "$ENV_SRC" "$TMP/nika.env" && ENV_SRC="$TMP/nika.env"
    [ -n "$DB_SRC" ] && [ "$DB_SRC" != "$DL/nika.db" ] && cp "$DB_SRC" "$TMP/nika.db" && DB_SRC="$TMP/nika.db"
    rm -rf "$OLD"
  fi
  say "Ставлю Ubuntu (один раз, несколько минут)"
  proot-distro install ubuntu < /dev/null
fi

say "Python и git внутри Ubuntu"
ubuntu "export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y python3 python3-venv git ca-certificates tzdata"

say "Код бота"
ubuntu "if [ -d /root/nika/.git ]; then git -C /root/nika pull --ff-only; else git clone $REPO /root/nika; fi"

say "Настройки и база"
if [ -n "$ENV_SRC" ]; then
  # Прокси для Telegram настраивается на самом телефоне (telegram-bypass.sh) — при замене настроек сохраняем его
  KEEP_PROXY="$(ubuntu "grep -E '^TELEGRAM_PROXY=.+' /root/nika/.env 2>/dev/null || true")"
  ubuntu_put "$ENV_SRC" /root/nika/.env
  if [ -n "$KEEP_PROXY" ] && ! ubuntu "grep -qE '^TELEGRAM_PROXY=.+' /root/nika/.env"; then
    ubuntu "sed -i '/^TELEGRAM_PROXY=/d' /root/nika/.env; printf '\n%s\n' '$KEEP_PROXY' >> /root/nika/.env"
  fi
  echo "настройки → /root/nika/.env"
fi
if [ -n "$DB_SRC" ]; then
  ubuntu "[ -f /root/nika/data/nika.db ] && cp /root/nika/data/nika.db /root/nika/data/nika.db.before-import || true"
  ubuntu_put "$DB_SRC" /root/nika/data/nika.db
  echo "база → /root/nika/data/nika.db"
fi

say "Проверяю файл настроек"
# Блокнот в Windows мог сохранить метку кодировки (BOM) и переводы строк CRLF — убираем
if ! ubuntu "test -f /root/nika/.env"; then
  echo "Файла настроек нет. Положи nika.env в «Загрузки» и запусти скрипт снова."
  exit 1
fi
ubuntu "sed -i '1s/^\xEF\xBB\xBF//; s/\r\$//' /root/nika/.env
        echo \"Файл настроек: \$(stat -c%s /root/nika/.env) байт, строк с настройками: \$(grep -cE '^[A-Z_]+=' /root/nika/.env)\""
if ! ubuntu "grep -qE '^TELEGRAM_BOT_TOKEN=[0-9]+:.+' /root/nika/.env && grep -qE '^OWNER_TELEGRAM_ID=[0-9]+' /root/nika/.env"; then
  echo "В файле нет TELEGRAM_BOT_TOKEN или OWNER_TELEGRAM_ID — похоже, nika.env скопировался не целиком."
  echo "Скопируй nika.env в «Загрузки» заново (дождись конца копирования) и запусти скрипт ещё раз."
  ubuntu "rm -f /root/nika/.env"
  exit 1
fi
# Файлы благополучно внутри Ubuntu — ключи не оставляем в общей папке
rm -f "$DL/nika.env" "$DL/nika.db"
rm -rf "$HOME/.nika-rescue"
if [ -d "$OLD" ] && [ ! -e "$OLD/etc/os-release" ]; then rm -rf "$OLD"; fi

say "Библиотеки Python (первый раз — несколько минут)"
ubuntu "cd /root/nika && { [ -x .venv/bin/python ] || python3 -m venv .venv; } && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt"

say "Скрипты запуска"
cat > "$HOME/nika-run.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Держит бота запущенным: если он упал — перезапускает через 10 секунд.
termux-wake-lock
LOG="$HOME/nika.log"
while true; do
  # лог не даём разрастись больше 5 МБ
  [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 5000000 ] && : > "$LOG"
  proot-distro login ubuntu -- bash -c "cd /root/nika && TZ=Asia/Vladivostok .venv/bin/python -m bot.main" < /dev/null >> "$LOG" 2>&1
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
proot-distro login ubuntu -- tail -n 40 -f /root/nika/data/logs/bot.log
EOF

cat > "$HOME/nika-update.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Обновить бота из GitHub и перезапустить
proot-distro login ubuntu -- bash -c "cd /root/nika && git pull --ff-only && .venv/bin/pip install -q -r requirements.txt" < /dev/null
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
if ! ubuntu "cd /root/nika && .venv/bin/python -m tools.check_connectivity"; then
  # Что Python видит в файле настроек — без значений, только имена
  ubuntu "cd /root/nika && .venv/bin/python -c \"from dotenv import dotenv_values; v = dotenv_values('.env'); print('Python видит в .env настроек:', len(v), '| токен:', bool(v.get('TELEGRAM_BOT_TOKEN')), '| ID:', bool(v.get('OWNER_TELEGRAM_ID')))\"" || true
  echo "Связь не прошла — бот не запущен. Пришли этот текст тому, кто помогает с настройкой."
  exit 1
fi

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
