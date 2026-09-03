"""
Threadsアクセストークンの延長（60日 → さらに60日）

Threadsの長期トークンは60日で失効する。失効“前”にこのスクリプトを実行すると、
同じ権限のまま有効期限が60日延長された新しいトークンが発行される。

⚠ 必ず手元のPCで実行してください。GitHub Actionsでは実行しないこと。
   新しいトークンを画面に表示するため、Actionsのログに残すと
   閲覧できる人全員にトークンが漏れます。

使い方（手元のPC）:
  1. .env に現在の THREADS_ACCESS_TOKEN を入れておく
     （または実行時に環境変数で渡す）
  2. python sns_scheduler/refresh_threads_token.py
  3. 表示された新しいトークンを、GitHub の
     Settings → Secrets and variables → Actions →
     THREADS_ACCESS_TOKEN に貼り替える

延長できる条件:
  - 現在のトークンが「まだ失効していない」こと
  - 発行から24時間以上たっていること

すでに失効している場合は延長できません。
Meta for Developers のアプリ画面から発行し直してください。
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()
JST = timezone(timedelta(hours=9))

REFRESH_URL = "https://graph.threads.net/refresh_access_token"
ME_URL      = "https://graph.threads.net/v1.0/me"


def _mask(token: str) -> str:
    """ログや画面に出す用。全体は絶対に出さない"""
    return f"{token[:6]}…{token[-4:]}（{len(token)}文字）" if len(token) > 12 else "（短すぎます）"


def run() -> None:
    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        print("THREADS_ACCESS_TOKEN が設定されていません（.env か環境変数）")
        sys.exit(1)

    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("⚠ このスクリプトはGitHub Actionsでは実行しないでください。")
        print("  新しいトークンがログに残り、閲覧できる人に漏れます。")
        print("  手元のPCで実行してください。")
        sys.exit(1)

    print(f"現在のトークン: {_mask(token)}")

    # まず生きているか確認する。失効後は延長できないため
    try:
        me = requests.get(ME_URL, params={"fields": "id,username",
                                          "access_token": token}, timeout=15)
    except Exception as e:
        print(f"通信エラー: {e}")
        sys.exit(1)

    if me.status_code in (400, 401):
        print("\n✗ 現在のトークンはすでに失効しています。延長はできません。")
        print("  Meta for Developers のアプリ画面から、トークンを発行し直してください。")
        print("  （発行後、GitHub Secrets の THREADS_ACCESS_TOKEN を更新）")
        sys.exit(1)
    if me.status_code != 200:
        print(f"✗ トークンの確認に失敗しました: HTTP {me.status_code} / {me.text[:160]}")
        sys.exit(1)

    print(f"アカウント　　: @{me.json().get('username', '?')}（有効）")

    try:
        r = requests.get(REFRESH_URL,
                         params={"grant_type": "th_refresh_token",
                                 "access_token": token}, timeout=20)
    except Exception as e:
        print(f"通信エラー: {e}")
        sys.exit(1)

    if r.status_code != 200:
        print(f"\n✗ 延長に失敗しました: HTTP {r.status_code}")
        print(f"  {r.text[:240]}")
        print("\n  発行から24時間たっていない場合は延長できません。"
              "翌日に再実行してください。")
        sys.exit(1)

    data      = r.json()
    new_token = data.get("access_token", "")
    expires   = int(data.get("expires_in", 0) or 0)
    if not new_token:
        print(f"✗ 新しいトークンが返りませんでした: {str(data)[:200]}")
        sys.exit(1)

    limit = datetime.now(JST) + timedelta(seconds=expires)
    print("\n" + "=" * 58)
    print("  新しいアクセストークン（この画面の外に出さないこと）")
    print("=" * 58)
    print(new_token)
    print("=" * 58)
    print(f"  有効期限: {limit.strftime('%Y年%m月%d日')} まで（約{expires // 86400}日）")
    print()
    print("  次の手順:")
    print("   1. GitHub → Settings → Secrets and variables → Actions")
    print("   2. THREADS_ACCESS_TOKEN を上のトークンに貼り替える")
    print("   3. Actions → Doctor を実行して ✅ になることを確認")
    print()
    print(f"   ※ 次回の延長期限をカレンダーに入れてください → "
          f"{(limit - timedelta(days=7)).strftime('%Y年%m月%d日')}ごろ")


if __name__ == "__main__":
    run()
