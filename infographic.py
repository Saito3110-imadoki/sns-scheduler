"""
SNS投稿用高解像度インフォグラフィック生成
playwright + インラインHTML/CSS → 1200×630px PNG
"""

import re
from pathlib import Path
import html as _h

# ── カラーパレット（白背景ライトテーマ）──────────────────────
_BG       = "#ffffff"   # 純白の背景
_SURFACE  = "#f1f5f9"   # slate-100（薄いグレー面）
_CARD     = "#f8fafc"   # slate-50（カード背景）
_BORDER   = "#e2e8f0"   # slate-200（罫線）
_MAIN     = "#4f46e5"   # indigo-600（白背景で映える濃い青紫）
_ACCENT   = "#d97706"   # amber-600（白背景でも読める濃いオレンジ）
_GREEN    = "#059669"   # emerald-600
_TEXT     = "#0f172a"   # slate-900（ほぼ黒）
_MUTED    = "#64748b"   # slate-500（補足テキスト）
_IMP_BG   = "#fffbeb"   # amber-50（インパクト帯の背景）
_IMP_BD   = "#fcd34d"   # amber-300（インパクト帯の枠）

W, H = 1200, 630


def _e(s: object) -> str:
    return _h.escape(str(s))


def _hl(s: object) -> str:
    """エスケープした上で【】で囲まれたフレーズをアクセント色の太字に変換"""
    escaped = _h.escape(str(s))
    return re.sub(
        r"【(.+?)】",
        rf'<span style="color:{_ACCENT};font-weight:800;">\1</span>',
        escaped,
    )


# ── SVGアイコン（lucide-react準拠のSVGパス）────────────────
_ICON_ZAP = """<svg width="42" height="42" viewBox="0 0 24 24" fill="none"
  stroke="#d97706" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
  <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
</svg>"""

_ICON_COINS = """<svg width="42" height="42" viewBox="0 0 24 24" fill="none"
  stroke="#059669" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="8" cy="8" r="6"/>
  <path d="M18.09 10.37A6 6 0 1 1 10.34 18"/>
  <path d="M7 6h1v4"/><path d="M16.71 13.88l1 1-2 1.99"/>
</svg>"""

_ICON_TRENDING = """<svg width="42" height="42" viewBox="0 0 24 24" fill="none"
  stroke="#d97706" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/>
  <polyline points="17 6 23 6 23 12"/>
</svg>"""

_ICON_ROCKET = """<svg width="42" height="42" viewBox="0 0 24 24" fill="none"
  stroke="#d97706" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 2s-4.5 4-4.5 10c0 2.49 2.01 4.5 4.5 4.5s4.5-2.01 4.5-4.5C16.5 6 12 2 12 2z"/>
  <path d="M8.5 14.5L6 17M15.5 14.5L18 17"/>
  <path d="M5 17c-1 1-1 2.5 0 2.5h14c0-1.5-1-2.5-2-3"/>
</svg>"""

_ICONS = [_ICON_ZAP, _ICON_COINS, _ICON_TRENDING, _ICON_ROCKET]


def _pict(paths: str, color: str, size: int = 26, weight: float | None = None) -> str:
    """項目に添えるピクトグラム（線画・24pxグリッド）。
    サイズを上げると線が細く見えるため、既定の太さをサイズから決める。"""
    if weight is None:
        weight = 2.1 if size >= 32 else 1.9
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="{weight}" stroke-linecap="round" '
            f'stroke-linejoin="round">{paths}</svg>')


# 項目の並び順で使い分ける汎用ピクトグラム（意味が固定されない中立的な図形）
_ITEM_PICTS = [
    '<path d="M4 12.5l5 5L20 6.5"/>',                                        # チェック
    '<circle cx="12" cy="12" r="9"/><path d="M12 8v5l3 2"/>',                # 時計
    '<path d="M3 20h18"/><path d="M6 20V10M12 20V4M18 20v-7"/>',             # 棒グラフ
    '<path d="M12 3l2.6 5.6 6.1.8-4.5 4.2 1.2 6-5.4-3-5.4 3 1.2-6L3.3 9.4l6.1-.8z"/>',  # 星
]


