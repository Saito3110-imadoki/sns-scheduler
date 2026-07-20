"""
LINE通知ユーティリティ（共通モジュール）
poster.py / analytics.py / daily_generator.py から共通で使用
"""

import os
import requests
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))


def _line_recipients() -> list[str]:
    """通知の宛先userIdを収集する。
    LINE_USER_ID / LINE_USER_ID2 / LINE_USER_IDS を参照し、
    カンマ・改行区切りにも対応。重複・空欄は除外する。
    宛先を増やしたい場合は LINE_USER_ID3... ではなく、
    いずれかにカンマ区切りで追加すればよい。"""
    ids: list[str] = []
    for key in ("LINE_USER_ID", "LINE_USER_ID2", "LINE_USER_IDS"):
        raw = os.environ.get(key, "")
        for part in raw.replace("\n", ",").split(","):
            uid = part.strip()
            if uid and uid not in ids:
                ids.append(uid)
    return ids


def push_line_messages(messages: list[dict]) -> bool:
    """任意のLINEメッセージ（最大5件）を全宛先にpushする。全宛先成功でTrue。"""
    token      = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    recipients = _line_recipients()
    if not token or not recipients:
        print("  LINE通知スキップ: LINE_CHANNEL_ACCESS_TOKEN または 宛先(LINE_USER_ID) が未設定")
        return False
    ok_all = True
    for uid in recipients:
        try:
            r = requests.post(
                "https://api.line.me/v2/bot/message/push",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"to": uid, "messages": messages},
                timeout=10,
            )
            if r.status_code != 200:
                # LINE APIのエラー詳細（レスポンス本文）を出力。原因特定に必須。
                # userIdは頭6文字のみ出す（ログへの全文露出を避ける）。
                print(f"  LINE通知エラー（宛先 {uid[:6]}…）: HTTP {r.status_code} / {r.text}")
                ok_all = False
        except Exception as e:
            print(f"  LINE通知エラー（宛先 {uid[:6]}…）: {e}")
            ok_all = False
    return ok_all


def send_line_message(text: str) -> bool:
    """LINEにテキストメッセージを送信。成功時True。"""
    return push_line_messages([{"type": "text", "text": text}])


def send_line_image(image_url: str, preview_url: str = "") -> bool:
    """LINEに画像メッセージを送信。URLはHTTPSかつJPEG/PNGであること。"""
    return push_line_messages([{
        "type": "image",
        "originalContentUrl": image_url,
        "previewImageUrl": preview_url or image_url,
    }])


def notify_error(context: str, detail: str) -> None:
    """エラー発生時のLINE通知。context=発生箇所、detail=エラー内容"""
    now  = datetime.now(JST)
    text = (
        f"⚠️ SNSスケジューラー エラー\n\n"
        f"📍 発生箇所: {context}\n"
        f"❌ 内容: {detail}\n"
        f"🕐 日時: {now.strftime('%Y/%m/%d %H:%M')} JST\n\n"
        "Notionまたはログを確認してください。"
    )
    send_line_message(text)


def notify_post_complete(posted: int, errors: int) -> None:
    """自動投稿完了通知"""
    now  = datetime.now(JST)
    icon = "✅" if errors == 0 else "⚠️"
    text = (
        f"{icon} 自動投稿 完了\n\n"
        f"✅ 投稿成功: {posted} 件\n"
        f"❌ エラー: {errors} 件\n"
        f"🕐 {now.strftime('%Y/%m/%d %H:%M')} JST"
    )
    if errors > 0:
        text += "\n\nエラーになった投稿はNotionで「エラー」ステータスになっています。"
    send_line_message(text)


def notify_analytics_complete(updated: int, extra: str = "") -> None:
    """パフォーマンス計測完了通知。extra にはフォロワー数の行などを渡す"""
    now  = datetime.now(JST)
    follower_block = f"👥 フォロワー\n{extra}\n\n" if extra else ""
    text = (
        f"📊 パフォーマンス計測 完了\n\n"
        f"{follower_block}"
        f"📈 更新件数: {updated} 件\n"
        f"🕐 {now.strftime('%Y/%m/%d %H:%M')} JST\n\n"
        "Notionで最新のいいね数・インプレッションを確認できます。"
    )
    send_line_message(text)
