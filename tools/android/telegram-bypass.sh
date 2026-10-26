#!/data/data/com.termux/files/usr/bin/bash
# Обход блокировки Telegram на телефоне без VPN и без root — через ByeDPI (https://github.com/hufrea/byedpi).
#
# Запуск в Termux (VPN на телефоне при этом выключить):
#   curl -fsSL https://raw.githubusercontent.com/liceriusminar-lab/nika-bot/main/tools/android/telegram-bypass.sh | bash
#
# Что делает: ставит ByeDPI, подбирает рабочие параметры обхода, проверяя реальное подключение
# к api.telegram.org, запускает ByeDPI в фоне как локальный SOCKS5-прокси (127.0.0.1:1080),
# включает его в автозапуск и прописывает боту TELEGRAM_PROXY. Через прокси идут только
# запросы бота к Telegram — агрегатор нейросетей и всё остальное работают напрямую.
set -euo pipefail
trap 'echo; echo "Прервано."; exit 130' INT

PORT=1080
ARGS_FILE="$HOME/.byedpi-args"
# Варианты параметров ByeDPI — от простых к сложным; берётся первый, с которым Telegram открывается
PRESETS=(
  "-d1"
  "-s1+s -d3+s"
  "-d1 -f-1"
  "-r3+s"
  "-d1 -s1+s"
  "-o1 -d1"
  "-s1 -d3+s -r1+s"
  "-f-1 -t6"
  "-d1 -f-1 -t8 -r1+s"
  "-s1 -o1 -d1 -r1+s -Ar -f-1 -As"
  "-s1 -q1 -Y -At,r,s -s5 -o1+s -At -f-1 -r1+s -As -s1 -o1+s -s-1"
)

say() { printf '\n\033[1;35m== %s\033[0m\n' "$1"; }
ubuntu() { proot-distro login ubuntu -- bash -c "$1" < /dev/null; }
# Код ответа api.telegram.org: 302 — открывается, 000 — нет
tg_code() { curl -s -o /dev/null -m 8 -w '%{http_code}' "$@" https://api.telegram.org/ || true; }

ubuntu "test -f /root/nika/.env" || { echo "Бот ещё не установлен — сначала запусти setup.sh"; exit 1; }

say "Проверяю Telegram напрямую"
if [ "$(tg_code)" = "302" ]; then
  echo "Telegram открывается и без обхода. Если включён VPN — выключи его и запусти скрипт снова."
  echo "(Обход нужен, когда без VPN Telegram не открывается.)"
  exit 0
fi
echo "напрямую не открывается — нужен обход"

say "Устанавливаю ByeDPI"
pkg update -y < /dev/null
if ! command -v ciadpi > /dev/null; then
  pkg install -y byedpi < /dev/null 2>/dev/null || true
fi
if ! command -v ciadpi > /dev/null; then
  echo "готового пакета нет — собираю из исходников"
  pkg install -y git clang make < /dev/null
  rm -rf "$HOME/byedpi-src"
  git clone --depth 1 https://github.com/hufrea/byedpi "$HOME/byedpi-src"
  make -C "$HOME/byedpi-src" < /dev/null
  cp "$HOME/byedpi-src/ciadpi" "$PREFIX/bin/ciadpi"
fi
echo "ByeDPI: $(command -v ciadpi)"

say "Подбираю параметры обхода"
pkill -f "ciadpi" 2>/dev/null || true
FOUND=""
for preset in "${PRESETS[@]}"; do
  # shellcheck disable=SC2086 — параметры специально разбиваются на слова
  ciadpi -i 127.0.0.1 -p "$PORT" $preset > /dev/null 2>&1 &
  pid=$!
  sleep 1
  code="$(tg_code --socks5-hostname "127.0.0.1:$PORT")"
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  echo "  [$preset] → $([ "$code" = "302" ] && echo "работает" || echo "нет")"
  if [ "$code" = "302" ]; then FOUND="$preset"; break; fi
done
if [ -z "$FOUND" ]; then
  echo
  echo "Ни один вариант не пробил блокировку. Пришли этот экран тому, кто помогает с настройкой."
  exit 1
fi
echo "$FOUND" > "$ARGS_FILE"

say "Запускаю ByeDPI в фоне и добавляю в автозапуск"
cat > "$HOME/byedpi-run.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# Держит ByeDPI запущенным (локальный прокси для Telegram на 127.0.0.1:$PORT)
while true; do
  ciadpi -i 127.0.0.1 -p $PORT \$(cat "$ARGS_FILE") > /dev/null 2>&1
  sleep 5
done
EOF
chmod +x "$HOME/byedpi-run.sh"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/00-start-byedpi.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
pgrep -f byedpi-run.sh > /dev/null || nohup "$HOME/byedpi-run.sh" > /dev/null 2>&1 &
EOF
chmod +x "$HOME/.termux/boot/00-start-byedpi.sh"
pkill -f byedpi-run.sh 2>/dev/null || true
pkill -f ciadpi 2>/dev/null || true
nohup "$HOME/byedpi-run.sh" > /dev/null 2>&1 &
sleep 1

say "Пускаю бота в Telegram через обход"
ubuntu "sed -i '/^TELEGRAM_PROXY=/d' /root/nika/.env && echo 'TELEGRAM_PROXY=socks5://127.0.0.1:$PORT' >> /root/nika/.env"

say "Проверяю связь"
if ! ubuntu "cd /root/nika && .venv/bin/python -m tools.check_connectivity"; then
  echo "Связь не прошла — пришли этот экран тому, кто помогает с настройкой."
  exit 1
fi

say "Перезапускаю Нику"
"$HOME/nika-stop.sh" > /dev/null
sleep 2
"$HOME/nika-start.sh"
echo
echo "Готово! Telegram — через ByeDPI, нейросеть — напрямую. Напиши Нике."
