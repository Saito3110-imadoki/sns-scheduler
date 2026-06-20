"""
毎朝自動実行：
1. RSSフィードからAI/マーケ/SNSニュース収集
2. X APIでトレンド投稿を検索
3. Claude AIで投稿文を5件生成（定量データがある場合は図解仕様も）
4. 図解が必要な投稿はmatplotlibで画像生成 → post-images/ へ保存
5. Notionに「承認待ち」で保存（画像URLも記録）
6. LINE Notifyで通知
"""
import sys
from pathlib import Path

# playwright版インフォグラフィック（利用可能な場合に優先使用）
_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
try:
    from infographic import generate_infographic as _gen_web
    _HAS_WEB_RENDERER = True
except Exception:
    _HAS_WEB_RENDERER = False
import os
import sys
import json
import warnings
import feedparser
import tweepy
import anthropic
import requests
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from matplotlib import font_manager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from notion_client import Client
from dotenv import load_dotenv

warnings.filterwarnings('ignore')
load_dotenv()

JST = timezone(timedelta(hours=9))

NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

PROP_TEXT      = "投稿文"
PROP_DATETIME  = "投稿日時"
PROP_PLATFORM  = "媒体"
PROP_STATUS    = "ステータス"
PROP_IMAGE_URL = "画像URL"

STATUS_PENDING_APPROVAL = "承認待ち"
PLATFORM_BOTH = "両方"

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
IMAGE_DIR = Path("post-images")

C_BASE    = '#0D1117'
C_SURFACE = '#161B22'
C_MAIN    = '#818CF8'
C_ACCENT  = '#FCD34D'
C_TEXT    = '#E2E8F0'
C_MUTED   = '#64748B'
C_BORDER  = '#30363D'

RSS_FEEDS = [
    "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",
    "https://www.publickey1.jp/atom.xml",
    "https://b.hatena.ne.jp/hotentry/it.rss",
    "https://blog.hubspot.com/marketing/rss.xml",
    "https://buffer.com/resources/feed/",
]

X_KEYWORDS = ["AI活用", "ChatGPT", "SNS運用", "WEBマーケティング"]

POST_TIMES_JST = ["09:00", "12:00", "18:00", "20:00", "22:00"]


def _get_font_prop():
    candidates = [
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc',
    ]
    for p in candidates:
        if Path(p).exists():
            font_manager.fontManager.addfont(p)
            return font_manager.FontProperties(fname=p)
    return None


_FP = _get_font_prop()


def _t(ax, x, y, s, size=10, color=C_TEXT, bold=False, ha='left', va='center'):
    kw = dict(ha=ha, va=va, fontsize=size,
              fontweight='bold' if bold else 'normal', color=color)
    if _FP:
        kw['fontproperties'] = _FP
    ax.text(x, y, s, **kw)


def _draw_bar(ax, chart: dict):
    labels  = chart.get("labels", [])
    values  = chart.get("values", [])
    unit    = chart.get("unit", "")
    max_val = max(values) if values else 1
    bar_h   = 0.55
    bar_max = 7.0

    for i, (label, val) in enumerate(zip(labels, values)):
        y     = 3.5 - i * 0.85
        bar_w = bar_max * val / max_val
        ax.add_patch(FancyBboxPatch((1.5, y - bar_h/2), bar_max, bar_h,
            boxstyle="round,pad=0.05", lw=0, facecolor='#1E293B'))
        ax.add_patch(FancyBboxPatch((1.5, y - bar_h/2), bar_w, bar_h,
            boxstyle="round,pad=0.05", lw=0, facecolor=C_MAIN))
        _t(ax, 0.3, y, label, size=10, color=C_TEXT, va='center')
        _t(ax, 1.5 + bar_w + 0.15, y, f"{val}{unit}", size=10,
           color=C_ACCENT, bold=True, va='center')


