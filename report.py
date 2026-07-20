"""
週次・月次レポート（PDF/画像つき）
Notionの投稿実績を集計し、
  ① 見やすいレポート（PDF + 画像）を生成してリポジトリ(GitHub Pages)に保存
  ② LINEへ「サマリー文 + レポート画像 + PDFリンク」を配信
する。分析コメントとネクストアクションはClaudeが自動生成する。

使い方:
  python sns_scheduler/report.py --mode weekly  --phase render   # 集計+PDF/PNG生成
  python sns_scheduler/report.py --mode weekly  --phase send     # (push後) LINE配信
  python sns_scheduler/report.py --mode weekly                   # render+send を連続実行
  python sns_scheduler/report.py --demo                          # モックデータで描画テスト

備考:
  - LINEはPDFの直接添付に非対応のため、レポート画像(PNG)をトークに表示し、
    PDFはGitHub PagesのURLをリンクとして送る設計。
  - 年齢・性別はThreads APIのfollower_demographicsを使用（要: insights権限、
    フォロワー100名以上）。取得できない場合は自動で省略する。Xは仕様上非公開。
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from notify import push_line_messages

load_dotenv()
JST = timezone(timedelta(hours=9))

REPORT_DIR   = Path("reports")
PENDING_FILE = REPORT_DIR / "_pending.json"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

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
STATUS_DONE        = _cfg("notion", "status", "done", default="投稿済")
COMPANY            = _cfg("company", "name", default="")
PRIMARY            = _cfg("branding", "primary_color", default="#4f46e5")
ACCENT             = _cfg("branding", "accent_color",  default="#ea580c")
# 月間KPI目標（config.yamlの report.monthly_goal_impressions。0なら非表示）
GOAL_IMP           = int(_cfg("report", "monthly_goal_impressions", default=0) or 0)

WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]


# ══════════════════════════════════════════════════════════
#  データ収集
# ══════════════════════════════════════════════════════════
def _num(page: dict, prop: str) -> int:
    v = page["properties"].get(prop, {}).get("number")
    return int(v) if v else 0


def _text_head(page: dict, limit: int = 34) -> str:
    prop = page["properties"].get(PROP_TEXT, {})
    parts = prop.get("title") or prop.get("rich_text") or []
    text = "".join(p.get("plain_text", "") for p in parts).replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _type(page: dict) -> str:
    sel = page["properties"].get(PROP_POST_TYPE, {}).get("select") or {}
    return sel.get("name") or "その他"


def _er(likes: int, imp: int) -> float:
    """エンゲージメント率（いいね÷表示、%）"""
    return round(likes / imp * 100, 1) if imp > 0 else 0.0


def _dt(page: dict) -> datetime | None:
    d = page["properties"].get(PROP_DATETIME, {}).get("date") or {}
    if d.get("start"):
        try:
            return datetime.fromisoformat(d["start"]).astimezone(JST)
        except ValueError:
            return None
    return None


def fetch_posts(notion, db_id: str, start: datetime, end: datetime) -> list[dict]:
    results, cursor = [], None
    while True:
        kwargs = {
            "database_id": db_id,
            "filter": {"and": [
                {"property": PROP_STATUS, "multi_select": {"contains": STATUS_DONE}},
                {"property": PROP_DATETIME, "date": {"on_or_after": start.isoformat()}},
                {"property": PROP_DATETIME, "date": {"before": end.isoformat()}},
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


def summarize(posts: list[dict]) -> dict:
    return {
        "count":    len(posts),
        "imp":      sum(_num(p, PROP_IMPRESSIONS) for p in posts),
        "likes":    sum(_num(p, PROP_LIKES) for p in posts),
        "rts":      sum(_num(p, PROP_RETWEETS) for p in posts),
        "th_views": sum(_num(p, PROP_THREADS_VIEWS) for p in posts),
        "th_likes": sum(_num(p, PROP_THREADS_LIKES) for p in posts),
    }


def build_ranking(posts: list[dict], top: int = 5) -> list[dict]:
    ranked = sorted(posts, key=lambda p: _num(p, PROP_IMPRESSIONS), reverse=True)
    out = []
    for p in ranked[:top]:
        imp, likes = _num(p, PROP_IMPRESSIONS), _num(p, PROP_LIKES)
        out.append({
            "head":  _text_head(p),
            "type":  _type(p),
            "imp":   imp,
            "likes": likes,
            "rts":   _num(p, PROP_RETWEETS),
            "er":    _er(likes, imp),
            "dt":    (_dt(p).strftime("%-m/%-d %H:%M") if _dt(p) else "-"),
        })
    return out


def build_type_analysis(posts: list[dict]) -> list[dict]:
    """投稿タイプ別の本数・平均表示・平均ERを集計（このアカウントの勝ちパターン分析）"""
    groups: dict[str, list[dict]] = {}
    for p in posts:
        imp = _num(p, PROP_IMPRESSIONS)
        if imp <= 0:
            continue
        groups.setdefault(_type(p), []).append(
            {"imp": imp, "likes": _num(p, PROP_LIKES)})
    out = []
    for label, items in groups.items():
        total_imp   = sum(i["imp"] for i in items)
        total_likes = sum(i["likes"] for i in items)
        out.append({
            "label":   label,
            "count":   len(items),
            "avg_imp": total_imp // len(items),
            "avg_er":  _er(total_likes, total_imp),
        })
    return sorted(out, key=lambda x: -x["avg_imp"])


def build_patterns(posts: list[dict]) -> dict:
    """曜日別・時間帯別の平均インプレッション（投稿設計の分析用）"""
    by_wd: dict[int, list[int]] = {}
    by_slot: dict[str, list[int]] = {}
    slots = [(0, 11, "午前"), (11, 15, "昼"), (15, 19, "夕方"), (19, 24, "夜")]
    for p in posts:
        dt = _dt(p)
        imp = _num(p, PROP_IMPRESSIONS)
        if not dt or imp <= 0:
            continue
        by_wd.setdefault(dt.weekday(), []).append(imp)
        for s, e, name in slots:
            if s <= dt.hour < e:
                by_slot.setdefault(name, []).append(imp)
    wd = [{"label": WEEKDAYS_JA[k] + "曜", "avg": sum(v) // len(v)}
          for k, v in sorted(by_wd.items())]
    slot = [{"label": name, "avg": sum(by_slot[name]) // len(by_slot[name])}
            for _, _, name in slots if name in by_slot]
    return {"weekday": wd, "timeslot": slot}


def fetch_threads_audience() -> dict:
    """Threadsのフォロワー数と年齢・性別分布。取得不可の項目は含めない。"""
    token   = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("THREADS_USER_ID", "").strip()
    out: dict = {}
    if not token or not user_id:
        return out
    base = f"https://graph.threads.net/v1.0/{user_id}/threads_insights"

    def _breakdown(kind: str) -> list[dict]:
        r = requests.get(base, params={
            "metric": "follower_demographics", "breakdown": kind,
            "access_token": token}, timeout=30)
        r.raise_for_status()
        items = []
        for m in r.json().get("data", []):
            tv = m.get("total_value") or {}
            for bd in tv.get("breakdowns", []):
                for res in bd.get("results", []):
                    dims = res.get("dimension_values") or ["?"]
                    items.append({"label": dims[0], "value": int(res.get("value", 0))})
        return sorted(items, key=lambda x: -x["value"])

    try:
        r = requests.get(base, params={"metric": "followers_count",
                                       "access_token": token}, timeout=30)
        r.raise_for_status()
        for m in r.json().get("data", []):
            tv = m.get("total_value") or {}
            if "value" in tv:
                out["followers"] = int(tv["value"])
    except Exception as e:
        print(f"  Threadsフォロワー数スキップ: {e}")
    for kind, key in (("age", "age"), ("gender", "gender")):
        try:
            data = _breakdown(kind)
            if data:
                out[key] = data
        except Exception as e:
            print(f"  Threads {kind}分布スキップ（権限/フォロワー100名未満の可能性）: {e}")
    return out


# ══════════════════════════════════════════════════════════
#  AI分析（Claude）
# ══════════════════════════════════════════════════════════
def ai_insights(data: dict) -> dict:
    """分析・勝ち/負けパターン・テーマ案・ネクストアクションを生成。失敗時はルールベース。
    週次は高速モデル、月次はより深い分析のため上位モデルを使う。"""
    fallback = _rule_based_insights(data)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return fallback
    model = "claude-sonnet-5" if data["mode"] == "monthly" else CLAUDE_MODEL
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        # 全投稿の文脈（内容・タイプ・タイミング・成績）を渡して固有の示唆を引き出す
        post_lines = [
            f'- [{p["type"]}] {p["dt"]}「{p["head"]}」表示{p["imp"]} いいね{p["likes"]} ER{p["er"]}%'
            for p in data["all_posts"][:60]
        ]
        compact = {
            "期間": data["period_label"], "今期合計": data["cur"], "前期合計": data["prev"],
            "タイプ別成績": data["type_analysis"],
            "曜日別平均表示": data["patterns"]["weekday"],
            "時間帯別平均表示": data["patterns"]["timeslot"],
        }
        prompt = (
            "あなたはSNS運用のプロコンサルタント兼編集者です。"
            f"クライアント（{COMPANY}）のX/Threads運用実績を分析し、"
            "レポートに載せる示唆を日本語で出してください。\n\n"
            f"■集計データ:\n{json.dumps(compact, ensure_ascii=False)}\n\n"
            "■期間内の全投稿（タイプ／投稿日時／冒頭／成績）:\n" + "\n".join(post_lines) + "\n\n"
            "出力は次のJSONのみ（前置き・コードブロック不要）:\n"
            "{\n"
            ' "analysis": ["所見1", "所見2", "所見3"],\n'
            ' "win_pattern": "このアカウントで伸びる投稿の共通点（60字程度・数字を根拠に）",\n'
            ' "lose_pattern": "伸びなかった投稿の共通点と改善方向（60字程度）",\n'
            ' "theme_ideas": [{"title": "来週の投稿タイトル案", "reason": "この案が伸びる根拠（30字）"},'
            ' {"title": "...", "reason": "..."}, {"title": "...", "reason": "..."}],\n'
            ' "next_actions": [{"title": "短い見出し", "detail": "来週すぐ実行できる具体策（40字程度）"},'
            ' {"title": "...", "detail": "..."}, {"title": "...", "detail": "..."}]\n'
            "}\n"
            "条件: 実データの数字・実在の投稿を根拠に語る／一般論やどのアカウントにも言える話は禁止／"
            "theme_ideasは実際に伸びた投稿の系統から発想し、タイトルはそのまま使える具体性にする／"
            "専門用語を避ける／断定しすぎない"
        )
        msg = client.messages.create(model=model, max_tokens=1600,
                                     messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        parsed = json.loads(m.group(0)) if m else {}
        if parsed.get("analysis") and parsed.get("next_actions"):
            print(f"  AI分析: 生成完了（{model}）")
            return parsed
    except Exception as e:
        print(f"  AI分析スキップ（ルールベースで代替）: {e}")
    return fallback


def _rule_based_insights(data: dict) -> dict:
    cur, prev = data["cur"], data["prev"]
    analysis = []
    if prev["imp"] > 0:
        diff = round((cur["imp"] - prev["imp"]) / prev["imp"] * 100)
        trend = "伸びています" if diff >= 0 else "落ち着いています"
        analysis.append(f"表示回数は前期間比{'+' if diff >= 0 else ''}{diff}%と{trend}。")
    if data["patterns"]["weekday"]:
        best = max(data["patterns"]["weekday"], key=lambda x: x["avg"])
        analysis.append(f"{best['label']}の投稿が平均{best['avg']:,}表示と最も反応が良い傾向です。")
    if data["ranking"]:
        analysis.append("上位投稿は具体的な数字・実例を含むものが中心でした。")
    win = ""
    if data.get("type_analysis"):
        t = data["type_analysis"][0]
        win = f"「{t['label']}」の投稿が平均{t['avg_imp']:,}表示と最も好調です。"
    return {
        "analysis": analysis or ["データが蓄積され次第、傾向を分析します。"],
        "win_pattern": win,
        "lose_pattern": "",
        "theme_ideas": [],
        "next_actions": [
            {"title": "好調テーマの深掘り", "detail": "ベスト投稿と同系統のテーマを来週2本追加する"},
            {"title": "反応の良い曜日に厚めに", "detail": "平均表示が高い曜日に注力テーマを配置する"},
            {"title": "承認ペースの維持", "detail": "投稿の承認漏れがないよう毎日のチェックを継続する"},
        ],
    }


# ══════════════════════════════════════════════════════════
#  レポートHTML → PDF/PNG
# ══════════════════════════════════════════════════════════
def _pct_badge(cur: int, prev: int) -> str:
    if prev <= 0:
        return ""
    diff = round((cur - prev) / prev * 100)
    cls = "up" if diff >= 0 else "down"
    return f'<span class="delta {cls}">{"+" if diff >= 0 else ""}{diff}%</span>'


def _bars(items: list[dict], value_key: str = "value", suffix: str = "人") -> str:
    if not items:
        return ""
    mx = max(i[value_key] for i in items) or 1
    rows = []
    for i in items:
        w = round(i[value_key] / mx * 100)
        rows.append(
            f'<div class="bar-row"><span class="bar-label">{i["label"]}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{w}%"></span></span>'
            f'<span class="bar-val">{i[value_key]:,}{suffix}</span></div>')
    return "".join(rows)


def build_html(data: dict, ins: dict) -> str:
    cur, prev = data["cur"], data["prev"]
    title = "週間レポート" if data["mode"] == "weekly" else "月間レポート"
    aud = data.get("audience", {})

    rank_rows = "".join(
        f'<tr><td class="rk">{i+1}</td>'
        f'<td class="head-t">{r["head"]}<div class="dt">{r["dt"]}　<span class="typetag">{r["type"]}</span></div></td>'
        f'<td class="n">{r["imp"]:,}</td><td class="n">{r["likes"]:,}</td>'
        f'<td class="n">{r["rts"]:,}</td><td class="n er">{r["er"]}%</td></tr>'
        for i, r in enumerate(data["ranking"]))

    # KPIゲージ（月間目標が設定されているときのみ）
    gauge_html = ""
    if data.get("goal_imp") and data.get("goal_progress") is not None:
        goal, prog = data["goal_imp"], data["goal_progress"]
        pct_v = min(100, round(prog / goal * 100)) if goal else 0
        gauge_label = "今月の進捗" if data["mode"] == "weekly" else "月間目標の達成度"
        gauge_html = f"""
  <div class="gauge">
    <div class="g-head"><span>🎯 {gauge_label}</span>
      <span class="g-num">{prog:,} / {goal:,} 表示（{pct_v}%）</span></div>
    <div class="g-track"><span class="g-fill" style="width:{pct_v}%"></span></div>
  </div>"""

    # 投稿タイプ別分析
    type_html = ""
    if data.get("type_analysis"):
        rows = "".join(
            f'<tr><td class="head-t">{t["label"]}</td><td class="n">{t["count"]}本</td>'
            f'<td class="n">{t["avg_imp"]:,}</td><td class="n er">{t["avg_er"]}%</td></tr>'
            for t in data["type_analysis"])
        best = data["type_analysis"][0]
        type_html = f"""
  <h2>投稿タイプ別の成績</h2>
  <table class="rank">
    <thead><tr><th>タイプ</th><th style="text-align:right;">本数</th>
    <th style="text-align:right;">平均表示</th><th style="text-align:right;">平均ER</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <p class="typenote">→ このアカウントは<b>「{best["label"]}」が平均{best["avg_imp"]:,}表示</b>と最も反応が良い傾向です。</p>"""

    audience_html = ""
    if aud.get("age") or aud.get("gender"):
        cols = ""
        if aud.get("age"):
            cols += f'<div class="aud-col"><h4>年齢層（Threads）</h4>{_bars(aud["age"])}</div>'
        if aud.get("gender"):
            g = [{"label": {"M": "男性", "F": "女性", "U": "不明"}.get(x["label"], x["label"]),
                  "value": x["value"]} for x in aud["gender"]]
            cols += f'<div class="aud-col"><h4>性別（Threads）</h4>{_bars(g)}</div>'
        audience_html = f'<div class="aud-grid">{cols}</div>'
    else:
        audience_html = ('<p class="aud-note">年齢・性別の分布は、Threadsフォロワーが100名に到達すると'
                         '表示されます（X APIは属性データを提供していません）。</p>')

    followers_html = ""
    if aud.get("followers") is not None:
        followers_html = f'<div class="kpi"><div class="kv">{aud["followers"]:,}</div><div class="kl">Threadsフォロワー</div></div>'

    pat = data["patterns"]
    pattern_html = ""
    if pat["weekday"] or pat["timeslot"]:
        c = ""
        if pat["weekday"]:
            c += f'<div class="aud-col"><h4>曜日別 平均表示</h4>{_bars([{"label": w["label"], "value": w["avg"]} for w in pat["weekday"]], suffix="")}</div>'
        if pat["timeslot"]:
            c += f'<div class="aud-col"><h4>時間帯別 平均表示</h4>{_bars([{"label": t["label"], "value": t["avg"]} for t in pat["timeslot"]], suffix="")}</div>'
        pattern_html = f'<div class="aud-grid">{c}</div>'

    analysis_html = "".join(f"<li>{a}</li>" for a in ins["analysis"])
    actions_html = "".join(
        f'<div class="action"><div class="a-num">{i+1}</div>'
        f'<div><div class="a-t">{a["title"]}</div><div class="a-d">{a["detail"]}</div></div></div>'
        for i, a in enumerate(ins["next_actions"]))

    # 勝ち/負けパターン
    winlose_html = ""
    if ins.get("win_pattern") or ins.get("lose_pattern"):
        cells = ""
        if ins.get("win_pattern"):
            cells += (f'<div class="wl win"><div class="wl-t">📈 伸びる投稿の共通点</div>'
                      f'<p>{ins["win_pattern"]}</p></div>')
        if ins.get("lose_pattern"):
            cells += (f'<div class="wl lose"><div class="wl-t">📉 伸び悩みの傾向と改善</div>'
                      f'<p>{ins["lose_pattern"]}</p></div>')
        winlose_html = f'<div class="wl-grid">{cells}</div>'

    # 来週のテーマ案
    themes_html = ""
    if ins.get("theme_ideas"):
        items = "".join(
            f'<div class="theme"><div class="th-t">💡「{t["title"]}」</div>'
            f'<div class="th-r">{t.get("reason", "")}</div></div>'
            for t in ins["theme_ideas"][:3])
        themes_html = f'<h2>来週の投稿テーマ案（AI提案）</h2><div class="themes">{items}</div>'

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><style>
:root{{--p:{PRIMARY};--a:{ACCENT};--ink:#12172b;--body:#3b415c;--muted:#6d7390;
--line:#e3e6f0;--soft:#f6f7fb;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Noto Sans JP','Hiragino Sans',sans-serif;color:var(--body);
width:794px;background:#fff;-webkit-font-smoothing:antialiased;font-feature-settings:"palt";}}
.page{{width:794px;min-height:1123px;padding:48px 52px;position:relative;page-break-after:always;}}
.page:last-child{{page-break-after:auto;}}
.head{{display:flex;justify-content:space-between;align-items:flex-start;
border-bottom:3px solid var(--p);padding-bottom:18px;}}
.head .t1{{font-size:13px;font-weight:800;color:var(--p);letter-spacing:.14em;}}
.head h1{{font-size:30px;font-weight:900;color:var(--ink);margin-top:4px;}}
.head .period{{font-size:14px;color:var(--muted);margin-top:4px;}}
.head .corp{{text-align:right;font-size:13px;font-weight:800;color:var(--ink);}}
.head .corp small{{display:block;font-weight:500;color:var(--muted);font-size:11px;margin-top:2px;}}
h2{{font-size:17px;font-weight:900;color:var(--ink);margin:26px 0 12px;
display:flex;align-items:center;gap:9px;}}
h2::before{{content:"";width:8px;height:20px;border-radius:4px;background:var(--p);}}
.kpis{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;}}
.kpi{{border:1px solid var(--line);border-radius:14px;padding:13px 18px;background:var(--soft);}}
.kpi .kv{{font-size:27px;font-weight:900;color:var(--ink);font-variant-numeric:tabular-nums;letter-spacing:-.01em;}}
.kpi .kl{{font-size:11.5px;color:var(--muted);font-weight:700;margin-top:3px;}}
.delta{{font-size:13px;font-weight:900;margin-left:7px;}}
.delta.up{{color:#047857;}}.delta.down{{color:#b45309;}}
table.rank{{width:100%;border-collapse:collapse;font-size:13px;}}
.rank th{{text-align:left;font-size:11px;color:var(--muted);font-weight:800;
letter-spacing:.05em;padding:7px 10px;border-bottom:2px solid var(--ink);}}
.rank td{{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top;}}
.rank .rk{{font-size:17px;font-weight:900;color:var(--p);width:34px;}}
.rank .head-t{{font-weight:700;color:var(--ink);line-height:1.6;}}
.rank .dt{{font-size:10.5px;color:var(--muted);font-weight:500;margin-top:2px;}}
.rank .n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}}
.rank th:nth-child(n+3){{text-align:right;}}
.aud-grid{{display:grid;grid-template-columns:1fr 1fr;gap:26px;}}
.aud-col h4{{font-size:13px;font-weight:900;color:var(--ink);margin-bottom:10px;}}
.bar-row{{display:flex;align-items:center;gap:9px;margin-bottom:7px;}}
.bar-label{{width:64px;font-size:12px;font-weight:700;color:var(--ink);flex-shrink:0;}}
.bar-track{{flex:1;height:14px;background:var(--soft);border-radius:7px;overflow:hidden;}}
.bar-fill{{display:block;height:100%;background:var(--p);border-radius:7px 4px 4px 7px;}}
.bar-val{{width:70px;font-size:11.5px;color:var(--muted);text-align:right;
font-variant-numeric:tabular-nums;flex-shrink:0;}}
.aud-note{{font-size:12.5px;color:var(--muted);background:var(--soft);
border:1px solid var(--line);border-radius:10px;padding:13px 16px;line-height:1.8;}}
ul.analysis{{list-style:none;display:flex;flex-direction:column;gap:9px;}}
ul.analysis li{{font-size:13.5px;line-height:1.8;background:var(--soft);
border:1px solid var(--line);border-radius:10px;padding:12px 16px;position:relative;padding-left:38px;}}
ul.analysis li::before{{content:"💡";position:absolute;left:13px;top:11px;}}
.actions{{display:flex;flex-direction:column;gap:10px;}}
.action{{display:flex;gap:13px;align-items:flex-start;border:1px solid var(--line);
border-radius:12px;padding:14px 16px;}}
.action .a-num{{width:28px;height:28px;border-radius:9px;background:var(--a);color:#fff;
font-weight:900;font-size:14px;display:flex;align-items:center;justify-content:center;flex-shrink:0;}}
.action .a-t{{font-size:14px;font-weight:900;color:var(--ink);}}
.action .a-d{{font-size:12.5px;margin-top:2px;line-height:1.7;}}
.foot{{position:absolute;bottom:26px;left:52px;right:52px;display:flex;
justify-content:space-between;font-size:10px;color:#9aa0b8;
border-top:1px solid var(--line);padding-top:10px;}}
.gauge{{margin-top:16px;border:1px solid var(--line);border-radius:14px;padding:14px 18px;background:var(--soft);}}
.g-head{{display:flex;justify-content:space-between;font-size:12.5px;font-weight:800;color:var(--ink);margin-bottom:8px;}}
.g-num{{font-variant-numeric:tabular-nums;color:var(--p);}}
.g-track{{height:14px;border-radius:7px;background:#e6e8f4;overflow:hidden;}}
.g-fill{{display:block;height:100%;background:linear-gradient(90deg,var(--p),var(--a));border-radius:7px;}}
.typetag{{display:inline-block;font-size:9.5px;font-weight:800;color:var(--p);
background:#eef0fd;border-radius:999px;padding:1px 8px;}}
.typenote{{font-size:12.5px;margin-top:10px;color:var(--body);}}
.typenote b{{color:var(--ink);}}
.rank .er{{color:var(--a);font-weight:800;}}
.wl-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px;}}
.wl{{border-radius:12px;padding:14px 16px;font-size:12.5px;line-height:1.75;}}
.wl.win{{background:#ecfdf5;border:1px solid #a7f3d0;}}
.wl.lose{{background:#fff7ed;border:1px solid #fed7aa;}}
.wl-t{{font-size:13px;font-weight:900;color:var(--ink);margin-bottom:5px;}}
.themes{{display:flex;flex-direction:column;gap:9px;}}
.theme{{border:1px solid var(--line);border-left:4px solid var(--p);border-radius:10px;padding:11px 15px;}}
.th-t{{font-size:13.5px;font-weight:900;color:var(--ink);}}
.th-r{{font-size:11.5px;color:var(--muted);margin-top:2px;}}
</style></head><body>

<div class="page">
  <div class="head">
    <div><div class="t1">SNS AUTO-PILOT REPORT</div><h1>📊 {title}</h1>
      <div class="period">{data["period_label"]}</div></div>
    <div class="corp">{COMPANY}<small>Powered by PostPilot</small></div>
  </div>

  <h2>サマリー</h2>
  <div class="kpis">
    <div class="kpi"><div class="kv">{cur["count"]}<small style="font-size:14px;">件</small></div><div class="kl">投稿数</div></div>
    <div class="kpi"><div class="kv">{cur["imp"]:,}{_pct_badge(cur["imp"], prev["imp"])}</div><div class="kl">𝕏 表示回数</div></div>
    <div class="kpi"><div class="kv">{cur["likes"]:,}{_pct_badge(cur["likes"], prev["likes"])}</div><div class="kl">𝕏 いいね</div></div>
    <div class="kpi"><div class="kv">{cur["rts"]:,}</div><div class="kl">𝕏 リポスト</div></div>
    <div class="kpi"><div class="kv">{cur["th_views"]:,}{_pct_badge(cur["th_views"], prev["th_views"])}</div><div class="kl">Threads 閲覧</div></div>
    {followers_html or f'<div class="kpi"><div class="kv">{cur["th_likes"]:,}</div><div class="kl">Threads いいね</div></div>'}
  </div>
  {gauge_html}

  <h2>投稿ランキング TOP{len(data["ranking"])}</h2>
  <table class="rank">
    <thead><tr><th></th><th>投稿</th><th>表示</th><th>いいね</th><th>RP</th><th>ER</th></tr></thead>
    <tbody>{rank_rows or '<tr><td colspan="6" style="text-align:center;color:var(--muted);">対象期間の投稿がありません</td></tr>'}</tbody>
  </table>
  {type_html}
  <div class="foot"><span>{COMPANY} — {title}</span><span>1 / 2</span></div>
</div>

<div class="page">
  <div class="head">
    <div><div class="t1">AUDIENCE & INSIGHTS</div><h1>オーディエンス と 分析</h1>
      <div class="period">{data["period_label"]}</div></div>
    <div class="corp">{COMPANY}<small>Powered by PostPilot</small></div>
  </div>

  <h2>フォロワー属性</h2>
  {audience_html}

  <h2>反応が良いタイミング</h2>
  {pattern_html or '<p class="aud-note">データが蓄積され次第、曜日・時間帯の傾向を表示します。</p>'}

  <h2>AI分析コメント</h2>
  {winlose_html}
  <ul class="analysis">{analysis_html}</ul>

  {themes_html}

  <h2>ネクストアクション（来週やること）</h2>
  <div class="actions">{actions_html}</div>
  <div class="foot"><span>{COMPANY} — {title}</span><span>2 / 2</span></div>
</div>
</body></html>"""


