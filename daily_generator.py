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
import time
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

try:
    import ai_image
except Exception as _e:
    ai_image = None

try:
    from notify import notify_error
except Exception:
    def notify_error(context: str, detail: str) -> None:  # フォールバック
        pass

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

NOTION_TOKEN       = os.environ["NOTION_TOKEN"].strip()
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"].strip()

PROP_TEXT         = _cfg("notion", "properties", "text",         default="投稿文")
PROP_THREADS_TEXT = _cfg("notion", "properties", "threads_text", default="Threads用文面")
PROP_REPLY_TEXT   = _cfg("notion", "properties", "reply_text",   default="リプライ文面")
PROP_POST_TYPE    = _cfg("notion", "properties", "post_type",    default="投稿タイプ")
PROP_DATETIME     = _cfg("notion", "properties", "datetime",     default="投稿日時")
PROP_PLATFORM     = _cfg("notion", "properties", "platform",     default="媒体")
PROP_STATUS       = _cfg("notion", "properties", "status",       default="ステータス")
PROP_IMAGE_URL    = _cfg("notion", "properties", "image_url",    default="画像URL")
PROP_IMAGE_URLS   = _cfg("notion", "properties", "image_urls",   default="画像URL一覧")
PROP_LIKES        = _cfg("notion", "properties", "likes",        default="いいね数")
PROP_RETWEETS     = _cfg("notion", "properties", "retweets",     default="RT数")
PROP_IMPRESSIONS  = _cfg("notion", "properties", "impressions",  default="インプレッション")

STATUS_PENDING_APPROVAL = _cfg("notion", "status",   "pending", default="承認待ち")
# poster.py が投稿対象として拾うステータス
STATUS_READY            = "未投稿"
# 完全自動投稿モード: true にすると承認をスキップし、生成した投稿を
# そのまま投稿対象（未投稿）として保存する。人のチェックが入らないため、
# 有効化はクライアントの明示的な同意のうえで行うこと
AUTO_APPROVE            = bool(_cfg("content", "auto_approve", default=False))
STATUS_POSTED           = _cfg("notion", "status",   "done",    default="投稿済")
PLATFORM_BOTH           = _cfg("notion", "platform", "default", default="両方")

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
IMAGE_DIR = Path("post-images")

# 3色ルール（白背景ライトテーマ）
C_BASE    = '#FFFFFF'
C_SURFACE = '#F1F5F9'
C_MAIN    = '#4F46E5'
C_ACCENT  = '#D97706'
C_TEXT    = '#0F172A'
C_MUTED   = '#64748B'
C_BORDER  = '#E2E8F0'

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
            boxstyle="round,pad=0.04", lw=0, facecolor='#E2E8F0'))
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
            edgecolor=C_MAIN, facecolor='#F8FAFC'))

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
            edgecolor='#FCD34D', facecolor='#FFFBEB'))
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
        edgecolor=C_MAIN, facecolor='#F8FAFC'))
    _t(ax, 9.35, 4.65, right_label, size=14, color=C_MAIN, bold=True, ha='center')
    ax.plot([7.3, 11.4], [4.35, 4.35], color=C_BORDER, lw=0.7)
    for i, item in enumerate(right_items[:4]):
        y = 3.8 - i * 0.78
        ax.plot(7.5, y, 'o', color=C_MAIN, markersize=6)
        _t(ax, 7.77, y, item, size=10.5, color=C_TEXT)


def _draw_flow(ax, chart: dict):
    steps = chart.get("steps", [])[:4]
    n     = len(steps)
    if not n:
        return
    top    = 5.0
    bottom = 0.9
    step_h = (top - bottom) / n
    for i, st in enumerate(steps):
        y = top - (i + 0.5) * step_h
        is_last = (i == n - 1)
        chip_c  = C_ACCENT if is_last else C_MAIN
        # ラベルチップ
        ax.add_patch(FancyBboxPatch((0.4, y - 0.28), 1.9, 0.56,
            boxstyle="round,pad=0.06", lw=0, facecolor=chip_c))
        _t(ax, 1.35, y, st.get("label", f"STEP{i+1}"), size=11,
           color='#ffffff', bold=True,
           ha='center', va='center')
        # テキストボックス
        ax.add_patch(FancyBboxPatch((2.7, y - 0.32), 8.9, 0.64,
            boxstyle="round,pad=0.06", lw=1,
            edgecolor=C_ACCENT if is_last else C_BORDER, facecolor='#F8FAFC'))
        _t(ax, 3.0, y, st.get("text", ""), size=10.5, color=C_TEXT, va='center')
        # 矢印
        if not is_last:
            ax.annotate('', xy=(1.35, y - step_h + 0.32), xytext=(1.35, y - 0.34),
                arrowprops=dict(arrowstyle='->', color=C_MUTED, lw=2))


