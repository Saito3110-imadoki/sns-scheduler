"""
SNS投稿自動化スクリプト
Notionデータベースから「未投稿」レコードを取得し、
指定日時にX（Twitter）・Threadsへ自動投稿する（画像付き対応）
"""

import os
import sys
import io
import time
import yaml
import requests
import tweepy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from notion_client import Client
from dotenv import load_dotenv

load_dotenv()

# 共通通知モジュール
_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from notify import notify_error, notify_post_complete

JST = timezone(timedelta(hours=9))

# ── config.yaml 読み込み ──────────────────────────────────
_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_CFG: dict = {}
if _CONFIG_PATH.exists():
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as _f:
            _CFG = yaml.safe_load(_f) or {}
    except Exception as _ce:
        print(f"[config] 読み込みエラー（デフォルト値を使用）: {_ce}")


def _cfg(*keys, default=None):
    node = _CFG
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k)
        if node is None:
            return default
    return node


def _env(name: str, prefix: str = "", required: bool = True) -> str:
    """env_prefix付きで環境変数を取得。prefix='CLIENT_A' → 'CLIENT_A_X_API_KEY'"""
    key = f"{prefix}_{name}" if prefix else name
    val = os.environ.get(key, "")
    if required and not val:
        raise EnvironmentError(f"環境変数 {key} が設定されていません")
    return val


# ── 設定値 ────────────────────────────────────────────────
NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

PROP_TEXT         = _cfg("notion", "properties", "text",         default="投稿文")
PROP_THREADS_TEXT = _cfg("notion", "properties", "threads_text", default="Threads用文面")
PROP_REPLY_TEXT   = _cfg("notion", "properties", "reply_text",   default="リプライ文面")
PROP_DATETIME     = _cfg("notion", "properties", "datetime",     default="投稿日時")
PROP_PLATFORM     = _cfg("notion", "properties", "platform",     default="媒体")
PROP_STATUS       = _cfg("notion", "properties", "status",       default="ステータス")
PROP_IMAGE_URL    = _cfg("notion", "properties", "image_url",    default="画像URL")
PROP_TWEET_ID     = _cfg("notion", "properties", "tweet_id",     default="X投稿ID")
PROP_THREADS_ID   = _cfg("notion", "properties", "threads_post_id", default="Threads投稿ID")

STATUS_PENDING = "未投稿"
STATUS_DONE    = "投稿済"
STATUS_ERROR   = "エラー"

PLATFORM_X       = "X"
PLATFORM_THREADS = "Threads"
PLATFORM_BOTH    = _cfg("notion", "platform", "default", default="両方")


# ── Notion ヘルパー ───────────────────────────────────────
def get_notion_client() -> Client:
    return Client(auth=NOTION_TOKEN)


def fetch_pending_posts(notion: Client) -> list[dict]:
    """ステータスが「未投稿」かつ投稿日時が現在以前のレコードを取得"""
    now_utc = datetime.now(timezone.utc).isoformat()
    response = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={
            "and": [
                {"property": PROP_STATUS,
                 "multi_select": {"contains": STATUS_PENDING}},
                {"property": PROP_DATETIME,
                 "date": {"on_or_before": now_utc}},
            ]
        },
        sorts=[{"property": PROP_DATETIME, "direction": "ascending"}],
    )
    return response.get("results", [])


def extract_text(page: dict) -> str:
    prop = page["properties"].get(PROP_TEXT, {})
    if prop.get("type") == "title":
        return "".join(p["plain_text"] for p in prop.get("title", []))
    if prop.get("type") == "rich_text":
        return "".join(p["plain_text"] for p in prop.get("rich_text", []))
    return ""


def extract_threads_text(page: dict) -> str:
    """Threads用文面を取得。未設定なら空文字列（呼び出し側でX文面にフォールバック）"""
    prop = page["properties"].get(PROP_THREADS_TEXT, {})
    if prop.get("type") == "rich_text":
        return "".join(p["plain_text"] for p in prop.get("rich_text", [])).strip()
    return ""


def extract_reply_text(page: dict) -> str:
    """セルフリプライ文面を取得。未設定なら空文字列（リプライなし）"""
    prop = page["properties"].get(PROP_REPLY_TEXT, {})
    if prop.get("type") == "rich_text":
        return "".join(p["plain_text"] for p in prop.get("rich_text", [])).strip()
    return ""


def extract_platform(page: dict) -> str:
    prop = page["properties"].get(PROP_PLATFORM, {})
    if prop.get("type") == "select":
        sel = prop.get("select")
        return sel["name"] if sel else ""
    if prop.get("type") == "multi_select":
        items = prop.get("multi_select", [])
        return items[0]["name"] if items else ""
    return ""