def render_files(html: str, base: Path) -> tuple[Path, Path]:
    from playwright.sync_api import sync_playwright
    pdf_path = base.with_suffix(".pdf")
    png_path = base.with_suffix(".png")
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM", "") or None
    with sync_playwright() as p:
        kw = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if exe:
            kw["executable_path"] = exe
        b = p.chromium.launch(**kw)
        pg = b.new_page(viewport={"width": 794, "height": 1123}, device_scale_factor=2)
        pg.set_content(html, wait_until="networkidle")
        pg.screenshot(path=str(png_path), full_page=True)
        pg.pdf(path=str(pdf_path), format="A4", print_background=True,
               margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        b.close()
    return pdf_path, png_path


# ══════════════════════════════════════════════════════════
#  メイン
# ══════════════════════════════════════════════════════════
def calc_period(mode: str, now: datetime):
    if mode == "weekly":
        end   = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=7)
        return start, end, start - timedelta(days=7), start
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = (first_this - timedelta(days=1)).replace(day=1)
    return start, first_this, (start - timedelta(days=1)).replace(day=1), start


def gather(mode: str) -> dict:
    from notion_client import Client
    now = datetime.now(JST)
    start, end, pstart, pend = calc_period(mode, now)
    notion = Client(auth=os.environ["NOTION_TOKEN"])
    db_id  = os.environ["NOTION_DATABASE_ID"]
    posts  = fetch_posts(notion, db_id, start, end)
    prev   = fetch_posts(notion, db_id, pstart, pend)
    label  = f"{start.strftime('%Y/%-m/%-d')} 〜 {(end - timedelta(days=1)).strftime('%-m/%-d')}"

    # KPIゲージ用: 対象月の月初からの累計表示（週次=当月進捗、月次=前月実績）
    goal_progress = None
    if GOAL_IMP > 0:
        if mode == "monthly":
            goal_progress = summarize(posts)["imp"]
        else:
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            mtd = fetch_posts(notion, db_id, month_start, end)
            goal_progress = summarize(mtd)["imp"]

    return {
        "mode": mode, "date": now.strftime("%Y-%m-%d"), "period_label": label,
        "cur": summarize(posts), "prev": summarize(prev),
        "ranking": build_ranking(posts),
        "all_posts": build_ranking(posts, top=len(posts)),
        "type_analysis": build_type_analysis(posts),
        "patterns": build_patterns(posts),
        "audience": fetch_threads_audience(),
        "goal_imp": GOAL_IMP, "goal_progress": goal_progress,
    }


