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
import yaml
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

# ── config.yaml 読み込み ──────────────────────────────────
_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_CFG: dict = {}
if _CONFIG_PATH.exists():
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as _f:
            _CFG = yaml.safe_load(_f) or {}
        print(f"[config] {_CONFIG_PATH.name} 読み込み完了")
    except Exception as _ce:
        print(f"[config] 読み込みエラー（デフォルト値を使用）: {_ce}")
else:
    print("[config] config.yaml が見つかりません。デフォルト値を使用します。")

def _cfg(*keys, default=None):
    """ネストしたキーを安全に取得"""
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

PROP_TEXT         = _cfg("notion", "properties", "text",         default="投稿文")
PROP_THREADS_TEXT = _cfg("notion", "properties", "threads_text", default="Threads用文面")
PROP_REPLY_TEXT   = _cfg("notion", "properties", "reply_text",   default="リプライ文面")
PROP_POST_TYPE    = _cfg("notion", "properties", "post_type",    default="投稿タイプ")
PROP_DATETIME     = _cfg("notion", "properties", "datetime",     default="投稿日時")
PROP_PLATFORM     = _cfg("notion", "properties", "platform",     default="媒体")
PROP_STATUS       = _cfg("notion", "properties", "status",       default="ステータス")
PROP_IMAGE_URL    = _cfg("notion", "properties", "image_url",    default="画像URL")
PROP_LIKES        = _cfg("notion", "properties", "likes",        default="いいね数")
PROP_RETWEETS     = _cfg("notion", "properties", "retweets",     default="RT数")
PROP_IMPRESSIONS  = _cfg("notion", "properties", "impressions",  default="インプレッション")

STATUS_PENDING_APPROVAL = _cfg("notion", "status",   "pending", default="承認待ち")
STATUS_POSTED           = _cfg("notion", "status",   "done",    default="投稿済")
PLATFORM_BOTH           = _cfg("notion", "platform", "default", default="両方")

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

RSS_FEEDS = _cfg("rss_feeds", default=[
    "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",
    "https://www.publickey1.jp/atom.xml",
    "https://b.hatena.ne.jp/hotentry/it.rss",
    "https://blog.hubspot.com/marketing/rss.xml",
    "https://buffer.com/resources/feed/",
])

X_KEYWORDS = _cfg("topics", "keywords_x", default=[
    "AI活用", "ChatGPT", "SNS運用", "WEBマーケティング"
])

POST_TIMES_JST = _cfg("schedule", "times", default=[
    "09:00", "12:00", "18:00", "20:00", "22:00"
])


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
    """長いラベルを複数行に分割"""
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

        # 背景トラック
        ax.add_patch(FancyBboxPatch((x_start, y - bar_h / 2), bar_max, bar_h,
            boxstyle="round,pad=0.04", lw=0, facecolor='#1A2235'))
        # 塗り棒
        ax.add_patch(FancyBboxPatch((x_start, y - bar_h / 2), bar_w, bar_h,
            boxstyle="round,pad=0.04", lw=0, facecolor=C_MAIN))

        # 左ラベル
        _t(ax, 0.4, y, label, size=11, color=C_TEXT, va='center')
        # 値ラベル
        _t(ax, x_start + bar_w + 0.22, y, f"{val}{unit}", size=12,
           color=C_ACCENT, bold=True, va='center')