def extract_datetime(page: dict) -> datetime | None:
    prop = page["properties"].get(PROP_DATETIME, {})
    if prop.get("type") == "date":
        date_obj = prop.get("date")
        if date_obj and date_obj.get("start"):
            dt = datetime.fromisoformat(date_obj["start"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=JST)
            return dt
    return None


def extract_image_url(page: dict) -> str:
    prop = page["properties"].get(PROP_IMAGE_URL, {})
    if prop.get("type") == "url":
        return prop.get("url") or ""
    return ""


def update_status(notion: Client, page_id: str, status: str,
                  tweet_id: str = "", threads_id: str = ""):
    props: dict = {PROP_STATUS: {"multi_select": [{"name": status}]}}
    if tweet_id:
        props[PROP_TWEET_ID] = {"rich_text": [{"text": {"content": tweet_id}}]}
    if threads_id:
        props[PROP_THREADS_ID] = {"rich_text": [{"text": {"content": threads_id}}]}

    # 任意プロパティが未作成でもステータス更新は必ず通す
    optional = [PROP_TWEET_ID, PROP_THREADS_ID]
    for _ in range(len(optional) + 1):
        try:
            notion.pages.update(page_id=page_id, properties=props)
            return
        except Exception as e:
            removable = [p for p in optional if p in props and p in str(e)]
            if not removable:
                raise
            for p in removable:
                print(f"  ※ プロパティ「{p}」が未作成のためスキップ")
                del props[p]


# ── 文字数ガード ──────────────────────────────────────────
# X: 重み付きカウント（半角系=1 / 全角・CJK=2）で280が上限。
#    X Premium なら実質無制限のため、config の x_char_limit=0 でスキップ。
# Threads: 500文字のハード上限（全プランで固定）。
X_CHAR_LIMIT       = int(_cfg("content", "x_char_limit", default=0))
THREADS_CHAR_LIMIT = 500


def _x_weighted_len(text: str) -> int:
    """Xの重み付き文字数（近似）。半角英数記号=1、それ以外=2"""
    return sum(1 if ord(ch) <= 0x10FF else 2 for ch in text)


def _trim_for_x(text: str, limit: int = X_CHAR_LIMIT) -> str:
    """X向けに重み付き文字数で切り詰める。limit=0 は制限なし（Premium）"""
    if limit <= 0 or _x_weighted_len(text) <= limit:
        return text
    out, w = [], 0
    for ch in text:
        cw = 1 if ord(ch) <= 0x10FF else 2
        if w + cw > limit - 2:  # 末尾の「…」分を確保
            break
        out.append(ch)
        w += cw
    trimmed = "".join(out).rstrip() + "…"
    print(f"  ⚠ X文字数超過のため切り詰め（重み{_x_weighted_len(text)}→上限{limit}）")
    return trimmed


def _trim_for_threads(text: str, limit: int = THREADS_CHAR_LIMIT) -> str:
    """Threadsの500文字上限を保証する"""
    if len(text) <= limit:
        return text
    print(f"  ⚠ Threads文字数超過のため切り詰め（{len(text)}→{limit}文字）")
    return text[: limit - 1].rstrip() + "…"


# ── 画像ダウンロード ──────────────────────────────────────
def download_image(url: str) -> bytes | None:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"  画像ダウンロードエラー: {e}")
        return None


# ── X（Twitter）投稿 ──────────────────────────────────────
def post_to_x(text: str, image_url: str = "") -> str:
    """投稿してツイートIDを返す。失敗時は空文字列。"""
    consumer_key    = os.environ["X_API_KEY"]
    consumer_secret = os.environ["X_API_KEY_SECRET"]
    access_token    = os.environ["X_ACCESS_TOKEN"]
    access_secret   = os.environ["X_ACCESS_TOKEN_SECRET"]

    client = tweepy.Client(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )

    media_ids = None
    if image_url:
        image_data = download_image(image_url)
        if image_data:
            try:
                auth   = tweepy.OAuth1UserHandler(consumer_key, consumer_secret,
                                                  access_token, access_secret)
                api_v1 = tweepy.API(auth)
                media  = api_v1.media_upload(filename="post_image.png",
                                             file=io.BytesIO(image_data))
                media_ids = [media.media_id]
                print("  X: 画像アップロード完了")
            except Exception as e:
                print(f"  X: 画像アップロードエラー（テキストのみで投稿）: {e}")

    response = client.create_tweet(
        text=text,
        **({"media_ids": media_ids} if media_ids else {}),
    )
    if response.data:
        return str(response.data["id"])
    return ""


def post_x_reply(tweet_id: str, text: str) -> bool:
    """投稿済みツイートに自分でリプライをぶら下げる（スレッド化）"""
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_KEY_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    response = client.create_tweet(text=text, in_reply_to_tweet_id=tweet_id)
    return response.data is not None


# ── Telegraph アップロード ────────────────────────────────
def upload_to_telegraph(image_data: bytes) -> str:
    try:
        r = requests.post(
            "https://telegra.ph/upload",
            files={"file": ("image.png", image_data, "image/png")},
            timeout=30,
        )
        r.raise_for_status()
        path = r.json()[0]["src"]
        url  = f"https://telegra.ph{path}"
        print(f"  Telegraph: アップロード完了 → {url}")
        return url
    except Exception as e:
        print(f"  Telegraph: アップロードエラー: {e}")
        return ""