# ── ブランディング適用 ────────────────────────────────────
def _apply_branding(html: str, branding: dict) -> str:
    """生成済みHTMLにブランドカラーとウォーターマークを適用する"""
    if not branding:
        return html

    primary = branding.get("primary_color", "").strip()
    accent  = branding.get("accent_color", "").strip()

    # 大文字小文字を統一してから置換
    if primary:
        html = re.sub(re.escape(_MAIN),   primary, html, flags=re.IGNORECASE)
    if accent:
        html = re.sub(re.escape(_ACCENT), accent,  html, flags=re.IGNORECASE)

    # ウォーターマーク（右下）
    logo_url     = branding.get("logo_url", "").strip()
    company_name = branding.get("company_name", "").strip()
    if logo_url or company_name:
        inner = (
            f'<img src="{logo_url}" '
            f'style="height:26px;opacity:0.75;object-fit:contain;">'
            if logo_url else
            f'<span style="font-size:11px;color:{_MUTED};font-weight:700;'
            f'letter-spacing:1px;opacity:0.85;">{_e(company_name)}</span>'
        )
        watermark = (
            f'<div style="position:absolute;bottom:8px;right:20px;'
            f'display:flex;align-items:center;z-index:99;">{inner}</div>'
        )
        html = html.replace("</div>\n</body>", f"{watermark}\n</div>\n</body>")

    return html


# ── ベースHTML ────────────────────────────────────────────
def _base(body: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{
  width:{W}px;height:{H}px;overflow:hidden;
  background:{_BG};
  font-family:'Noto Sans CJK JP','Noto Sans JP','Hiragino Sans',
              'BIZ UDPGothic','Yu Gothic','Meiryo',sans-serif;
  color:{_TEXT};
  -webkit-font-smoothing:antialiased;
}}
</style></head><body>
<div style="width:{W}px;height:{H}px;display:flex;flex-direction:column;position:relative;">
{body}
</div>
</body></html>"""


def _header(title: str, subtitle: str = "") -> str:
    sub = (f'<div style="margin-top:6px;font-size:17px;color:{_MUTED};'
           f'font-weight:500;">{_e(subtitle)}</div>') if subtitle else ""
    return f"""
<div style="display:flex;align-items:flex-start;padding:30px 52px 0;flex-shrink:0;">
  <div style="width:7px;background:linear-gradient(180deg,{_ACCENT},{_MAIN});
    border-radius:3px;min-height:64px;margin-right:20px;flex-shrink:0;"></div>
  <div>
    <div style="font-size:36px;font-weight:900;letter-spacing:-0.5px;
      color:{_TEXT};line-height:1.25;">{_e(title)}</div>
    {sub}
  </div>
</div>
<div style="height:1px;background:{_BORDER};margin:14px 52px 0;flex-shrink:0;"></div>"""


def _impact_footer(impact: str = "", caption: str = "") -> str:
    parts: list[str] = []
    if impact:
        bulb = _pict('<path d="M9 19h6"/><path d="M10 22h4"/>'
                     '<path d="M12 2a6.5 6.5 0 0 0-4 11.6c.6.5 1 1.3 1 2.1h6c0-.8.4-1.6 1-2.1'
                     'A6.5 6.5 0 0 0 12 2z"/>', "#ffffff", 24)
        parts.append(f"""
<div style="background:{_TEXT};border-radius:12px;
  padding:15px 26px;margin:0 52px 26px;flex-shrink:0;
  display:flex;align-items:center;gap:16px;">
  <span style="display:flex;flex-shrink:0;">{bulb}</span>
  <span style="font-size:19px;font-weight:800;color:#ffffff;
    line-height:1.5;">{_hl(impact)}</span>
