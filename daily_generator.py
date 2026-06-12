import os
import sys
import json
import feedparser
import tweepy
import anthropic
import requests
from datetime import datetime, timezone, timedelta
from notion_client import Client
from dotenv import load_dotenv

load_dotenv()

JST = timezone(timedelta(hours=9))
NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
PROP_TEXT     = "投稿文"
PROP_DATETIME = "投稿日時"
PROP_PLATFORM = "媒体"
PROP_STATUS   = "ステータス"
STATUS_PENDING_APPROVAL = "承認待ち"
PLATFORM_BOTH = "両方"
RSS_FEEDS = [
    "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",
    "https://www.publickey1.jp/atom.xml",
    "https://b.hatena.ne.jp/hotentry/it.rss",
    "https://blog.hubspot.com/marketing/rss.xml",
    "https://buffer.com/resources/feed/",
]
X_KEYWORDS = ["AI活用", "ChatGPT", "SNS運用", "WEBマーケティング"]
POST_TIMES_JST = ["09:00", "12:00", "18:00", "20:00", "22:00"]


def fetch_rss_news(max_items=12):
    items = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                title = entry.get("title", "").strip()
                if title:
                    items.append(title)
        except Exception as e:
            print(f"  RSS取得スキップ: {e}")
    return items[:max_items]


def fetch_trending_tweets(max_tweets=6):
    try:
        client = tweepy.Client(
            consumer_key=os.environ["X_API_KEY"],
            consumer_secret=os.environ["X_API_KEY_SECRET"],
            access_token=os.environ["X_ACCESS_TOKEN"],
            access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        )
        tweets = []
        for keyword in X_KEYWORDS[:2]:
            query = f"{keyword} lang:ja -is:retweet min_faves:50"
            response = client.search_recent_tweets(
                query=query,
                max_results=5,
                tweet_fields=["public_metrics", "text"],
                sort_order="relevancy",
            )
            if response.data:
                for tweet in response.data:
                    tweets.append(tweet.text[:150])
        return tweets[:max_tweets]
    except Exception as e:
        print(f"  X API検索スキップ: {e}")
        return []


def generate_posts_with_claude(news_items, trending, count=5):
    ai_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    news_text  = "\n".join(f"・{item}" for item in news_items) or "（情報なし）"
    trend_text = "\n".join(f"・{t}" for t in trending) or "（情報なし）"
    practical_count = count - 2

    あなたはフォロワーから「わかりやすい」「刺さる」と言われる人気SNSライターです。
以下のニュースとトレンドをヒントに、一般の人が思わず「いいね」や「保存」したくなる投稿を{count}件作成してください。

【参考ニュース】
{news_text}

【参考トレンド】
{trend_text}

【投稿の種類と配分】
A. 気づき・メタ認知型（{count - 2}件）
   - 「実はみんな無意識にやってること」を言語化する
   - 「あ、これ自分のことだ」と思わせる自己認識フック
   - 難しい概念を日常の場面に置き換えて説明
   - 書き出しパターン例：
     「なぜか○○してしまう人の特徴」
     「○○できない本当の理由は○○じゃなくて○○だった」
     「みんな○○と思ってるけど、実は逆」

B. 個人の本音・共感型（2件）
   - 自分が実際に感じた違和感・気づき・失敗
   - 「わかる〜」「言語化してくれてありがとう」と思わせる内容
   - 専門家っぽくなく、友達に話すような口調
   - 書き出しパターン例：
     「正直に言うと〜」
     「最近ずっと気になってること」
     「これ言うと怒られそうだけど〜」

【文字数・形式】
- 各投稿200〜400文字
- 専門用語は使わない（使うなら必ず日常語で言い換える）
- 絵文字は1〜2個、押しつけがましくない使い方
- ハッシュタグは1個まで（なくてもOK）
- 最後に問いかけや余韻を残す

【禁止】
- カタカナ専門用語の羅列（DX、メタバース、ペルソナ など単独使用）
- 「〜しましょう」「〜が重要です」など上から目線
- 箇条書きで終わる投稿
- 「まとめると」で始まる締め方

以下のJSON形式のみを出力してください（説明文不要）：

    message = ai_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"  JSON解析エラー: {e}")
        return []


def save_to_notion(posts):
    notion = Client(auth=NOTION_TOKEN)
    now    = datetime.now(JST)
    saved  = 0
    for i, post in enumerate(posts):
        time_str = POST_TIMES_JST[i % len(POST_TIMES_JST)]
        hour, minute = map(int, time_str.split(":"))
        post_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if post_dt <= now:
            post_dt += timedelta(days=1)
        try:
            notion.pages.create(
                parent={"database_id": NOTION_DATABASE_ID},
                properties={
                    PROP_TEXT: {"title": [{"text": {"content": post["text"]}}]},
                    PROP_DATETIME: {"date": {"start": post_dt.isoformat()}},
                    PROP_PLATFORM: {"multi_select": [{"name": PLATFORM_BOTH}]},
                    PROP_STATUS: {"multi_select": [{"name": STATUS_PENDING_APPROVAL}]},
                },
            )
            print(f"  保存: {post['text'][:40]}...")
            saved += 1
        except Exception as e:
            print(f"  Notion保存エラー: {e}")
    return saved


def send_line_notification(count):
    token   = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")
    if not token or not user_id:
        print("  LINE設定未完了のためスキップ")
        return
    now = datetime.now(JST)
    text = (
        f"📱 今日のSNS投稿案 {count}件が届きました！\n\n"
        f"📅 {now.strftime('%Y年%m月%d日')}\n\n"
        "Notionで内容を確認してください👇\n"
        "✅ 投稿したい → ステータスを「未投稿」に変更\n"
        "❌ 投稿しない → ステータスを「却下」に変更\n\n"
        "承認した投稿は自動で配信されます！"
    )
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": user_id, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        r.raise_for_status()
        print("  LINE通知送信完了")
    except Exception as e:
        print(f"  LINE通知エラー: {e}")


def run():
    now = datetime.now(JST)
    print(f"[{now.strftime('%Y-%m-%d %H:%M')} JST] 投稿生成開始")
    print("ニュース収集中...")
    news = fetch_rss_news()
    print(f"  RSS: {len(news)}件")
    print("トレンド投稿検索中...")
    trending = fetch_trending_tweets()
    print(f"  Xトレンド: {len(trending)}件")
    print("Claude AIで投稿文生成中...")
    posts = generate_posts_with_claude(news, trending, count=5)
    print(f"  生成: {len(posts)}件")
    if not posts:
        print("投稿の生成に失敗しました", file=sys.stderr)
        sys.exit(1)
    print("Notionに保存中...")
    saved = save_to_notion(posts)
    print(f"  保存: {saved}件")
    print("LINE通知送信中...")
    send_line_notification(saved)
    print(f"\n完了 — {saved}件の投稿案を「承認待ち」でNotionに保存しました")


if __name__ == "__main__":
    run()
