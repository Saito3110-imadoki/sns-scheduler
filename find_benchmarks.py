"""
ベンチマークアカウント候補の抽出

config の topics.benchmark_accounts に何を入れるかを、勘ではなく実データで決めるためのツール。
設定キーワードで実際に伸びている投稿を集め、その投稿主を反応の良さ順に並べて出す。

使い方:
  python sns_scheduler/find_benchmarks.py
  python sns_scheduler/find_benchmarks.py --keywords 転職 面接対策 --top 15

出力された候補を目視で確認し、自社と方向性が合うアカウントだけを
config.yaml の topics.benchmark_accounts に3件ほど書き写してください。

※ X APIの検索を使うためクレジットが必要です（402が出る場合は残高切れ）。
※ Notionへの書き込みは行いません。読み取りと表示だけです。
"""

import argparse
import os
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import tweepy
import yaml
from dotenv import load_dotenv

load_dotenv()

_SCRIPT_DIR = Path(__file__).parent
_CFG: dict = {}
for _p in (_SCRIPT_DIR / "config.yaml", Path("config.yaml")):
    if _p.exists():
        with open(_p, encoding="utf-8") as f:
            _CFG = yaml.safe_load(f) or {}
        break


def _cfg(*keys, default=None):
    node = _CFG
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def _pad(s: str, width: int) -> str:
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    return s + " " * max(0, width - w)


def collect(keywords: list[str], per_query: int = 10) -> list[dict]:
    """キーワードごとに、直近で反応の良い投稿と投稿主を集める"""
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_KEY_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    me_id = ""
    try:
        me = client.get_me(user_auth=True)
        me_id = str(me.data.id) if me and me.data else ""
    except Exception as e:
        print(f"  自アカウント判定スキップ: {e}")

    rows: list[dict] = []
    for kw in keywords:
        query = f"{kw} lang:ja -is:retweet -is:reply"
        try:
            resp = client.search_recent_tweets(
                query=query, max_results=per_query,
                tweet_fields=["public_metrics", "text", "author_id"],
                expansions=["author_id"],
                user_fields=["username", "name", "public_metrics", "description"],
                sort_order="relevancy", user_auth=True,
            )
        except Exception as e:
            msg = str(e)
            print(f"  検索スキップ（{kw}）: {msg[:120]}")
            if "402" in msg:
                print("  ※ 402はX APIのクレジット切れです。"
                      "残高を補充しないとこのツールは動きません")
                return rows
            continue

        users = {str(u.id): u for u in ((resp.includes or {}).get("users") or [])}
        for tw in (resp.data or []):
            author = str(tw.author_id or "")
            if not author or (me_id and author == me_id):
                continue
            u = users.get(author)
            if not u:
                continue
            m  = tw.public_metrics or {}
            um = getattr(u, "public_metrics", None) or {}
            rows.append({
                "username":  u.username,
                "name":      getattr(u, "name", ""),
                "bio":       (getattr(u, "description", "") or "").replace("\n", " "),
                "followers": um.get("followers_count", 0),
                "likes":     m.get("like_count", 0),
                "rts":       m.get("retweet_count", 0),
                "reps":      m.get("reply_count", 0),
                "keyword":   kw,
                "text":      tw.text.replace("\n", " ")[:60],
            })
    return rows


def rank(rows: list[dict], top: int) -> list[dict]:
    """アカウント単位に集約する。
    フォロワーが多いだけの大手より、フォロワー比で反応が取れているアカウントの方が
    型として参考になるため、絶対数と反応率の両方を出す。"""
    by_user: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_user[r["username"]].append(r)

    out = []
    for username, hits in by_user.items():
        n     = len(hits)
        likes = sum(h["likes"] for h in hits) / n
        rts   = sum(h["rts"] for h in hits) / n
        reps  = sum(h["reps"] for h in hits) / n
        fol   = hits[0]["followers"]
        # フォロワー1000人あたりのいいね数。規模の違うアカウントを横並びにできる
        per_k = round(likes / fol * 1000, 1) if fol else 0.0
        out.append({
            "username": username, "name": hits[0]["name"], "bio": hits[0]["bio"],
            "followers": fol, "hits": n,
            "likes": round(likes, 1), "rts": round(rts, 1), "reps": round(reps, 1),
            "per_k": per_k,
            "keywords": "/".join(sorted({h["keyword"] for h in hits})),
            "sample": hits[0]["text"],
        })
    out.sort(key=lambda r: (r["hits"], r["likes"]), reverse=True)
    return out[:top]


def run(keywords: list[str], top: int) -> None:
    if not keywords:
        print("キーワードがありません。config の topics.keywords_x を設定するか "
              "--keywords で指定してください")
        sys.exit(1)

    print(f"■ 検索キーワード: {' / '.join(keywords)}")
    rows = collect(keywords)
    print(f"  収集: {len(rows)}投稿")
    if not rows:
        print("\n候補が集まりませんでした。キーワードを見直すか、"
              "X APIのクレジット残高を確認してください。")
        return

    ranked = rank(rows, top)
    print(f"\n■ ベンチマーク候補（{len(ranked)}件）")
    print("  アカウント              フォロワー  ヒット  いいね/本  1000フォロワー比  ヒットしたキーワード")
    print("  " + "-" * 96)
    for r in ranked:
        print(f"  {_pad('@' + r['username'], 22)} {r['followers']:>9} "
              f"{r['hits']:>6} {r['likes']:>9} {r['per_k']:>16}  {r['keywords']}")

    print("\n■ 候補の詳細")
    for r in ranked:
        print(f"\n  @{r['username']}（{r['name']}）")
        if r["bio"]:
            print(f"    プロフィール: {r['bio'][:80]}")
        print(f"    投稿例: {r['sample']}")

    print("\n■ 次の手順")
    print("  1. 上の候補から、自社と方向性が合うアカウントを3件選ぶ")
    print("     ・フォロワー数より「1000フォロワー比」が高い方が型として参考になります")
    print("     ・企業の公式アカウントより、個人アカウントの方が学べる型が多いです")
    print("     ・同業の競合より、少し隣接した分野の方が真似だと思われにくいです")
    print("  2. config.yaml の topics.benchmark_accounts に @なしで書く")
    print("       benchmark_accounts:")
    if ranked:
        for r in ranked[:3]:
            print(f"         - {r['username']}")
    print("  3. コミットすれば、翌日の生成から手本として学習に使われます")


def main() -> None:
    ap = argparse.ArgumentParser(description="ベンチマークアカウント候補の抽出")
    ap.add_argument("--keywords", nargs="*", default=None,
                    help="検索キーワード（未指定なら config の topics.keywords_x）")
    ap.add_argument("--top", type=int, default=12, help="表示する候補数（既定: 12）")
    args = ap.parse_args()
    kws = args.keywords or (_cfg("topics", "keywords_x", default=[]) or [])
    run([str(k) for k in kws][:5], args.top)


if __name__ == "__main__":
    main()