</div>""")
    if caption:
        parts.append(
            f'<div style="font-size:12px;color:{_MUTED};padding:0 52px 18px;'
            f'flex-shrink:0;">{_e(caption)}</div>')
    return "\n".join(parts)


# ── stat チャート ─────────────────────────────────────────
def _html_stat(chart: dict) -> str:
    stats   = chart.get("stats", [])
    impact  = chart.get("impact", "")
    caption = chart.get("caption", "")

    cards = []
    for i, st in enumerate(stats[:3]):
        val   = st.get("value", "")
        label = st.get("label", "")
        ctx   = st.get("context", "")
        icon  = _ICONS[i % len(_ICONS)]

        # 値の長さに応じてフォントサイズを調整
        vsize = ("80px" if len(val) <= 3
                 else "68px" if len(val) <= 5
                 else "52px" if len(val) <= 8
                 else "38px")

        ctx_html = (
            f'<div style="font-size:11px;color:{_MUTED};letter-spacing:2px;'
            f'text-transform:uppercase;margin-bottom:6px;font-weight:600;">{_e(ctx)}</div>'
        ) if ctx else '<div style="height:17px;"></div>'

        cards.append(f"""
<div style="flex:1;background:{_CARD};border:2px solid {_MAIN};border-radius:18px;
  padding:28px 20px 24px;text-align:center;position:relative;overflow:hidden;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;
  box-shadow:0 2px 10px rgba(15,23,42,0.06);">
  <!-- グラデーション上部バー -->
  <div style="position:absolute;top:0;left:20px;right:20px;height:4px;
    background:linear-gradient(90deg,{_ACCENT},{_MAIN});border-radius:0 0 4px 4px;"></div>
  <!-- アイコン -->
  <div style="margin-bottom:10px;">{icon}</div>
  {ctx_html}
  <!-- 数値 -->
  <div style="font-size:{vsize};font-weight:900;color:{_ACCENT};line-height:1;
    letter-spacing:-2px;">{_e(val)}</div>
  <!-- ラベル -->
  <div style="font-size:15px;color:{_TEXT};margin-top:14px;font-weight:600;
    line-height:1.5;max-width:180px;">{_e(label)}</div>
</div>""")

    return _base(f"""
{_header(chart.get("title",""), chart.get("subtitle",""))}
<div style="display:flex;gap:20px;padding:22px 52px;flex:1;align-items:stretch;">
  {"".join(cards)}
</div>
{_impact_footer(impact, caption)}""")


# ── bar チャート ──────────────────────────────────────────
def _html_bar(chart: dict) -> str:
    labels  = chart.get("labels", [])
    values  = chart.get("values", [])
    unit    = chart.get("unit", "")
    caption = chart.get("caption", "")
    max_val = max(values) if values else 1

    rows = []
    for label, val in zip(labels, values):
        pct = int(val / max_val * 100)
        rows.append(f"""
<div style="display:flex;align-items:center;gap:18px;">
  <div style="width:160px;font-size:14px;color:{_TEXT};text-align:right;
    flex-shrink:0;font-weight:500;line-height:1.3;">{_e(label)}</div>
  <div style="flex:1;background:{_SURFACE};border-radius:8px;height:38px;
    overflow:hidden;position:relative;border:1px solid {_BORDER};">
    <div style="width:{pct}%;background:linear-gradient(90deg,{_MAIN},{_ACCENT});
      height:100%;border-radius:8px;transition:width 0s;"></div>
  </div>
  <div style="width:90px;font-size:18px;font-weight:900;color:{_ACCENT};
    flex-shrink:0;">{_e(val)}{_e(unit)}</div>
</div>""")

    return _base(f"""
{_header(chart.get("title",""), chart.get("subtitle",""))}
<div style="flex:1;display:flex;flex-direction:column;justify-content:center;
  gap:18px;padding:20px 52px;">
  {"".join(rows)}
