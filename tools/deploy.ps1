# Выкладка бота на сервер с ПК (Windows). Запуск из папки проекта:
#
#   первый раз — подготовить сервер (Docker и т.п.):
#     powershell -ExecutionPolicy Bypass -File tools\deploy.ps1 -Server root@1.2.3.4 -Setup
#   первый переезд — выложить бота и перенести базу с ПК (вся память Ники):
#     powershell -ExecutionPolicy Bypass -File tools\deploy.ps1 -Server root@1.2.3.4 -WithData
#   обновление после изменений в коде, persona.yaml или .env:
#     powershell -ExecutionPolicy Bypass -File tools\deploy.ps1 -Server root@1.2.3.4
#
# Нужны встроенные в Windows ssh, scp и tar (есть в Windows 10/11).

param(
    [Parameter(Mandatory = $true)][string]$Server,   # пользователь@адрес сервера, например root@1.2.3.4
    [switch]$Setup,                                  # подготовить сервер (один раз)
    [switch]$WithData                                # перенести базу и картинки с ПК (ПЕРЕЗАПИШЕТ базу на сервере!)
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Run-Remote([string]$command) {
    ssh $Server $command
    if ($LASTEXITCODE -ne 0) { throw "Команда на сервере завершилась с ошибкой: $command" }
}

if ($Setup) {
    Write-Host "== Подготовка сервера (это займёт пару минут)" -ForegroundColor Cyan
    scp tools/server_setup.sh "${Server}:/tmp/server_setup.sh"
    Run-Remote "bash /tmp/server_setup.sh"
    Write-Host "== Сервер готов. Теперь запусти этот же скрипт с -WithData" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path .env)) { throw "Нет файла .env — без него бот на сервере не запустится" }

Write-Host "== Упаковываю проект" -ForegroundColor Cyan
$archive = Join-Path $env:TEMP "nika-deploy.tar.gz"
tar -czf $archive --exclude=__pycache__ --exclude=tools/image_test --exclude="tools/*.html" `
    bot tools persona.yaml requirements.txt Dockerfile docker-compose.yml .dockerignore .env
if ($LASTEXITCODE -ne 0) { throw "Не удалось упаковать проект" }

Write-Host "== Отправляю на сервер" -ForegroundColor Cyan
scp $archive "${Server}:/tmp/nika-deploy.tar.gz"
Run-Remote "mkdir -p ~/nika/data && tar -xzf /tmp/nika-deploy.tar.gz -C ~/nika && rm /tmp/nika-deploy.tar.gz"
Remove-Item $archive

if ($WithData) {
    Write-Host "== Переношу базу и картинки с ПК" -ForegroundColor Cyan
    # Останавливаем бота на сервере и на всякий случай сохраняем его старую базу
    Run-Remote "cd ~/nika && (docker compose down || true) && (test -f data/nika.db && cp data/nika.db data/nika.db.before-upload || true)"
    scp data/nika.db "${Server}:~/nika/data/nika.db"
    if (Test-Path data/images) { scp -r data/images "${Server}:~/nika/data/" }
}

Write-Host "== Собираю и запускаю бота" -ForegroundColor Cyan
Run-Remote "cd ~/nika && docker compose up -d --build && docker compose ps"

Write-Host "== Готово. Логи: ssh $Server `"cd ~/nika && docker compose logs -f --tail 50`"" -ForegroundColor Green
Write-Host "   Не забудь: бот на ПК должен быть остановлен, иначе два бота будут мешать друг другу" -ForegroundColor Yellow
