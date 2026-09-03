"""
エンゲージメント低下の原因調査ツール

「最近いいねが付きづらい」と感じたときに、体感ではなく実データで
  ① いつ落ちたのか（週次推移と変化点）
  ② X側だけの問題か、コンテンツ自体の問題か（X と Threads の分離）
  ③ 何が変わったのが効いているのか（締め方・タイプ・画像・時刻などの前後比較）
を切り分ける。

使い方:
  python sns_scheduler/diagnose.py                  # 直近12週を分析
  python sns_scheduler/diagnose.py --weeks 8
  python sns_scheduler/diagnose.py --min-age-days 5 # 計測が育ちきった投稿だけで比較

必要な環境変数: NOTION_TOKEN / NOTION_DATABASE_ID（.env でも可）

重要な前提:
  analytics.py は毎日「投稿済」の全件を測り直すため、古い投稿ほど数字が
  育っている。投稿直後のものを混ぜると「最近は落ちている」と誤判定するので、
  --min-age-days より新しい投稿は比較から除外している。
"""

import argparse
import os
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()
JST = timezone(timedelta(hours=9))

# ── config.yaml ───────────────────────────────────────────
_config: dict = {}
for _p in (Path(__file__).resolve().parent / "config.yaml", Path("config.yaml")):
    if _p.exists():
        with open(_p, encoding="utf-8") as f:
            _config = yaml.safe_load(f) or {}
        break


def _cfg(*keys, default=None):
    node = _config
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


PROP_TEXT          = _cfg("notion", "properties", "text",          default="投稿文")
PROP_DATETIME      = _cfg("notion", "properties", "datetime",      default="投稿日時")
PROP_STATUS        = _cfg("notion", "properties", "status",        default="ステータス")
PROP_LIKES         = _cfg("notion", "properties", "likes",         default="いいね数")
PROP_RETWEETS      = _cfg("notion", "properties", "retweets",      default="RT数")
PROP_IMPRESSIONS   = _cfg("notion", "properties", "impressions",   default="インプレッション")
PROP_THREADS_LIKES = _cfg("notion", "properties", "threads_likes", default="Threadsいいね数")
PROP_THREADS_VIEWS = _cfg("notion", "properties", "threads_views", default="Threads閲覧数")
PROP_POST_TYPE     = _cfg("notion", "properties", "post_type",     default="投稿タイプ")
PROP_IMAGE_URL     = _cfg("notion", "properties", "image_url",     default="画像URL")
PROP_REPLY_TEXT    = _cfg("notion", "properties", "reply_text",    default="リプライ文面")
STATUS_DONE        = _cfg("notion", "status", "done", default="投稿済")


# ── Notionの値取り出し ────────────────────────────────────
def _num(page: dict, prop: str) -> int:
    return page["properties"].get(prop, {}).get("number") or 0


def _title(page: dict) -> str:
    return "".join(t.get("plain_text", "")
                   for t in page["properties"].get(PROP_TEXT, {}).get("title", []))


def _rich(page: dict, prop: str) -> str:
    return "".join(t.get("plain_text", "")
                   for t in page["properties"].get(prop, {}).get("rich_text", []))


def _url(page: dict, prop: str) -> str:
    return (page["properties"].get(prop, {}) or {}).get("url") or ""


def _type(page: dict) -> str:
    sel = page["properties"].get(PROP_POST_TYPE, {}).get("select") or {}
    return sel.get("name") or "未分類"


def _dt(page: dict):
    d = page["properties"].get(PROP_DATETIME, {}).get("date") or {}
    if not d.get("start"):
        return None
    try:
        return datetime.fromisoformat(d["start"]).astimezone(JST)
    except ValueError:
        return None


def _rate(likes: int, base: int) -> float:
    """反応率（いいね ÷ 表示、%）"""
    return round(likes / base * 100, 2) if base > 0 else 0.0


# ── 投稿の特徴量 ──────────────────────────────────────────
_Q_TAIL = 40  # 「締め」とみなす末尾の文字数

def _ends_with_question(text: str) -> bool:
    """締めが問いかけになっているか（末尾付近に ？ があるか）"""
    return "？" in text[-_Q_TAIL:] or "?" in text[-_Q_TAIL:]


def _hashtags(text: str) -> int:
    return len(re.findall(r"[#＃]\S+", text))


def _length_band(text: str) -> str:
    n = len(text)
    if n < 150:
        return "〜149字"
    if n < 250:
        return "150〜249字"
    if n < 350:
        return "250〜349字"
    return "350字〜"


