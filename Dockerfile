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
COPY data/markov_model.pkl /app/data/markov_model.pkl

# cookie.json は Coolify の環境変数 COOKIE_JSON から注入
# 起動時に env var → /app/data/cookie.json に書き出す
# マルコフモデルは data/markov_model.pkl をビルド時に組み込み

CMD ["python3", "hikabooru_x_poster.py"]