def _draw_list(ax, chart: dict):
    items = chart.get("items", [])[:4]
    n     = len(items)
    if not n:
        return
    top    = 5.0
    bottom = 0.8
    row_h  = (top - bottom) / n
    for i, it in enumerate(items):
        y = top - (i + 0.5) * row_h
        ax.add_patch(FancyBboxPatch((0.4, y - row_h / 2 + 0.08), 11.2, row_h - 0.16,
            boxstyle="round,pad=0.06", lw=1,
            edgecolor=C_BORDER, facecolor='#F8FAFC'))
        # 番号
        ax.add_patch(FancyBboxPatch((0.7, y - 0.26), 0.52, 0.52,
            boxstyle="round,pad=0.05", lw=0, facecolor=C_MAIN))
        _t(ax, 0.96, y, str(i + 1), size=14, color='#ffffff', bold=True,
           ha='center', va='center')
        # 見出しと補足
        _t(ax, 1.6, y + 0.16, it.get("head", ""), size=12.5, color=C_TEXT, bold=True)
        _t(ax, 1.6, y - 0.22, it.get("text", ""), size=9.5, color=C_MUTED)


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
            boxstyle="round,pad=0.05", lw=0, facecolor='#F1F5F9'))
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
        elif chart_type == "flow":
            _draw_flow(ax, chart)
        elif chart_type == "list":
            _draw_list(ax, chart)

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


def fetch_trending_tweets(max_tweets: int = 12) -> list[dict]:
    """伸びている参考投稿を、本文全文＋エンゲージメント実数つきで収集する。
    数字を添えることで「なぜ伸びたか」をAIが学習できるようにする。
    config の topics.benchmark_accounts を指定すると、そのアカウントの
    投稿も参考ソースに加える（業界の勝ちパターンを継続学習）。"""
    keywords   = X_KEYWORDS[:4]
    benchmarks = _cfg("topics", "benchmark_accounts", default=[]) or []
    try:
        client = tweepy.Client(
            consumer_key=os.environ["X_API_KEY"],
            consumer_secret=os.environ["X_API_KEY_SECRET"],
            access_token=os.environ["X_ACCESS_TOKEN"],
            access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        )

        def _search(query: str) -> list[dict]:
            out = []
            try:
                resp = client.search_recent_tweets(
                    query=query, max_results=10,
                    tweet_fields=["public_metrics", "text"],
                    sort_order="relevancy", user_auth=True,
                )
            except Exception as e:
                print(f"  検索スキップ（{query}）: {e}")
                return out
            for tw in (resp.data or []):
                m = tw.public_metrics or {}
                out.append({
                    "text":  tw.text,                      # 全文（構成を学ぶため切らない）
                    "likes": m.get("like_count", 0),
                    "rts":   m.get("retweet_count", 0),
                    "reps":  m.get("reply_count", 0),
                    "imp":   m.get("impression_count", 0),
                })
            return out

        collected: list[dict] = []
        # キーワード検索。min_faves等の絞り込み演算子は上位APIプラン専用で
        # 400になるため使わず、取得後に自前のスコアで並べ替えて上位を採用する
        for kw in keywords:
            collected.extend(_search(f"{kw} lang:ja -is:retweet -is:reply"))
        # ベンチマークアカウント（業界の手本）
        for acct in benchmarks[:3]:
            handle = str(acct).lstrip("@")
            collected.extend(_search(f"from:{handle} -is:retweet -is:reply"))

        # 重複排除＋エンゲージメント順（RT重視）で上位を採用
        seen, uniq = set(), []
        for t in collected:
            key = t["text"][:60]
            if key in seen:
                continue
            seen.add(key)
            uniq.append(t)
        uniq.sort(key=lambda t: t["rts"] * 5 + t["likes"] * 2 + t["reps"] * 3,
                  reverse=True)
        return uniq[:max_tweets]
    except Exception as e:
        print(f"  X API検索スキップ: {e}")
        return []