</div>
{_impact_footer("", caption)}""")


# ── comparison チャート ───────────────────────────────────
def _html_comparison(chart: dict) -> str:
    ll    = chart.get("left_label", "従来")
    rl    = chart.get("right_label", "新手法")
    li    = chart.get("left_items", [])
    ri    = chart.get("right_items", [])
    title = chart.get("title", "")
    sub   = chart.get("subtitle", "")
    cap   = chart.get("caption", "")

    def _panel(items: list, label: str, active: bool) -> str:
        bg      = _CARD if active else _SURFACE
        border  = _MAIN if active else _BORDER
        dot_c   = _MAIN if active else _MUTED
        txt_c   = _TEXT if active else _MUTED
        lbl_c   = _MAIN if active else _MUTED
        shadow  = f";box-shadow:0 2px 10px rgba(15,23,42,0.06)" if active else ""
        rows    = "".join(
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">'
            f'<div style="width:8px;height:8px;border-radius:50%;background:{dot_c};flex-shrink:0;"></div>'
            f'<span style="font-size:14px;color:{txt_c};line-height:1.4;">{_e(it)}</span></div>'
            for it in items[:4]
        )
        return f"""
<div style="flex:1;background:{bg};border:2px solid {border};border-radius:16px;
  padding:26px 28px{shadow};">
  <div style="font-size:19px;font-weight:800;color:{lbl_c};margin-bottom:14px;
    text-align:center;">{_e(label)}</div>
  <div style="height:1px;background:{_BORDER};margin-bottom:16px;"></div>
  {rows}
</div>"""

    arrow = f"""<div style="display:flex;align-items:center;justify-content:center;
  width:60px;flex-shrink:0;">
  <svg width="44" height="44" viewBox="0 0 24 24" fill="none"
    stroke="{_ACCENT}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <line x1="5" y1="12" x2="19" y2="12"/>
    <polyline points="12 5 19 12 12 19"/>
  </svg>
</div>"""

    return _base(f"""
{_header(title, sub)}
<div style="flex:1;display:flex;align-items:center;gap:0;padding:20px 52px;">
  {_panel(li, ll, active=False)}
  {arrow}
  {_panel(ri, rl, active=True)}
</div>
{_impact_footer("", cap)}""")


# ── flow チャート（プロセス・流れ）────────────────────────
def _html_flow(chart: dict) -> str:
    steps   = chart.get("steps", [])[:4]
    impact  = chart.get("impact", "")
    caption = chart.get("caption", "")
    n       = len(steps)

    # ステップ数に応じてサイズ調整（スマホ表示前提で大きめ）
    chip_fs = "19px" if n <= 3 else "17px"
    text_fs = "23px" if n <= 3 else "21px"
    pad     = "16px 24px" if n <= 3 else "12px 22px"

    rows = []
    for i, st in enumerate(steps):
        label   = st.get("label", f"STEP{i+1}")
        text    = st.get("text", "")
        is_last = (i == n - 1)
        # 最後のステップ（次の一歩）はアクセントカラーで強調
        chip_bg = _ACCENT if is_last else _MAIN
        chip_fg = "#ffffff"
        border  = _ACCENT if is_last else _BORDER

        rows.append(f"""
<div style="display:flex;align-items:stretch;gap:16px;">
  <div style="width:150px;flex-shrink:0;display:flex;flex-direction:column;
    align-items:center;justify-content:center;background:{chip_bg};
    border-radius:12px;padding:8px 6px;">
    <div style="font-size:12px;font-weight:700;color:{chip_fg};opacity:0.65;
      letter-spacing:2px;line-height:1;">{i+1:02d}</div>
    <div style="font-size:{chip_fs};font-weight:900;color:{chip_fg};
      letter-spacing:1px;margin-top:3px;">{_e(label)}</div>
  </div>
  <div style="flex:1;display:flex;align-items:center;background:{_CARD};
    border:1px solid {border};border-radius:12px;padding:{pad};
    font-size:{text_fs};font-weight:600;line-height:1.5;
    color:{_TEXT};">{_hl(text)}</div>
