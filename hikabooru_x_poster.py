#!/usr/bin/env python3
"""
hikabooru_x_poster.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
hikabooru（Oxibooru）から完全ランダムに画像/動画/GIFを選び、
15分毎に X (Twitter) にメディアのみ投稿する bot。

使い方:
  # 本番（15分間隔で永続実行）
  python hikabooru_x_poster.py

  # テスト（投稿せずランダム選出のみ表示）
  python hikabooru_x_poster.py --test

  # 1回だけ投稿して終了
  python hikabooru_x_poster.py --once

  # 間隔を変更
  python hikabooru_x_poster.py --interval 600

依存:
  pip install twifork httpx
  ffprobe (ffmpeg)
"""

from __future__ import annotations

import argparse
import asyncio
import orjson
import logging
import os
import random
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Optional

import httpx
from twikit import Client

# twifork 2.3.5 のバグ修正: ClientTransaction.__init__ が
# self.key / self.animation_key を初期化していない
from twikit.x_client_transaction import transaction as _txn
_orig_init = _txn.ClientTransaction.__init__
def _patched_init(self):
    _orig_init(self)
    self.key = None
    self.animation_key = None
_txn.ClientTransaction.__init__ = _patched_init

# ═══════════════════════════════════════════════════════════════
# 設定
# ═══════════════════════════════════════════════════════════════

HIKABOORU_BASE = "https://hikabooru.hikamer.f5.si"
MAX_VIDEO_DURATION = 140  # 秒（Xの制限）
DEFAULT_INTERVAL = 900  # 15分（秒）

# 環境に応じたデフォルトCookieパス
#   Docker/Coolify → /data/cookie.json （docker-composeで明示的に渡すので実質使われない）
#   WSL/Linux      → プロジェクトフォルダ内の data/cookie.json
#   Windows        → 同上
def _default_cookie_path() -> str:
    # Docker: /data/ が存在すればそれを使う
    if os.path.isdir("/data"):
        return "/data/cookie.json"
    # それ以外: スクリプトと同じ階層の data/cookie.json
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hikabooru_x")


# ═══════════════════════════════════════════════════════════════
# Cookie コンバーター（ブラウザエクスポート→twikit形式）
# ═══════════════════════════════════════════════════════════════

def convert_cookies(browser_cookie_path: str) -> dict[str, str]:
    with open(browser_cookie_path, "rb") as f:
        data = orjson.loads(f.read())

    if isinstance(data, dict) and "auth_token" in data:
        return {"auth_token": data["auth_token"], "ct0": data.get("ct0", "")}

    if isinstance(data, list):
        cookies = {}
        for c in data:
            name = c.get("name", "")
            if name in ("auth_token", "ct0"):
                cookies[name] = c["value"]
        if "auth_token" not in cookies:
            raise ValueError(
                "auth_token がCookieファイルに見つかりません。"
            )
        return cookies

    raise ValueError(f"不明なCookie形式: {type(data)}")


# ═══════════════════════════════════════════════════════════════
# hikabooru API クライアント
# ═══════════════════════════════════════════════════════════════

