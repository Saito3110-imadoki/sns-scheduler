"""
事前チェック（ヘルスチェック）スクリプト
本番投入の前に、各サービスの認証情報が正しいかを実際にAPIへpingして検証する。
今日ハマった 401(認証) / 402(クレジット) / 400(LINE userId・友だち未追加) の
ような問題を、投稿を出す前にまとめてあぶり出すのが目的。

使い方:
  # 標準の環境変数（GitHub Secrets / .env）でチェック
  python sns_scheduler/doctor.py

  # マルチテナント: クライアント設定のプレフィックスを適用してチェック
  python sns_scheduler/doctor.py --client clients/stora.yaml

終了コード: 1つでも「設定済みなのに失敗」があれば 1（CIのゲートに使える）
"""

import argparse
import os
import sys
from pathlib import Path

import requests

# 同ディレクトリのモジュールを import できるように
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


OK, NG, SKIP, WARN = "✅", "❌", "⏭️ ", "⚠️ "
_results: list[tuple[str, str]] = []   # (status, label)


def _record(status: str, label: str, detail: str = ""):
    line = f"  {status} {label}"
    if detail:
        line += f"  —  {detail}"
    print(line)
    _results.append((status, label))


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _line_recipients() -> list[str]:
    ids: list[str] = []
    for key in ("LINE_USER_ID", "LINE_USER_ID2", "LINE_USER_IDS"):
        for part in _env(key).replace("\n", ",").split(","):
            uid = part.strip()
            if uid and uid not in ids:
                ids.append(uid)
    return ids


# ── クライアントプレフィックス適用（マルチテナント用）───────────
def apply_client_prefix(client_path: str):
    import yaml
    cfg = yaml.safe_load(Path(client_path).read_text(encoding="utf-8")) or {}
    prefix = str(cfg.get("env_prefix", "")).strip()
    if not prefix:
        print(f"  （{client_path} に env_prefix が無いため標準の環境変数を使用）")
        return
    names = [
        "NOTION_TOKEN", "NOTION_DATABASE_ID",
        "X_API_KEY", "X_API_KEY_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET",
        "ANTHROPIC_API_KEY", "LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID",
        "LINE_USER_ID2", "THREADS_USER_ID", "THREADS_ACCESS_TOKEN",
    ]
    for std in names:
        val = os.environ.get(f"{prefix}_{std}", "")
        if val:
            os.environ[std] = val
    print(f"  （プレフィックス {prefix}_ を標準名に適用しました）")


# ── 各サービスのチェック ──────────────────────────────────
def check_notion():
    print("\n■ Notion")
    token = _env("NOTION_TOKEN")
    db    = _env("NOTION_DATABASE_ID")
    if not token or not db:
        _record(NG, "Notion", "NOTION_TOKEN / NOTION_DATABASE_ID が未設定")
        return
    try:
        r = requests.get(
            f"https://api.notion.com/v1/databases/{db}",
            headers={"Authorization": f"Bearer {token}",
                     "Notion-Version": "2022-06-28"},
            timeout=15,
        )
        if r.status_code == 200:
            title = "".join(t.get("plain_text", "")
                            for t in r.json().get("title", [])) or "(無題)"
            _record(OK, "Notion", f"DB接続OK: 「{title}」")
        elif r.status_code == 404:
            _record(NG, "Notion",
                    "DBが見つからない/未接続。インテグレーションをDBに『コネクト』したか、DB IDを確認")
        elif r.status_code == 401:
            _record(NG, "Notion", "NOTION_TOKEN が無効（401）")
        else:
            _record(NG, "Notion", f"HTTP {r.status_code} / {r.text[:120]}")
    except Exception as e:
        _record(NG, "Notion", str(e))


def check_x():
    print("\n■ X（Twitter）")
    need = ["X_API_KEY", "X_API_KEY_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    missing = [n for n in need if not _env(n)]
    if missing:
        _record(NG, "X 認証", f"未設定: {', '.join(missing)}")
        return
    try:
        import tweepy
    except Exception:
        _record(WARN, "X", "tweepy 未インストールのためスキップ（本番環境では入っています）")
        return
    # v2（投稿API）の認証確認
    try:
        client = tweepy.Client(
            consumer_key=_env("X_API_KEY"), consumer_secret=_env("X_API_KEY_SECRET"),
            access_token=_env("X_ACCESS_TOKEN"), access_token_secret=_env("X_ACCESS_TOKEN_SECRET"),
        )
        me = client.get_me()
        uname = getattr(me.data, "username", "?")
        _record(OK, "X 認証（v2/投稿）", f"@{uname} で認証OK")
    except Exception as e:
        msg = str(e)
        hint = ""
        if "401" in msg or "Unauth" in msg:
            hint = " → キーの誤り。権限をRead+Writeにした後、Access Tokenを再生成して4つ入れ直す"
        _record(NG, "X 認証（v2/投稿）", msg[:120] + hint)
    # v1.1（画像アップロード）の認証確認
    try:
        import tweepy
        auth = tweepy.OAuth1UserHandler(
            _env("X_API_KEY"), _env("X_API_KEY_SECRET"),
            _env("X_ACCESS_TOKEN"), _env("X_ACCESS_TOKEN_SECRET"))
        tweepy.API(auth).verify_credentials()
        _record(OK, "X 認証（v1.1/画像）", "画像アップロード用の認証OK")
    except Exception as e:
        _record(NG, "X 認証（v1.1/画像）", str(e)[:120])
    _record(WARN, "X クレジット", "残高は投稿時のみ判定可（402=credits depleted）。Developer Consoleで残高を確認")


def check_anthropic():
    print("\n■ Anthropic（Claude）")
    key = _env("ANTHROPIC_API_KEY")
    if not key:
        _record(NG, "Anthropic", "ANTHROPIC_API_KEY が未設定")
        return
    try:
        r = requests.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=15,
        )
        if r.status_code == 200:
            _record(OK, "Anthropic", "APIキー有効")
        elif r.status_code in (401, 403):
            _record(NG, "Anthropic", f"APIキーが無効（{r.status_code}）")
        else:
            _record(NG, "Anthropic", f"HTTP {r.status_code} / {r.text[:120]}")
    except Exception as e:
        _record(NG, "Anthropic", str(e))