</div>""")
        if not is_last:
            rows.append(f"""
<div style="width:150px;display:flex;justify-content:center;padding:1px 0;">
  <svg width="20" height="15" viewBox="0 0 24 24" fill="none"
    stroke="{_MUTED}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
    <line x1="12" y1="3" x2="12" y2="19"/>
    <polyline points="5 13 12 20 19 13"/>
  </svg>
</div>""")

    return _base(f"""
{_header(chart.get("title",""), chart.get("subtitle",""))}
<div style="flex:1;display:flex;flex-direction:column;justify-content:center;
  gap:6px;padding:12px 52px;">
  {"".join(rows)}
</div>
{_impact_footer(impact, caption)}""")


# ── list チャート（ポイント解説）──────────────────────────
def _html_list(chart: dict) -> str:
    items   = chart.get("items", [])[:4]
    impact  = chart.get("impact", "")
    caption = chart.get("caption", "")
    n       = len(items)

    head_fs = "25px" if n <= 3 else "22px"
    text_fs = "18px" if n <= 3 else "16px"
    num_sz  = "56px" if n <= 3 else "50px"
    num_fs  = "26px" if n <= 3 else "23px"
    row_pad = "18px 26px" if n <= 3 else "14px 24px"

    rows = []
    for i, it in enumerate(items):
        head = it.get("head", "")
        text = it.get("text", "")
        pic = _pict(_ITEM_PICTS[i % len(_ITEM_PICTS)], _MAIN, 34)
        rows.append(f"""
<div style="display:flex;align-items:center;gap:18px;background:{_CARD};
  border:1px solid {_BORDER};border-left:5px solid {_MAIN};
  border-radius:14px;padding:{row_pad};">
  <div style="width:{num_sz};height:{num_sz};flex-shrink:0;border-radius:14px;
    background:{_MAIN};display:flex;
    align-items:center;justify-content:center;font-size:{num_fs};font-weight:900;
    color:#ffffff;">{i+1}</div>
  <div style="display:flex;align-items:center;justify-content:center;
    flex-shrink:0;">{pic}</div>
  <div style="flex:1;">
    <div style="font-size:{head_fs};font-weight:800;color:{_TEXT};
      line-height:1.3;margin-bottom:5px;">{_hl(head)}</div>
    <div style="font-size:{text_fs};color:{_MUTED};font-weight:500;
      line-height:1.5;">{_hl(text)}</div>
  </div>
</div>""")

    return _base(f"""
{_header(chart.get("title",""), chart.get("subtitle",""))}
<div style="flex:1;display:flex;flex-direction:column;justify-content:center;
  gap:16px;padding:4px 52px;">
  {"".join(rows)}
</div>
{_impact_footer(impact, caption)}""")


# ── compare_flow チャート（左右対比＋因果） ─────────────────
def _html_compare_flow(chart: dict) -> str:
    """「問題 vs 解決」を左右に並べる対比型。
    left/right それぞれに label（見出し）と items（要素の配列）を持つ。
    左を赤系・右をブランド色にして、負の連鎖と正の打ち手を対比させる。"""
    left    = chart.get("left", {}) or {}
    right   = chart.get("right", {}) or {}
    impact  = chart.get("impact", "")
    caption = chart.get("caption", "")
    NEG     = "#c0392b"

    def _side(side: dict, color: str, tint: str, arrow: str) -> str:
        label = side.get("label", "")
        items = [str(x) for x in (side.get("items", []) or [])][:5]
        n     = len(items)
        fs    = "20px" if n <= 4 else "18px"
        pad   = "13px 18px" if n <= 4 else "10px 16px"
        rows  = []
        for i, it in enumerate(items):
            sep = (f'<div style="text-align:center;color:{color};font-size:17px;'
                   f'line-height:1;margin:-2px 0;">{arrow}</div>') if i else ""
            rows.append(sep + f"""