def _draw_stat(ax, chart: dict):
    stats = chart.get("stats", [])
    n     = len(stats)
    if not n:
        return
    xs = [5.0] if n == 1 else [10.0 / (n + 1) * (i + 1) for i in range(n)]

    for x, stat in zip(xs, stats):
        ax.add_patch(plt.Circle((x, 2.7), 1.3, color='#1E1B4B', zorder=1))
        ax.add_patch(plt.Circle((x, 2.7), 1.35, color=C_MAIN,
                                fill=False, lw=2, zorder=2))
        _t(ax, x, 2.7, stat.get("value", ""), size=20, color=C_ACCENT,
           bold=True, ha='center', va='center')
        _t(ax, x, 1.2, stat.get("label", ""), size=9.5, color=C_TEXT,
           ha='center', va='center')


def _draw_comparison(ax, chart: dict):
    left_label  = chart.get("left_label", "Before")
    right_label = chart.get("right_label", "After")
    left_items  = chart.get("left_items", [])
    right_items = chart.get("right_items", [])

    ax.add_patch(FancyBboxPatch((0.3, 0.8), 4.0, 3.4,
        boxstyle="round,pad=0.1", lw=1.2,
        edgecolor=C_BORDER, facecolor=C_SURFACE))
    _t(ax, 2.3, 3.95, left_label, size=13, color=C_MUTED, bold=True, ha='center')
    for i, item in enumerate(left_items[:4]):
        y = 3.35 - i * 0.62
        ax.plot(0.62, y, 'o', color=C_MUTED, markersize=5)
        _t(ax, 0.88, y, item, size=9.5, color=C_MUTED)

    ax.annotate('', xy=(5.85, 2.5), xytext=(4.45, 2.5),
        arrowprops=dict(arrowstyle='->', color=C_ACCENT, lw=3, mutation_scale=20))

    ax.add_patch(FancyBboxPatch((6.0, 0.8), 4.0, 3.4,
        boxstyle="round,pad=0.1", lw=1.5,
        edgecolor=C_MAIN, facecolor=C_SURFACE))
    _t(ax, 8.0, 3.95, right_label, size=13, color=C_MAIN, bold=True, ha='center')
    for i, item in enumerate(right_items[:4]):
        y = 3.35 - i * 0.62
        ax.plot(6.32, y, 'o', color=C_MAIN, markersize=5)
        _t(ax, 6.58, y, item, size=9.5, color=C_TEXT)


def generate_chart_image(chart: dict, output_path: Path) -> bool:
    try:
        chart_type = chart.get("chart_type", "stat")
        title      = chart.get("title", "")
        subtitle   = chart.get("subtitle", "")
        caption    = chart.get("caption", "")

        fig, ax = plt.subplots(figsize=(10, 5.6))
        fig.patch.set_facecolor(C_BASE)
        ax.set_facecolor(C_BASE)
        ax.set_xlim(0, 10)
        ax.set_ylim(0, 5.6)
        ax.axis('off')

        ax.add_patch(FancyBboxPatch((0.4, 5.0), 9.2, 0.45,
            boxstyle="round,pad=0.05", lw=0, facecolor='#161B22'))
        _t(ax, 0.6, 5.22, title, size=14, color=C_TEXT, bold=True)
        if subtitle:
            _t(ax, 0.4, 4.65, subtitle, size=9.5, color=C_MUTED)
        ax.plot([0.4, 9.6], [4.45, 4.45], color=C_BORDER, lw=0.8)
        ax.plot([0.4, 0.4], [4.45, 5.45], color=C_ACCENT, lw=4,
                solid_capstyle='round')

        if chart_type == "bar":
            _draw_bar(ax, chart)
        elif chart_type == "stat":
            _draw_stat(ax, chart)
        elif chart_type == "comparison":
            _draw_comparison(ax, chart)

        if caption:
            _t(ax, 0.4, 0.2, caption, size=8, color=C_MUTED)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(output_path, dpi=120, bbox_inches='tight', facecolor=C_BASE)
        plt.close(fig)
        return True
    except Exception as e:
        print(f"  画像生成エラー: {e}")
        plt.close('all')
        return False


