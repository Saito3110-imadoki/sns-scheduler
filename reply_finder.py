"""
リプ周り（他アカウントへの返信）文案の自動作成

自分の投稿だけでは露出は増えない。伸びている他アカウントの投稿に価値のある返信を
することで、その投稿の読者に見つけてもらう ── いわゆる「リプ周り」を半自動化する。

やること:
  1. ベンチマークアカウント／キーワードから、直近の伸びている投稿を集める
  2. それぞれに対する返信文案をClaudeで作成する
  3. Notionに「承認待ち」で保存する（返信先URLつき）

投稿は poster.py が行う。返信先URLが入っているレコードは、
Threadsには出さず、Xで対象投稿への返信としてのみ配信される。

安全側の設計:
  - config の reply_outreach.enabled が true のときだけ動作する（既定 false）
  - content.auto_approve が true でも、リプ周りだけは必ず「承認待ち」で保存する
    （他社の投稿に自動で絡むため、人の目を必ず通す）
  - 同じ投稿に二重に返信しないよう、Notionの既存レコードと突き合わせる

使い方:
  python sns_scheduler/reply_finder.py
  python sns_scheduler/reply_finder.py --dry-run   # Notionに保存せず文案だけ表示
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tweepy
import yaml
from dotenv import load_dotenv
from notion_client import Client

import anthropic

_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from notify import notify_error

load_dotenv()
JST = timezone(timedelta(hours=9))
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# ── config.yaml ───────────────────────────────────────────
_CFG: dict = {}
for _p in (_SCRIPT_DIR / "config.yaml", Path("config.yaml")):
    if _p.exists():
        with open(_p, encoding="utf-8") as f:
            _CFG = yaml.safe_load(f) or {}
        break


def _cfg(*keys, default=None):
    node = _CFG
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


NOTION_TOKEN       = os.environ["NOTION_TOKEN"].strip()
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"].strip()

PROP_TEXT      = _cfg("notion", "properties", "text",      default="投稿文")
PROP_DATETIME  = _cfg("notion", "properties", "datetime",  default="投稿日時")
PROP_PLATFORM  = _cfg("notion", "properties", "platform",  default="媒体")
PROP_STATUS    = _cfg("notion", "properties", "status",    default="ステータス")
PROP_POST_TYPE = _cfg("notion", "properties", "post_type", default="投稿タイプ")
PROP_REPLY_TO  = _cfg("notion", "properties", "reply_to",  default="返信先URL")

STATUS_PENDING_APPROVAL = _cfg("notion", "status", "pending", default="承認待ち")
POST_TYPE_OUTREACH      = "リプ周り"


# ── X から返信先候補を集める ──────────────────────────────
def fetch_reply_targets(max_targets: int = 5) -> list[dict]:
    """伸びている直近の投稿を、返信先の候補として集める。
    自分の投稿・リプライ・RTは除外し、エンゲージメント順の上位を返す。"""
    accounts = [str(a).lstrip("@") for a in
                (_cfg("reply_outreach", "accounts", default=None)
                 or _cfg("topics", "benchmark_accounts", default=[]) or [])]
    keywords = (_cfg("reply_outreach", "keywords", default=None)
                or _cfg("topics", "keywords_x", default=[]) or [])

    if not accounts and not keywords:
        print("  返信先の候補設定がありません"
              "（reply_outreach.accounts / keywords か topics.benchmark_accounts）")
        return []

    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_KEY_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )

    # 自分自身の投稿に返信しないよう、自アカウントのIDを控えておく
    me_id = ""
    try:
        me = client.get_me(user_auth=True)
        me_id = str(me.data.id) if me and me.data else ""
    except Exception as e:
        print(f"  自アカウント判定スキップ: {e}")

    def _search(query: str) -> list[dict]:
        out = []
        try:
            resp = client.search_recent_tweets(
                query=query, max_results=10,
                tweet_fields=["public_metrics", "text", "created_at", "author_id"],
                expansions=["author_id"], user_fields=["username"],
                sort_order="relevancy", user_auth=True,
            )
        except Exception as e:
            print(f"  検索スキップ（{query}）: {e}")
            return out
        users = {str(u.id): u.username
                 for u in ((resp.includes or {}).get("users") or [])}
        for tw in (resp.data or []):
            author = str(tw.author_id or "")
            if me_id and author == me_id:
                continue
            m = tw.public_metrics or {}
            out.append({
                "id":       str(tw.id),
                "text":     tw.text,
                "username": users.get(author, ""),
                "likes":    m.get("like_count", 0),
                "rts":      m.get("retweet_count", 0),
                "reps":     m.get("reply_count", 0),
            })
        return out

    collected: list[dict] = []
    for acct in accounts[:3]:
        collected.extend(_search(f"from:{acct} -is:retweet -is:reply"))
    for kw in list(keywords)[:3]:
        collected.extend(_search(f"{kw} lang:ja -is:retweet -is:reply"))

    seen, uniq = set(), []
    for t in collected:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        uniq.append(t)

    # 反応が多い投稿ほど、返信が読まれる人数も多い
    uniq.sort(key=lambda t: t["likes"] * 2 + t["reps"] * 3 + t["rts"] * 4,
              reverse=True)
    return uniq[:max_targets]


# ── すでに返信済みの投稿を除外 ────────────────────────────
def fetch_already_replied(notion: Client, days: int = 14) -> set[str]:
    """直近に作成したリプ周りレコードの返信先IDを集める（二重返信の防止）"""
    since = (datetime.now(JST) - timedelta(days=days)).isoformat()
    ids: set[str] = set()
    cursor = None
    while True:
        try:
            kwargs = {
                "database_id": NOTION_DATABASE_ID,
                "filter": {"property": PROP_DATETIME, "date": {"on_or_after": since}},
                "page_size": 100,
            }
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = notion.databases.query(**kwargs)
        except Exception as e:
            print(f"  返信済みチェックskip: {e}")
            return ids
        for page in resp.get("results", []):
            prop = page["properties"].get(PROP_REPLY_TO, {})
            url  = prop.get("url") if prop.get("type") == "url" else ""
            m    = re.search(r"/status/(\d+)", url or "")
            if m:
                ids.add(m.group(1))
        if not resp.get("has_more"):
            return ids
        cursor = resp.get("next_cursor")


# ── 返信文案の生成 ────────────────────────────────────────
def _parse_json_array(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  JSON解析エラー: {e}")
        return []


def generate_replies(targets: list[dict]) -> list[dict]:
    """各投稿に対する返信文案を作成する。件数は targets と同じ順序で返す"""
    audience = _cfg("content", "target_audience", default="")
    topics   = "、".join(_cfg("topics", "primary", default=[]) or [])
    max_len  = int(_cfg("reply_outreach", "max_chars", default=120) or 120)

    listed = "\n\n".join(
        f"[{i}] @{t['username']}（いいね{t['likes']} / リプ{t['reps']}）\n{t['text']}"
        for i, t in enumerate(targets))

    prompt = (
        "あなたはX運用に慣れた個人アカウントの中の人です。\n"
        "他の人の投稿に返信（リプライ）をして、その投稿の読者に見つけてもらうのが目的です。\n"
        f"あなたの発信テーマ: {topics}\n"
        f"あなたが届けたい相手: {audience}\n\n"
        f"以下の投稿それぞれに対する返信文案を1つずつ作ってください。\n\n{listed}\n\n"
        "【返信の鉄則】\n"
        f"- {max_len}文字以内。長い返信は読まれない\n"
        "- 相手の投稿を読んだ人が「この人の話も聞きたい」と思う内容にする\n"
        "- 共感だけで終わらせない。自分の実体験・具体的な数字・別の視点のどれかを必ず1つ足す\n"
        "- 相手を否定しない。マウンティング・訂正・上から目線は厳禁\n"
        "- 宣伝・リンク・プロフィール誘導は一切書かない\n"
        "- 「勉強になります」「その通りですね」等の中身のない定型文は禁止\n"
        "- ハッシュタグ・絵文字は使わない\n"
        "- 相手の投稿内容に実際に触れる（どの投稿にも使い回せる文は失格）\n\n"
        "以下のJSON形式のみを出力してください（説明文不要）。index は上の番号:\n"
        '[{"index":0,"reply":"返信文案","reason":"この返信を出す狙いを20文字以内"}]'
    )

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    out = []
    for item in _parse_json_array(raw):
        idx = item.get("index")
        text = (item.get("reply") or "").strip()
        if not isinstance(idx, int) or not (0 <= idx < len(targets)) or not text:
            continue
        out.append({"target": targets[idx], "text": text[:max_len],
                    "reason": (item.get("reason") or "").strip()})
    return out


# ── Notion 保存 ───────────────────────────────────────────
def save_to_notion(notion: Client, drafts: list[dict]) -> int:
    """リプ周りの文案を「承認待ち」で保存する。
    他社の投稿に絡むため、auto_approve が有効でも必ず承認待ちにする。"""
    now   = datetime.now(JST)
    saved = 0
    for i, d in enumerate(drafts):
        t   = d["target"]
        url = f"https://x.com/{t['username'] or 'i'}/status/{t['id']}"
        # 返信は鮮度が命なので、承認後すぐ配信されるよう直近の時刻を入れる
        when = now + timedelta(minutes=30 + i * 10)

        properties = {
            PROP_TEXT:      {"title": [{"text": {"content": d["text"]}}]},
            PROP_DATETIME:  {"date": {"start": when.isoformat()}},
            PROP_PLATFORM:  {"multi_select": [{"name": "X"}]},
            PROP_STATUS:    {"multi_select": [{"name": STATUS_PENDING_APPROVAL}]},
            PROP_REPLY_TO:  {"url": url},
            PROP_POST_TYPE: {"select": {"name": POST_TYPE_OUTREACH}},
        }
        optional = [PROP_POST_TYPE]

        try:
            for _ in range(len(optional) + 1):
                try:
                    notion.pages.create(
                        parent={"database_id": NOTION_DATABASE_ID},
                        properties=properties)
                    break
                except Exception as e:
                    removable = [p for p in optional
                                 if p in properties and p in str(e)]
                    if not removable:
                        raise
                    for p in removable:
                        print(f"  ※ プロパティ「{p}」が未作成のためスキップして保存します")
                        del properties[p]
            print(f"  保存: @{t['username']} へ「{d['text'][:32]}…」")
            saved += 1
        except Exception as e:
            print(f"  Notion保存エラー: {e}")
    return saved


def run(dry_run: bool = False) -> None:
    if not _cfg("reply_outreach", "enabled", default=False):
        print("リプ周りは無効です（config の reply_outreach.enabled を true にすると動きます）")
        return

    count = int(_cfg("reply_outreach", "count", default=5) or 5)
    print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST] リプ周り文案の作成開始")

    print("返信先の候補を検索中...")
    targets = fetch_reply_targets(max_targets=count * 2)
    print(f"  候補: {len(targets)}件")
    if not targets:
        return

    notion = Client(auth=NOTION_TOKEN)
    replied = fetch_already_replied(notion)
    targets = [t for t in targets if t["id"] not in replied][:count]
    print(f"  返信済みを除外: {len(targets)}件が対象")
    if not targets:
        print("  新しい返信先がありませんでした")
        return

    print("返信文案を生成中...")
    try:
        drafts = generate_replies(targets)
    except Exception as e:
        notify_error("リプ周り文案の生成（reply_finder.py）", str(e))
        print(f"生成に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  生成: {len(drafts)}件")

    if dry_run:
        for d in drafts:
            t = d["target"]
            print(f"\n  → @{t['username']}（いいね{t['likes']}）")
            print(f"    元投稿: {t['text'][:70]}")
            print(f"    返信案: {d['text']}")
            print(f"    狙い  : {d['reason']}")
        print("\n（--dry-run のためNotionには保存していません）")
        return

    saved = save_to_notion(notion, drafts)
    print(f"\n完了 — {saved}件を「承認待ち」で保存しました")
    print("Notionで内容を確認し、送ってよいものだけ「未投稿」に変更してください")


def main() -> None:
    ap = argparse.ArgumentParser(description="リプ周り文案の自動作成")
    ap.add_argument("--dry-run", action="store_true",
                    help="Notionに保存せず、文案を表示するだけ")
    args = ap.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