def check_line():
    print("\n■ LINE")
    token = _env("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        _record(SKIP, "LINE", "LINE_CHANNEL_ACCESS_TOKEN 未設定（LINE通知を使わないならOK）")
        return
    # トークン検証（公式アカウント情報）
    oa_name = ""
    try:
        r = requests.get("https://api.line.me/v2/bot/info",
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if r.status_code == 200:
            oa_name = r.json().get("displayName", "")
            _record(OK, "LINE トークン", f"公式アカウント「{oa_name}」")
        else:
            _record(NG, "LINE トークン", f"HTTP {r.status_code} / {r.text[:120]}")
            return
    except Exception as e:
        _record(NG, "LINE トークン", str(e))
        return
    # 宛先ごとに profile を取得（＝userId正当性＋友だち追加を同時判定）
    recipients = _line_recipients()
    if not recipients:
        _record(WARN, "LINE 宛先", "LINE_USER_ID が未設定（通知が誰にも届きません）")
        return
    for uid in recipients:
        try:
            r = requests.get(f"https://api.line.me/v2/bot/profile/{uid}",
                             headers={"Authorization": f"Bearer {token}"}, timeout=15)
            if r.status_code == 200:
                name = r.json().get("displayName", "?")
                _record(OK, f"LINE 宛先 {uid[:6]}…", f"「{name}」に送信可能（友だち登録OK）")
            elif r.status_code in (400, 404):
                _record(NG, f"LINE 宛先 {uid[:6]}…",
                        "userIdが不正、または公式アカウントを友だち追加していない/ブロック中")
            else:
                _record(NG, f"LINE 宛先 {uid[:6]}…", f"HTTP {r.status_code} / {r.text[:100]}")
        except Exception as e:
            _record(NG, f"LINE 宛先 {uid[:6]}…", str(e))


def check_ai_image():
    print("\n■ AIアイキャッチ（任意）")
    key = _env("FAL_KEY")
    if not key:
        _record(SKIP, "AIアイキャッチ", "FAL_KEY 未設定（使わないならOK。config ai_eyecatch: false）")
        return
    # 実生成は課金が発生するためキーの存在のみ確認（形式: 通常 uuid:hex）
    if ":" in key and len(key) > 20:
        _record(OK, "AIアイキャッチ", "FAL_KEY 設定済み（config ai_eyecatch: true で有効化）")
    else:
        _record(WARN, "AIアイキャッチ", "FAL_KEYの形式が想定と異なります。fal.aiのキーか確認")


def check_threads():
    print("\n■ Threads")
    token = _env("THREADS_ACCESS_TOKEN")
    uid   = _env("THREADS_USER_ID")
    if not token:
        _record(SKIP, "Threads", "THREADS_ACCESS_TOKEN 未設定（Threads投稿を使わないならOK）")
        return
    try:
        r = requests.get("https://graph.threads.net/v1.0/me",
                         params={"fields": "id,username", "access_token": token},
                         timeout=15)
        if r.status_code == 200:
            data = r.json()
            got_id = str(data.get("id", ""))
            uname  = data.get("username", "?")
            _record(OK, "Threads トークン", f"@{uname}（id: {got_id[:6]}…）")
            if uid and uid != got_id:
                _record(NG, "Threads userId",
                        f"THREADS_USER_ID がトークンの持ち主と不一致（設定値 {uid[:6]}… ≠ 実際 {got_id[:6]}…）")
            elif not uid:
                _record(WARN, "Threads userId",
                        f"THREADS_USER_ID 未設定。この値を登録してください → {got_id}")
            else:
                _record(OK, "Threads userId", "トークンと一致")
        elif r.status_code in (401, 400):
            _record(NG, "Threads トークン",
                    "アクセストークンが無効/期限切れ（60日で失効。再生成が必要かも）")
        else:
            _record(NG, "Threads トークン", f"HTTP {r.status_code} / {r.text[:120]}")
    except Exception as e:
        _record(NG, "Threads", str(e))


def main():
    ap = argparse.ArgumentParser(description="PostPilot 事前チェック")
    ap.add_argument("--client", help="clients/xxx.yaml（env_prefixを適用）")
    args = ap.parse_args()

    print("=" * 54)
    print("  PostPilot 事前チェック（doctor）")
    print("=" * 54)
    if args.client:
        apply_client_prefix(args.client)

    check_notion()
    check_x()
    check_anthropic()
    check_line()
    check_threads()
    check_ai_image()

    ng = sum(1 for s, _ in _results if s == NG)
    ok = sum(1 for s, _ in _results if s == OK)
    print("\n" + "=" * 54)
    if ng == 0:
        print(f"  結果: すべてOK（✅ {ok}件）。本番投入して大丈夫です🚀")
        print("=" * 54)
        sys.exit(0)
    else:
        print(f"  結果: ❌ {ng}件 の問題あり（✅ {ok}件）。上記を直してから本番投入を。")
        print("=" * 54)
        sys.exit(1)


if __name__ == "__main__":
    main()
