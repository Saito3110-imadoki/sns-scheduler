"""
AIアイキャッチ画像生成（fal.ai / FLUX schnell）
チャート図解が不要な投稿に、文字なしの映えるアイキャッチ画像を付ける補完モジュール。

動作条件（両方そろったときだけ有効。片方でも欠ければ何もしない＝安全）:
  - 環境変数 FAL_KEY が設定されている（fal.ai のAPIキー）
  - config.yaml の content.ai_eyecatch が true

コスト目安: FLUX schnell は約 $0.003/枚（1クライアント月60〜90枚で月20〜30円程度）。
画像は文字なし。人物リアル系は避け、抽象・象徴・モノ/風景中心のプロンプトにする。
"""

import os
import requests

try:
    import anthropic
except Exception:
    anthropic = None

FAL_ENDPOINT = "https://fal.run/fal-ai/flux/schnell"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


def _fal_key() -> str:
    return os.environ.get("FAL_KEY", "").strip()


def enabled() -> bool:
    """FAL_KEYが設定されていれば有効（configフラグは呼び出し側で判定）"""
    return bool(_fal_key())


def build_prompt(theme: str, post_text: str, audience: str = "",
                 brand_color: str = "") -> str:
    """投稿内容から、文字なしアイキャッチ用の英語画像プロンプトをClaudeで生成。
    失敗時は汎用プロンプトにフォールバックする。"""
    fallback = (
        f"Clean modern flat vector illustration representing '{theme}', "
        "minimal, professional, soft gradient background, symbolic objects, "
        "social media banner, clean minimal flat design, professional business illustration, "
        "high quality, no text, no words, no letters, no numbers, no watermark"
    )
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or anthropic is None:
        return fallback
    try:
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=220,
            messages=[{"role": "user", "content": (
                "次のSNS投稿に合う『アイキャッチ画像』を作るための英語の画像生成プロンプトを1つ作ってください。\n"
                f"投稿テーマ: {theme}\n"
                f"投稿の要旨: {post_text[:200]}\n"
                f"読者層: {audience}\n"
                f"ブランドカラーの雰囲気: {brand_color}\n\n"
                "条件:\n"
                "- 文字・数字・ロゴは一切入れない\n"
                "- リアルな人物の顔は避け、抽象的・象徴的・モノや風景中心にする\n"
                "- モダンでクリーン、SNSで目を引くスタイル\n"
                "- 出力は英語プロンプト本文のみ（説明・引用符・前置き不要）")}],
        )
        text = "".join(b.text for b in msg.content
                       if getattr(b, "type", "") == "text").strip()
        if text:
            # スタイルを毎回そろえる接尾辞＋文字混入を防ぐネガティブ指定。
            # schnellは細部が甘くなりやすいので、
            # ディテール依存の少ないフラット/ミニマル方向に寄せて品質差を吸収する
            return (text + ", clean minimal flat design, soft depth, "
                    "professional business illustration, uncluttered composition, "
                    "no text, no words, no letters, no numbers, no watermark, "
                    "no realistic human faces")
    except Exception as e:
        print(f"  アイキャッチ: プロンプト生成失敗（汎用にフォールバック）: {e}")
    return fallback


def generate(theme: str, post_text: str, out_path, audience: str = "",
             brand_color: str = "") -> bool:
    """アイキャッチ画像を生成して out_path に保存。成功でTrue、失敗でFalse。
    失敗しても例外は投げない（投稿生成全体を止めないため）。"""
    key = _fal_key()
    if not key:
        return False
    prompt = build_prompt(theme, post_text, audience, brand_color)
    try:
        r = requests.post(
            FAL_ENDPOINT,
            headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
            json={
                "prompt": prompt,
                "image_size": "landscape_16_9",
                "num_images": 1,
                "num_inference_steps": 4,
                "enable_safety_checker": True,
            },
            timeout=90,
        )
        if r.status_code != 200:
            print(f"  アイキャッチ生成エラー: HTTP {r.status_code} / {r.text[:200]}")
            return False
        data = r.json()
        img_url = (data.get("images") or [{}])[0].get("url")
        if not img_url:
            print(f"  アイキャッチ: 画像URLが取得できませんでした: {str(data)[:160]}")
            return False
        img = requests.get(img_url, timeout=60)
        img.raise_for_status()
        out_path.write_bytes(img.content)
        print(f"  アイキャッチ: 生成完了 ({out_path.name})")
        return True
    except Exception as e:
        print(f"  アイキャッチ生成スキップ: {e}")
        return False
