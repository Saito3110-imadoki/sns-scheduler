"""
LINE通知ユーティリティ（共通モジュール）
poster.py / analytics.py / daily_generator.py から共通で使用
"""

import os
import requests
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))


def send_line_message(text: str) -> bool:
    """LINEにテキストメッセージを送信。成功時True。"""
    token   = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")
    if not token or not user_id:
        return False
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"to": user_id, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  LINE通知エラー: {e}")
        return False


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


def notify_analytics_complete(updated: int) -> None:
    """パフォーマンス計測完了通知"""
    now  = datetime.now(JST)
    text = (
        f"📊 パフォーマンス計測 完了\n\n"
        f"📈 更新件数: {updated} 件\n"
        f"🕐 {now.strftime('%Y/%m/%d %H:%M')} JST\n\n"
        "Notionで最新のいいね数・インプレッションを確認できます。"
    )
    send_line_message(text)
