"""
ID取得・検証ヘルパー
オンボーディング時に面倒な「ID探し」を自動化する。
トークン・IDは .env ファイルから読み、結果も .env に直接書き戻すため、
ターミナル画面に秘密の値が表示されない（表示名とマスク値のみ）。

使い方:
  # ① Threads: トークンから userId を自動取得して .env に書き込む
  #    （.env に THREADS_ACCESS_TOKEN を書いておくこと）
  python sns_scheduler/get_ids.py threads --env clients/stora.env

  # ② LINE: .env の LINE_USER_ID / LINE_USER_ID2 が正しいか検証
  #    （表示名が出れば、userIdが正しく友だち追加済み）
  python sns_scheduler/get_ids.py line --env clients/stora.env

  # ③ X: キー4点で認証できるか＋どのアカウントかを確認
  python sns_scheduler/get_ids.py x --env clients/stora.env
"""

import argparse
import sys
from pathlib import Path

import requests


def _mask(v: str, head: int = 4) -> str:
    if len(v) <= head:
        return "*" * len(v)
    return v[:head] + "…" + f"（全{len(v)}文字）"


def _get(url: str, **kwargs):
    """requests.get のラッパー。ネットワークエラーを綺麗に表示して終了する。"""
    try:
        return requests.get(url, timeout=15, **kwargs)
    except requests.RequestException as e:
        print(f"❌ 通信エラー: {type(e).__name__}")
        print("   ネットワーク接続（プロキシ/ファイアウォール含む）を確認してください。")
        sys.exit(1)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'").strip()
    return values


def write_env_value(path: Path, key: str, value: str):
    """既存の KEY= 行を置換、無ければ末尾に追記する。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Threads ───────────────────────────────────────────────
def cmd_threads(env_path: Path, env: dict[str, str]):
    token = env.get("THREADS_ACCESS_TOKEN", "")
    if not token:
        print("❌ .env に THREADS_ACCESS_TOKEN がありません。")
        print("   Meta for Developers → ユーザートークン生成ツールで発行し、.env に書いてください。")
        sys.exit(1)
    r = _get("https://graph.threads.net/v1.0/me",
             params={"fields": "id,username", "access_token": token})
    if r.status_code != 200:
        print(f"❌ トークンが無効のようです（HTTP {r.status_code}）。")
        print("   60日で失効します。再発行して .env を更新してください。")
        sys.exit(1)
    data = r.json()
    uid, uname = str(data.get("id", "")), data.get("username", "?")
    write_env_value(env_path, "THREADS_USER_ID", uid)
    print(f"✅ Threadsアカウント: @{uname}")
    print(f"✅ THREADS_USER_ID（{_mask(uid)}）を {env_path} に書き込みました。")
    print("   → このまま setup_secrets.py で一括登録できます。")


# ── LINE ──────────────────────────────────────────────────
def cmd_line(env_path: Path, env: dict[str, str]):
    token = env.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        print("❌ .env に LINE_CHANNEL_ACCESS_TOKEN がありません。")
        sys.exit(1)
    r = _get("https://api.line.me/v2/bot/info",
             headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        print(f"❌ チャネルアクセストークンが無効です（HTTP {r.status_code}）。")
        sys.exit(1)
    print(f"✅ 公式アカウント: 「{r.json().get('displayName', '?')}」")

    found = False
    for key in ("LINE_USER_ID", "LINE_USER_ID2"):
        uid = env.get(key, "")
        if not uid:
            continue
        found = True
        pr = _get(f"https://api.line.me/v2/bot/profile/{uid}",
                  headers={"Authorization": f"Bearer {token}"})
        if pr.status_code == 200:
            name = pr.json().get("displayName", "?")
            print(f"✅ {key}（{_mask(uid)}）→ 「{name}」さん：送信可能（友だち登録OK）")
        else:
            print(f"❌ {key}（{_mask(uid)}）→ 無効。userIdの誤り、または友だち未追加/ブロック中")
    if not found:
        print("⚠️  .env に LINE_USER_ID がありません。")
        print("   取得方法: webhook.site のURLをLINE DevelopersのWebhookに設定し、")
        print("   相手に公式アカウントへ一言送ってもらう → 届いたJSONの source.userId をコピー。")


# ── X ─────────────────────────────────────────────────────
def cmd_x(env_path: Path, env: dict[str, str]):
    need = ["X_API_KEY", "X_API_KEY_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    missing = [k for k in need if not env.get(k)]
    if missing:
        print(f"❌ .env に不足: {', '.join(missing)}")
        sys.exit(1)
    try:
        import tweepy
    except ImportError:
        print("❌ tweepy が必要です: pip install tweepy")
        sys.exit(1)
    try:
        client = tweepy.Client(
            consumer_key=env["X_API_KEY"], consumer_secret=env["X_API_KEY_SECRET"],
            access_token=env["X_ACCESS_TOKEN"], access_token_secret=env["X_ACCESS_TOKEN_SECRET"])
        me = client.get_me()
        print(f"✅ X認証OK: @{me.data.username} として投稿できます。")
    except Exception as e:
        print(f"❌ X認証エラー: {str(e)[:120]}")
        print("   → 権限をRead+Writeにした後、Access Tokenを再生成して4つとも入れ直してください。")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="ID取得・検証ヘルパー")
    ap.add_argument("service", choices=["threads", "line", "x"],
                    help="対象サービス")
    ap.add_argument("--env", required=True, help=".env ファイルのパス")
    args = ap.parse_args()

    env_path = Path(args.env)
    if not env_path.exists():
        print(f"❌ ファイルが見つかりません: {env_path}")
        sys.exit(1)
    env = parse_env(env_path)

    {"threads": cmd_threads, "line": cmd_line, "x": cmd_x}[args.service](env_path, env)


if __name__ == "__main__":
    main()