def demo_data() -> dict:
    ranking = [
        {"head": "内定後に『実は月給18万です』担当が『給料を隠す』仕組み…", "type": "ノウハウ", "imp": 2130, "likes": 46, "rts": 12, "er": 2.2, "dt": "7/17 22:45"},
        {"head": "『地方移住したい』って転職希望者が失敗する最大の理由…", "type": "ノウハウ", "imp": 1820, "likes": 38, "rts": 9, "er": 2.1, "dt": "7/17 21:30"},
        {"head": "求人票に『和気あいあい』『アットホーム』って書いてある職場…", "type": "あるある", "imp": 1540, "likes": 31, "rts": 8, "er": 2.0, "dt": "7/16 20:00"},
        {"head": "面接で『何か質問ありますか』に絶対聞くべき逆質問3つ…", "type": "ノウハウ", "imp": 1210, "likes": 24, "rts": 5, "er": 2.0, "dt": "7/15 18:30"},
        {"head": "『空白期間3年、ニート経験あり』面接で採用側が見てること…", "type": "共感", "imp": 980, "likes": 19, "rts": 4, "er": 1.9, "dt": "7/14 12:15"},
    ]
    return {
        "mode": "weekly", "date": "2026-07-20", "period_label": "2026/7/13 〜 7/19",
        "cur": {"count": 33, "imp": 12480, "likes": 214, "rts": 45, "th_views": 3120, "th_likes": 88},
        "prev": {"count": 30, "imp": 10850, "likes": 198, "rts": 39, "th_views": 2500, "th_likes": 71},
        "ranking": ranking,
        "all_posts": ranking,
        "type_analysis": [
            {"label": "ノウハウ", "count": 12, "avg_imp": 680, "avg_er": 2.1},
            {"label": "あるある", "count": 8, "avg_imp": 420, "avg_er": 2.4},
            {"label": "共感", "count": 7, "avg_imp": 350, "avg_er": 1.8},
            {"label": "ニュース", "count": 6, "avg_imp": 210, "avg_er": 1.2},
        ],
        "goal_imp": 50000, "goal_progress": 31200,
        "patterns": {
            "weekday": [{"label": "月曜", "avg": 310}, {"label": "水曜", "avg": 420},
                        {"label": "木曜", "avg": 505}, {"label": "金曜", "avg": 380}, {"label": "日曜", "avg": 350}],
            "timeslot": [{"label": "昼", "avg": 290}, {"label": "夕方", "avg": 340}, {"label": "夜", "avg": 560}],
        },
        "audience": {
            "followers": 412,
            "age": [{"label": "25-34", "value": 148}, {"label": "18-24", "value": 121},
                    {"label": "35-44", "value": 86}, {"label": "45-54", "value": 39}, {"label": "55+", "value": 18}],
            "gender": [{"label": "M", "value": 231}, {"label": "F", "value": 165}, {"label": "U", "value": 16}],
        },
    }