def _features(page: dict) -> dict:
    text = _title(page)
    dt   = _dt(page)
    return {
        "dt":        dt,
        "text":      text,
        "type":      _type(page),
        "question":  "締めが問いかけ" if _ends_with_question(text) else "締めが問いかけでない",
        "image":     "画像あり" if (_url(page, PROP_IMAGE_URL) or "").strip() else "画像なし",
        "reply":     "セルフリプあり" if _rich(page, PROP_REPLY_TEXT).strip() else "セルフリプなし",
        "length":    _length_band(text),
        "hashtag":   f"タグ{_hashtags(text)}個",
        "hour":      f"{dt.hour:02d}時台" if dt else "不明",
        "weekday":   "月火水木金土日"[dt.weekday()] + "曜" if dt else "不明",
        "x_imp":     _num(page, PROP_IMPRESSIONS),
        "x_likes":   _num(page, PROP_LIKES),
        "x_rts":     _num(page, PROP_RETWEETS),
        "th_views":  _num(page, PROP_THREADS_VIEWS),
        "th_likes":  _num(page, PROP_THREADS_LIKES),
    }


# ── Notion取得 ────────────────────────────────────────────
def fetch_posts(notion: Client, db_id: str, start: datetime) -> list[dict]:
    results, cursor = [], None
    while True:
        kwargs = {
            "database_id": db_id,
            "filter": {"and": [
                {"property": PROP_STATUS, "multi_select": {"contains": STATUS_DONE}},
                {"property": PROP_DATETIME, "date": {"on_or_after": start.isoformat()}},
            ]},
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            return results
        cursor = resp.get("next_cursor")


# ── 集計 ──────────────────────────────────────────────────
def _agg(rows: list[dict]) -> dict:
    x_imp   = sum(r["x_imp"] for r in rows)
    x_likes = sum(r["x_likes"] for r in rows)
    th_v    = sum(r["th_views"] for r in rows)
    th_l    = sum(r["th_likes"] for r in rows)
    return {
        "n":        len(rows),
        "x_imp":    x_imp,
        "x_likes":  x_likes,
        "x_er":     _rate(x_likes, x_imp),
        "x_imp_pp": round(x_imp / len(rows), 1) if rows else 0.0,
        "x_lk_pp":  round(x_likes / len(rows), 2) if rows else 0.0,
        "th_views": th_v,
        "th_likes": th_l,
        "th_er":    _rate(th_l, th_v),
        "th_v_pp":  round(th_v / len(rows), 1) if rows else 0.0,
        "th_lk_pp": round(th_l / len(rows), 2) if rows else 0.0,
    }


def _delta(cur: float, prev: float) -> str:
    if prev == 0:
        return "  —  "
    d = (cur - prev) / prev * 100
    return f"{d:+6.0f}%"


def weekly_table(rows: list[dict]) -> list[tuple]:
    """週（月曜起点）ごとの推移"""
    buckets = defaultdict(list)
    for r in rows:
        monday = (r["dt"] - timedelta(days=r["dt"].weekday())).date()
        buckets[monday].append(r)
    return [(wk, _agg(buckets[wk])) for wk in sorted(buckets)]


def segment_compare(before: list[dict], after: list[dict], key: str) -> list[tuple]:
    """特徴量ごとに前期・後期を比較する。
    どのセグメントで落ちているかが分かると、原因の当たりが付けられる。"""
    b, a = defaultdict(list), defaultdict(list)
    for r in before:
        b[r[key]].append(r)
    for r in after:
        a[r[key]].append(r)
    out = []
    for name in sorted(set(b) | set(a)):
        out.append((name, _agg(b.get(name, [])), _agg(a.get(name, []))))
    return out


# ── 表示 ──────────────────────────────────────────────────
def _print_weekly(table: list[tuple]) -> None:
    print("\n■ 週次推移（月曜起点 / ERはいいね÷表示）")
    print("  週          本数 |    X表示  Xいいね   X_ER |  Th閲覧 Thいいね  Th_ER")
    print("  " + "-" * 74)
    for wk, s in table:
        print(f"  {wk}  {s['n']:>4} | {s['x_imp']:>8} {s['x_likes']:>8} "
              f"{s['x_er']:>6.2f}% | {s['th_views']:>7} {s['th_likes']:>8} {s['th_er']:>6.2f}%")


def _pad(s: str, width: int) -> str:
    """全角を2文字幅として左詰めする（等幅端末で列をそろえるため）"""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    return s + " " * max(0, width - w)


def _print_segment(title: str, seg: list[tuple], min_n: int = 3) -> None:
    print(f"\n■ {title}（前期 → 後期 / 1本あたり）")
    print("  区分                    前期n  Xいいね/本   後期n  Xいいね/本    変化 |"
          "  Thいいね/本 → 後期    変化")
    print("  " + "-" * 96)
    for name, b, a in seg:
        if b["n"] < min_n and a["n"] < min_n:
            continue
        print(f"  {_pad(name, 22)} {b['n']:>4}  {b['x_lk_pp']:>9.2f}   {a['n']:>4}  "
              f"{a['x_lk_pp']:>9.2f}  {_delta(a['x_lk_pp'], b['x_lk_pp'])} |"
              f"  {b['th_lk_pp']:>9.2f} → {a['th_lk_pp']:>7.2f}  {_delta(a['th_lk_pp'], b['th_lk_pp'])}")


def _measured(r: dict) -> bool:
    """計測値が入っているか。X・Threadsのどちらにも表示数がない行は
    「反応がなかった投稿」ではなく「計測されていない投稿」として扱う"""
    return r["x_imp"] > 0 or r["th_views"] > 0


def _print_health(rows: list[dict], now: datetime) -> list[dict]:
    """比較の前に、そもそもデータが正常かを確認する。
    投稿が止まっている・計測が抜けている場合は、前後比較より先にそちらが原因。
    戻り値は計測値のある行だけに絞ったリスト。"""
    print("\n■ データの健全性")
    if not rows:
        print("  投稿済のレコードが1件もありません")
        return []

    last     = rows[-1]["dt"]
    gap_days = (now.date() - last.date()).days
    print(f"  最終投稿日: {last.date()}（{gap_days}日前）")

    measured   = [r for r in rows if _measured(r)]
    unmeasured = len(rows) - len(measured)
    print(f"  計測済み  : {len(measured)}本 / 未計測 {unmeasured}本")

    if gap_days >= 3:
        print()
        print(f"  ⚠ {gap_days}日間、投稿済のレコードが増えていません。")
        print("     『いいねが付かない』の原因は文面ではなく、投稿自体が止まっていることです。")
        print("     次を順に確認してください:")
        print("       1. Actions の Auto Poster が失敗していないか（X APIのクレジット切れなど）")
        print("       2. Notionに「承認待ち」のまま溜まっていないか（未投稿に変更が必要）")
        print("       3. Notionに「エラー」ステータスの行が増えていないか")

    if unmeasured:
        print()
        print(f"  ※ 未計測の{unmeasured}本は、いいね0ではなく「数字が取れていない」投稿です。")
        print("     そのまま平均に入れると実力より低く出るため、以降の比較からは除外します。")
        print("     （--include-unmeasured を付けると含めて計算します）")
    return measured


def _verdict(before: dict, after: dict) -> None:
    print("\n■ 判定")
    x_reach_down  = after["x_imp_pp"] < before["x_imp_pp"] * 0.85
    x_er_down     = after["x_er"]     < before["x_er"]     * 0.85
    th_er_down    = after["th_er"]    < before["th_er"]    * 0.85
    th_reach_down = after["th_v_pp"]  < before["th_v_pp"]  * 0.85

    print(f"  X   : 表示/本 {before['x_imp_pp']}→{after['x_imp_pp']}"
          f"（{_delta(after['x_imp_pp'], before['x_imp_pp']).strip()}） / "
          f"ER {before['x_er']}%→{after['x_er']}%"
          f"（{_delta(after['x_er'], before['x_er']).strip()}）")
    print(f"  Th  : 閲覧/本 {before['th_v_pp']}→{after['th_v_pp']}"
          f"（{_delta(after['th_v_pp'], before['th_v_pp']).strip()}） / "
          f"ER {before['th_er']}%→{after['th_er']}%"
          f"（{_delta(after['th_er'], before['th_er']).strip()}）")
    print()

    # 同じ文面を両方に出しているので、片方だけ落ちていれば媒体側の問題。
    # 両方同時に落ちていれば文面そのものの問題と切り分けられる。
    if x_er_down and th_er_down:
        print("  → 両媒体でER低下。文面（コンテンツ）側の要因が濃厚です。")
        print("     下のセグメント比較で、どの条件の投稿が落ちたかを確認してください。")
    elif x_er_down and not th_er_down:
        print("  → XだけER低下。Threadsで同じ文面が反応を取れているなら、")
        print("     文面の問題ではなくX側（露出・アカウント状態）の問題です。")
    elif th_er_down and not x_er_down:
        print("  → ThreadsだけER低下。Threads側の露出／投稿時間帯を見直してください。")
    elif x_reach_down or th_reach_down:
        print("  → ERは維持、表示数が減少。いいねの総数が減った主因は『露出の減少』です。")
        print("     文面ではなくアカウント成長（フォロワー・リプライ・投稿時間）側の課題です。")
    else:
        print("  → 明確な低下は検出されませんでした（15%以上の悪化なし）。")
        print("     体感差の可能性、または母数不足です。--weeks を伸ばして再実行してください。")

    if after["x_imp_pp"] > 0 and after["x_imp_pp"] < 100:
        print(f"\n  ※ Xの表示数が1本あたり{after['x_imp_pp']}と非常に少ないため、")
        print("     いいね数の増減はほぼ誤差の範囲です。まず露出（フォロワー・リプライ）を")
        print("     増やさない限り、文面の改善は数字に表れません。")


def run(weeks: int, min_age_days: int, include_unmeasured: bool = False) -> None:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    db_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not token or not db_id:
        print("NOTION_TOKEN / NOTION_DATABASE_ID が設定されていません（.env か環境変数）")
        sys.exit(1)

    now    = datetime.now(JST)
    start  = now - timedelta(weeks=weeks)
    cutoff = now - timedelta(days=min_age_days)

    print(f"■ 対象: {start.date()} 〜 {now.date()}（直近{weeks}週）")
    print(f"  ※ 計測が育ちきっていない直近{min_age_days}日の投稿は比較から除外します")

    notion = Client(auth=token)
    pages  = fetch_posts(notion, db_id, start)
    rows   = [f for f in (_features(p) for p in pages) if f["dt"]]
    rows.sort(key=lambda r: r["dt"])
    mature = [r for r in rows if r["dt"] <= cutoff]

    print(f"  取得: {len(rows)}本（うち比較対象 {len(mature)}本）")

    _print_weekly(weekly_table(rows))
    measured = _print_health(rows, now)
    if not include_unmeasured:
        mature = [r for r in mature if _measured(r)]

    if len(mature) < 10:
        print(f"\n  比較できる投稿が{len(mature)}本しかありません。"
              "--weeks を伸ばして再実行してください。")
        return

    # 前期・後期を同じ本数で二分し、条件をそろえて比較する
    half   = len(mature) // 2
    before, after = mature[:half], mature[half:]
    print(f"\n  前期: {before[0]['dt'].date()}〜{before[-1]['dt'].date()}（{len(before)}本）")
    print(f"  後期: {after[0]['dt'].date()}〜{after[-1]['dt'].date()}（{len(after)}本）")

    _verdict(_agg(before), _agg(after))

    for key, title in [
        ("question", "締め方別（問いかけで終わるか）"),
        ("type",     "投稿タイプ別"),
        ("image",    "画像の有無別"),
        ("length",   "文字数帯別"),
        ("hour",     "投稿時刻別"),
        ("weekday",  "曜日別"),
        ("hashtag",  "ハッシュタグ数別"),
        ("reply",    "セルフリプライの有無別"),
    ]:
        _print_segment(title, segment_compare(before, after, key))

    print("\n■ 反応が取れた投稿 / 取れなかった投稿（後期）")
    ranked = sorted(after, key=lambda r: -(r["x_likes"] + r["th_likes"]))
    for label, subset in [("上位3本", ranked[:3]), ("下位3本", ranked[-3:])]:
        print(f"  【{label}】")
        for r in subset:
            print(f"    {r['dt'].date()} X{r['x_likes']}いいね/{r['x_imp']}表示 "
                  f"Th{r['th_likes']}いいね/{r['th_views']}閲覧 [{r['type']}] "
                  f"{r['text'][:38]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="エンゲージメント低下の原因調査")
    ap.add_argument("--weeks", type=int, default=12, help="分析対象の週数（既定: 12）")
    ap.add_argument("--min-age-days", type=int, default=3,
                    help="計測が育ちきったとみなす日数。これより新しい投稿は比較から除外（既定: 3）")
    ap.add_argument("--include-unmeasured", action="store_true",
                    help="表示数が記録されていない投稿も、いいね0として比較に含める")
    args = ap.parse_args()
    run(args.weeks, args.min_age_days, args.include_unmeasured)


if __name__ == "__main__":
    main()
