#!/data/data/com.termux/files/usr/bin/bash
# Удалённый доступ к телефону-серверу: SSH через Tailscale.
#
# Запуск в Termux (в кавычках — публичный ключ компьютера, содержимое ~/.ssh/id_ed25519.pub):
#   curl -fsSL https://raw.githubusercontent.com/liceriusminar-lab/nika-bot/main/tools/android/remote.sh | bash -s -- "ssh-ed25519 AAAA..."
# Ключ можно не указывать, а положить файлом *.pub в «Загрузки» — или входить по паролю (команда passwd).
#
# Почему Tailscale: в домашнем Wi-Fi роутер не пускает соединения между устройствами,
# а белого IP нет. Tailscale соединяет телефон и компьютер напрямую или через свои серверы.
# Работает в «userspace»-режиме — без root и без слота VPN в Android, так что VPN
# для Telegram (Happ) остаётся включённым. Входящие соединения на адрес Tailscale он
# отдаёт локальным программам — то есть SSH-серверу Termux на порту 8022.
#
# tailscaled запускается внутри Ubuntu (proot-distro): там есть /etc/resolv.conf,
# без которого официальная сборка не может узнавать адреса по имени.
# Повторный запуск безопасен: обновит Tailscale и перезапустит его.
set -euo pipefail
trap 'echo; echo "Прервано."; exit 130' INT

KEY="${1:-}"
DL="$HOME/storage/downloads"
HOSTNAME_TS="nika-phone"
TS_DIR=/root/.tailscale

say() { printf '\n\033[1;35m== %s\033[0m\n' "$1"; }
ubuntu() { proot-distro login ubuntu -- bash -c "$1" < /dev/null; }