def build_digest(data: dict, pdf_url: str) -> str:
    cur, prev = data["cur"], data["prev"]
    title = "📊 週間レポート" if data["mode"] == "weekly" else "📊 月間レポート"

    def pct(c, p):
        if p <= 0:
            return ""
        d = round((c - p) / p * 100)
        return f"（{'+' if d >= 0 else ''}{d}%）"

    lines = [
        f"{title}（{data['period_label']}）",
        "━━━━━━━━━━━━",
        f"投稿数: {cur['count']}件",
        f"𝕏 表示回数: {cur['imp']:,}回{pct(cur['imp'], prev['imp'])}",
        f"𝕏 いいね: {cur['likes']:,}{pct(cur['likes'], prev['likes'])}",
        f"Threads 閲覧: {cur['th_views']:,}",
    ]
    if data["ranking"]:
        lines += ["", f"🏆 ベスト投稿", f"「{data['ranking'][0]['head']}」"]
    lines += ["", "詳しい分析・ネクストアクションは", "添付のレポート画像をご覧ください👇"]
    if pdf_url:
        lines += ["", f"📎 PDF版: {pdf_url}"]
    return "\n".join(lines)


def page_urls(filename_base: str) -> tuple[str, str]:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return "", ""
    owner, name = repo.split("/", 1)
    root = f"https://{owner.lower()}.github.io/{name}/reports"
    return f"{root}/{filename_base}.png", f"{root}/{filename_base}.pdf"