<div style="background:#ffffff;border:1px solid {tint};border-radius:11px;
  padding:{pad};font-size:{fs};font-weight:700;color:{_TEXT};line-height:1.45;">
  {_hl(it)}</div>""")
        return f"""
<div style="flex:1;background:{tint}1a;border:2px solid {tint};border-radius:16px;
  padding:18px 20px;display:flex;flex-direction:column;">
  <div style="background:{color};color:#ffffff;font-size:18px;font-weight:900;
    border-radius:9px;padding:8px 16px;text-align:center;margin-bottom:14px;
    flex-shrink:0;">{_e(label)}</div>
  <div style="flex:1;display:flex;flex-direction:column;justify-content:center;
    gap:7px;">{"".join(rows)}</div>
</div>"""

    return _base(f"""
{_header(chart.get("title",""), chart.get("subtitle",""))}
<div style="flex:1;display:flex;gap:22px;padding:4px 52px;align-items:stretch;">
  {_side(left,  NEG,   "#e8b4ae", "↓")}
  <div style="display:flex;align-items:center;flex-shrink:0;">
    <div style="width:44px;height:44px;border-radius:50%;background:{_TEXT};
      color:#ffffff;display:flex;align-items:center;justify-content:center;
      font-size:22px;font-weight:900;">&#8594;</div>
  </div>
  {_side(right, _MAIN, "#a9dcc9", "↓")}
</div>
{_impact_footer(impact, caption)}""")


# ── matrix チャート（分類マトリクス・網羅型） ─────────────────
def _html_matrix(chart: dict) -> str:
    """テーマを系統ごとに分類して一覧化する網羅型。
    groups: [{label, items:[{head, text}]}] を2〜3列に並べる。
    「全体像を1枚で押さえられる」ため保存されやすい。"""
    groups  = (chart.get("groups") or [])[:3]
    impact  = chart.get("impact", "")
    caption = chart.get("caption", "")
    if not groups:
        return _html_list(chart)

    # 系統ごとに色を変え、分類が視覚的に分かれるようにする
    palette = [_MAIN, "#0369a1", _ACCENT]
    ncol    = len(groups)
    max_it  = max((len(g.get("items") or []) for g in groups), default=0)
    head_fs = "17px" if max_it <= 4 else "15px"
    text_fs = "13.5px" if max_it <= 4 else "12.5px"
    pad     = "10px 13px" if max_it <= 4 else "8px 12px"

    cols = []
    for gi, g in enumerate(groups):
        color = palette[gi % len(palette)]
        items = (g.get("items") or [])[:6]
        rows  = []
        for it in items:
            if isinstance(it, dict):
                head, text = it.get("head", ""), it.get("text", "")
            else:
                head, text = str(it), ""
            sub = (f'<div style="font-size:{text_fs};color:{_MUTED};font-weight:500;'
                   f'line-height:1.4;margin-top:3px;">{_hl(text)}</div>') if text else ""
            rows.append(f"""
<div style="background:#ffffff;border:1px solid {_BORDER};border-left:3px solid {color};
  border-radius:8px;padding:{pad};">
  <div style="font-size:{head_fs};font-weight:800;color:{_TEXT};
    line-height:1.35;">{_hl(head)}</div>
  {sub}
</div>""")
        cols.append(f"""
<div style="flex:1;display:flex;flex-direction:column;min-width:0;">
  <div style="background:{color};color:#ffffff;font-size:17px;font-weight:900;
    border-radius:9px 9px 0 0;padding:9px 14px;text-align:center;
    flex-shrink:0;">{_e(g.get("label",""))}</div>
  <div style="flex:1;background:{_CARD};border:1px solid {_BORDER};border-top:none;
    border-radius:0 0 9px 9px;padding:11px;display:flex;flex-direction:column;
    justify-content:space-evenly;gap:7px;">{"".join(rows)}</div>