say "SSH-сервер"
pkg install -y openssh < /dev/null
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys" && chmod 600 "$HOME/.ssh/authorized_keys"
add_key() {
  case "$1" in
    ssh-*|ecdsa-*) grep -qF "$1" "$HOME/.ssh/authorized_keys" || echo "$1" >> "$HOME/.ssh/authorized_keys"
                   echo "ключ добавлен: ${1:0:20}…${1: -20}" ;;
    *) echo "Это не похоже на публичный ключ SSH (должен начинаться с ssh-ed25519 или ssh-rsa) — пропускаю." ;;
  esac
}
[ -n "$KEY" ] && add_key "$KEY"
if [ -d "$DL" ]; then
  for f in "$DL"/*.pub; do
    [ -f "$f" ] || continue
    add_key "$(head -n1 "$f" | tr -d '\r')" && rm -f "$f"
  done
fi
if [ ! -s "$HOME/.ssh/authorized_keys" ]; then
  echo "Ключей нет — входить можно будет только по паролю. Если пароль ещё не задан: passwd"
fi

say "Tailscale внутри Ubuntu"
proot-distro login ubuntu -- true < /dev/null 2>/dev/null || { echo "Ubuntu не найдена — сначала поставь бота (setup.sh)."; exit 1; }
ubuntu "command -v curl > /dev/null || { export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y curl ca-certificates; }"
ubuntu '
set -e
case "$(dpkg --print-architecture)" in
  arm64) A=arm64 ;; armhf) A=arm ;; amd64) A=amd64 ;; i386) A=386 ;;
  *) echo "Неизвестная архитектура: $(dpkg --print-architecture)"; exit 1 ;;
esac
F=$(curl -fsSL "https://pkgs.tailscale.com/stable/?mode=json" | grep -o "tailscale_[0-9.]*_${A}\.tgz" | head -n1)
[ -n "$F" ] || { echo "Не нашёл сборку Tailscale для $A"; exit 1; }
V=${F#tailscale_}; V=${V%%_*}
if [ "$(tailscale version 2>/dev/null | head -n1)" = "$V" ]; then echo "Tailscale $V уже стоит"; exit 0; fi
cd /tmp
curl -fsSLO "https://pkgs.tailscale.com/stable/$F"
echo "$(curl -fsSL "https://pkgs.tailscale.com/stable/$F.sha256")  $F" | sha256sum -c --quiet
tar xzf "$F"
install -m 755 "${F%.tgz}/tailscale" "${F%.tgz}/tailscaled" /usr/local/bin/
rm -rf "$F" "${F%.tgz}"
echo "Tailscale $V установлен"
'
ubuntu "mkdir -p $TS_DIR"

say "Скрипты"
# Команда tailscale прямо в Termux — сама сходит в Ubuntu
cat > "$PREFIX/bin/tailscale" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
exec proot-distro login ubuntu -- tailscale --socket=$TS_DIR/tailscaled.sock "\$@"
EOF
chmod +x "$PREFIX/bin/tailscale"

cat > "$HOME/remote-run.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# Держит tailscaled запущенным: упал — перезапуск через 10 секунд.
LOG="\$HOME/tailscaled.log"
while true; do
  proot-distro login ubuntu -- tailscaled --tun=userspace-networking --statedir=$TS_DIR \\
    --socket=$TS_DIR/tailscaled.sock < /dev/null >> "\$LOG" 2>&1 &
  PID=\$!
  while kill -0 "\$PID" 2> /dev/null; do
    # лог больше 2 МБ — обнуляем
    [ "\$(stat -c%s "\$LOG" 2> /dev/null || echo 0)" -gt 2000000 ] && : > "\$LOG"
    sleep 60
  done
  echo "\$(date '+%F %T') tailscaled остановился, перезапуск через 10 с" >> "\$LOG"
  sleep 10
done
EOF

cat > "$HOME/remote-start.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Запустить SSH-сервер и Tailscale (если ещё не запущены)
pgrep -x sshd > /dev/null || sshd
pgrep -f remote-run.sh > /dev/null || nohup "$HOME/remote-run.sh" > /dev/null 2>&1 &
echo "SSH и Tailscale запущены"
EOF

cat > "$HOME/remote-stop.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Остановить удалённый доступ
pkill -f remote-run.sh || true
pkill -f tailscaled || true
pkill -x sshd || true
echo "Удалённый доступ выключен"
EOF
chmod +x "$HOME"/remote-*.sh

mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-remote.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
"$HOME/remote-start.sh"
EOF
chmod +x "$HOME/.termux/boot/start-remote.sh"

say "Запускаю"
pkill -f remote-run.sh || true
pkill -f tailscaled || true
sleep 1
"$HOME/remote-start.sh"
for _ in $(seq 1 30); do ubuntu "test -S $TS_DIR/tailscaled.sock" && break; sleep 1; done
if ! ubuntu "test -S $TS_DIR/tailscaled.sock"; then
  echo "tailscaled не запустился. Последние строки лога:"; tail -n 20 "$HOME/tailscaled.log"; exit 1
fi

say "Вход в Tailscale"
UP_LOG="$HOME/.tailscale-up.log"
tailscale up --hostname="$HOSTNAME_TS" < /dev/null > "$UP_LOG" 2>&1 &
UP=$!
OPENED=""
while kill -0 "$UP" 2> /dev/null; do
  if [ -z "$OPENED" ]; then
    URL="$(grep -o 'https://login\.tailscale\.com/[^ ]*' "$UP_LOG" | head -n1 || true)"
    if [ -n "$URL" ]; then
      echo "Открываю страницу входа. Войди тем же аккаунтом, что и на компьютере:"
      echo "  $URL"
      termux-open-url "$URL" || true
      OPENED=1
    fi
  fi
  sleep 1
done
if ! wait "$UP"; then
  echo "Не получилось войти:"; cat "$UP_LOG"; exit 1
fi
rm -f "$UP_LOG"

IP="$(tailscale ip -4 2> /dev/null | head -n1 || true)"
cat <<EOF

Готово! Телефон в сети Tailscale.
  адрес:        $IP
  пользователь: $(whoami)
С компьютера (на нём тоже Tailscale, тот же аккаунт):
  ssh -p 8022 $(whoami)@$IP

  ~/remote-stop.sh   — выключить удалённый доступ
  ~/remote-start.sh  — включить
  tailscale status   — кто в сети
После перезагрузки всё поднимется само (Termux:Boot).
EOF