def demo_insights() -> dict:
    return {
        "analysis": [
            "表示回数は前週比+15%。特に木曜夜の投稿が牽引しています。",
            "「ノウハウ」タイプが平均680表示と、他タイプの1.6〜3倍の成績です。",
            "22時台の投稿はER2%超と高く、就寝前の閲覧層と相性が良い状態です。",
        ],
        "win_pattern": "「具体的な金額・数字＋読者目線の対策」を含むノウハウ投稿が伸びています（上位5本中3本）。",
        "lose_pattern": "ニュース紹介のみで「読者への示唆」がない投稿は平均210表示にとどまっています。",
        "theme_ideas": [
            {"title": "『固定残業代45時間込み』の求人票、実際の手取りを計算してみた", "reason": "金額系ノウハウが最も高ER"},
            {"title": "面接の『最後に一言』で内定率が変わる、たった1つの言い方", "reason": "面接ノウハウ系が安定して上位"},
            {"title": "『未経験歓迎』の求人、本当に歓迎してるか見分ける3行", "reason": "求人票の裏読み系が保存されやすい"},
        ],
        "next_actions": [
            {"title": "ノウハウ系を週2本増枠", "detail": "ニュース系を1本減らし、金額・数字入りノウハウに振り替える"},
            {"title": "木曜夜にエース投稿を配置", "detail": "最も反応が良い木曜22時台に、その週の本命テーマを置く"},
            {"title": "ニュース系に「示唆」を必ず添える", "detail": "事実紹介で終わらせず「求職者にとっての意味」を1文加える"},
        ],
    }