def _draw_stat(ax, chart: dict):
    stats = chart.get("stats", [])
    n     = len(stats)
    if not n:
        return

    # カード配置（最大3枚）
    if n == 1:
        cards = [(1.5, 10.5)]
    elif n == 2:
        cards = [(0.5, 5.6), (6.4, 11.5)]
    else:
        cards = [(0.3, 3.9), (4.4, 7.9), (8.5, 11.9)]

    for (x0, x1), stat in zip(cards, stats[:3]):
        cx = (x0 + x1) / 2
        w  = x1 - x0

        # カード背景
        ax.add_patch(FancyBboxPatch((x0, 0.75), w, 4.25,
            boxstyle="round,pad=0.15", lw=2,
            edgecolor=C_MAIN, facecolor='#0D1B2A'))

        # 上部アクセントバー
        ax.add_patch(FancyBboxPatch((x0 + 0.08, 4.72), w - 0.16, 0.16,
            boxstyle="round,pad=0.03", lw=0, facecolor=C_ACCENT))

        # コンテキスト（小ラベル）
        context = stat.get("context", "")
        if context:
            _t(ax, cx, 4.42, context, size=9.5, color=C_MUTED,
               ha='center', va='center')

        # 大きな数値
        val      = stat.get("value", "")
        val_size = 38 if len(val) <= 4 else (30 if len(val) <= 6 else 24)
        val_y    = 3.15 if context else 3.3
        _t(ax, cx, val_y, val, size=val_size, color=C_ACCENT,
           bold=True, ha='center', va='center')

        # ラベル（折り返し対応）
        label   = stat.get("label", "")
        label_y = val_y - 1.35
        lines   = _split_label(label, max_chars=12)
        for j, line in enumerate(lines[:2]):
            _t(ax, cx, label_y - j * 0.42, line, size=12, color=C_TEXT,
               ha='center', va='center')

    # インパクト文（最下部）
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

    # 左パネル
    ax.add_patch(FancyBboxPatch((0.3, 0.85), 5.0, 4.15,
        boxstyle="round,pad=0.12", lw=1.5,
        edgecolor=C_BORDER, facecolor=C_SURFACE))
    _t(ax, 2.8, 4.65, left_label, size=14, color=C_MUTED, bold=True, ha='center')
    ax.plot([0.6, 5.0], [4.35, 4.35], color=C_BORDER, lw=0.7)
    for i, item in enumerate(left_items[:4]):
        y = 3.8 - i * 0.78
        ax.plot(0.78, y, 'o', color=C_MUTED, markersize=6)
        _t(ax, 1.05, y, item, size=10.5, color=C_MUTED)

    # 矢印
    ax.annotate('', xy=(6.8, 2.9), xytext=(5.55, 2.9),
        arrowprops=dict(arrowstyle='->', color=C_ACCENT, lw=3.5, mutation_scale=28))

    # 右パネル
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
    """チャート仕様から画像を生成してファイルに保存"""
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

        # ヘッダー背景
        ax.add_patch(FancyBboxPatch((0.4, 6.05), 11.2, 0.58,
            boxstyle="round,pad=0.05", lw=0, facecolor='#161B22'))
        # 左アクセントバー
        ax.plot([0.4, 0.4], [5.9, 6.65], color=C_ACCENT, lw=5,
                solid_capstyle='round')
        _t(ax, 0.68, 6.34, title, size=16, color=C_TEXT, bold=True)

        if subtitle:
            _t(ax, 0.5, 5.65, subtitle, size=10.5, color=C_MUTED)

        # 区切り線
        ax.plot([0.4, 11.6], [5.4, 5.4], color=C_BORDER, lw=0.8)

        # チャート本体
        if chart_type == "bar":
            _draw_bar(ax, chart)
        elif chart_type == "stat":
            _draw_stat(ax, chart)
        elif chart_type == "comparison":
            _draw_comparison(ax, chart)

        # フッター
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
    """RSSから新鮮な記事タイトルだけを収集する。
    公開日時が取得できる記事は max_age_hours 以内のものだけ採用。
    日時情報のないフィードは従来どおり先頭から採用する。"""
    from calendar import timegm
    import time as _time

    max_age_hours = _cfg("rss_max_age_hours", default=48)
    cutoff = _time.time() - max_age_hours * 3600

    items = []
    for url in RSS_FEEDS:
        try:
            feed  = feedparser.parse(url)
            fresh = 0
            stale = 0
            for entry in feed.entries[:10]:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                ts = entry.get("published_parsed") or entry.get("updated_parsed")
                if ts and timegm(ts) < cutoff:
                    stale += 1
                    continue
                items.append(title)
                fresh += 1
                if fresh >= 3:
                    break
            if stale and not fresh:
                print(f"  RSS ({url}): {max_age_hours}時間以内の新着なし")
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


