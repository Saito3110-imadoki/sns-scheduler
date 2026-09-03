"""
投稿パフォーマンス計測スクリプト
- Notionの「投稿済」レコードから X投稿ID / Threads投稿ID を取得
- X API v2 でいいね数・RT数・リプライ数・インプレッションを一括取得
- Threads Insights API で閲覧数・いいね数を取得
- フォロワー数（X / Threads）を記録し、前日比・前週比をLINE通知
"""

import os
import sys
import json
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


NOTION_TOKEN       = os.environ["NOTION_TOKEN"].strip()
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"].strip()

PROP_STATUS        = _cfg("notion", "properties", "status",        default="ステータス")
PROP_TWEET_ID      = _cfg("notion", "properties", "tweet_id",      default="X投稿ID")
PROP_THREADS_ID    = _cfg("notion", "properties", "threads_post_id", default="Threads投稿ID")
PROP_LIKES         = _cfg("notion", "properties", "likes",         default="いいね数")
PROP_RETWEETS      = _cfg("notion", "properties", "retweets",      default="RT数")
PROP_IMPRESSIONS   = _cfg("notion", "properties", "impressions",   default="インプレッション")
PROP_THREADS_LIKES = _cfg("notion", "properties", "threads_likes", default="Threadsいいね数")
PROP_THREADS_VIEWS = _cfg("notion", "properties", "threads_views", default="Threads閲覧数")

STATUS_DONE = "投稿済"

# フォロワー数の履歴ファイル（リポジトリにコミットして蓄積）
_HISTORY_PATH = _SCRIPT_DIR / "follower_history.json"


# ── Notion ────────────────────────────────────────────────
def get_notion_client() -> Client:
    return Client(auth=NOTION_TOKEN)


def fetch_posted_pages(notion: Client) -> list[dict]:
    """「投稿済」レコードを全件取得（X/ThreadsのIDはコード側で判定）"""
    results = []
    cursor  = None
    while True:
        kwargs: dict = {
            "database_id": NOTION_DATABASE_ID,
            "filter": {"property": PROP_STATUS,
                       "multi_select": {"contains": STATUS_DONE}},
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results


def _extract_rich_text(page: dict, prop_name: str) -> str:
    prop = page["properties"].get(prop_name, {})
    parts = prop.get("rich_text", [])
    return "".join(p["plain_text"] for p in parts).strip()


def update_page_props(notion: Client, page_id: str, props: dict,
                      optional: list[str]) -> bool:
    """プロパティ更新。Notion側に未作成の任意プロパティは外して再試行する"""
    for _ in range(len(optional) + 1):
        try:
            notion.pages.update(page_id=page_id, properties=props)
            return True
        except Exception as e:
            removable = [p for p in optional if p in props and p in str(e)]
            if not removable:
                raise
            for p in removable:
                print(f"  ※ プロパティ「{p}」が未作成のためスキップ")
                del props[p]
    return False


# ── X API ─────────────────────────────────────────────────
def _x_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_KEY_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )


def fetch_tweet_metrics_batch(tweet_ids: list[str]) -> dict[str, dict]:
    """最大100件のツイートIDを一括でメトリクス取得。{tweet_id: metrics} を返す"""
    client = _x_client()
    result: dict[str, dict] = {}
    for i in range(0, len(tweet_ids), 100):
        batch = tweet_ids[i:i + 100]
        try:
            # user_auth=True が必須。省略するとtweepyはBearer Token（未設定）で
            # リクエストし、401 Unauthorized になる
            resp = client.get_tweets(ids=batch, tweet_fields=["public_metrics"],
                                     user_auth=True)
            if resp.data:
                for tweet in resp.data:
                    if tweet.public_metrics:
                        result[str(tweet.id)] = dict(tweet.public_metrics)
        except Exception as e:
            print(f"  X API エラー（バッチ {i//100 + 1}）: {e}")
            msg = str(e)
            if "401" in msg or "Unauthorized" in msg:
                print("  ※ 401: X認証キー4点（Secrets）を確認してください。"
                      "コード側では get_tweets に user_auth=True が付いているかも確認")
            elif "402" in msg:
                print("  ※ 402はAPIクレジット切れです。Developer Consoleで残高を確認してください")
    return result


def fetch_x_followers() -> int | None:
    """自アカウントのフォロワー数を取得"""
    try:
        me = _x_client().get_me(user_fields=["public_metrics"])
        if me.data and me.data.public_metrics:
            return me.data.public_metrics.get("followers_count")
    except Exception as e:
        print(f"  Xフォロワー数取得スキップ: {e}")
    return None