def phase_render(mode: str, demo: bool = False):
    data = demo_data() if demo else gather(mode)
    ins  = ai_insights(data) if not demo else demo_insights()
    REPORT_DIR.mkdir(exist_ok=True)
    base = REPORT_DIR / f"{data['date']}_{data['mode']}"
    pdf_path, png_path = render_files(build_html(data, ins), base)
    png_url, pdf_url = page_urls(base.name)
    message = build_digest(data, pdf_url)
    PENDING_FILE.write_text(json.dumps({
        "mode": data["mode"], "message": message,
        "png_url": png_url, "pdf_url": pdf_url,
        "png": str(png_path), "pdf": str(pdf_path),
    }, ensure_ascii=False), encoding="utf-8")
    print(f"  生成完了: {pdf_path} / {png_path}")
    return pdf_path, png_path


def phase_send():
    if not PENDING_FILE.exists():
        print("送信対象がありません（先に --phase render を実行してください）", file=sys.stderr)
        sys.exit(1)
    info = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    png_url = info.get("png_url", "")

    if png_url:
        # GitHub Pagesのデプロイ完了を待つ（最大4分）
        print(f"  画像URLの公開待ち: {png_url}")
        deadline = time.time() + 240
        ok = False
        while time.time() < deadline:
            try:
                if requests.head(png_url, timeout=10).status_code == 200:
                    ok = True
                    break
            except requests.RequestException:
                pass
            time.sleep(15)
        if not ok:
            print("  画像URLが時間内に公開されなかったため、テキストのみ送信します")
            png_url = ""

    messages: list[dict] = [{"type": "text", "text": info["message"]}]
    if png_url:
        messages.append({"type": "image",
                         "originalContentUrl": png_url,
                         "previewImageUrl": png_url})
    if push_line_messages(messages):
        print("  LINE配信完了")
        PENDING_FILE.unlink(missing_ok=True)
    else:
        print("  LINE配信に失敗しました", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="週次・月次レポート（PDF/画像つき）")
    ap.add_argument("--mode", choices=["weekly", "monthly"], default="weekly")
    ap.add_argument("--phase", choices=["render", "send", "all"], default="all")
    ap.add_argument("--demo", action="store_true", help="モックデータで描画テスト")
    args = ap.parse_args()

    if args.demo:
        phase_render(args.mode, demo=True)
    elif args.phase == "render":
        phase_render(args.mode)
    elif args.phase == "send":
        phase_send()
    else:
        phase_render(args.mode)
        phase_send()