def analyze_winning_patterns(trending: list[dict]) -> str:
    """伸びている参考投稿を分析し「今このジャンルで効いている型」を抽出する。
    汎用ノウハウではなく直近の実データから型を学ぶための1パス。
    失敗しても空文字を返し、生成処理は止めない。"""
    if len(trending) < 3:
        return ""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return ""
    sample = "\n\n".join(
        f"[いいね{t['likes']} / RT{t['rts']} / リプ{t['reps']}]\n{t['text']}"
        for t in trending[:10])
    try:
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=700,
            messages=[{"role": "user", "content": (
                "あなたはSNSの分析官です。以下は、いま実際に伸びている投稿群です。"
                "エンゲージメント数値と本文を照らし合わせ、"
                "『いま このジャンルで効いている型』を抽出してください。\n\n"
                f"{sample}\n\n"
                "次の観点で、日本語の箇条書き5行以内にまとめてください:\n"
                "・1行目（フック）に共通する言い回しや切り口\n"
                "・本文の構成パターン（順序・情報の出し方）\n"
                "・数字や固有名詞の使われ方\n"
                "・読者のどの感情に触れているか\n"
                "・リプライ/RTを誘発している要素\n"
                "※内容の要約ではなく『再現できる型』として書くこと。前置き不要。")}],
        )
        text = "".join(b.text for b in msg.content
                       if getattr(b, "type", "") == "text").strip()
        if text:
            print("  勝ちパターン分析: 完了")
            return ("【いま伸びている投稿の型 — 実データ分析】\n" + text +
                    "\nこの型を今日の投稿に適用すること。ただし文面の模倣は禁止、"
                    "型のみを再現し、テーマは自社のものにする。")
    except Exception as e:
        print(f"  勝ちパターン分析スキップ: {e}")
    return ""


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


