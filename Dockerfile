# hikabooru → X ランダムメディア投稿 bot
# Coolify 用 Dockerfile

FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hikabooru_x_poster.py .
COPY data/cookie.json data/cookie.json

CMD ["python3", "hikabooru_x_poster.py", "--cookie", "/app/data/cookie.json", "--interval", "900"]