# ── 曜日別コンテンツフォーカス ────────────────────────────
_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WEEKDAY_JA   = ["月", "火", "水", "木", "金", "土", "日"]

_DEFAULT_CALENDAR = {
    "mon": "週初めの仕事モードに合わせて、今週すぐ使える実践ノウハウを厚めに",
    "tue": "データ・数字を切り口にした発見系。図解映えするテーマを優先",
    "wed": "週の中だるみに刺さる「あるある」の言語化・共感系",
    "thu": "実践ノウハウ強化。月曜と違う角度のテクニック・ツール活用",
    "fri": "週末前のゆるい本音・失敗談。1週間の振り返りに絡めた学び",
    "sat": "ライトなライフハック・意外な小ネタ。休日のながら読みに合う軽さ",
    "sun": "明日からの1週間に向けた前向きな行動提案・モチベートで締める",
}


def get_daily_focus(now: datetime) -> tuple[str, str]:
    """今日の曜日名（日本語）とコンテンツフォーカスを返す"""
    idx      = now.weekday()
    key      = _WEEKDAY_KEYS[idx]
    calendar = _cfg("schedule", "weekly_calendar", default=None) or _DEFAULT_CALENDAR
    return _WEEKDAY_JA[idx], str(calendar.get(key, "")).strip()


# ── 直近テーマの重複防止 ──────────────────────────────────
def fetch_recent_themes(days: int = 7, max_items: int = 15) -> list[str]:
    """直近N日にNotionへ保存した投稿の冒頭を取得（重複防止用）。
    取得失敗時は空リストを返し、生成には影響しない。"""
    try:
        notion = Client(auth=NOTION_TOKEN)
        since  = (datetime.now(JST) - timedelta(days=days)).isoformat()
        resp = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={"property": PROP_DATETIME,
                    "date": {"on_or_after": since}},
            sorts=[{"property": PROP_DATETIME, "direction": "descending"}],
            page_size=max_items,
        )
        themes = []
        for page in resp.get("results", []):
            title = page["properties"].get(PROP_TEXT, {}).get("title", [])
            text  = "".join(p["plain_text"] for p in title).replace("\n", " ")[:50]
            if text:
                themes.append(text)
        return themes
    except Exception as e:
        print(f"  直近テーマ取得スキップ: {e}")
        return []


# ── 投稿実績の学習コンテキスト ────────────────────────────
def fetch_performance_insights(max_posts: int = 20) -> str:
    """直近の投稿実績（いいね・RT・インプレッション）から、
    生成AIに渡す学習コンテキストを組み立てる。
    実績データが3件未満・プロパティ未作成・取得失敗の場合は空文字を返し、
    生成処理には影響を与えない。"""
    try:
        notion = Client(auth=NOTION_TOKEN)
        resp = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={
                "and": [
                    {"property": PROP_STATUS,
                     "multi_select": {"contains": STATUS_POSTED}},
                    {"property": PROP_LIKES,
                     "number": {"is_not_empty": True}},
                ]
            },
            sorts=[{"property": PROP_DATETIME, "direction": "descending"}],
            page_size=max_posts,
        )

        rows = []
        for page in resp.get("results", []):
            props = page["properties"]
            title = props.get(PROP_TEXT, {}).get("title", [])
            text  = "".join(p["plain_text"] for p in title).replace("\n", " ")[:60]
            likes = props.get(PROP_LIKES, {}).get("number") or 0
            rts   = props.get(PROP_RETWEETS, {}).get("number") or 0
            imp   = props.get(PROP_IMPRESSIONS, {}).get("number") or 0
            sel   = props.get(PROP_POST_TYPE, {}).get("select") or {}
            ptype = sel.get("name", "")
            if text:
                # RT > いいね > インプの順に重み付けした簡易スコア
                rows.append({"text": text, "likes": likes, "rts": rts,
                             "imp": imp, "type": ptype,
                             "score": rts * 5 + likes * 3 + imp * 0.01})

        if len(rows) < 3:
            return ""

        rows.sort(key=lambda r: r["score"], reverse=True)
        top    = rows[:3]
        bottom = rows[-2:]

        lines = [
            "【直近の投稿実績 — 必ず分析して今日の投稿に反映すること】",
            "▼ 伸びた投稿（この切り口・書き出しの型を強化する）:",
        ]
        for r in top:
            tmark = f"［{r['type']}］" if r["type"] else ""
            lines.append(f"・{tmark}「{r['text']}…」→ いいね{r['likes']} / RT{r['rts']} / imp{r['imp']}")
        lines.append("▼ 伸びなかった投稿（同じパターンを避ける）:")
        for r in bottom:
            tmark = f"［{r['type']}］" if r["type"] else ""
            lines.append(f"・{tmark}「{r['text']}…」→ いいね{r['likes']} / RT{r['rts']} / imp{r['imp']}")

        # タイプ別の平均いいね（タイプ情報がある場合のみ）
        by_type: dict = {}
        for r in rows:
            if r["type"]:
                by_type.setdefault(r["type"], []).append(r["likes"])
        if by_type:
            stats = " / ".join(
                f"{t}型: 平均いいね{sum(v)/len(v):.1f}（{len(v)}件）"
                for t, v in sorted(by_type.items(),
                                   key=lambda kv: -sum(kv[1])/len(kv[1])))
            lines.append(f"▼ タイプ別実績: {stats}")

        lines.append(
            "伸びた投稿に共通するフックの型・テーマ・具体性のレベルを抽出し、"
            "今日の投稿に反映すること。ただし文面の使い回しは禁止。新しいテーマで型だけ再現する。")
        return "\n".join(lines)

    except Exception as e:
        print(f"  実績データ取得スキップ: {e}")
        return ""