def fetch_rss_news(max_items: int = 12) -> list[str]:
    items = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                title = entry.get("title", "").strip()
                if title:
                    items.append(title)
        except Exception as e:
            print(f"  RSS取得スキップ ({url}): {e}")
    return items[:max_items]


def fetch_trending_tweets(max_tweets: int = 6) -> list[str]:
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
                query=query, max_results=5,
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


def generate_posts_with_claude(
    news_items: list[str], trending: list[str], count: int = 5
) -> list[dict]:
    ai_client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    news_text  = "\n".join(f"・{item}" for item in news_items) or "（情報なし）"
    trend_text = "\n".join(f"・{t}" for t in trending) or "（情報なし）"
    type_a     = count - 2

    prompt = (
        f"あなたはフォロワーから「わかりやすい」「刺さる」と言われる人気SNSライターです。\n"
        f"以下のニュースとトレンドをヒントに、一般の人が思わず「いいね」や「保存」したくなる投稿を{count}件作成してください。\n\n"
        f"【参考ニュース】\n{news_text}\n\n"
        f"【参考トレンド】\n{trend_text}\n\n"
        "【投稿の種類と配分】\n"
        f"A. 気づき・メタ認知型（{type_a}件）\n"
        "   - 「実はみんな無意識にやってること」を言語化する\n"
        "   - 「あ、これ自分のことだ」と思わせる自己認識フック\n"
        "   - 難しい概念を日常の場面に置き換えて説明\n\n"
        "B. 個人の本音・共感型（2件）\n"
        "   - 自分が実際に感じた違和感・気づき・失敗\n"
        "   - 専門家っぽくなく、友達に話すような口調\n\n"
        "【文字数・形式】\n"
        "- 各投稿200〜400文字\n"
        "- 専門用語は使わない\n"
        "- 絵文字は1〜2個\n"
        "- ハッシュタグは1個まで\n"
        "- 最後に問いかけや余韻を残す\n\n"
        "【禁止】カタカナ専門用語の羅列・上から目線・箇条書き終わり\n\n"
        "【図解生成ルール】\n"
        "投稿文の中に具体的な数字（○○件、○○%、○○倍、○○万円、○○人 など）が\n"
        "1つでも含まれていれば needs_image: true にしてください。\n"
        "数字が一切ない投稿のみ needs_image: false とし、chart フィールドは省略してください。\n\n"
        "chart フォーマット（chart_type に応じて1つ選択）:\n\n"
        "① bar（棒グラフ）: 複数の数値を並べて比較\n"
        '   {"chart_type":"bar","title":"グラフタイトル","subtitle":"補足（任意）",'
        '"labels":["項目A","項目B"],"values":[45,30],"unit":"%","caption":"出典"}\n\n'
        "② stat（数字強調）: 1〜3個の大きな数字を印象的に見せる\n"
        '   {"chart_type":"stat","title":"グラフタイトル","subtitle":"補足（任意）",'
        '"stats":[{"value":"67%","label":"の企業が導入"},{"value":"3倍","label":"に増加"}],'
        '"caption":"出典"}\n\n'
        "③ comparison（左右比較）: 旧来の手法 vs 新手法\n"
        '   {"chart_type":"comparison","title":"グラフタイトル","subtitle":"補足（任意）",'
        '"left_label":"従来","right_label":"新手法",'
        '"left_items":["特徴1","特徴2"],"right_items":["特徴1","特徴2"],'
        '"caption":"出典（任意）"}\n\n'
        "以下のJSON形式のみを出力してください（説明文不要）:\n"
        "[\n"
        '  {"text":"投稿文","type":"気づき","theme":"テーマ","needs_image":false},\n'
        '  {"text":"数字を含む投稿文","type":"データ","theme":"テーマ","needs_image":true,'
        '"chart":{"chart_type":"stat","title":"...","stats":[...]}}\n'
        "]"
    )

    message = ai_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=5000,
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
        print(f"  Claude出力: {raw[:300]}")
        return []


