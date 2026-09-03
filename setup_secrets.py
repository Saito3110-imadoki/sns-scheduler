"""
GitHub Secrets 一括登録スクリプト
ローカルの .env ファイルを読み込み、gh CLI で対象リポジトリの
Actions Secrets へ一括登録する。UIでの手入力（コピペ事故・空白混入）を撲滅する。

前提:
  - GitHub CLI (gh) がインストール済みで `gh auth login` 済みであること
    https://cli.github.com/

使い方:
  # 1) .env.example をコピーして値を埋める（値はこのファイルにしか置かない）
  cp sns_scheduler/.env.example clients/stora.env
  # 2) まず dry-run で登録内容（キー名のみ）を確認
  python sns_scheduler/setup_secrets.py --env clients/stora.env --repo Saito3110-imadoki/pp-stora --dry-run
  # 3) 本登録
  python sns_scheduler/setup_secrets.py --env clients/stora.env --repo Saito3110-imadoki/pp-stora

セキュリティ:
  - 値は画面に一切表示しない（キー名と文字数のみ表示）
  - 登録が終わったら .env ファイルは安全な場所に保管 or 削除を推奨
"""

import argparse
import subprocess
import sys
from pathlib import Path

# 登録対象のSecret名（これ以外のキーが.envにあっても無視する）
ALLOWED_KEYS = [
    "NOTION_TOKEN",
    "NOTION_DATABASE_ID",
    "X_API_KEY",
    "X_API_KEY_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
    "ANTHROPIC_API_KEY",
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_USER_ID",
    "LINE_USER_ID2",
    "THREADS_USER_ID",
    "THREADS_ACCESS_TOKEN",
]


def parse_env(path: Path) -> dict[str, str]:
    """.env を読み込む。KEY=VALUE 形式。前後の空白・引用符・改行を除去。"""
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'").strip()
        if key in ALLOWED_KEYS and val:
            values[key] = val
    return values


def check_gh() -> bool:
    try:
        r = subprocess.run(["gh", "auth", "status"],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="GitHub Secrets 一括登録")
    ap.add_argument("--env", required=True, help=".env ファイルのパス")
    ap.add_argument("--repo", required=True,
                    help="対象リポジトリ（例: Saito3110-imadoki/pp-stora）")
    ap.add_argument("--dry-run", action="store_true",
                    help="登録せず、対象キーの一覧だけ表示")
    args = ap.parse_args()

    env_path = Path(args.env)
    if not env_path.exists():
        print(f"❌ ファイルが見つかりません: {env_path}")
        sys.exit(1)

    values = parse_env(env_path)
    if not values:
        print("❌ 登録対象のキーが1つも見つかりませんでした。.env の中身を確認してください。")
        print(f"   対象キー: {', '.join(ALLOWED_KEYS)}")
        sys.exit(1)

    missing = [k for k in ALLOWED_KEYS if k not in values]

    print("=" * 56)
    print(f"  Secrets 一括登録 → {args.repo}")
    print("=" * 56)
    print(f"\n  登録するキー（{len(values)}件）※値は表示しません")
    for k, v in values.items():
        print(f"    ・{k}（{len(v)}文字）")
    if missing:
        print(f"\n  ⏭️  .envに無いためスキップ: {', '.join(missing)}")
        print("     （LINE_USER_ID2 / THREADS_* は使わない構成なら無くてOK）")

    if args.dry_run:
        print("\n  --dry-run のため登録は行いませんでした。")
        sys.exit(0)

    if not check_gh():
        print("\n❌ GitHub CLI (gh) が使えません。")
        print("   インストール: https://cli.github.com/")
        print("   ログイン    : gh auth login")
        sys.exit(1)

    print()
    ok = ng = 0
    for k, v in values.items():
        r = subprocess.run(
            ["gh", "secret", "set", k, "--repo", args.repo, "--body", v],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            print(f"  ✅ {k} 登録完了")
            ok += 1
        else:
            print(f"  ❌ {k} 登録失敗: {r.stderr.strip()[:120]}")
            ng += 1

    print("\n" + "=" * 56)
    if ng == 0:
        print(f"  完了: {ok}件すべて登録しました🎉")
        print("  次は Actions → Doctor (事前チェック) で全✅か確認しましょう。")
    else:
        print(f"  完了: ✅{ok}件 / ❌{ng}件。失敗分は上記メッセージを確認してください。")
    print("=" * 56)
    sys.exit(0 if ng == 0 else 1)


if __name__ == "__main__":
    main()
