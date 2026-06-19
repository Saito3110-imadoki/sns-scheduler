"""
SNS投稿自動化スクリプト
Notionデータベースから「未投稿」レコードを取得し、
指定日時にX（Twitter）・Threadsへ自動投稿する（画像付き対応）
"""

import os
import sys
import io
import time
import requests
import tweepy
from datetime import datetime, timezone, timedelta
from notion_client import Client
from dotenv import load_dotenv

load_dotenv()

JST = timezone(timedelta(hours=9))

NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

PROP_TEXT      = "投稿文"
PROP_DATETIME  = "投稿日時"
PROP_PLATFORM  = "媒体"
PROP_STATUS    = "ステータス"
PROP_IMAGE_URL = "画像URL"

STATUS_PENDING = "未投稿"
STATUS_DONE    = "投稿済"
STATUS_ERROR   = "エラー"

PLATFORM_X       = "X"
PLATFORM_THREADS = "Threads"
PLATFORM_BOTH    = "両方"


def get_notion_client() -> Client:
    return Client(auth=NOTION_TOKEN)


def fetch_pending_posts(notion: Client) -> list[dict]:
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


def update_status(notion: Client, page_id: str, status: str):
    notion.pages.update(
        page_id=page_id,
        properties={PROP_STATUS: {"multi_select": [{"name": status}]}},
    )


def download_image(url: str) -> bytes | None:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"  画像ダウンロードエラー: {e}")
        return None


def post_to_x(text: str, image_url: str = "") -> bool:
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
    return response.data is not None


def post_to_threads(text: str, image_url: str = "") -> bool:
    user_id = os.environ["THREADS_USER_ID"]
    token   = os.environ["THREADS_ACCESS_TOKEN"]
    base    = f"https://graph.threads.net/v1.0/{user_id}"

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
    return "id" in r.json()


def run():
    now = datetime.now(JST)
    print(f"[{now.strftime('%Y-%m-%d %H:%M')} JST] 実行開始")

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
        page_id   = page["id"]
        text      = extract_text(page)
        platform  = extract_platform(page)
        sched_dt  = extract_datetime(page)
        image_url = extract_image_url(page)

        label = text[:30] + ("..." if len(text) > 30 else "")
        print(f"\n  ページID: {page_id}")
        print(f"  投稿文  : {label}")
        print(f"  媒体    : {platform}")
        print(f"  予定日時: {sched_dt}")
        print(f"  画像URL : {image_url or '（なし）'}")

        if not text:
            print("  → 投稿文が空のためスキップ")
            continue
        if platform not in (PLATFORM_X, PLATFORM_THREADS, PLATFORM_BOTH):
            print(f"  → 媒体の値が不正のためスキップ（値: {platform!r}）")
            continue

        ok_x       = True
        ok_threads = True

        try:
            if platform in (PLATFORM_X, PLATFORM_BOTH):
                ok_x = post_to_x(text, image_url)
                print(f"  X       : {'✓ 投稿成功' if ok_x else '✗ 投稿失敗'}")

            if platform in (PLATFORM_THREADS, PLATFORM_BOTH):
                ok_threads = post_to_threads(text, image_url)
                print(f"  Threads : {'✓ 投稿成功' if ok_threads else '✗ 投稿失敗'}")

            if ok_x and ok_threads:
                update_status(notion, page_id, STATUS_DONE)
                print("  ステータス → 投稿済")
                posted_count += 1
            else:
                update_status(notion, page_id, STATUS_ERROR)
                print("  ステータス → エラー")
                error_count += 1

        except Exception as e:
            print(f"  エラー発生: {e}", file=sys.stderr)
            try:
                update_status(notion, page_id, STATUS_ERROR)
            except Exception:
                pass
            error_count += 1

    print(f"\n完了 — 投稿済: {posted_count} 件 / エラー: {error_count} 件")


if __name__ == "__main__":
    run()