class HikabooruClient:
    """Oxibooru (hikabooru) API からランダム投稿を取得"""

    def __init__(self, base_url: str = HIKABOORU_BASE):
        self.base = base_url
        self.api = f"{base_url}/api"
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=30.0,
            )
        return self._http

    async def close(self):
        if self._http:
            await self._http.aclose()
            self._http = None

    async def random_post(self) -> dict:
        """
        完全ランダムに1件取得。
        
        sort:random は決定論的だが、毎回 fresh な total から
        ランダムオフセットを生成 → 真の一様ランダム。
        """
        http = await self._client()

        # ① 総投稿数を毎回取得（limit=1 は軽量、total は常に返る）
        resp = await http.get(
            f"{self.api}/posts",
            params={"query": "sort:random", "limit": 1},
        )
        resp.raise_for_status()
        total = resp.json().get("total")
        if not total:
            raise RuntimeError("総投稿数を取得できませんでした")

        # ② total の範囲内でランダムオフセット
        offset = random.randint(0, total - 1)
        resp = await http.get(
            f"{self.api}/posts",
            params={
                "query": "sort:random",
                "limit": 1,
                "offset": offset,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            raise RuntimeError("hikabooruから投稿が取得できませんでした")
        return results[0]

    def content_url(self, post: dict) -> str:
        return f"{self.base}/{post['contentUrl']}"

    def thumbnail_url(self, post: dict) -> str:
        return f"{self.base}/{post['thumbnailUrl']}"

    @staticmethod
    def post_type(post: dict) -> str:
        return post.get("type", "unknown")

    @staticmethod
    def post_summary(post: dict) -> str:
        pid = post["id"]
        ptype = post.get("type", "?")
        safety = post.get("safety", "?")
        tags = [t["names"][0] for t in post.get("tags", [])[:5]]
        tagstr = ", ".join(tags)
        filesize = post.get("fileSize", 0)
        size_mb = filesize / (1024 * 1024)
        return f"[#{pid}] {ptype} | {safety} | {size_mb:.1f}MB | {tagstr}"


# ═══════════════════════════════════════════════════════════════
# 動画時間チェック
# ═══════════════════════════════════════════════════════════════

def get_video_duration(url: str) -> float:
    """ffprobeで動画の長さ（秒）を取得。失敗時は -1"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                url,
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return -1.0


# ═══════════════════════════════════════════════════════════════
# X 投稿クライアント（twifork / cookie認証）
# ═══════════════════════════════════════════════════════════════

class XPoster:
    """twiforkを使ってXにメディア投稿。Cookie認証のみ（login禁止）"""

    def __init__(self, cookie_path: str):
        self.cookie_path = cookie_path
        self.client: Optional[Client] = None

    async def setup(self):
        cookies = convert_cookies(self.cookie_path)
        self.client = Client(language="ja")
        self.client.set_cookies(cookies)
        try:
            uid = await self.client.user_id()
            log.info("X認証OK (user_id=%s)", uid)
        except Exception as e:
            log.warning("X認証確認中の警告（投稿は試行されます）: %s", e)
        return self

    async def post_media(self, media_path: str) -> str:
        if self.client is None:
            raise RuntimeError("XPoster.setup() を先に呼んでください")
        media_id = await self.client.upload_media(media_path)
        tweet = await self.client.create_tweet(text="", media_ids=[media_id])
        return tweet.id if hasattr(tweet, 'id') else str(tweet)

    async def close(self):
        # logout() は呼ばない（Cookieが無効化されるため）
        pass


# ═══════════════════════════════════════════════════════════════
# メディア変換（X非対応フォーマット → 対応フォーマット）
# ═══════════════════════════════════════════════════════════════

# X が受け付けるフォーマット
#   画像: .jpg .png .gif
#   動画: .mp4
X_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif"}
X_VIDEO_EXTS = {".mp4"}
X_OK_EXTS = X_IMAGE_EXTS | X_VIDEO_EXTS

# ffmpeg 変換マップ: 入力拡張子 → (出力拡張子, ffmpeg引数)
CONVERSION_MAP = {
    ".webp": (".jpg", ["-q:v", "2"]),          # WebP画像 → JPEG
    ".avif": (".jpg", ["-q:v", "2"]),          # AVIF画像 → JPEG
    ".heif": (".jpg", ["-q:v", "2"]),          # HEIF画像 → JPEG
    ".heic": (".jpg", ["-q:v", "2"]),          # HEIC画像 → JPEG
    ".webm": (".mp4", ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart"]),  # WebM動画 → MP4
    ".mov":  (".mp4", ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart"]),  # MOV動画 → MP4
}

def convert_for_x(input_path: str) -> str:
    """
    X非対応フォーマットを ffmpeg で変換。
    変換不要なら input_path をそのまま返す。
    失敗時は元ファイルを返す（Xが受け付ければOK、ダメならエラー）。
    """
    ext = os.path.splitext(input_path)[1].lower()

    # すでに対応フォーマットならスルー
    if ext in X_OK_EXTS:
        return input_path

    # 変換定義がないならそのまま（.swf等はここで止まるが、pickでスキップ済み）
    if ext not in CONVERSION_MAP:
        log.warning("未知の拡張子 %s、変換なしで試行", ext)
        return input_path

    out_ext, ffmpeg_args = CONVERSION_MAP[ext]
    fd, out_path = tempfile.mkstemp(suffix=out_ext, prefix="hikabooru_conv_")
    os.close(fd)

    cmd = ["ffmpeg", "-y", "-i", input_path, *ffmpeg_args, out_path]
    log.info("変換: %s → %s (%s)", ext, out_ext, os.path.basename(out_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and os.path.getsize(out_path) > 0:
            log.info("変換成功: %.1fMB", os.path.getsize(out_path) / (1024*1024))
            return out_path
        else:
            log.warning("変換失敗: %s", result.stderr[-200:] if result.stderr else "unknown")
            os.unlink(out_path)
            return input_path  # フォールバック
    except Exception as e:
        log.warning("変換エラー: %s", e)
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return input_path


# ═══════════════════════════════════════════════════════════════
# メインロジック
# ═══════════════════════════════════════════════════════════════

async def pick_random_media(hikabooru: HikabooruClient) -> dict:
    """
    動画140秒以内のメディアをランダム選出。
    重複チェックなし。44749件から15分間隔なら衝突確率は無視できる。
    """
    while True:
        post = await hikabooru.random_post()
        ptype = hikabooru.post_type(post)
        pid = post["id"]

        if ptype == "flash":
            log.debug("#%d はflash、スキップ", pid)
            continue

        if ptype == "video":
            url = hikabooru.content_url(post)
            duration = get_video_duration(url)
            if duration < 0:
                log.debug("#%d 動画時間取得失敗、スキップ", pid)
                continue
            if duration > MAX_VIDEO_DURATION:
                log.info("#%d 動画が%.0f秒（>%d秒）、再抽選", pid, duration, MAX_VIDEO_DURATION)
                continue
            log.info("#%d 動画%.0f秒 OK", pid, duration)

        log.info("✅ %s", hikabooru.post_summary(post))
        return post


async def download_media(http: httpx.AsyncClient, url: str) -> str:
    """ダウンロード → 必要ならX用に変換 → 変換後パスを返す"""
    resp = await http.get(url)
    resp.raise_for_status()

    # 拡張子をURLから推測
    url_ext = os.path.splitext(url.split("?")[0])[1].lower() or ".bin"

    fd, tmp_path = tempfile.mkstemp(suffix=url_ext, prefix="hikabooru_")
    os.close(fd)
    with open(tmp_path, "wb") as f:
        f.write(resp.content)

    size_mb = len(resp.content) / (1024 * 1024)
    log.info("ダウンロード: %.1fMB → %s", size_mb, tmp_path)

    # 変換（X非対応フォーマット → jpg/mp4）
    converted = convert_for_x(tmp_path)
    if converted != tmp_path:
        # 変換後ファイルが別なら元ファイルを削除
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return converted


async def run_once(hikabooru: HikabooruClient,
                   xposter: Optional[XPoster] = None,
                   test_mode: bool = False):
    log.info("━" * 50)
    log.info("ランダム選出開始 (test_mode=%s)", test_mode)

    post = await pick_random_media(hikabooru)
    content_url = hikabooru.content_url(post)

    print(f"\n{'🧪 TEST ' if test_mode else '🐦'} 選出: {hikabooru.post_summary(post)}")
    print(f"   URL: {content_url}")
    print(f"   Tags: {len(post.get('tags', []))}件")
    print(f"   サムネ: {hikabooru.thumbnail_url(post)}")

    if test_mode:
        print("   (テストモードのため投稿はスキップ)\n")
        return post

    http = await hikabooru._client()
    tmp_path = await download_media(http, content_url)

    try:
        tweet_id = await xposter.post_media(tmp_path)
        log.info("🎉 投稿成功! tweet_id=%s | post_id=%d", tweet_id, post["id"])
        print(f"   ✅ 投稿成功! tweet_id={tweet_id}\n")
    except Exception as e:
        log.error("投稿失敗 (post #%d): %s", post["id"], e)
        print(f"   ❌ 投稿失敗: {e}\n")
        raise
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return post


async def main_loop(args):
    log.info("hikabooru_x_poster 起動")
    log.info("  hikabooru: %s", HIKABOORU_BASE)
    log.info("  cookie: %s", args.cookie)
    log.info("  interval: %d秒 (%.1f分)", args.interval, args.interval / 60)
    log.info("  test_mode: %s", args.test)

    hikabooru = HikabooruClient()

    xposter = None
    if not args.test:
        xposter = await XPoster(args.cookie).setup()

    try:
        if args.once:
            await run_once(hikabooru, xposter, test_mode=args.test)
        else:
            while True:
                try:
                    await run_once(hikabooru, xposter, test_mode=args.test)
                except Exception as e:
                    log.error("実行エラー: %s", e)

                next_run = datetime.now().timestamp() + args.interval
                next_str = datetime.fromtimestamp(next_run).strftime("%H:%M:%S")
                log.info("次回実行: %s (%d秒後)", next_str, args.interval)
                await asyncio.sleep(args.interval)

    except KeyboardInterrupt:
        log.info("割り込みにより終了")
    finally:
        await hikabooru.close()
        if xposter:
            await xposter.close()


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="hikabooru → X ランダムメディア投稿bot")
    parser.add_argument("--test", action="store_true",
                        help="テストモード（ランダム選出のみ、投稿しない）")
    parser.add_argument("--once", action="store_true",
                        help="1回だけ実行して終了")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"実行間隔（秒）（デフォルト: {DEFAULT_INTERVAL}秒=15分）")
    parser.add_argument("--cookie", type=str, default=_default_cookie_path(),
                        help="ブラウザエクスポートCookieのJSONファイルパス")
    args = parser.parse_args()

    if not os.path.exists(args.cookie):
        print(f"❌ Cookieファイルが見つかりません: {args.cookie}")
        sys.exit(1)

    asyncio.run(main_loop(args))


if __name__ == "__main__":
    main()
