"""
毎朝自動実行：
1. RSSフィードからAI/マーケ/SNSニュース収集
2. X APIでトレンド投稿を検索
3. Claude AIで投稿文を5件生成（定量データがある場合は図解仕様も）
4. 図解が必要な投稿はmatplotlibで画像生成 → post-images/ へ保存
5. Notionに「承認待ち」で保存（画像URLも記録）
6. LINE Notifyで通知
"""

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

# playwright版インフォグラフィック（利用可能な場合に優先使用）
_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
try:
    from infographic import generate_infographic as _gen_web
    _HAS_WEB_RENDERER = True
except Exception as _e:
    print(f"[infographic] import失敗: {type(_e).__name__}: {_e}")
    _HAS_WEB_RENDERER = False

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

# 3色ルール
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


# ── フォントセットアップ ──────────────────────────────────
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


# ── チャート描画 ──────────────────────────────────────────
# Canvas: 12 × 6.75 (16:9), dpi=200 → 2400×1350px
_CW, _CH = 12.0, 6.75


def _split_label(text: str, max_chars: int = 12) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    mid = len(text) // 2
    return [text[:mid], text[mid:]]


def _draw_bar(ax, chart: dict):
    labels  = chart.get("labels", [])
    values  = chart.get("values", [])
    unit    = chart.get("unit", "")
    max_val = max(values) if values else 1
    n       = len(labels)

    x_start = 2.8
    bar_max = 8.2
    y_top   = 4.9
    step    = min((y_top - 0.9) / max(n, 1), 1.0)
    bar_h   = step * 0.52

    for i, (label, val) in enumerate(zip(labels, values)):
        y     = y_top - (i + 0.5) * step
        bar_w = bar_max * val / max_val

        ax.add_patch(FancyBboxPatch((x_start, y - bar_h / 2), bar_max, bar_h,
            boxstyle="round,pad=0.04", lw=0, facecolor='#1A2235'))
        ax.add_patch(FancyBboxPatch((x_start, y - bar_h / 2), bar_w, bar_h,
            boxstyle="round,pad=0.04", lw=0, facecolor=C_MAIN))

        _t(ax, 0.4, y, label, size=11, color=C_TEXT, va='center')
        _t(ax, x_start + bar_w + 0.22, y, f"{val}{unit}", size=12,
           color=C_ACCENT, bold=True, va='center')


def _draw_stat(ax, chart: dict):
    stats = chart.get("stats", [])
    n     = len(stats)
    if not n:
        return

    if n == 1:
        cards = [(1.5, 10.5)]
    elif n == 2:
        cards = [(0.5, 5.6), (6.4, 11.5)]
    else:
        cards = [(0.3, 3.9), (4.4, 7.9), (8.5, 11.9)]

    for (x0, x1), stat in zip(cards, stats[:3]):
        cx = (x0 + x1) / 2
        w  = x1 - x0

        ax.add_patch(FancyBboxPatch((x0, 0.75), w, 4.25,
            boxstyle="round,pad=0.15", lw=2,
            edgecolor=C_MAIN, facecolor='#0D1B2A'))

        ax.add_patch(FancyBboxPatch((x0 + 0.08, 4.72), w - 0.16, 0.16,
            boxstyle="round,pad=0.03", lw=0, facecolor=C_ACCENT))

        context = stat.get("context", "")
        if context:
            _t(ax, cx, 4.42, context, size=9.5, color=C_MUTED,
               ha='center', va='center')

        val      = stat.get("value", "")
        val_size = 38 if len(val) <= 4 else (30 if len(val) <= 6 else 24)
        val_y    = 3.15 if context else 3.3
        _t(ax, cx, val_y, val, size=val_size, color=C_ACCENT,
           bold=True, ha='center', va='center')

        label   = stat.get("label", "")
        label_y = val_y - 1.35
        lines   = _split_label(label, max_chars=12)
        for j, line in enumerate(lines[:2]):
            _t(ax, cx, label_y - j * 0.42, line, size=12, color=C_TEXT,
               ha='center', va='center')

    impact = chart.get("impact", "")
    if impact:
        ax.add_patch(FancyBboxPatch((0.4, 0.1), 11.2, 0.52,
            boxstyle="round,pad=0.05", lw=1,
            edgecolor='#2D2500', facecolor='#1C1400'))
        _t(ax, 6.0, 0.37, impact, size=10, color=C_ACCENT,
           ha='center', va='center')