# ── Threads 投稿 ──────────────────────────────────────────
def post_to_threads(text: str, image_url: str = "") -> str:
    """投稿して公開後のメディアIDを返す。失敗時は空文字列。"""
    user_id = os.environ["THREADS_USER_ID"]
    token   = os.environ["THREADS_ACCESS_TOKEN"]
    base    = f"https://graph.threads.net/v1.0/{user_id}"

    if image_url:
        image_data = download_image(image_url)
        if image_data:
            telegraph_url = upload_to_telegraph(image_data)
            if telegraph_url:
                image_url = telegraph_url
            else:
                print("  Threads: 画像アップロード失敗のためテキストのみで投稿")
                image_url = ""

    if image_url:
        params = {
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": text,
            "access_token": token,
        }
    else:
        params = {
            "media_type": "TEXT",
            "text": text,
            "access_token": token,
        }

    r = requests.post(f"{base}/threads", params=params, timeout=30)
    r.raise_for_status()
    container_id = r.json()["id"]

    time.sleep(5)

    r = requests.post(
        f"{base}/threads_publish",
        params={"creation_id": container_id, "access_token": token},
        timeout=30,
    )
    r.raise_for_status()
    return str(r.json().get("id", ""))


# ── メイン処理 ────────────────────────────────────────────
def run():
    now = datetime.now(JST)
    print(f"[{now.strftime('%Y-%m-%d %H:%M')} JST] 自動投稿 実行開始")

    notion = get_notion_client()

    try:
        posts = fetch_pending_posts(notion)
    except Exception as e:
        print(f"Notion からの取得に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)

    if not posts:
        print("投稿対象なし。終了します。")
        return

    print(f"投稿対象: {len(posts)} 件")

    posted_count = 0
    error_count  = 0

    for page in posts:
        page_id      = page["id"]
        text         = _trim_for_x(extract_text(page))
        threads_text = _trim_for_threads(extract_threads_text(page)
                                         or extract_text(page))  # 未設定ならX文面を使用
        reply_text   = _trim_for_x(extract_reply_text(page))
        platform     = extract_platform(page)
        sched_dt     = extract_datetime(page)
        image_url    = extract_image_url(page)

        label = text[:30] + ("..." if len(text) > 30 else "")
        print(f"\n  投稿文  : {label}")
        print(f"  媒体    : {platform}")
        print(f"  予定日時: {sched_dt}")
        print(f"  画像URL : {image_url or '（なし）'}")
        if threads_text != text:
            print(f"  Threads文面: あり（{len(threads_text)}文字）")

        if not text:
            print("  → 投稿文が空のためスキップ")
            continue
        if platform not in (PLATFORM_X, PLATFORM_THREADS, PLATFORM_BOTH):
            print(f"  → 媒体の値が不正のためスキップ（値: {platform!r}）")
            continue

        tweet_id   = ""
        threads_id = ""

        try:
            if platform in (PLATFORM_X, PLATFORM_BOTH):
                tweet_id = post_to_x(text, image_url)
                print(f"  X       : {'✓ 投稿成功 ID=' + tweet_id if tweet_id else '✗ 投稿失敗'}")

                # セルフリプライ（失敗しても投稿自体は成功扱い）
                if tweet_id and reply_text:
                    try:
                        time.sleep(3)
                        if post_x_reply(tweet_id, reply_text):
                            print("  X リプライ: ✓ 投稿成功")
                        else:
                            print("  X リプライ: ✗ 投稿失敗（本文は投稿済み）")
                    except Exception as re_err:
                        print(f"  X リプライ: ✗ エラー（本文は投稿済み）: {re_err}")

            if platform in (PLATFORM_THREADS, PLATFORM_BOTH):
                threads_id = post_to_threads(threads_text, image_url)
                print(f"  Threads : {'✓ 投稿成功 ID=' + threads_id if threads_id else '✗ 投稿失敗'}")

            x_ok       = bool(tweet_id)   if platform in (PLATFORM_X, PLATFORM_BOTH) else True
            threads_ok = bool(threads_id) if platform in (PLATFORM_THREADS, PLATFORM_BOTH) else True
            if x_ok and threads_ok:
                update_status(notion, page_id, STATUS_DONE,
                              tweet_id=tweet_id, threads_id=threads_id)
                print("  ステータス → 投稿済")
                posted_count += 1
            else:
                update_status(notion, page_id, STATUS_ERROR)
                print("  ステータス → エラー")
                error_count += 1

        except Exception as e:
            detail = f"{text[:30]}... / {e}"
            print(f"  エラー発生: {e}", file=sys.stderr)
            notify_error("自動投稿（poster.py）", detail)
            try:
                update_status(notion, page_id, STATUS_ERROR)
            except Exception:
                pass
            error_count += 1

    print(f"\n完了 — 投稿済: {posted_count} 件 / エラー: {error_count} 件")
    notify_post_complete(posted_count, error_count)


if __name__ == "__main__":
    run()
