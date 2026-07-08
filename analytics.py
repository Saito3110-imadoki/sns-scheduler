"""
投稿パフォーマンス計測スクリプト
- Notionの「投稿済」レコードから X投稿ID を取得
- X API v2 でいいね数・RT数・リプライ数・インプレッションを一括取得
- Notionの各レコードに書き戻す
"""

import os
import sys
import yaml
import tweepy
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from notion_client import Client
from dotenv import load_dotenv

load_dotenv()

_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from notify import notify_error, notify_analytics_complete

JST = timezone(timedelta(hours=9))

# ── config.yaml 読み込み ──────────────────────────────────
_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_CFG: dict = {}
if _CONFIG_PATH.exists():
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as _f:
            _CFG = yaml.safe_load(_f) or {}
    except Exception as _ce:
        print(f"[config] 読み込みエラー: {_ce}")


def _cfg(*keys, default=None):
    node = _CFG
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k)
        if node is None:
            return default
    return node


NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

PROP_STATUS       = _cfg("notion", "properties", "status",       default="ステータス")
PROP_TWEET_ID     = _cfg("notion", "properties", "tweet_id",     default="X投稿ID")
PROP_LIKES        = _cfg("notion", "properties", "likes",        default="いいね数")
PROP_RETWEETS     = _cfg("notion", "properties", "retweets",     default="RT数")
PROP_REPLIES      = _cfg("notion", "properties", "replies",      default="リプライ数")
PROP_IMPRESSIONS  = _cfg("notion", "properties", "impressions",  default="インプレッション")
PROP_ANALYTICS_DT = _cfg("notion", "properties", "analytics_date", default="計測日時")

STATUS_DONE = "投稿済"

# ── Notion ────────────────────────────────────────────────
def get_notion_client() -> Client:
    return Client(auth=NOTION_TOKEN)


def fetch_posted_with_tweet_id(notion: Client) -> list[dict]:
    """「投稿済」かつ X投稿ID が入っているレコードを全件取得"""
    results = []
    cursor  = None
    while True:
        kwargs: dict = {
            "database_id": NOTION_DATABASE_ID,
            "filter": {
                "and": [
                    {"property": PROP_STATUS,
                     "multi_select": {"contains": STATUS_DONE}},
                    {"property": PROP_TWEET_ID,
                     "rich_text": {"is_not_empty": True}},
                ]
            },
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        resp   = notion.databases.query(**kwargs)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results


def extract_tweet_id(page: dict) -> str:
    prop = page["properties"].get(PROP_TWEET_ID, {})
    parts = prop.get("rich_text", [])
    return "".join(p["plain_text"] for p in parts).strip()


def update_metrics(notion: Client, page_id: str, metrics: dict):
    now_jst = datetime.now(JST).isoformat()
    props: dict = {
        PROP_ANALYTICS_DT: {"date": {"start": now_jst}},
    }
    if "like_count" in metrics:
        props[PROP_LIKES]       = {"number": metrics["like_count"]}
    if "retweet_count" in metrics:
        props[PROP_RETWEETS]    = {"number": metrics["retweet_count"]}
    if "reply_count" in metrics:
        props[PROP_REPLIES]     = {"number": metrics["reply_count"]}
    if "impression_count" in metrics:
        props[PROP_IMPRESSIONS] = {"number": metrics["impression_count"]}
    notion.pages.update(page_id=page_id, properties=props)


# ── X API ─────────────────────────────────────────────────
def fetch_tweet_metrics_batch(tweet_ids: list[str]) -> dict[str, dict]:
    """最大100件のツイートIDを一括でメトリクス取得。{tweet_id: metrics} を返す"""
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_KEY_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )

    result: dict[str, dict] = {}
    # X API は一度に最大100件
    for i in range(0, len(tweet_ids), 100):
        batch = tweet_ids[i:i + 100]
        try:
            resp = client.get_tweets(
                ids=batch,
                tweet_fields=["public_metrics"],
            )
            if resp.data:
                for tweet in resp.data:
                    if tweet.public_metrics:
                        result[str(tweet.id)] = dict(tweet.public_metrics)
        except Exception as e:
            print(f"  X API エラー（バッチ {i//100 + 1}）: {e}")

    return result


# ── メイン ────────────────────────────────────────────────
def run():
    now = datetime.now(JST)
    print(f"[{now.strftime('%Y-%m-%d %H:%M')} JST] パフォーマンス計測 開始")

    notion = get_notion_client()

    pages = fetch_posted_with_tweet_id(notion)
    if not pages:
        print("計測対象なし（投稿済みかつX投稿IDがあるレコードが0件）")
        return

    print(f"計測対象: {len(pages)} 件")

    tweet_ids = [extract_tweet_id(p) for p in pages]
    id_to_page = {tid: p for tid, p in zip(tweet_ids, pages) if tid}

    print("X APIでメトリクス取得中...")
    try:
        metrics_map = fetch_tweet_metrics_batch(list(id_to_page.keys()))
    except Exception as e:
        notify_error("パフォーマンス計測（analytics.py）", f"X API取得失敗: {e}")
        print(f"X API取得失敗: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  取得成功: {len(metrics_map)} 件")

    updated = 0
    for tweet_id, metrics in metrics_map.items():
        page    = id_to_page[tweet_id]
        page_id = page["id"]
        try:
            update_metrics(notion, page_id, metrics)
            likes   = metrics.get("like_count", "-")
            rts     = metrics.get("retweet_count", "-")
            imp     = metrics.get("impression_count", "-")
            print(f"  更新: ツイート {tweet_id} → いいね {likes} / RT {rts} / imp {imp}")
            updated += 1
        except Exception as e:
            print(f"  Notion更新エラー（{tweet_id}）: {e}")

    print(f"\n完了 — {updated} 件のメトリクスをNotionに保存しました")
    notify_analytics_complete(updated)


if __name__ == "__main__":
    run()