def save_to_notion(posts: list[dict], image_urls: dict) -> int:
    notion = Client(auth=NOTION_TOKEN)
    now    = datetime.now(JST)
    saved  = 0

    for i, post in enumerate(posts):
        time_str     = POST_TIMES_JST[i % len(POST_TIMES_JST)]
        hour, minute = map(int, time_str.split(":"))
        post_dt      = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if post_dt <= now:
            post_dt += timedelta(days=1)

        properties = {
            PROP_TEXT:     {"title": [{"text": {"content": post["text"]}}]},
            PROP_DATETIME: {"date": {"start": post_dt.isoformat()}},
            PROP_PLATFORM: {"multi_select": [{"name": PLATFORM_BOTH}]},
            PROP_STATUS:   {"multi_select": [{"name": STATUS_PENDING_APPROVAL}]},
        }
        if i in image_urls:
            properties[PROP_IMAGE_URL] = {"url": image_urls[i]}

        try:
            notion.pages.create(
                parent={"database_id": NOTION_DATABASE_ID},
                properties=properties,
            )
            img_mark = " 🖼" if i in image_urls else ""
            print(f"  保存: {post['text'][:40]}...{img_mark}")
            saved += 1
        except Exception as e:
            print(f"  Notion保存エラー: {e}")

    return saved


def send_line_notification(count: int, image_count: int):
    token   = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")
    if not token or not user_id:
        print("  LINE設定未完了のためスキップ")
        return

    now      = datetime.now(JST)
    img_info = f"うち図解付き {image_count}件\n" if image_count else ""
    text = (
        f"📱 今日のSNS投稿案 {count}件が届きました！\n"
        f"{img_info}\n"
        f"📅 {now.strftime('%Y年%m月%d日')}\n\n"
        "Notionで内容を確認してください👇\n"
        "✅ 投稿したい → ステータスを「未投稿」に変更\n"
        "❌ 投稿しない → ステータスを「却下」に変更\n\n"
        "承認した投稿は自動で配信されます！"
    )

    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"to": user_id, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        r.raise_for_status()
        print("  LINE通知送信完了")
    except Exception as e:
        print(f"  LINE通知エラー: {e}")


def run():
    now      = datetime.now(JST)
    date_str = now.strftime("%Y-%m-%d")
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

    image_urls: dict = {}
    image_count = 0
    for i, post in enumerate(posts):
        if not post.get("needs_image") or "chart" not in post:
            continue
        filename = f"{date_str}-{i+1}.png"
        out_path = IMAGE_DIR / filename
        print(f"  画像生成中 [{i+1}]: {post.get('theme', '')}")
            # playwright優先、失敗時はmatplotlibにフォールバック
            ok = (_HAS_WEB_RENDERER and _gen_web(post["chart"], out_path)) \
                 or generate_chart_image(post["chart"], out_path)
            if _HAS_WEB_RENDERER and ok:
                print("    レンダラー: playwright (高解像度)")
            elif ok:
                print("    レンダラー: matplotlib (フォールバック)")
            if ok:
                if GITHUB_REPOSITORY:
                    owner, repo = GITHUB_REPOSITORY.split("/", 1)
                    url = f"https://{owner}.github.io/{repo}/post-images/{filename}"
                else:
                    url = str(out_path)
                image_urls[i] = url
                image_count += 1
                print(f"    保存先: {out_path}")
    print(f"  画像生成: {image_count}件")

    print("Notionに保存中...")
    saved = save_to_notion(posts, image_urls)
    print(f"  保存: {saved}件")

    print("LINE通知送信中...")
    send_line_notification(saved, image_count)

    print(f"\n完了 — {saved}件の投稿案（図解付き {image_count}件）を「承認待ち」で保存しました")


if __name__ == "__main__":
    run()