def _draw_comparison(ax, chart: dict):
    left_label  = chart.get("left_label", "Before")
    right_label = chart.get("right_label", "After")
    left_items  = chart.get("left_items", [])
    right_items = chart.get("right_items", [])

    ax.add_patch(FancyBboxPatch((0.3, 0.85), 5.0, 4.15,
        boxstyle="round,pad=0.12", lw=1.5,
        edgecolor=C_BORDER, facecolor=C_SURFACE))
    _t(ax, 2.8, 4.65, left_label, size=14, color=C_MUTED, bold=True, ha='center')
    ax.plot([0.6, 5.0], [4.35, 4.35], color=C_BORDER, lw=0.7)
    for i, item in enumerate(left_items[:4]):
        y = 3.8 - i * 0.78
        ax.plot(0.78, y, 'o', color=C_MUTED, markersize=6)
        _t(ax, 1.05, y, item, size=10.5, color=C_MUTED)

    ax.annotate('', xy=(6.8, 2.9), xytext=(5.55, 2.9),
        arrowprops=dict(arrowstyle='->', color=C_ACCENT, lw=3.5, mutation_scale=28))

    ax.add_patch(FancyBboxPatch((7.0, 0.85), 4.7, 4.15,
        boxstyle="round,pad=0.12", lw=2,
        edgecolor=C_MAIN, facecolor='#0D1B2A'))
    _t(ax, 9.35, 4.65, right_label, size=14, color=C_MAIN, bold=True, ha='center')
    ax.plot([7.3, 11.4], [4.35, 4.35], color=C_BORDER, lw=0.7)
    for i, item in enumerate(right_items[:4]):
        y = 3.8 - i * 0.78
        ax.plot(7.5, y, 'o', color=C_MAIN, markersize=6)
        _t(ax, 7.77, y, item, size=10.5, color=C_TEXT)


def generate_chart_image(chart: dict, output_path: Path) -> bool:
    try:
        chart_type = chart.get("chart_type", "stat")
        title      = chart.get("title", "")
        subtitle   = chart.get("subtitle", "")
        caption    = chart.get("caption", "")

        fig, ax = plt.subplots(figsize=(_CW, _CH))
        fig.patch.set_facecolor(C_BASE)
        ax.set_facecolor(C_BASE)
        ax.set_xlim(0, _CW)
        ax.set_ylim(0, _CH)
        ax.axis('off')

        ax.add_patch(FancyBboxPatch((0.4, 6.05), 11.2, 0.58,
            boxstyle="round,pad=0.05", lw=0, facecolor='#161B22'))
        ax.plot([0.4, 0.4], [5.9, 6.65], color=C_ACCENT, lw=5,
                solid_capstyle='round')
        _t(ax, 0.68, 6.34, title, size=16, color=C_TEXT, bold=True)

        if subtitle:
            _t(ax, 0.5, 5.65, subtitle, size=10.5, color=C_MUTED)

        ax.plot([0.4, 11.6], [5.4, 5.4], color=C_BORDER, lw=0.8)

        if chart_type == "bar":
            _draw_bar(ax, chart)
        elif chart_type == "stat":
            _draw_stat(ax, chart)
        elif chart_type == "comparison":
            _draw_comparison(ax, chart)

        if caption:
            _t(ax, 0.4, 0.25, caption, size=8.5, color=C_MUTED)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(output_path, dpi=200, bbox_inches='tight', facecolor=C_BASE)
        plt.close(fig)
        return True
    except Exception as e:
        print(f"  画像生成エラー: {e}")
        plt.close('all')
        return False


# ── RSS / X ──────────────────────────────────────────────
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


# ── Claude AI 投稿生成 ────────────────────────────────────
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
        "   statsの各要素に context（「従来比」「導入後」「2024年」など比較の基準を5文字以内で）を必ず入れること\n"
        "   impactには投稿の要点を1文（30文字以内）で入れること\n"
        "   valueは必ず数字＋単位の形式（「50倍」「67%」「1/2」「月1000件」など）にすること\n"
        '   {"chart_type":"stat","title":"グラフタイトル","subtitle":"補足（任意）",'
        '"stats":[{"value":"50倍","label":"処理速度が向上","context":"従来比"},'
        '{"value":"1/2","label":"のトークンコスト","context":"削減率"}],'
        '"impact":"今あるツールを使い倒すだけで劇的に変わる",'
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


# ── Notion 保存 ───────────────────────────────────────────
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


# ── LINE 通知 ─────────────────────────────────────────────
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


# ── メイン ────────────────────────────────────────────────
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

    # 画像生成
    image_urls: dict = {}
    image_count = 0
    for i, post in enumerate(posts):
        if not post.get("needs_image") or "chart" not in post:
            continue
        filename = f"{date_str}-{i+1}.png"
        out_path = IMAGE_DIR / filename
        print(f"  画像生成中 [{i+1}]: {post.get('theme', '')}")
        # playwright優先、失敗時はmatplotlibにフォールバック
        ok = False
        if _HAS_WEB_RENDERER:
            ok = _gen_web(post["chart"], out_path)
            if ok:
                print("    レンダラー: playwright (高解像度)")
        if not ok:
            ok = generate_chart_image(post["chart"], out_path)
            if ok:
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
