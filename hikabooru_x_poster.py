#!/usr/bin/env python3
"""
hikabooru_x_poster.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
hikabooru から完全ランダムに画像/動画を選び X に投稿する bot。
- マルコフ連鎖テキスト付き投稿
- 投稿後に hikabooru ソースリンクをリプライ
- 自分のツイートへのリプライに自動返信

使い方:
  python hikabooru_x_poster.py                     # 本番
  python hikabooru_x_poster.py --once              # 1回だけ
  python hikabooru_x_poster.py --test              # テスト
  python hikabooru_x_poster.py --no-reply          # 自動返信なし
  python hikabooru_x_poster.py --no-markov         # マルコフなし

依存: pip install twifork httpx, ffmpeg/ffprobe
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set

import httpx
from twikit import Client

# twifork 2.3.5 バグ修正
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

HIKABOORU_BASE = "https://hikabooru.hikamers.app"
MAX_VIDEO_DURATION = 140
DEFAULT_INTERVAL = 1800  # 30分
REPLY_CHECK_INTERVAL = 90  # リプライチェック間隔（秒）
REPLY_BACKOFF = 300  # 最後の返信から最低この秒数は次の返信を打たない

def _default_markov_path() -> str:
    if os.path.isdir("/data"):
        return "/data/markov_model.pkl"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "markov_model.pkl")

def _default_cookie_path() -> str:
    if os.path.isdir("/data"):
        return "/data/cookie.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cookie.json")

def _default_reply_db_path() -> str:
    if os.path.isdir("/data"):
        return "/data/replied_ids.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "replied_ids.json")

def _default_tweet_db_path() -> str:
    if os.path.isdir("/data"):
        return "/data/tweet_ids.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tweet_ids.json")

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
    def __init__(self, model_path: str):
        with open(model_path, "rb") as f:
            model = pickle.load(f)
        self.n = model["n_gram"]
        self.transitions = model["transitions"]
        log.info("Markov: n_gram=%d, contexts=%d, sentences=%d",
                 self.n, model.get("contexts", 0), model.get("total_sentences", 0))

    def generate(self, min_len: int = 1, max_tokens: int = 200) -> str:
        for _ in range(100):
            ctx = tuple([BOS] * (self.n - 1))
            tokens = []
            for _ in range(max_tokens):
                if ctx not in self.transitions:
                    break
                candidates = self.transitions[ctx]
                token_names = [t for t, _ in candidates]
                weights = [c for _, c in candidates]
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
        return ""


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
            if c.get("name") in ("auth_token", "ct0"):
                cookies[c["name"]] = c["value"]
        if "auth_token" not in cookies:
            raise ValueError("auth_token 不在")
        return cookies
    raise ValueError(f"不明なCookie形式: {type(data)}")


# ═══════════════════════════════════════════════════════════════
# hikabooru API クライアント
# ═══════════════════════════════════════════════════════════════

class HikabooruClient:
    def __init__(self, base_url: str = HIKABOORU_BASE):
        self.base = base_url
        self.api = f"{base_url}/api"
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30.0)
        return self._http

    async def close(self):
        if self._http:
            await self._http.aclose()
            self._http = None

    async def random_post(self) -> dict:
        http = await self._client()
        resp = await http.get(f"{self.api}/posts", params={"query": "sort:random", "limit": 1})
        resp.raise_for_status()
        total = resp.json().get("total")
        if not total:
            raise RuntimeError("総投稿数取得失敗")
        offset = random.randint(0, total - 1)
        resp = await http.get(f"{self.api}/posts", params={"query": "sort:random", "limit": 1, "offset": offset})
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            raise RuntimeError("投稿取得失敗")
        return results[0]

    def content_url(self, post: dict) -> str:
        url = post.get('contentUrl') or post.get('content_url') or ''
        if not url:
            return ''
        return f"{self.base}/{url}"

    def view_url(self, post: dict) -> str:
        return f"{self.base}/post/{post['id']}"

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
# 動画時間
# ═══════════════════════════════════════════════════════════════

def get_video_duration(url: str) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", url],
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
    def __init__(self, cookie_path: str, tweet_db_path: str = ""):
        self.cookie_path = cookie_path
        self.tweet_db_path = tweet_db_path
        self.client: Optional[Client] = None
        self._my_user_id: Optional[str] = None
        self.my_tweet_ids: Set[str] = self._load_tweet_ids()

    def _load_tweet_ids(self) -> Set[str]:
        if not self.tweet_db_path:
            return set()
        try:
            with open(self.tweet_db_path) as f:
                return set(orjson.loads(f.read()))
        except Exception:
            return set()

    def _save_tweet_ids(self):
        if not self.tweet_db_path:
            return
        try:
            os.makedirs(os.path.dirname(self.tweet_db_path), exist_ok=True)
            with open(self.tweet_db_path, "wb") as f:
                f.write(orjson.dumps(list(self.my_tweet_ids)[-5000:]))
        except Exception:
            pass

    async def setup(self):
        cookies = convert_cookies(self.cookie_path)
        self.client = Client(language="ja")
        self.client.set_cookies(cookies)
        try:
            self._my_user_id = str(await self.client.user_id())
            log.info("X認証OK (user_id=%s)", self._my_user_id)
        except Exception as e:
            log.warning("X認証警告: %s", e)
        # 再起動後も過去の投稿へのリプに反応できるよう直近ツイートをロード
        await self.load_recent_tweets()
        return self

    async def load_recent_tweets(self, limit: int = 100):
        """自分の直近のオリジナル投稿を取得して my_tweet_ids に追加"""
        if self.client is None:
            return
        my_id = self.user_id
        if not my_id:
            return
        try:
            tweets = await self.client.get_user_tweets(my_id, "Tweets", count=limit)
            added = 0
            for t in tweets:
                tid = str(t.id)
                if tid not in self.my_tweet_ids:
                    self.my_tweet_ids.add(tid)
                    added += 1
            self._save_tweet_ids()
            log.info("直近ツイート %d件ロード (my_tweet_ids=%d)", added, len(self.my_tweet_ids))
        except Exception as e:
            log.warning("直近ツイートロード失敗: %s", e)

    @property
    def user_id(self) -> str:
        return self._my_user_id or ""

    def is_my_original(self, tweet_id: str) -> bool:
        """そのIDが自分のオリジナル投稿か（リプライではない）"""
        return tweet_id in self.my_tweet_ids

    async def upload_and_tweet(self, media_path: str, is_video: bool = False, text: str = "",
                                reply_to: str = "") -> str:
        """メディアをアップロードしてツイート。reply_to 指定でリプライになる"""
        if self.client is None:
            raise RuntimeError("setup() を先に呼んでください")
        kwargs = {}
        if is_video:
            media_id = await self.client.upload_media(
                media_path, wait_for_completion=True, media_category="tweet_video")
        else:
            media_id = await self.client.upload_media(media_path)
        kwargs["media_ids"] = [media_id]
        if reply_to:
            kwargs["reply_to"] = reply_to
        tweet = await self.client.create_tweet(text=text, **kwargs)
        return tweet.id if hasattr(tweet, 'id') else str(tweet)

    async def favorite_tweet(self, tweet_id: str) -> bool:
        """ツイートにいいねする"""
        if self.client is None:
            return False
        try:
            await self.client.favorite_tweet(tweet_id)
            log.info("❤️ いいね: %s", tweet_id)
            return True
        except Exception as e:
            log.warning("いいね失敗 %s: %s", tweet_id, e)
            return False

    async def reply_text(self, reply_to_id: str, text: str) -> str:
        """テキストのみのリプライ"""
        if self.client is None:
            raise RuntimeError("setup() を先に呼んでください")
        tweet = await self.client.create_tweet(text=text, reply_to=reply_to_id)
        return tweet.id if hasattr(tweet, 'id') else str(tweet)

    async def search_mentions(self, query: str, count: int = 20):
        """検索してツイートリストを返す"""
        if self.client is None:
            return []
        try:
            return await self.client.search_tweet(query, "Latest", count=count)
        except Exception as e:
            log.warning("search_tweet失敗: %s", e)
            return []

    async def get_mentions_from_notifications(self, count: int = 40):
        """Mentions通知から自分宛のツイートを取得する。

        XのMentions通知レスポンスは globalObjects.notifications を持たず、
        globalObjects.tweets にメンション/返信ツイートが直接入っている。
        twiforkの get_notifications() は古い形式を期待するため0件になる
        ので、rawレスポンスから直接ツイートを抽出する。"""
        if self.client is None:
            return []
        try:
            response, _ = await self.client.v11.notifications_mentions(count, None)
            tweets_map = response.get('globalObjects', {}).get('tweets', {})
            bot_uid = self.user_id

            tweets = []
            for tid, tdata in tweets_map.items():
                try:
                    # 自分自身のツイートは除外（通知に混ざる元ツイート等）
                    if bot_uid and str(tdata.get('user_id_str', '')) == bot_uid:
                        continue
                    # Tweetオブジェクト化（ReplyHandlerの判定ロジックをそのまま使う）
                    t = self._tweet_from_data(tid, tdata)
                    if t is not None:
                        tweets.append(t)
                except Exception:
                    continue
            log.info("🔔 通知取得(Mentions): %d件のツイート", len(tweets))
            return tweets
        except Exception as e:
            log.warning("get_notifications失敗: %s", e)
            return []

    def _tweet_from_data(self, tid: str, tdata: dict):
        """globalObjects.tweets のデータから Tweet オブジェクトを生成"""
        try:
            from twikit.user import User
            from twikit.utils import build_user_data, build_tweet_data
            from twikit.tweet import Tweet

            uid = str(tdata.get('user_id_str', ''))
            if not uid:
                return None
            # ユーザーデータは無いので最小のUserを作る
            ud = build_user_data({
                'id': uid,
                'rest_id': uid,
                'screen_name': tdata.get('user_id_str', ''),
            })
            if isinstance(ud.get('location'), str):
                ud['location'] = {'location': ud['location']}
            user = User(self.client, ud)
            return Tweet(self.client, build_tweet_data(tdata), user)
        except Exception:
            return None

    async def close(self):
        pass


# ═══════════════════════════════════════════════════════════════
# 自動返信ハンドラ
# ═══════════════════════════════════════════════════════════════

class ReplyHandler:
    """自分のツイートへのリプライを監視して自動返信"""

    def __init__(self, xposter: XPoster, hikabooru: HikabooruClient,
                 markov: Optional[MarkovGenerator], db_path: str):
        self.xposter = xposter
        self.hikabooru = hikabooru
        self.markov = markov
        self.db_path = db_path
        self.processed: Set[str] = self._load()
        self.last_reply_time = 0.0

    def _load(self) -> Set[str]:
        try:
            with open(self.db_path) as f:
                return set(orjson.loads(f.read()))
        except Exception:
            return set()

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            with open(self.db_path, "wb") as f:
                f.write(orjson.dumps(list(self.processed)[-10000:]))
        except Exception as e:
            log.warning("replied_ids保存失敗: %s", e)

    def _mark_processed(self, tweet_id: str):
        self.processed.add(tweet_id)
        self._save()

    async def check_and_reply(self):
        """新規リプライをチェックして1件だけ返信"""
        now = datetime.now(timezone.utc).timestamp()
        if now - self.last_reply_time < REPLY_BACKOFF:
            return  # 前回の返信から最低300秒

        bot_uid = self.xposter.user_id
        if not bot_uid:
            return

        try:
            tweets = await self.xposter.get_mentions_from_notifications(count=40)
        except Exception:
            return

        if not tweets:
            return

        found_count = 0
        for tweet in tweets:
            tid = str(tweet.id) if hasattr(tweet, 'id') else ""
            if not tid or tid in self.processed:
                continue
            found_count += 1

            # 自分のツイート（ソースリプライ等）→ 絶対スキップ
            tweet_uid = ""
            try:
                tweet_uid = str(tweet.user.id)
            except Exception:
                pass
            if tweet_uid == bot_uid:
                log.info("🔕 自己ツイートをスキップ: %s", tid)
                self._mark_processed(tid)
                continue

            # 自分のツイートへの直接リプライ or 引用リツイートが対象
            # twiforkの検索結果では in_reply_to にリプライ先ツイートIDが入る
            is_quote = False
            quote_target = ""
            try:
                is_quote = bool(getattr(tweet, "is_quote_status", False))
            except Exception:
                is_quote = False
            try:
                if is_quote:
                    quote_target = str(tweet.quoted_status_id() or 0)
            except Exception:
                pass

            if is_quote:
                # 引用リツイート: 引用元が自分のオリジナル投稿なら対象
                if not (quote_target and self.xposter.is_my_original(quote_target)):
                    log.info("🔕 引用リツイート(他人宛): %s → %s", tid, quote_target)
                    self._mark_processed(tid)
                    continue
                log.info("🔔 引用リツイート検出: %s (引用元 %s)", tid, quote_target)
            else:
                # 通常のリプライ
                reply_to_tid = str(getattr(tweet, "in_reply_to", 0) or 0)
                if reply_to_tid == '0':
                    log.info("🔕 reply先なし (直接メンション): %s", tid)
                    self._mark_processed(tid)
                    continue

                # 自分の「オリジナル投稿」へのリプライのみ（リプライのリプライは除外）
                if not self.xposter.is_my_original(reply_to_tid):
                    log.info("🔕 リプライのリプライ/他人宛をスキップ: %s → %s", tid, reply_to_tid)
                    self._mark_processed(tid)
                    continue
                log.info("🔔 リプライ検出: %s → tweet %s", tid, reply_to_tid)

            # 返信前に元ツイートにいいね
            try:
                await self.xposter.favorite_tweet(tid)
            except Exception as e:
                log.warning("いいね失敗（続行）: %s", e)

            # 返信する
            try:
                await self._do_reply(tid)
                self.last_reply_time = now
            except Exception as e:
                log.error("自動返信失敗: %s", e)
            finally:
                self._mark_processed(tid)
            return  # 1件処理したら終了（連投防止）

        if found_count > 0:
            log.info("[返信] チェック: 新規%d件 → 対象なし", found_count)

    async def _do_reply(self, reply_to_tweet_id: str):
        """ランダム画像 + マルコフ文 でリプライ"""
        # ランダムメディア選出
        while True:
            post = await self.hikabooru.random_post()
            ptype = HikabooruClient.post_type(post)
            if ptype == "flash":
                continue
            if ptype == "video":
                duration = get_video_duration(self.hikabooru.content_url(post))
                if duration < 0 or duration > MAX_VIDEO_DURATION:
                    continue
            break

        is_video = (ptype == "video")
        markov_text = self.markov.generate(max_tokens=50) if self.markov else ""
        source_url = self.hikabooru.view_url(post)

        text = f"{markov_text}\n\n{source_url}" if markov_text else source_url

        http = await self.hikabooru._client()
        tmp_path = await download_media(http, self.hikabooru.content_url(post))
        converted = convert_for_platform(tmp_path, X_OK_EXTS)
        if converted != tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        try:
            tid = await self.xposter.upload_and_tweet(
                converted, is_video=is_video, text=text, reply_to=reply_to_tweet_id)
            log.info("🤖 自動返信成功! tweet_id=%s", tid)
        finally:
            try:
                os.unlink(converted)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════
# メディア変換
# ═══════════════════════════════════════════════════════════════

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
    ext = os.path.splitext(input_path)[1].lower()
    if ext in ok_exts:
        return input_path
    if ext not in CONVERSION_MAP:
        log.warning("未知の拡張子 %s、変換なし", ext)
        return input_path
    out_ext, ffmpeg_args = CONVERSION_MAP[ext]
    fd, out_path = tempfile.mkstemp(suffix=out_ext, prefix="hikabooru_conv_")
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-i", input_path, *ffmpeg_args, out_path]
    log.info("変換: %s → %s", ext, out_ext)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and os.path.getsize(out_path) > 0:
            return out_path
        log.warning("変換失敗: %s", result.stderr[-200:] if result.stderr else "?")
        os.unlink(out_path)
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
    resp = await http.get(url)
    resp.raise_for_status()
    url_ext = os.path.splitext(url.split("?")[0])[1].lower() or ".bin"
    fd, tmp_path = tempfile.mkstemp(suffix=url_ext, prefix="hikabooru_")
    os.close(fd)
    with open(tmp_path, "wb") as f:
        f.write(resp.content)
    log.info("ダウンロード: %.1fMB → %s", len(resp.content) / (1024 * 1024), tmp_path)
    return tmp_path


async def x_post_loop(
    xposter: XPoster,
    hikabooru: HikabooruClient,
    interval: int,
    test_mode: bool,
    markov: Optional[MarkovGenerator],
):
    """Xへの定期投稿ループ"""
    log.info("[X] 開始 (間隔=%d秒)", interval)

    while True:
        try:
            # ランダム選出
            while True:
                post = await hikabooru.random_post()
                ptype = HikabooruClient.post_type(post)
                if ptype == "flash":
                    continue
                if ptype == "video":
                    duration = get_video_duration(hikabooru.content_url(post))
                    if duration < 0 or duration > MAX_VIDEO_DURATION:
                        continue
                break

            pid = post["id"]
            is_video = (ptype == "video")
            summary = HikabooruClient.post_summary(post)
            source_url = hikabooru.view_url(post)
            log.info("[X] ✅ %s", summary)

            if test_mode:
                print(f"\n🧪 TEST 選出: {summary}")
                print(f"   ソース: {source_url}\n")
            else:
                content_url = hikabooru.content_url(post)
                if not content_url:
                    log.warning("[X] contentUrlなし → 再抽選 (hikabooru #%d)", pid)
                    continue
                http = await hikabooru._client()
                tmp_path = await download_media(http, content_url)
                converted = convert_for_platform(tmp_path, X_OK_EXTS)
                if converted != tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

                try:
                    markov_text = markov.generate(max_tokens=50) if markov else ""
                    tweet_id = await xposter.upload_and_tweet(
                        converted, is_video=is_video, text=markov_text)

                    if markov_text:
                        log.info("[X] マルコフ文: %s", markov_text[:80])
                    log.info("[X] 🎉 投稿成功! tweet_id=%s | hikabooru_id=%d", tweet_id, pid)

                    # オリジナル投稿として登録（自動返信の対象判定に使う）
                    xposter.my_tweet_ids.add(tweet_id)
                    xposter._save_tweet_ids()

                    # ソースリンクをリプライで貼る
                    try:
                        reply_id = await xposter.reply_text(tweet_id, f"🔗 {source_url}")
                        log.info("[X] ソースリプライ: %s", reply_id)
                    except Exception as e:
                        log.warning("[X] ソースリプライ失敗: %s", e)

                    print(f"   ✅ tweet_id={tweet_id}\n")
                except Exception as e:
                    log.error("[X] 投稿失敗 (hikabooru #%d): %s", pid, e)
                finally:
                    try:
                        os.unlink(converted)
                    except OSError:
                        pass

        except Exception as e:
            log.error("[X] 実行エラー: %s", e)

        next_run = datetime.now().timestamp() + interval
        next_str = datetime.fromtimestamp(next_run).strftime("%H:%M:%S")
        log.info("[X] 次回実行: %s (%d秒後)", next_str, interval)
        await asyncio.sleep(interval)


async def reply_loop(
    handler: ReplyHandler,
    interval: int,
):
    """リプライ監視ループ"""
    log.info("[返信] 監視開始 (間隔=%d秒)", interval)
    while True:
        try:
            await handler.check_and_reply()
        except Exception as e:
            log.error("[返信] エラー: %s", e)
        await asyncio.sleep(interval)


async def main_loop(args):
    log.info("hikabooru_x_poster 起動")
    log.info("  hikabooru: %s", HIKABOORU_BASE)
    log.info("  間隔: %d秒 (%.1f分)", args.interval, args.interval / 60)
    log.info("  テスト: %s", args.test)
    log.info("  マルコフ: %s", "OFF" if args.no_markov else "ON")
    log.info("  自動返信: %s", "OFF" if args.no_reply else "ON")

    hikabooru = HikabooruClient()

    # マルコフ連鎖
    markov = None
    if not args.no_markov and not args.test:
        try:
            markov = MarkovGenerator(args.markov_model)
        except Exception as e:
            log.warning("マルコフモデル読み込み失敗: %s", e)

    tasks = []

    if args.once:
        await run_once(hikabooru, args)
        await hikabooru.close()
        return

    # X投稿ループ
    if not args.no_x:
        if not args.test:
            xposter = await XPoster(args.cookie, args.tweet_db).setup()
        else:
            xposter = None
        tasks.append(x_post_loop(xposter, hikabooru, args.interval, args.test, markov))

        # 自動返信ループ
        if not args.test and not args.no_reply and xposter:
            handler = ReplyHandler(xposter, hikabooru, markov, args.reply_db)
            tasks.append(reply_loop(handler, REPLY_CHECK_INTERVAL))

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        log.info("割り込みにより終了")

    await hikabooru.close()


async def run_once(hikabooru: HikabooruClient, args):
    """--once モード"""
    log.info("━" * 50)
    log.info("[once] ランダム選出 (test_mode=%s)", args.test)

    post = None
    while True:
        post = await hikabooru.random_post()
        ptype = HikabooruClient.post_type(post)
        if ptype == "flash":
            continue
        if ptype == "video":
            if get_video_duration(hikabooru.content_url(post)) > MAX_VIDEO_DURATION:
                continue
        break

    summary = HikabooruClient.post_summary(post)
    source_url = hikabooru.view_url(post)
    content_url = hikabooru.content_url(post)
    is_video = (ptype == "video")
    log.info("✅ %s", summary)
    print(f"\n📤 選出: {summary}")
    print(f"   URL: {content_url}")
    print(f"   ソース: {source_url}")

    if args.test:
        print("   (テストモードのため投稿スキップ)\n")
        return

    http = await hikabooru._client()
    tmp_path = await download_media(http, content_url)
    converted = convert_for_platform(tmp_path, X_OK_EXTS)
    if converted != tmp_path:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    markov_text = ""
    if not args.no_markov:
        try:
            m = MarkovGenerator(args.markov_model)
            markov_text = m.generate(max_tokens=50)
            print(f"   💬 マルコフ: {markov_text[:100]}")
        except Exception as e:
            log.warning("マルコフ生成失敗: %s", e)

    if not args.no_x:
        xposter = await XPoster(args.cookie).setup()
        try:
            tid = await xposter.upload_and_tweet(converted, is_video=is_video, text=markov_text)
            log.info("🎉 投稿成功! tweet_id=%s | post_id=%d", tid, post["id"])
            print(f"   ✅ X: tweet_id={tid}")

            # ソースリプライ
            try:
                rid = await xposter.reply_text(tid, f"🔗 {source_url}")
                log.info("ソースリプライ: %s", rid)
                print(f"   🔗 ソースリプライ: {rid}")
            except Exception as e:
                log.warning("ソースリプライ失敗: %s", e)
        except Exception as e:
            log.error("投稿失敗: %s", e)
            print(f"   ❌ X: {e}")

    try:
        os.unlink(converted)
    except OSError:
        pass
    print()


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="hikabooru → X 投稿bot")
    parser.add_argument("--test", action="store_true", help="テストモード")
    parser.add_argument("--once", action="store_true", help="1回だけ実行")
    parser.add_argument("--cookie", type=str, default=_default_cookie_path(), help="Cookie JSON")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"投稿間隔（秒）（デフォルト: {DEFAULT_INTERVAL}秒=30分）")
    parser.add_argument("--no-x", action="store_true", help="Xを無効")
    parser.add_argument("--markov-model", type=str, default=_default_markov_path(),
                        help="マルコフモデル.pkl")
    parser.add_argument("--no-markov", action="store_true", help="マルコフ無効")
    parser.add_argument("--no-reply", action="store_true", help="自動返信無効")
    parser.add_argument("--reply-db", type=str, default=_default_reply_db_path(),
                        help="返信済みIDの保存先")
    parser.add_argument("--tweet-db", type=str, default=_default_tweet_db_path(),
                        help="自分のツイートIDの保存先")
    args = parser.parse_args()

    if not args.no_x and not os.path.exists(args.cookie):
        print(f"❌ Cookieファイル不在: {args.cookie}")
        sys.exit(1)

    asyncio.run(main_loop(args))


if __name__ == "__main__":
    main()