</div>""")

    return _base(f"""
{_header(chart.get("title",""), chart.get("subtitle",""))}
<div style="flex:1;display:flex;gap:14px;padding:4px 52px;align-items:stretch;">
  {"".join(cols)}
</div>
{_impact_footer(impact, caption)}""")


# ── ページバッジ（カルーセル用）──────────────────────────
def _apply_page_badge(html: str, page: tuple | None, role: str = "") -> str:
    """複数枚スライドのとき右上に「起 1 / 4」バッジを付ける。
    role には起承転結のいずれかが入る。1枚目にはスワイプを促す矢印も添える。"""
    # ページバッジは視線を散らすため表示しない（カルーセルは画像の並びで伝わる）
    return html
    if not page or page[1] <= 1:
        return html
    cur, total = page
    arrow = "&nbsp;→" if cur == 1 else ""
    role_html = (
        f'<span style="color:#ffffff;background:{_MAIN};border-radius:999px;'
        f'padding:3px 12px;margin-right:12px;font-size:15px;">{_e(role)}</span>'
    ) if role else ""
    badge = (
        f'<div style="position:absolute;top:18px;right:28px;display:flex;'
        f'align-items:center;background:rgba(15,23,42,0.05);'
        f'border:1px solid {_BORDER};border-radius:999px;'
        f'padding:6px 18px 6px {("8px" if role else "18px")};'
        f'font-size:16px;font-weight:800;'
        f'color:{_TEXT};letter-spacing:1px;z-index:99;">'
        f'{role_html}{cur} / {total}{arrow}</div>'
    )
    return html.replace("</div>\n</body>", f"{badge}\n</div>\n</body>")


# ── メイン描画関数 ────────────────────────────────────────
def generate_infographic(chart: dict, output_path: Path,
                         branding: dict | None = None,
                         page: tuple | None = None) -> bool:
    """HTMLをplaywrightでレンダリングしPNG保存。失敗時はFalse
    page=(現在ページ, 総ページ数) を渡すとカルーセル用バッジを表示"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    chart_type = chart.get("chart_type", "stat")
    if chart_type == "stat":
        html = _html_stat(chart)
    elif chart_type == "bar":
        html = _html_bar(chart)
    elif chart_type == "comparison":
        html = _html_comparison(chart)
    elif chart_type == "flow":
        html = _html_flow(chart)
    elif chart_type == "matrix":
        html = _html_matrix(chart)
    elif chart_type == "compare_flow":
        html = _html_compare_flow(chart)
    elif chart_type == "list":
        html = _html_list(chart)
    else:
        # 想定外のchart_typeでも画像なしにせず、データ形状から最も近い型で描く
        print(f"  図解: 未知のchart_type「{chart_type}」→ 代替テンプレートで描画")
        if chart.get("groups"):
            html = _html_matrix(chart)
        elif chart.get("left") or chart.get("right"):
            html = _html_compare_flow(chart)
        elif chart.get("steps"):
            html = _html_flow(chart)
        elif chart.get("stats"):
            html = _html_stat(chart)
        elif chart.get("items"):
            html = _html_list(chart)
        else:
            return False

    html = _apply_branding(html, branding or {})
    html = _apply_page_badge(html, page, role=str(chart.get("role", "")).strip())

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            # device_scale_factor=3 → 3600×1890px の高解像度PNG
            page = browser.new_page(viewport={"width": W, "height": H},
                                    device_scale_factor=3)
            page.set_content(html, wait_until="domcontentloaded")
            page.screenshot(path=str(output_path))
            browser.close()
        return True
    except Exception as e:
        print(f"  インフォグラフィック生成エラー: {e}")
        return False
