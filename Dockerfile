# hikabooru → X ランダムメディア投稿 bot
# Coolify 用 Dockerfile

FROM python:3.12-slim

# ffprobe (ffmpeg) を動画時間チェック用にインストール
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 依存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーション
COPY hikabooru_x_poster.py .

# Cookieファイルと重複防止DBはボリュームマウント
# /data/cookie.json
# /data/posted.json

ENV COOKIE_PATH=/data/cookie.json
ENV DEDUP_PATH=/data/posted.json

# デフォルト: 15分間隔で永続実行
CMD ["python3", "hikabooru_x_poster.py", \
     "--cookie", "/data/cookie.json", \
     "--dedup", "/data/posted.json", \
     "--interval", "900"]