# ── Claude AI 投稿生成 ────────────────────────────────────
def generate_posts_with_claude(
    news_items: list[str], trending: list[str], count: int = 5,
    insights: str = "", weekday_ja: str = "", daily_focus: str = "",
    recent_themes: list[str] | None = None,
) -> list[dict]:
    ai_client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    news_text  = "\n".join(f"・{item}" for item in news_items) or "（情報なし）"
    trend_text = "\n".join(f"・{t}" for t in trending) or "（情報なし）"

    # タイプ配分: A=実践ノウハウ / B=データ図解 / C=気づき言語化 / D=本音失敗談
    n_b = 1 if count >= 2 else 0
    n_d = 1 if count >= 3 else 0
    n_c = 1 if count >= 4 else 0
    n_a = count - n_b - n_c - n_d

    # config.yaml からスタイル情報を取得
    target_audience = _cfg("content", "target_audience",
                           default="中小企業経営者・マーケター・20〜40代ビジネスパーソン")
    tone            = _cfg("content", "tone",
                           default="親しみやすい・専門用語なし・友達に話すような口調")
    post_min        = _cfg("content", "post_length_min", default=200)
    post_max        = _cfg("content", "post_length_max", default=400)
    hashtags        = _cfg("content", "hashtags_per_post", default=1)
    emoji           = _cfg("content", "emoji_per_post", default=2)
    forbidden_list  = _cfg("content", "forbidden", default=[
        "カタカナ専門用語の羅列", "上から目線の表現", "箇条書きで終わる投稿"
    ])
    topics_list     = _cfg("topics", "primary", default=["AI活用", "SNS運用", "マーケティング"])
    company_name    = _cfg("company", "name", default="")

    forbidden_str  = "・".join(forbidden_list)
    topics_str     = "、".join(topics_list)
    company_str    = f"（{company_name}向け）" if company_name else ""
    insights_block = f"{insights}\n\n" if insights else ""
    dedup_block    = ""
    if recent_themes:
        theme_lines = "\n".join(f"・{t}…" for t in recent_themes)
        dedup_block = (
            "【直近7日間にすでに投稿したテーマ — 重複禁止】\n"
            f"{theme_lines}\n"
            "上記と同じテーマ・同じ切り口・同じツールの同じ使い方は禁止。\n"
            "似たテーマを扱う場合は、必ず別の角度（対象者を変える・逆の主張・別の事例）から書くこと。\n\n"
        )
    focus_block    = (
        f"【今日のコンテンツフォーカス（{weekday_ja}曜日）】\n"
        f"{daily_focus}\n"
        "タイプ配分のルールは守りつつ、テーマ選定と切り口をこのフォーカスに寄せること。\n\n"
    ) if daily_focus else ""

    prompt = (
        f"あなたはX・Threadsで累計10万フォロワーを獲得してきたプロのSNSマーケターです{company_str}。\n"
        "インプレッションではなく「保存・フォロー・リプライ」を最大化する投稿を設計します。\n"
        f"ターゲット読者: {target_audience}\n"
        f"トーン: {tone}\n"
        f"コンテンツテーマ: {topics_str}\n\n"
        f"以下のニュースとトレンドをもとに、投稿を{count}件作成してください。\n\n"
        f"【参考ニュース】\n{news_text}\n\n"
        f"【参考トレンド】\n{trend_text}\n\n"
        f"{insights_block}"
        f"{dedup_block}"
        f"{focus_block}"
        "【投稿タイプと配分】\n"
        f"A. 実践ノウハウ型（{n_a}件）— フォロワー獲得の主力。保存されることが目的\n"
        "   - 読んだ人が「明日そのままマネできる」具体的な手順・使い方・テクニック\n"
        "   - 例:「議事録作成が10分で終わるChatGPTの使い方。手順は3つだけ」\n"
        "   - 抽象論・心構えは禁止。ツール名・操作・順番まで具体的に\n\n"
        f"B. データ・図解型（{n_b}件）— 権威性と拡散を作る。図解画像 必須\n"
        "   - 参考ニュースに実際に出てくる数字だけを使い、意外性のある事実を提示\n"
        "   - ニュースに使える数字がない場合は、このタイプをA型に差し替えること\n\n"
        f"C. 気づき・言語化型（{n_c}件）— 共感リポスト狙い\n"
        "   - みんなが薄々感じているのに言語化できていないことを代弁する\n"
        "   - 「あ、これ自分のことだ」と思わせる自己認識フック\n\n"
        f"D. 本音・失敗談型（{n_d}件）— リプライ誘発・親近感\n"
        "   - 実体験ベースの失敗と、そこからの学び。かっこつけない\n"
        "   - 専門家っぽくなく、友達に話すような口調\n\n"
        "【1行目のルール — 最重要】\n"
        "タイムラインに表示されるのは1行目だけ。1行目で止まらなければ本文は存在しないのと同じです。\n"
        "必ず以下のいずれかのパターンで書き始めてください:\n"
        "・数字インパクト:「資料作成が3時間→20分になった、たった1つの設定」\n"
        "・損失回避:「これ知らないだけで、毎月10時間損してます」\n"
        "・意外性・逆張り:「実は、SNSの毎日投稿って逆効果になることがあります」\n"
        "・自分ごと化・名指し:「AIをまだ部下に使わせてない管理職の方、正直まずいです」\n"
        "・ビフォーアフター:「フォロワー200人だった弊社アカウントが、3ヶ月で1万人になった話」\n"
        "禁止する書き出し: 挨拶 /「今日は〜についてお話しします」/「〜だと思います」/ 抽象的な問いかけ\n\n"
        "【本文のルール】\n"
        f"- 各投稿{post_min}〜{post_max}文字\n"
        "- 1〜2文ごとに改行し、スマホで読んだときの視覚的リズムを作る\n"
        "- 抽象語（「効率化」「活用」だけ等）で終わらせず、必ず具体的な数字・手順・固有名詞を入れる\n"
        "- 専門用語は中学生にもわかる言葉に置き換える\n"
        f"- 絵文字は{emoji}個まで\n"
        f"- ハッシュタグは{hashtags}個まで\n\n"
        "【締めのルール（タイプ別）】\n"
        "- A/B型: 保存を促す一言（「あとで見返せるように保存推奨です」等）か、今日やる最初の一歩を1つ提示\n"
        "- C/D型: 読者への具体的な問いかけで終わり、リプライを誘発（「みなさんはどっち派ですか？」等）\n\n"
        f"【禁止】{forbidden_str}・数字の捏造（参考ニュースにない統計数字を作らない）\n\n"
        "【図解生成ルール】\n"
        "B型の投稿は needs_image: true とし、chart を必ず付けてください。\n"
        "他のタイプは、参考ニュース由来の実在する数字が投稿に含まれる場合のみ needs_image: true。\n"
        "数字の出典が参考ニュースにない場合は図解を作らず needs_image: false とし、chart は省略してください。\n\n"
        "図解の品質ルール:\n"
        "- title は名詞形ではなく結論型にする（×「AI導入の実態」→ ○「AI導入企業の7割が売上増」）\n"
        "- 数字は最大3つ。詰め込まない。1画像1メッセージ\n"
        "- caption には必ず出典（ニュース媒体名など）を入れる\n\n"
        "chart フォーマット（chart_type に応じて1つ選択）:\n\n"
        "① bar（棒グラフ）: 複数の数値を並べて比較\n"
        '   {"chart_type":"bar","title":"結論型タイトル","subtitle":"補足（任意）",'
        '"labels":["項目A","項目B"],"values":[45,30],"unit":"%","caption":"出典"}\n\n'
        "② stat（数字強調）: 1〜3個の大きな数字を印象的に見せる\n"
        "   statsの各要素に context（「従来比」「導入後」「2024年」など比較の基準を5文字以内で）を必ず入れること\n"
        "   impactには投稿の要点を1文（30文字以内）で入れること\n"
        "   valueは必ず数字＋単位の形式（「50倍」「67%」「1/2」「月1000件」など）にすること\n"
        '   {"chart_type":"stat","title":"結論型タイトル","subtitle":"補足（任意）",'
        '"stats":[{"value":"50倍","label":"処理速度が向上","context":"従来比"},'
        '{"value":"1/2","label":"のトークンコスト","context":"削減率"}],'
        '"impact":"今あるツールを使い倒すだけで劇的に変わる",'
        '"caption":"出典"}\n\n'
        "③ comparison（左右比較）: 旧来の手法 vs 新手法\n"
        '   {"chart_type":"comparison","title":"結論型タイトル","subtitle":"補足（任意）",'
        '"left_label":"従来","right_label":"新手法",'
        '"left_items":["特徴1","特徴2"],"right_items":["特徴1","特徴2"],'
        '"caption":"出典（任意）"}\n\n'
        "【媒体別の書き分け — 各投稿につきX版とThreads版の2つを書く】\n"
        "XとThreadsはアルゴリズムも文化も別物です。同じ内容を、それぞれに最適化して書き分けてください。\n\n"
        "X版（text）:\n"
        "- 情報密度を高く。断言調。リスト・番号付き手順が強い\n"
        "- 1行目のフックで勝負。無駄な前置きゼロ\n\n"
        "Threads版（text_threads）:\n"
        "- 450文字以内厳守（Threadsの上限は500文字）\n"
        "- 同じテーマを「友達に話しかける」口調に書き直す。宣伝臭・断言調はThreadsでは嫌われる\n"
        "- 途中or最後にゆるい問いかけを入れて会話を誘発する（Threadsのアルゴリズムはリプライを最重視）\n"
        "- リスト形式より、流れのある話し言葉。絵文字は控えめでOK\n"
        "- X版のコピペは禁止。必ず書き直すこと\n\n"
        "【セルフリプライ（reply）— X投稿の2投稿目】\n"
        "各投稿に、X投稿の直後に自分でぶら下げるリプライを1つ書いてください（100〜200文字）。\n"
        "リプライはスレッドの滞在時間を伸ばし、アルゴリズム評価を上げるための本文の続きです。\n"
        "- A/B型: 本文で書ききれなかった補足・つまずきやすいポイント・応用ワザ\n"
        "- C/D型: 本文の裏話・もう一歩踏み込んだ本音\n"
        "- 「詳しくはプロフィールへ」等の宣伝は禁止。純粋に価値を追加する\n\n"
        "【出力前のセルフチェック】\n"
        "各投稿について次を確認し、満たさない場合は書き直してから出力すること:\n"
        "1. 1行目だけ読んで、続きが読みたくなるか？\n"
        "2. この投稿を読んだ人が「保存」か「リプライ」をする理由が明確か？\n"
        "3. 数字はすべて参考ニュースに実在するか？\n"
        "4. Threads版は450文字以内で、X版と口調が変わっているか？\n"
        "5. replyは本文の繰り返しではなく、新しい価値を足しているか？\n\n"
        "以下のJSON形式のみを出力してください（説明文不要）:\n"
        "[\n"
        '  {"text":"X向け投稿文","text_threads":"Threads向け投稿文","reply":"セルフリプライ",'
        '"type":"ノウハウ","theme":"テーマ","needs_image":false},\n'
        '  {"text":"数字を含むX向け投稿文","text_threads":"Threads向け投稿文","reply":"セルフリプライ",'
        '"type":"データ","theme":"テーマ","needs_image":true,'
        '"chart":{"chart_type":"stat","title":"...","stats":[...]}}\n'
        "]"
    )

    message = ai_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=11000,
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
        threads_text = (post.get("text_threads") or "").strip()
        if threads_text:
            properties[PROP_THREADS_TEXT] = {
                "rich_text": [{"text": {"content": threads_text[:2000]}}]
            }
        reply_text = (post.get("reply") or "").strip()
        if reply_text:
            properties[PROP_REPLY_TEXT] = {
                "rich_text": [{"text": {"content": reply_text[:2000]}}]
            }
        post_type = (post.get("type") or "").strip()
        if post_type:
            properties[PROP_POST_TYPE] = {"select": {"name": post_type[:50]}}
        if i in image_urls:
            properties[PROP_IMAGE_URL] = {"url": image_urls[i]}

        # 任意プロパティ（Notion側に未作成でも保存が失敗しないように、
        # エラーに名前が含まれていたら外して再試行する）
        optional_props = [PROP_THREADS_TEXT, PROP_REPLY_TEXT, PROP_POST_TYPE]

        try:
            for _attempt in range(len(optional_props) + 1):
                try:
                    notion.pages.create(
                        parent={"database_id": NOTION_DATABASE_ID},
                        properties=properties,
                    )
                    break
                except Exception as e:
                    removable = [p for p in optional_props
                                 if p in properties and p in str(e)]
                    if not removable:
                        raise
                    for p in removable:
                        print(f"  ※ プロパティ「{p}」が未作成のためスキップして保存します")
                        del properties[p]
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

    print("投稿実績データ取得中...")
    insights = fetch_performance_insights()
    print(f"  実績学習: {'あり（プロンプトに反映）' if insights else 'データ不足のためスキップ'}")

    print("直近テーマ取得中...")
    recent_themes = fetch_recent_themes()
    print(f"  重複防止: 直近{len(recent_themes)}件のテーマを回避対象に設定")

    weekday_ja, daily_focus = get_daily_focus(now)
    if daily_focus:
        print(f"  {weekday_ja}曜フォーカス: {daily_focus[:30]}...")

    posts_per_day = _cfg("content", "posts_per_day", default=5)
    print("Claude AIで投稿文生成中...")
    posts = generate_posts_with_claude(news, trending, count=posts_per_day,
                                       insights=insights,
                                       weekday_ja=weekday_ja,
                                       daily_focus=daily_focus,
                                       recent_themes=recent_themes)
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
            ok = _gen_web(post["chart"], out_path, _cfg("branding"))
            if ok:
                print("    レンダラー: playwright (高解像度)")
        if not ok:
            ok = generate_chart_image(post["chart"], out_path)
            if ok:
                print("    レンダラー: matplotlib (フォールバック)")
        if ok:
            if GITHUB_REPOSITORY:
                owner_repo = GITHUB_REPOSITORY  # e.g. "Saito3110-imadoki/sns-scheduler"
                owner, repo = owner_repo.split("/", 1)
                url = (f"https://{owner}.github.io/{repo}/post-images/{filename}")
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
