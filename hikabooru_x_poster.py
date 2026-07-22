#!/usr/bin/env python3
"""
hikabooru_x_poster.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
hikabooru（Oxibooru）から完全ランダムに画像/動画/GIFを選び、
X (Twitter) と Misskey にマルチ投稿する bot。

使い方:
  # 本番（X=30分、Misskey=5分で永続実行）
  python hikabooru_x_poster.py

  # Misskeyのみ
  python hikabooru_x_poster.py --no-x

  # Xのみ（30分間隔）
  python hikabooru_x_poster.py --no-misskey

  # テスト（投稿せずランダム選出のみ表示）
  python hikabooru_x_poster.py --test

  # 1回だけ投稿して終了
  python hikabooru_x_poster.py --once

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
import pickle
import random
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
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
X_DEFAULT_INTERVAL = 1800  # 30分
MISSKEY_DEFAULT_INTERVAL = 300  # 5分（レート制限なし）

MISSKEY_BASE = "https://sikotter.hikamer.f5.si"
# MISSKEY_TOKEN は環境変数または --misskey-token で指定

def _default_markov_path() -> str:
    if os.path.isdir("/data"):
        return "/data/markov_model.pkl"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "markov_model.pkl")

def _default_cookie_path() -> str:
    if os.path.isdir("/data"):
        return "/data/cookie.json"
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
# マルコフ連鎖テキスト生成器
# ═══════════════════════════════════════════════════════════════

BOS, EOS = "__BOS__", "__EOS__"


class MarkovGenerator:
    """マルコフ連鎖でランダムな日本語テキストを生成"""

    def __init__(self, model_path: str):
        with open(model_path, "rb") as f:
            model = pickle.load(f)
        self.n = model["n_gram"]
        self.transitions = model["transitions"]
        log.info("Markov model loaded: n_gram=%d, contexts=%d, sentences=%d",
                 self.n, model.get("contexts", 0), model.get("total_sentences", 0))

    def generate(self, min_len: int = 1, max_tokens: int = 200) -> str:
        """テキストを1つ生成。@やURLを含むトークンは除外"""
        for _ in range(100):
            ctx = tuple([BOS] * (self.n - 1))
            tokens = []
            for _ in range(max_tokens):
                if ctx not in self.transitions:
                    break
                candidates = self.transitions[ctx]
                token_names = [t for t, _ in candidates]
                weights = [c for _, c in candidates]

                # @とURLを含むトークンはスキップ
                for _ in range(50):
                    t = random.choices(token_names, weights=weights, k=1)[0]
                    if not t.startswith("@") and not t.startswith("http"):
                        break
                else:
                    break

                if t == EOS:
                    if len(tokens) < min_len:
                        continue
                    break
                tokens.append(t)
                ctx = ctx[1:] + (t,)

            if len(tokens) >= min_len:
                return "".join(tokens).replace("\\n", "\n")

        return ""  # 生成失敗


# ═══════════════════════════════════════════════════════════════
# Cookie コンバーター
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
            raise ValueError("auth_token がCookieファイルに見つかりません。")
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
        http = await self._client()
        resp = await http.get(
            f"{self.api}/posts",
            params={"query": "sort:random", "limit": 1},
        )
        resp.raise_for_status()
        total = resp.json().get("total")
        if not total:
            raise RuntimeError("総投稿数を取得できませんでした")
        offset = random.randint(0, total - 1)
        resp = await http.get(
            f"{self.api}/posts",
            params={"query": "sort:random", "limit": 1, "offset": offset},
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
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return -1.0


# ═══════════════════════════════════════════════════════════════
# X 投稿クライアント
# ═══════════════════════════════════════════════════════════════

class XPoster:
    """twiforkを使ってXにメディア投稿。Cookie認証のみ"""

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
            log.warning("X認証確認中の警告: %s", e)
        return self

    async def post_media(self, media_path: str, is_video: bool = False, text: str = "") -> str:
        if self.client is None:
            raise RuntimeError("XPoster.setup() を先に呼んでください")
        if is_video:
            media_id = await self.client.upload_media(
                media_path, wait_for_completion=True, media_category="tweet_video",
            )
        else:
            media_id = await self.client.upload_media(media_path)
        tweet = await self.client.create_tweet(text=text, media_ids=[media_id])
        return tweet.id if hasattr(tweet, 'id') else str(tweet)

    async def close(self):
        pass


# ═══════════════════════════════════════════════════════════════
# Misskey 投稿クライアント
# ═══════════════════════════════════════════════════════════════

class MisskeyPoster:
    """Misskey API でメディア投稿。drive/files/create → notes/create"""

    def __init__(self, base_url: str = MISSKEY_BASE, token: str = ""):
        self.base = base_url
        self.token = token
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=60.0,
            )
        return self._http

    async def close(self):
        if self._http:
            await self._http.aclose()
            self._http = None

    async def post_media(self, media_path: str, is_video: bool = False, text: str = "") -> str:
        """メディアをアップロードしてノート作成。戻り値は note_id。
        is_video は Misskey では未使用（XPosterとのインターフェース統一のため）"""
        http = await self._client()

        # 1. ファイルをドライブにアップロード
        file_name = os.path.basename(media_path)
        with open(media_path, "rb") as f:
            resp = await http.post(
                f"{self.base}/api/drive/files/create",
                data={"i": self.token, "force": "true"},
                files={"file": (file_name, f)},
            )
        resp.raise_for_status()
        file_data = resp.json()
        file_id = file_data["id"]
        log.info("Misskey drive upload: %s (id=%s)", file_name, file_id)

        # 2. ノート作成（text は1文字以上必須）
        note_text = text if text else " "
        resp = await http.post(
            f"{self.base}/api/notes/create",
            json={
                "i": self.token,
                "text": note_text,
                "fileIds": [file_id],
                "visibility": "public",
            },
        )
        resp.raise_for_status()
        note = resp.json()["createdNote"]
        return note["id"]


# ═══════════════════════════════════════════════════════════════
# メディア変換
# ═══════════════════════════════════════════════════════════════

MISSKEY_OK_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".mp4", ".webm", ".mov"}
X_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif"}
X_VIDEO_EXTS = {".mp4"}
X_OK_EXTS = X_IMAGE_EXTS | X_VIDEO_EXTS

CONVERSION_MAP = {
    ".webp": (".jpg", ["-q:v", "2"]),
    ".avif": (".jpg", ["-q:v", "2"]),
    ".heif": (".jpg", ["-q:v", "2"]),
    ".heic": (".jpg", ["-q:v", "2"]),
    ".webm": (".mp4", ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart"]),
    ".mov":  (".mp4", ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart"]),
}


def convert_for_platform(input_path: str, ok_exts: set[str]) -> str:
    """
    X/Misskey非対応フォーマットを ffmpeg で変換。
    変換不要なら input_path をそのまま返す。
    """
    ext = os.path.splitext(input_path)[1].lower()
    if ext in ok_exts:
        return input_path
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
            log.info("変換成功: %.1fMB", os.path.getsize(out_path) / (1024 * 1024))
            return out_path
        else:
            log.warning("変換失敗: %s", result.stderr[-200:] if result.stderr else "?")
            os.unlink(out_path)
            return input_path
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

async def download_media(http: httpx.AsyncClient, url: str) -> str:
    """ダウンロードして一時ファイルパスを返す（変換は呼び出し側で）"""
    resp = await http.get(url)
    resp.raise_for_status()
    url_ext = os.path.splitext(url.split("?")[0])[1].lower() or ".bin"
    fd, tmp_path = tempfile.mkstemp(suffix=url_ext, prefix="hikabooru_")
    os.close(fd)
    with open(tmp_path, "wb") as f:
        f.write(resp.content)
    size_mb = len(resp.content) / (1024 * 1024)
    log.info("ダウンロード: %.1fMB → %s", size_mb, tmp_path)
    return tmp_path


async def platform_loop(
    name: str,
    hikabooru: HikabooruClient,
    poster,  # XPoster | MisskeyPoster
    interval: int,
    max_video_duration: int,
    ok_exts: set[str],
    test_mode: bool,
    markov: Optional[MarkovGenerator] = None,
):
    """1プラットフォーム分の投稿ループ"""
    log.info("[%s] 開始 (間隔=%d秒, 動画制限=%d秒)", name, interval, max_video_duration)

    while True:
        try:
            # ランダム選出（動画制限はプラットフォーム別）
            while True:
                post = await hikabooru.random_post()
                ptype = HikabooruClient.post_type(post)
                pid = post["id"]

                if ptype == "flash":
                    continue

                if ptype == "video":
                    url = hikabooru.content_url(post)
                    duration = get_video_duration(url)
                    if duration < 0 or duration > max_video_duration:
                        log.debug("[%s] #%d 動画%.0f秒 スキップ", name, pid, duration)
                        continue

                break

            summary = HikabooruClient.post_summary(post)
            is_video = (ptype == "video")
            log.info("[%s] ✅ %s", name, summary)
            print(f"\n{'🧪 TEST ' if test_mode else '📤'} [{name}] 選出: {summary}")

            if test_mode:
                print(f"   (テストモードのため投稿スキップ)\n")
            else:
                content_url = hikabooru.content_url(post)
                http = await hikabooru._client()
                tmp_path = await download_media(http, content_url)

                # 変換
                converted = convert_for_platform(tmp_path, ok_exts)
                if converted != tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

                try:
                    markov_text = markov.generate(max_tokens=50) if markov else ""
                    post_id = await poster.post_media(converted, is_video=is_video, text=markov_text)
                    if markov_text:
                        log.info("[%s] マルコフ文: %s", name, markov_text[:80])
                    log.info("[%s] 🎉 投稿成功! post_id=%s | hikabooru_id=%d", name, post_id, pid)
                    print(f"   ✅ [{name}] 投稿成功! id={post_id}\n")
                except Exception as e:
                    log.error("[%s] 投稿失敗 (hikabooru #%d): %s", name, pid, e)
                    print(f"   ❌ [{name}] 投稿失敗: {e}\n")
                finally:
                    try:
                        os.unlink(converted)
                    except OSError:
                        pass

        except Exception as e:
            log.error("[%s] 実行エラー: %s", name, e)

        next_run = datetime.now().timestamp() + interval
        next_str = datetime.fromtimestamp(next_run).strftime("%H:%M:%S")
        log.info("[%s] 次回実行: %s (%d秒後)", name, next_str, interval)
        await asyncio.sleep(interval)


async def main_loop(args):
    log.info("hikabooru_x_poster 起動")
    log.info("  hikabooru: %s", HIKABOORU_BASE)
    log.info("  test_mode: %s", args.test)
    log.info("  X: %s (間隔=%ds, 動画制限=%ds)", "ON" if not args.no_x else "OFF", args.x_interval, MAX_VIDEO_DURATION)
    log.info("  Misskey: %s (間隔=%ds, 動画制限=%ds)", "ON" if not args.no_misskey else "OFF", args.misskey_interval, args.misskey_max_duration)

    hikabooru = HikabooruClient()

    # マルコフ連鎖モデル読み込み
    markov = None
    if not args.no_markov and not args.test:
        try:
            markov = MarkovGenerator(args.markov_model)
            log.info("マルコフ連鎖: 有効")
        except Exception as e:
            log.warning("マルコフモデル読み込み失敗（無効で続行）: %s", e)

    tasks = []

    if not args.once:
        if not args.no_x:
            if not args.test:
                xposter = await XPoster(args.cookie).setup()
            else:
                xposter = None
            tasks.append(platform_loop(
                "X", hikabooru, xposter,
                args.x_interval, MAX_VIDEO_DURATION, X_OK_EXTS,
                args.test, markov=markov,
            ))

        if not args.no_misskey:
            misskey_poster = MisskeyPoster(token=args.misskey_token)
            tasks.append(platform_loop(
                "Misskey", hikabooru, misskey_poster,
                args.misskey_interval, args.misskey_max_duration, MISSKEY_OK_EXTS,
                args.test, markov=markov,
            ))

    if args.once:
        await run_once_all(hikabooru, args)
    else:
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            log.info("割り込みにより終了")

    await hikabooru.close()


async def run_once_all(hikabooru: HikabooruClient, args):
    """--once モード: 1回選出して全プラットフォームに投稿"""
    log.info("━" * 50)
    log.info("[once] ランダム選出開始 (test_mode=%s)", args.test)

    post = None
    while True:
        post = await hikabooru.random_post()
        ptype = HikabooruClient.post_type(post)
        if ptype == "flash":
            continue
        if ptype == "video":
            url = hikabooru.content_url(post)
            duration = get_video_duration(url)
            if duration < 0 or duration > MAX_VIDEO_DURATION:
                continue
        break

    summary = HikabooruClient.post_summary(post)
    content_url = hikabooru.content_url(post)
    log.info("✅ %s", summary)
    print(f"\n📤 選出: {summary}")
    print(f"   URL: {content_url}")

    if args.test:
        print("   (テストモードのため投稿スキップ)\n")
        return

    http = await hikabooru._client()
    tmp_path = await download_media(http, content_url)

    results = {}

    # X
    ptype = HikabooruClient.post_type(post)
    is_video = (ptype == "video")

    # マルコフ文
    markov_text = ""
    if not args.no_markov and not args.test:
        try:
            m = MarkovGenerator(args.markov_model)
            markov_text = m.generate(max_tokens=50)
            print(f"   💬 マルコフ: {markov_text[:100]}")
        except Exception as e:
            log.warning("マルコフ生成失敗: %s", e)

    if not args.no_x:
        x_conv = convert_for_platform(tmp_path, X_OK_EXTS)
        xposter = await XPoster(args.cookie).setup()
        try:
            tid = await xposter.post_media(x_conv, is_video=is_video, text=markov_text)
            log.info("🎉 X投稿成功! tweet_id=%s | post_id=%d", tid, post["id"])
            print(f"   ✅ X: tweet_id={tid}")
            results["X"] = tid
        except Exception as e:
            log.error("X投稿失敗: %s", e)
            print(f"   ❌ X: {e}")
        if x_conv != tmp_path:
            os.unlink(x_conv)

    # Misskey
    if not args.no_misskey:
        mk_conv = convert_for_platform(tmp_path, MISSKEY_OK_EXTS)
        misskey = MisskeyPoster(token=args.misskey_token)
        try:
            nid = await misskey.post_media(mk_conv, text=markov_text)
            log.info("🎉 Misskey投稿成功! note_id=%s | post_id=%d", nid, post["id"])
            print(f"   ✅ Misskey: note_id={nid}")
            results["Misskey"] = nid
        except Exception as e:
            log.error("Misskey投稿失敗: %s", e)
            print(f"   ❌ Misskey: {e}")
        if mk_conv != tmp_path:
            os.unlink(mk_conv)

    try:
        os.unlink(tmp_path)
    except OSError:
        pass

    print()
    return results


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="hikabooru → X/Misskey マルチ投稿bot")
    parser.add_argument("--test", action="store_true",
                        help="テストモード（ランダム選出のみ、投稿しない）")
    parser.add_argument("--once", action="store_true",
                        help="1回だけ実行して終了")
    parser.add_argument("--cookie", type=str, default=_default_cookie_path(),
                        help="ブラウザエクスポートCookieのJSONファイルパス")
    # X 用
    parser.add_argument("--x-interval", type=int, default=X_DEFAULT_INTERVAL,
                        help=f"X投稿間隔（秒）（デフォルト: {X_DEFAULT_INTERVAL}秒=30分）")
    parser.add_argument("--no-x", action="store_true",
                        help="Xを無効にする")
    # Misskey 用
    parser.add_argument("--misskey-interval", type=int, default=MISSKEY_DEFAULT_INTERVAL,
                        help=f"Misskey投稿間隔（秒）（デフォルト: {MISSKEY_DEFAULT_INTERVAL}秒=5分）")
    parser.add_argument("--misskey-token", type=str, required=False, default="",
                        help="Misskey APIトークン")
    parser.add_argument("--misskey-max-duration", type=int, default=600,
                        help="Misskeyの動画最大秒数（デフォルト: 600秒=10分）")
    parser.add_argument("--no-misskey", action="store_true",
                        help="Misskeyを無効にする")
    # マルコフ連鎖
    parser.add_argument("--markov-model", type=str, default=_default_markov_path(),
                        help="マルコフ連鎖モデルの.pklファイルパス")
    parser.add_argument("--no-markov", action="store_true",
                        help="マルコフ連鎖テキスト生成を無効にする")
    args = parser.parse_args()

    if args.no_x and args.no_misskey:
        print("❌ --no-x と --no-misskey の両方は指定できません")
        sys.exit(1)

    if not args.no_x and not os.path.exists(args.cookie):
        print(f"❌ Cookieファイルが見つかりません: {args.cookie}")
        sys.exit(1)

    asyncio.run(main_loop(args))


if __name__ == "__main__":
    main()