# ── Threads API ───────────────────────────────────────────
def fetch_threads_media_insights(media_id: str) -> dict:
    """Threads投稿の閲覧数・いいね数を取得。失敗時は空dict"""
    token = os.environ.get("THREADS_ACCESS_TOKEN", "")
    if not token:
        return {}
    try:
        r = requests.get(
            f"https://graph.threads.net/v1.0/{media_id}/insights",
            params={"metric": "views,likes", "access_token": token},
            timeout=30,
        )
        r.raise_for_status()
        out = {}
        for m in r.json().get("data", []):
            name = m.get("name", "")
            val  = None
            tv   = m.get("total_value") or {}
            if "value" in tv:
                val = tv["value"]
            else:
                vals = m.get("values") or []
                if vals:
                    val = vals[-1].get("value")
            if name and val is not None:
                out[name] = val
        return out
    except Exception as e:
        print(f"  Threads Insights取得スキップ（{media_id}）: {e}")
        return {}


def fetch_threads_followers() -> int | None:
    """Threadsのフォロワー数を取得"""
    user_id = os.environ.get("THREADS_USER_ID", "")
    token   = os.environ.get("THREADS_ACCESS_TOKEN", "")
    if not user_id or not token:
        return None
    try:
        r = requests.get(
            f"https://graph.threads.net/v1.0/{user_id}/threads_insights",
            params={"metric": "followers_count", "access_token": token},
            timeout=30,
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            if m.get("name") == "followers_count":
                tv = m.get("total_value") or {}
                if "value" in tv:
                    return tv["value"]
                vals = m.get("values") or []
                if vals:
                    return vals[-1].get("value")
    except Exception as e:
        print(f"  Threadsフォロワー数取得スキップ: {e}")
    return None


# ── フォロワー履歴 ────────────────────────────────────────
def update_follower_history(x_followers: int | None,
                            threads_followers: int | None) -> str:
    """履歴JSONに今日の値を追記し、前日比・前週比の表示行を返す"""
    history: list[dict] = []
    if _HISTORY_PATH.exists():
        try:
            with open(_HISTORY_PATH, encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []

    today = datetime.now(JST).strftime("%Y-%m-%d")
    history = [h for h in history if h.get("date") != today]
    history.append({"date": today, "x": x_followers, "threads": threads_followers})
    history = history[-365:]  # 1年分保持

    try:
        with open(_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"  履歴保存エラー: {e}")

    def _find(days_ago: int) -> dict | None:
        target = (datetime.now(JST) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        for h in history:
            if h.get("date") == target:
                return h
        return None

    def _delta(cur, past) -> str:
        if cur is None or past is None:
            return ""
        d = cur - past
        return f"+{d}" if d >= 0 else str(d)

    lines = []
    yesterday = _find(1)
    last_week = _find(7)
    if x_followers is not None:
        d1 = _delta(x_followers, (yesterday or {}).get("x"))
        d7 = _delta(x_followers, (last_week or {}).get("x"))
        detail = []
        if d1: detail.append(f"前日{d1}")
        if d7: detail.append(f"前週{d7}")
        suffix = f"（{' / '.join(detail)}）" if detail else ""
        lines.append(f"X: {x_followers:,}人{suffix}")
    if threads_followers is not None:
        d1 = _delta(threads_followers, (yesterday or {}).get("threads"))
        d7 = _delta(threads_followers, (last_week or {}).get("threads"))
        detail = []
        if d1: detail.append(f"前日{d1}")
        if d7: detail.append(f"前週{d7}")
        suffix = f"（{' / '.join(detail)}）" if detail else ""
        lines.append(f"Threads: {threads_followers:,}人{suffix}")
    return "\n".join(lines)


# ── メイン ────────────────────────────────────────────────
def check_threads_token() -> bool:
    """Threadsトークンの生死を毎日確認する。

    Threadsの長期トークンは60日で失効する。失効に気づかないと、
    投稿がエラー扱いになったまま何週間も止まり続けるため、
    切れた時点でLINEに通知して気づけるようにしている。"""
    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        return True  # Threadsを使わない運用なら何もしない
    try:
        r = requests.get("https://graph.threads.net/v1.0/me",
                         params={"fields": "id,username", "access_token": token},
                         timeout=15)
    except Exception as e:
        print(f"  Threadsトークン確認スキップ（通信エラー）: {e}")
        return True  # 一時的な通信断で誤通知しない

    if r.status_code == 200:
        print(f"  Threadsトークン: 有効（@{r.json().get('username', '?')}）")
        return True
    if r.status_code in (400, 401):
        print("  Threadsトークン: ✗ 無効／期限切れ")
        notify_error(
            "Threadsのアクセストークンが失効しました",
            "Threads投稿が失敗し、投稿が「エラー」のまま止まります。\n"
            "60日で失効する仕様のため、再発行して GitHub Secrets の\n"
            "THREADS_ACCESS_TOKEN を更新してください。\n"
            "※ 更新するまでX投稿も「投稿済」に変わりません。")
        return False
    print(f"  Threadsトークン: 確認できず（HTTP {r.status_code}）")
    return True


def run():
    now = datetime.now(JST)
    print(f"[{now.strftime('%Y-%m-%d %H:%M')} JST] パフォーマンス計測 開始")

    print("Threadsトークン確認中...")
    check_threads_token()

    notion = get_notion_client()

    # ── フォロワー数の記録 ─────────────────────────────
    print("フォロワー数取得中...")
    x_followers       = fetch_x_followers()
    threads_followers = fetch_threads_followers()
    follower_line     = update_follower_history(x_followers, threads_followers)
    if follower_line:
        for line in follower_line.split("\n"):
            print(f"  {line}")
    else:
        print("  フォロワー数を取得できませんでした")

    # ── 投稿ごとのメトリクス ───────────────────────────
    try:
        pages = fetch_posted_pages(notion)
    except Exception as e:
        notify_error("パフォーマンス計測（analytics.py）", f"Notion取得失敗: {e}")
        print(f"Notion取得失敗: {e}", file=sys.stderr)
        sys.exit(1)

    x_targets       = {}   # tweet_id   -> page
    threads_targets = {}   # threads_id -> page
    for page in pages:
        tid = _extract_rich_text(page, PROP_TWEET_ID)
        thid = _extract_rich_text(page, PROP_THREADS_ID)
        if tid:
            x_targets[tid] = page
        if thid:
            threads_targets[thid] = page

    print(f"計測対象: X {len(x_targets)}件 / Threads {len(threads_targets)}件")

    updated = 0

    # X メトリクス（100件ずつバッチ）
    if x_targets:
        print("X APIでメトリクス取得中...")
        metrics_map = fetch_tweet_metrics_batch(list(x_targets.keys()))
        print(f"  取得成功: {len(metrics_map)} 件")
        for tweet_id, metrics in metrics_map.items():
            page_id = x_targets[tweet_id]["id"]
            props: dict = {}
            if "like_count" in metrics:
                props[PROP_LIKES] = {"number": metrics["like_count"]}
            if "retweet_count" in metrics:
                props[PROP_RETWEETS] = {"number": metrics["retweet_count"]}
            if "impression_count" in metrics:
                props[PROP_IMPRESSIONS] = {"number": metrics["impression_count"]}
            if not props:
                continue
            try:
                update_page_props(notion, page_id, props,
                                  optional=[PROP_LIKES, PROP_RETWEETS,
                                            PROP_IMPRESSIONS])
                print(f"  X更新: {tweet_id} → いいね {metrics.get('like_count','-')}"
                      f" / RT {metrics.get('retweet_count','-')}"
                      f" / imp {metrics.get('impression_count','-')}")
                updated += 1
            except Exception as e:
                print(f"  Notion更新エラー（{tweet_id}）: {e}")

    # Threads メトリクス（1件ずつ）
    if threads_targets:
        print("Threads Insightsでメトリクス取得中...")
        for threads_id, page in threads_targets.items():
            insights = fetch_threads_media_insights(threads_id)
            if not insights:
                continue
            props = {}
            if "likes" in insights:
                props[PROP_THREADS_LIKES] = {"number": insights["likes"]}
            if "views" in insights:
                props[PROP_THREADS_VIEWS] = {"number": insights["views"]}
            if not props:
                continue
            try:
                update_page_props(notion, page["id"], props,
                                  optional=[PROP_THREADS_LIKES,
                                            PROP_THREADS_VIEWS])
                print(f"  Threads更新: {threads_id} → いいね {insights.get('likes','-')}"
                      f" / 閲覧 {insights.get('views','-')}")
                updated += 1
            except Exception as e:
                print(f"  Notion更新エラー（Threads {threads_id}）: {e}")

    print(f"\n完了 — {updated} 件のメトリクスをNotionに保存しました")
    notify_analytics_complete(updated, extra=follower_line)


if __name__ == "__main__":
    run()
