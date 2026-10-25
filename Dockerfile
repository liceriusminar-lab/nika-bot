# Образ бота. Собирается на сервере командой: docker compose up -d --build
FROM python:3.12-slim

# Часовой пояс и вывод логов без задержки
ENV TZ=Asia/Vladivostok \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала только зависимости: если поменялся лишь код, Docker не будет ставить их заново
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
COPY tools ./tools
COPY persona.yaml .

# База, логи, картинки и бэкапы — в /app/data, эта папка подключается с сервера (см. docker-compose.yml)
CMD ["python", "-m", "bot.main"]