def get_weekly_theme(now: datetime) -> str:
    """今週の特集テーマを返す。config schedule.weekly_themes に
    テーマ配列を書くと、週ごとに順番に切り替わる（ISO週番号でローテーション）。
    同じ週の投稿を1つの文脈で積み上げ、「何の専門家か」を伝わりやすくする。
    未設定なら空文字（＝従来どおり曜日フォーカスのみ）。"""
    themes = _cfg("schedule", "weekly_themes", default=[]) or []
    if not themes:
        return ""
    week = now.isocalendar()[1]
    return str(themes[week % len(themes)]).strip()


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
            text  = "".join(p["plain_text"] for p in title).replace("\n", " ")[:160]
            likes = props.get(PROP_LIKES, {}).get("number") or 0
            rts   = props.get(PROP_RETWEETS, {}).get("number") or 0
            imp   = props.get(PROP_IMPRESSIONS, {}).get("number") or 0
            sel   = props.get(PROP_POST_TYPE, {}).get("select") or {}
            ptype = sel.get("name", "")
            if text:
                # RT > いいね > インプの順に重み付けした簡易スコア
                er = round(likes / imp * 100, 1) if imp else 0.0
                rows.append({"text": text, "likes": likes, "rts": rts,
                             "imp": imp, "type": ptype, "er": er,
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
            lines.append(f"・{tmark}「{r['text']}…」→ いいね{r['likes']} / RT{r['rts']} / imp{r['imp']} / 反応率{r['er']}%")
        lines.append("▼ 伸びなかった投稿（同じパターンを避ける）:")
        for r in bottom:
            tmark = f"［{r['type']}］" if r["type"] else ""
            lines.append(f"・{tmark}「{r['text']}…」→ いいね{r['likes']} / RT{r['rts']} / imp{r['imp']} / 反応率{r['er']}%")

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

        # 実際に伸びた投稿の「1行目」だけを抜き出す。
        # 1行目でスクロールが止まるかが全てなので、ここに絞って学習させる
        hooks = []
        for r in rows[:8]:
            first = r["text"].split("。")[0].split("？")[0][:48]
            if first and r["imp"]:
                hooks.append(f"・「{first}」→ imp{r['imp']} / 反応率{r['er']}%")
        if hooks:
            lines.append("▼ このアカウントで実際に反応が取れた1行目:")
            lines.extend(hooks)
            lines.append(
                "上記はこのアカウントの読者に実際に刺さった1行目です。"
                "この言い回し・切り口の傾向を今日の1行目に必ず反映すること。")

        lines.append(
            "伸びた投稿に共通するフックの型・テーマ・具体性のレベルを抽出し、"
            "今日の投稿に反映すること。ただし文面の使い回しは禁止。新しいテーマで型だけ再現する。")
        return "\n".join(lines)

    except Exception as e:
        print(f"  実績データ取得スキップ: {e}")
        return ""


# ── Claude API 共通ヘルパー ───────────────────────────────
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


def _claude_call(prompt: str, max_tokens: int, retries: int = 3) -> str:
    """Claude APIを呼び出してテキストを返す。一時エラーは指数バックオフで再試行"""
    ai_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    delays    = [5, 15, 30]
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            message = ai_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text.strip()
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = delays[min(attempt, len(delays) - 1)]
                print(f"  Claude APIエラー（{attempt+1}/{retries}回目）: "
                      f"{type(e).__name__} → {wait}秒後に再試行")
                time.sleep(wait)
    raise last_err  # 全リトライ失敗


def _parse_json_array(raw: str) -> list:
    """Claudeの出力からJSON配列を取り出す。失敗時は空リスト"""
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  JSON解析エラー: {e}")
        print(f"  Claude出力: {raw[:300]}")
        return []


# ── Claude AI 投稿生成 ────────────────────────────────────
def generate_posts_with_claude(
    news_items: list[str], trending: list[dict], count: int = 5,
    insights: str = "", weekday_ja: str = "", daily_focus: str = "",
    recent_themes: list[str] | None = None, patterns: str = "",
    weekly_theme: str = "",
) -> list[dict]:
    news_text  = "\n".join(f"・{item}" for item in news_items) or "（情報なし）"
    # 参考トレンドは「実数つき全文」で渡す。数字があることで
    # AIが「どの型が伸びたか」を判断できる
    if trending and isinstance(trending[0], dict):
        trend_text = "\n\n".join(
            f"［いいね{t['likes']} / RT{t['rts']} / リプ{t['reps']}］\n{t['text']}"
            for t in trending) or "（情報なし）"
    else:
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
    x_limit         = int(_cfg("content", "x_char_limit", default=0) or 0)
    # 無料アカウント（x_limit>0）では超過分が途中で切れて公開されるため、
    # 上限を絶対条件としてAIに伝える
    if x_limit > 0:
        length_rule = (
            f"- 各投稿{post_min}〜{post_max}文字。⚠️上限は絶対条件。"
            f"X無料アカウントのため、超過した投稿は途中でぶつ切りになって公開される。"
            f"短くても薄くせず、1投稿=1メッセージに絞って必ず{post_max}文字以内に収めること\n"
        )
    else:
        length_rule = f"- 各投稿{post_min}〜{post_max}文字\n"
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
    patterns_block = f"{patterns}\n\n" if patterns else ""
    dedup_block    = ""
    if recent_themes:
        theme_lines = "\n".join(f"・{t}…" for t in recent_themes)
        dedup_block = (
            "【直近7日間にすでに投稿したテーマ — 重複禁止】\n"
            f"{theme_lines}\n"
            "上記と同じテーマ・同じ切り口・同じツールの同じ使い方は禁止。\n"
            "似たテーマを扱う場合は、必ず別の角度（対象者を変える・逆の主張・別の事例）から書くこと。\n\n"
        )
    weekly_block = (
        f"【今週の特集テーマ】\n{weekly_theme}\n"
        "今週の投稿はこの特集の文脈で積み上げること。"
        "毎回バラバラのテーマにせず、この切り口を別角度から掘り下げ、"
        "「このアカウントをフォローすれば、この分野が分かる」と伝わる状態を作る。\n"
        "ただし前日までと同じ内容の繰り返しは禁止。必ず新しい角度で。\n\n"
    ) if weekly_theme else ""

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
        f"{patterns_block}"
        f"{dedup_block}"
        f"{weekly_block}"
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
        f"{length_rule}"
        "- 1〜2文ごとに改行し、スマホで読んだときの視覚的リズムを作る\n"
        "- 抽象語（「効率化」「活用」だけ等）で終わらせず、必ず具体的な数字・手順・固有名詞を入れる\n"
        "- 専門用語は中学生にもわかる言葉に置き換える\n"
        f"- 絵文字は{emoji}個まで\n"
        f"- ハッシュタグは{hashtags}個まで\n\n"
        "【締めのルール（タイプ別）】\n"
        "- A/B型: 保存を促す一言（「あとで見返せるように保存推奨です」等）か、今日やる最初の一歩を1つ提示\n"
        "- C/D型: リプライを誘発する問いで終わる\n"
        "  ※Xは「いいね」より「リプライ」を圧倒的に高く評価する。答えやすさが命。\n"
        "  必ず次のいずれかの形にすること:\n"
        "  ・二択:「面接で年収を先に聞くのは、アリ？ナシ？」\n"
        "  ・経験の呼び水:「あなたが一番『やられた』と思った求人票の表現、なんでしたか？」\n"
        "  ・数字で答えられる問い:「入社を決めるまで、何社受けましたか？」\n"
        "  禁止:「どう思いますか？」「参考になれば嬉しいです」など、"
        "何を答えればいいか分からない曖昧な締め\n\n"
        f"【禁止】{forbidden_str}・数字の捏造（参考ニュースにない統計数字を作らない）\n\n"
        "【図解生成ルール】\n"
        "図解はSNSで最も保存されるコンテンツです。以下のルールで付けてください:\n"
        "- B型（データ型）: needs_image: true 必須。数字系チャート（stat / bar / comparison）を付ける\n"
        "- A型（実践ノウハウ）: 少なくとも1件に flow または list の図解を付けること（needs_image: true）。\n"
        "  手順の流れ→ flow、コツ・ポイントの列挙→ list が向いている\n"
        "- C/D型: 原則 needs_image: false。ただし「原因→対策」のような構造が明確な場合は flow を付けてよい\n"
        "- 「悪い流れ vs 良い流れ」「よくある失敗 vs 正しいやり方」のように"
        "２つの筋道を対比できるテーマなら compare_flow を使う（最も保存されやすい型）\n"
        "- 「◯◯の全類型」「見抜き方まとめ」のように、系統立てて網羅できるテーマなら"
        " matrix を使う。2〜3の系統に分け、各系統に3〜5項目を入れる（保存率が最も高い）\n\n"
        "【複数枚スライド（カルーセル）— 起承転結で構成する】\n"
        "枚数は投稿内容に合わせて最適化すること。4枚は上限でありノルマではない。\n"
        "スライドを増やすほど離脱も増える。その枚数でしか伝えられない時だけ増やすこと:\n"
        "- 1枚で伝わる内容 → 1枚（迷ったらこれ。1枚で伝わるなら1枚が最強）\n"
        "- 対比や理由を1つ足すと伝わる → 2枚（起→結）\n"
        "- 「実は◯◯」という発見でストーリーを作れる → 3枚（起→転→結）\n"
        "- 背景・原因まで丁寧に語る価値がある濃いテーマ → 4枚（起→承→転→結）\n\n"
        "複数枚にする場合の役割:\n"
        "- 起（1枚目・表紙）: 問題提起・共感フック。「あ、自分のことだ」と思わせて手を止めさせる\n"
        "- 承: 背景・原因の深掘り。なぜその問題が起きるのかを納得させる\n"
        "- 転: 視点の転換・解決策。「実は◯◯だった」という一番の見せ場\n"
        "- 結（最終枚）: まとめと次の一歩。読者が今日やることを1つ提示して保存を促す\n"
        "- 複数枚のとき、各チャートに \"role\" フィールド（起/承/転/結 のいずれか1文字）を必ず入れること（画像右上に表示される）\n"
        "- チャート種類は自由に組み合わせる（例: 起=list、承=comparison、転=flow、結=stat）\n"
        "- 各スライドは単体でも意味が通ること。前のスライドを読まないと分からない書き方は禁止\n"
        "- 水増し禁止: 同じ内容の言い換えでスライドを増やさない。枚数を1枚減らせないか必ず自問すること\n\n"
        "数字の扱い:\n"
        "- 数字を使うチャート（stat / bar）は参考ニュース由来の実在する数字のみ。捏造禁止\n"
        "- flow / list は投稿内容の構造化なので数字は不要。自由に作ってよい\n\n"
        "図解の品質ルール:\n"
        "- title は名詞形ではなく結論型にする（×「AI導入の実態」→ ○「AI導入企業の7割が売上増」）\n"
        "- 1画像1メッセージ。詰め込まない（数字は最大3つ、ステップ・項目は最大4つ）\n"
        "- 数字系チャートの caption には必ず出典（ニュース媒体名など）を入れる\n"
        "- 図解は投稿文の要約ではなく「投稿文を補完する構造」にする（投稿を読んで図解を保存したくなる関係）\n\n"
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
        "④ flow（プロセス・流れ）: 原因→仮説→対策→次の一歩、手順のステップなど\n"
        "   label は6文字以内（「原因」「仮説」「対策」「次の一歩」「STEP1」など）\n"
        "   text は28文字以内で言い切る。説明は投稿文に任せ、図解は骨組みだけにする。steps は2〜4個\n"
        "   各 text の中で最も重要なフレーズを1つだけ【】で囲むこと（図解上で金色の太字になる）\n"
        "   impact には読者への行動提案を1文（30文字以内・任意）\n"
        '   {"chart_type":"flow","title":"結論型タイトル","subtitle":"補足（任意）",'
        '"steps":[{"label":"原因","text":"反応が悪いのは【1行目】の問題"},'
        '{"label":"仮説","text":"TLでは【1行目しか読まれない】"},'
        '{"label":"対策","text":"【数字か意外性】で書き出す"},'
        '{"label":"次の一歩","text":"過去投稿の1行目を書き直す"}],'
        '"impact":"1行目を変えるだけで反応は2倍変わる","caption":"出典（任意）"}\n\n'
        "⑤ list（ポイント解説）: コツ・チェックリスト・要点まとめ\n"
        "   head は12文字以内の見出し、text は28文字以内の補足。items は3〜4個\n"
        "   text の中で最も重要なフレーズを1つだけ【】で囲むこと（図解上で金色の太字になる）\n"
        '   {"chart_type":"list","title":"結論型タイトル","subtitle":"補足（任意）",'
        '"items":[{"head":"完璧を目指さない","text":"【6割の出来】で投稿し反応から学ぶ"},'
        '{"head":"数字を1つ入れる","text":"数字は【信頼と保存率】を上げる"},'
        '{"head":"問いかけで締める","text":"リプライは【アルゴリズム評価が最大】"}],'
        '"impact":"まず1つだけ今日の投稿で試す","caption":"出典（任意）"}\n\n'
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
        "【1行目のA/B】\n"
        "各投稿には hook_alt として、本文の1行目とは\"別の切り口\"の代案を1つ必ず付けること。\n"
        "本文の1行目と代案は、異なるパターン（例: 数字インパクト vs 損失回避）にすること。\n"
        "出稿前に編集長が強い方を採用する。\n\n"
        "以下のJSON形式のみを出力してください（説明文不要）:\n"
        "[\n"
        '  {"text":"X向け投稿文","hook_alt":"別案の1行目","text_threads":"Threads向け投稿文","reply":"セルフリプライ",'
        '"type":"ノウハウ","theme":"テーマ","needs_image":false},\n'
        '  {"text":"X向け投稿文","text_threads":"Threads向け投稿文","reply":"セルフリプライ",'
        '"type":"ノウハウ","theme":"テーマ","needs_image":true,'
        '"charts":[{"chart_type":"list","title":"...","items":[...]}]},\n'
        '  {"text":"数字を含むX向け投稿文","text_threads":"Threads向け投稿文","reply":"セルフリプライ",'
        '"type":"データ","theme":"テーマ","needs_image":true,'
        '"charts":[{"role":"起","chart_type":"list","title":"...","items":[...]},'
        '{"role":"承","chart_type":"matrix","title":"...",'
        '"groups":[{"label":"系統A","items":[{"head":"項目","text":"補足"}]},'
        '{"label":"系統B","items":[{"head":"項目","text":"補足"}]}]},'
        '{"role":"転","chart_type":"compare_flow","title":"...",'
        '"left":{"label":"よくある流れ","items":["…","…","…"]},'
        '"right":{"label":"うまくいく流れ","items":["…","…","…"]}},'
        '{"role":"結","chart_type":"stat","title":"...","stats":[...]}]}\n'
        "]"
    )

    raw = _claude_call(prompt, max_tokens=13000)
    return _parse_json_array(raw)


# ── 文字数ガード（X無料アカウント向け）────────────────────
def _x_weighted_len(text: str) -> int:
    """Xの重み付き文字数（半角=1 / 全角=2）"""
    return sum(1 if ord(c) < 128 else 2 for c in text)


def enforce_x_length(posts: list[dict]) -> list[dict]:
    """x_char_limit>0（無料アカウント）のとき、上限超過の投稿をAIで短縮する。
    超過したまま投稿すると途中で切れて公開されるため、生成段階で必ず収める。"""
    limit = int(_cfg("content", "x_char_limit", default=0) or 0)
    if limit <= 0:
        return posts
    post_max = int(_cfg("content", "post_length_max", default=135))
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    fixed = 0
    for post in posts:
        text = post.get("text", "")
        if not text or _x_weighted_len(text) <= limit:
            continue
        try:
            msg = client.messages.create(
                model=CLAUDE_MODEL, max_tokens=500,
                messages=[{"role": "user", "content": (
                    f"次のX投稿を、1行目のフックの強さと内容の核を保ったまま"
                    f"{post_max}文字以内に凝縮してください。"
                    "箇条書きは削るか1行にまとめ、改行は最小限に。"
                    "出力は投稿本文のみ（前置き・説明・引用符は不要）。\n\n" + text)}],
            )
            new_text = "".join(b.text for b in msg.content
                               if getattr(b, "type", "") == "text").strip()
            # 上限内に収まった、または少なくとも短くなったなら採用
            # （それでも超過が残る分は poster 側の最終トリムが保険になる）
            if new_text and _x_weighted_len(new_text) < _x_weighted_len(text):
                post["text"] = new_text
                fixed += 1
        except Exception as e:
            print(f"  文字数短縮スキップ: {e}")
    if fixed:
        print(f"  文字数ガード: {fixed}件を短縮リライト")
    return posts


# ── 編集長レビュー（2パス目）──────────────────────────────
def review_posts_with_claude(posts: list[dict],
                             news_items: list[str]) -> list[dict]:
    """生成済み投稿を「編集長」として審査し、弱い投稿を書き直す。
    レビューに失敗した場合は元の投稿をそのまま返す（投稿ゼロは絶対に避ける）。"""
    news_text  = "\n".join(f"・{item}" for item in news_items) or "（情報なし）"
    posts_json = json.dumps(posts, ensure_ascii=False, indent=1)

    prompt = (
        "あなたはSNS運用歴10年の編集長です。ライターが書いた投稿案を出稿前に最終審査し、"
        "弱い投稿だけを書き直してください。\n\n"
        f"【参考ニュース（数字の根拠確認用）】\n{news_text}\n\n"
        f"【投稿案（JSON）】\n{posts_json}\n\n"
        "【審査基準】\n"
        "1. フック: 1行目だけ読んで手が止まるか。弱ければ1行目を書き直す"
        "（挨拶・「〜と思います」・抽象的な問いかけで始まる投稿は失格）\n"
        "   各投稿には hook_alt（1行目の代案）が付いている。本文の1行目と読み比べ、"
        "スクロールを止める力が強い方を本文の1行目として採用すること"
        "（代案が強ければ差し替える。hook_altは出力に含めなくてよい）\n"
        "2. 具体性: 抽象論で終わっていないか。数字・手順・固有名詞が入っているか\n"
        "3. 行動理由: 読者が「保存」か「リプライ」をしたくなる要素が明確か\n"
        "4. 数字の根拠: 参考ニュースにない統計数字は削除するか、ニュースにある数字に差し替える\n"
        "5. Threads版: 450文字以内で、X版のコピペになっていないか（口調が会話調に変わっているか）\n"
        "6. 図解: title が結論型か。flow/list のテキストが簡潔か（28文字以内）\n\n"
        "【絶対に守るルール】\n"
        "- JSONの構造・キー名は変えない（text, text_threads, reply, type, theme, needs_image, charts, role など）\n"
        "- 投稿の件数を増減させない。順番も変えない\n"
        "- 良い投稿はそのまま残す。全部を書き直す必要はない\n"
        "- 各投稿に \"edited\" フィールド（true/false）を追加し、書き直した場合のみ true にする\n\n"
        "審査後の全投稿を、入力と同じJSON配列形式のみで出力してください（説明文不要）:"
    )

    try:
        raw      = _claude_call(prompt, max_tokens=13000)
        reviewed = _parse_json_array(raw)
        # 検証: 件数一致・全件にtextがあること。壊れていたら元を使う
        if (len(reviewed) == len(posts)
                and all(isinstance(p, dict) and p.get("text") for p in reviewed)):
            edited = sum(1 for p in reviewed if p.get("edited"))
            print(f"  編集長レビュー: {edited}件を改稿 / {len(reviewed)}件中")
            return reviewed
        print("  編集長レビュー: 出力が不正のため原稿をそのまま使用")
        return posts
    except Exception as e:
        print(f"  編集長レビュー: スキップ（{type(e).__name__}: {e}）")
        return posts


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
            PROP_STATUS:   {"multi_select": [{"name": STATUS_READY if AUTO_APPROVE else STATUS_PENDING_APPROVAL}]},
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
            urls = image_urls[i]
            if isinstance(urls, str):  # 旧形式（単一URL）との互換
                urls = [urls]
            properties[PROP_IMAGE_URL] = {"url": urls[0]}
            if len(urls) > 1:
                properties[PROP_IMAGE_URLS] = {
                    "rich_text": [{"text": {"content": "\n".join(urls)[:2000]}}]
                }

        # 任意プロパティ（Notion側に未作成でも保存が失敗しないように、
        # エラーに名前が含まれていたら外して再試行する）
        optional_props = [PROP_THREADS_TEXT, PROP_REPLY_TEXT, PROP_POST_TYPE,
                          PROP_IMAGE_URLS]

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
    print(f"  モード: {'完全自動投稿（承認スキップ）' if AUTO_APPROVE else '承認制（承認待ちで保存）'}")

    print("ニュース収集中...")
    news = fetch_rss_news()
    print(f"  RSS: {len(news)}件")

    print("トレンド投稿検索中...")
    trending = fetch_trending_tweets()
    print(f"  Xトレンド: {len(trending)}件（参考: 伸びている投稿）")

    print("勝ちパターン分析中...")
    patterns = analyze_winning_patterns(trending)
    if not patterns:
        print("  勝ちパターン分析: 参考投稿が少ないためスキップ")

    print("投稿実績データ取得中...")
    insights = fetch_performance_insights()
    print(f"  実績学習: {'あり（プロンプトに反映）' if insights else 'データ不足のためスキップ'}")

    print("直近テーマ取得中...")
    recent_themes = fetch_recent_themes()
    print(f"  重複防止: 直近{len(recent_themes)}件のテーマを回避対象に設定")

    weekday_ja, daily_focus = get_daily_focus(now)
    weekly_theme = get_weekly_theme(now)
    if weekly_theme:
        print(f"  今週の特集: {weekly_theme[:50]}")
    if daily_focus:
        print(f"  {weekday_ja}曜フォーカス: {daily_focus[:30]}...")

    posts_per_day = _cfg("content", "posts_per_day", default=5)
    print("Claude AIで投稿文生成中...")
    try:
        posts = generate_posts_with_claude(news, trending, count=posts_per_day,
                                           insights=insights,
                                           weekday_ja=weekday_ja,
                                           daily_focus=daily_focus,
                                           recent_themes=recent_themes,
                                           patterns=patterns,
                                           weekly_theme=weekly_theme)
    except Exception as e:
        notify_error("投稿生成（daily_generator.py）",
                     f"Claude API呼び出しが全リトライ失敗: {e}")
        print(f"投稿の生成に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  生成: {len(posts)}件")

    if not posts:
        notify_error("投稿生成（daily_generator.py）",
                     "生成結果が空でした（JSON解析失敗の可能性）")
        print("投稿の生成に失敗しました", file=sys.stderr)
        sys.exit(1)

    # 編集長レビュー（config で editor_review: false にすると無効化できる）
    if _cfg("content", "editor_review", default=True):
        print("編集長レビュー中...")
        posts = review_posts_with_claude(posts, news)

    # X無料アカウント向けの文字数ガード（上限超過をAIで短縮）
    posts = enforce_x_length(posts)

    # 画像生成（charts配列 = カルーセル対応。旧形式の chart 単体も受け付ける）
    image_urls: dict = {}
    image_count = 0
    slide_count = 0
    for i, post in enumerate(posts):
        charts = post.get("charts")
        if not charts and isinstance(post.get("chart"), dict):
            charts = [post["chart"]]
        if not post.get("needs_image") or not charts:
            continue
        charts = [c for c in charts if isinstance(c, dict)][:4]
        total  = len(charts)
        print(f"  画像生成中 [{i+1}]: {post.get('theme', '')}（{total}枚）")

        urls = []
        for j, chart in enumerate(charts, start=1):
            suffix   = f"-{j}" if total > 1 else ""
            filename = f"{date_str}-{i+1}{suffix}.png"
            out_path = IMAGE_DIR / filename
            # playwright優先、失敗時はmatplotlibにフォールバック
            ok = False
            if _HAS_WEB_RENDERER:
                ok = _gen_web(chart, out_path, _cfg("branding"), page=(j, total))
                if ok:
                    print(f"    [{j}/{total}] playwright (高解像度)")
            if not ok:
                ok = generate_chart_image(chart, out_path)
                if ok:
                    print(f"    [{j}/{total}] matplotlib (フォールバック)")
            if ok:
                if GITHUB_REPOSITORY:
                    owner, repo = GITHUB_REPOSITORY.split("/", 1)
                    # GitHub Pages の正規ホストは小文字。大文字混じりだと Threads が
                    # 画像URLを直接フェッチできず失敗するため、owner を小文字化する
                    urls.append(f"https://{owner.lower()}.github.io/{repo}/post-images/{filename}")
                else:
                    urls.append(str(out_path))
                slide_count += 1

        if urls:
            image_urls[i] = urls
            image_count += 1

    # チャート図解が付かなかった投稿に、AIアイキャッチを補完
    # （config content.ai_eyecatch: true かつ 環境変数 FAL_KEY がある時だけ動作）
    _eye_on   = bool(_cfg("content", "ai_eyecatch", default=False))
    _eye_mod  = ai_image is not None
    _eye_key  = _eye_mod and ai_image.enabled()
    if not (_eye_on and _eye_mod and _eye_key):
        # 無言でスキップすると原因が分からないため、欠けている条件を明示する
        _miss = []
        if not _eye_on:  _miss.append("config content.ai_eyecatch が false")
        if not _eye_mod: _miss.append("ai_image.py が配置されていない")
        elif not _eye_key: _miss.append("環境変数 FAL_KEY が未設定")
        print(f"  AIアイキャッチ: 無効（{' / '.join(_miss)}）")
    if (_eye_on and _eye_mod and _eye_key):
        audience    = _cfg("content", "target_audience", default="")
        brand_color = _cfg("branding", "primary_color", default="")
        made = 0
        for i, post in enumerate(posts):
            if i in image_urls:
                continue  # 既に図解が付いている投稿はスキップ
            filename = f"{date_str}-{i+1}-eye.png"
            out_path = IMAGE_DIR / filename
            if ai_image.generate(post.get("theme", ""), post.get("text", ""),
                                 out_path, audience, brand_color):
                if GITHUB_REPOSITORY:
                    owner, repo = GITHUB_REPOSITORY.split("/", 1)
                    image_urls[i] = [f"https://{owner.lower()}.github.io/{repo}/post-images/{filename}"]
                else:
                    image_urls[i] = [str(out_path)]
                image_count += 1
                slide_count += 1
                made += 1
        if made:
            print(f"  AIアイキャッチ: {made}件を生成")

    print(f"  画像生成: {image_count}投稿 / 計{slide_count}枚")

    print("Notionに保存中...")
    saved = save_to_notion(posts, image_urls)
    print(f"  保存: {saved}件")

    print("LINE通知送信中...")
    send_line_notification(saved, image_count)

    _mode_label = "未投稿（このまま自動投稿されます）" if AUTO_APPROVE else "承認待ち"
    print(f"\n完了 — {saved}件の投稿案（図解付き {image_count}件）を「{_mode_label}」で保存しました")


if __name__ == "__main__":
    run()
