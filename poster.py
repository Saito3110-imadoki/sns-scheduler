"""
SNS投稿自動化スクリプト
Notionの「未投稿」レコードを取得して、X・Threadsに自動投稿する
"""

import os
import sys
import time
import requests
import tweepy
from datetime import datetime, timezone, timedelta
from notion_client import Client
from dotenv import load_dotenv

# .envファイルを読み込む
load_dotenv()

# ── タイムゾーン（日本時間） ──────────────────────────
JST = timezone(timedelta(hours=9))

# ── Notionの設定 ──────────────────────────────────────
NOTION_TOKEN       = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

# Notionデータベースの列名
COL_TEXT     = "投稿文"
COL_DATETIME = "投稿日時"
COL_PLATFORM = "媒体"
COL_STATUS   = "ステータス"

# ステータスの値
STATUS_PENDING = "未投稿"
STATUS_DONE    = "投稿済"
STATUS_ERROR   = "エラー"


# ── Notionからデータを取得 ─────────────────────────────
def get_pending_posts(notion: Client) -> list:
    """ステータスが「未投稿」かつ投稿日時が現在以前のレコードを取得"""
    now_utc = datetime.now(timezone.utc).isoformat()
    result = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={
            "and": [
                {
                    "property": COL_STATUS,
                    "multi_select": {"contains": STATUS_PENDING},
                },
                {
                    "property": COL_DATETIME,
                    "date": {"on_or_before": now_utc},
                },
            ]
        },
        sorts=[{"property": COL_DATETIME, "direction": "ascending"}],
    )
    return result.get("results", [])


def get_text(page: dict) -> str:
    """投稿文を取得"""
    prop = page["properties"].get(COL_TEXT, {})
    if prop.get("type") == "title":
        return "".join(p["plain_text"] for p in prop.get("title", []))
    if prop.get("type") == "rich_text":
        return "".join(p["plain_text"] for p in prop.get("rich_text", []))
    return ""


def get_platform(page: dict) -> str:
    """媒体（X / Threads / 両方）を取得"""
    prop = page["properties"].get(COL_PLATFORM, {})
    if prop.get("type") == "select" and prop.get("select"):
        return prop["select"]["name"]
    if prop.get("type") == "multi_select" and prop.get("multi_select"):
        return prop["multi_select"][0]["name"]
    return ""


def update_status(notion: Client, page_id: str, status: str):
    """Notionのステータスを更新"""
    notion.pages.update(
        page_id=page_id,
        properties={COL_STATUS: {"multi_select": [{"name": status}]}},
    )


# ── X（Twitter）に投稿 ────────────────────────────────
def post_to_x(text: str) -> bool:
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_KEY_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    response = client.create_tweet(text=text)
    return response.data is not None


# ── Threadsに投稿 ─────────────────────────────────────
def post_to_threads(text: str) -> bool:
    user_id = os.environ["THREADS_USER_ID"]
    token   = os.environ["THREADS_ACCESS_TOKEN"]
    base    = f"https://graph.threads.net/v1.0/{user_id}"

    # Step1: 投稿コンテナを作成
    r = requests.post(
        f"{base}/threads",
        params={"media_type": "TEXT", "text": text, "access_token": token},
        timeout=30,
    )
    r.raise_for_status()
    container_id = r.json()["id"]

    time.sleep(5)  # Threads APIは少し待ってから公開する必要がある

    # Step2: 公開
    r = requests.post(
        f"{base}/threads_publish",
        params={"creation_id": container_id, "access_token": token},
        timeout=30,
    )
    r.raise_for_status()
    return "id" in r.json()


# ── メイン処理 ────────────────────────────────────────
def main():
    now = datetime.now(JST)
    print(f"▶ 実行開始：{now.strftime('%Y年%m月%d日 %H:%M')} JST")
    print("-" * 40)

    # APIキーの確認
    if not NOTION_TOKEN:
        print("❌ エラー：.envファイルにNOTION_TOKENが設定されていません")
        sys.exit(1)
    if not NOTION_DATABASE_ID:
        print("❌ エラー：.envファイルにNOTION_DATABASE_IDが設定されていません")
        sys.exit(1)

    notion = Client(auth=NOTION_TOKEN)

    # Notionからデータ取得
    try:
        posts = get_pending_posts(notion)
    except Exception as e:
        print(f"❌ Notionからの取得に失敗しました：{e}")
        sys.exit(1)

    if not posts:
        print("📭 投稿対象なし（未投稿かつ投稿日時が現在以前のレコードがありません）")
        return

    print(f"📬 投稿対象：{len(posts)} 件")

    posted = 0
    errors = 0

    for page in posts:
        page_id  = page["id"]
        text     = get_text(page)
        platform = get_platform(page)

        print(f"\n  📝 投稿文：{text[:40]}{'...' if len(text) > 40 else ''}")
        print(f"  📡 媒体  ：{platform}")

        if not text:
            print("  ⏭ 投稿文が空のためスキップ")
            continue
        if platform not in ("X", "Threads", "両方"):
            print(f"  ⚠️ 媒体の値「{platform}」が不正のためスキップ")
            continue

        ok_x = ok_threads = True

        try:
            if platform in ("X", "両方"):
                ok_x = post_to_x(text)
                print(f"  {'✅' if ok_x else '❌'} X：{'投稿成功' if ok_x else '投稿失敗'}")

            if platform in ("Threads", "両方"):
                ok_threads = post_to_threads(text)
                print(f"  {'✅' if ok_threads else '❌'} Threads：{'投稿成功' if ok_threads else '投稿失敗'}")

            new_status = STATUS_DONE if (ok_x and ok_threads) else STATUS_ERROR
            update_status(notion, page_id, new_status)
            print(f"  🔄 ステータス更新：→「{new_status}」")

            if ok_x and ok_threads:
                posted += 1
            else:
                errors += 1

        except Exception as e:
            print(f"  ❌ エラー：{e}")
            try:
                update_status(notion, page_id, STATUS_ERROR)
                print(f"  🔄 ステータス更新：→「{STATUS_ERROR}」")
            except Exception:
                pass
            errors += 1

    print("\n" + "-" * 40)
    print(f"✅ 完了：投稿済 {posted} 件 ／ エラー {errors} 件")


if __name__ == "__main__":
    main()