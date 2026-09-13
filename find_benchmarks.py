"""
ベンチマークアカウント候補の抽出

config の topics.benchmark_accounts に何を入れるかを、勘ではなく実データで決めるためのツール。
設定キーワードで実際に伸びている投稿を集め、その投稿主を反応の良さ順に並べて出す。

手本の基準はフォロワー数ではなく「表示回数（インプレッション）」。
フォロワーが多くても読まれていないアカウントはあり、少なくても伸びる投稿を
出せるアカウントもある。実際に読まれている投稿を出しているかで判定する。

使い方:
  python sns_scheduler/find_benchmarks.py
  python sns_scheduler/find_benchmarks.py --min-impressions 10000
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


def collect(keywords: list[str], per_query: int = 30) -> list[dict]:
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
                "imp":       m.get("impression_count", 0) or 0,
                "keyword":   kw,
                "text":      tw.text.replace("\n", " ")[:60],
            })
    return rows


def rank(rows: list[dict], top: int,
         min_impressions: int = 5000) -> tuple[list[dict], dict]:
    """「実際に表示されている投稿」を出しているアカウントに絞って集約する。

    フォロワー数は手本の基準にならない。フォロワーが多くても読まれていない
    アカウントはあるし、少なくても伸びる投稿を出せるアカウントもある。
    そこで投稿1件ごとの表示回数（インプレッション）で足切りし、
    その基準を超えた投稿を出しているアカウントだけを候補にする。

    戻り値は (候補, 集計情報)。"""
    with_imp = [r for r in rows if r["imp"] > 0]
    # 0を指定したときは足切りせず、いいね数で見る（表示回数が取れない環境向け）
    hot = rows if min_impressions <= 0 else [r for r in with_imp
                                             if r["imp"] >= min_impressions]

    stats = {
        "collected":   len(rows),
        "with_imp":    len(with_imp),   # 表示回数が取得できた投稿
        "hot":         len(hot),        # 基準を超えた投稿
        "imp_missing": len(rows) - len(with_imp),
    }

    by_user: dict[str, list[dict]] = defaultdict(list)
    for r in hot:
        by_user[r["username"]].append(r)

    out = []
    for username, hits in by_user.items():
        n     = len(hits)
        likes = sum(h["likes"] for h in hits) / n
        imp   = sum(h["imp"] for h in hits) / n
        best  = max(h["imp"] for h in hits)
        # 表示に対して何%が反応したか。規模の違うアカウントを横並びにできる
        er    = round(likes / imp * 100, 2) if imp else 0.0
        out.append({
            "username": username, "name": hits[0]["name"], "bio": hits[0]["bio"],
            "followers": hits[0]["followers"], "hits": n,
            "likes": round(likes, 1),
            "rts":  round(sum(h["rts"] for h in hits) / n, 1),
            "reps": round(sum(h["reps"] for h in hits) / n, 1),
            "imp": int(imp), "best_imp": int(best), "er": er,
            "keywords": "/".join(sorted({h["keyword"] for h in hits})),
            "sample": max(hits, key=lambda h: h["imp"])["text"],
        })
    # よく読まれている投稿を多く出しているアカウントほど手本にする価値がある。
    # 同点なら平均表示回数の多い順（表示が取れない環境ではいいね数で代用）
    key = ((lambda r: (r["hits"], r["likes"])) if min_impressions <= 0
           else (lambda r: (r["hits"], r["imp"])))
    out.sort(key=key, reverse=True)
    return out[:top], stats


def run(keywords: list[str], top: int, min_impressions: int = 5000,
        per_query: int = 30) -> None:
    if not keywords:
        print("キーワードがありません。config の topics.keywords_x を設定するか "
              "--keywords で指定してください")
        sys.exit(1)

    print(f"■ 検索キーワード: {' / '.join(keywords)}")
    print(f"■ 基準: 表示回数（インプレッション）{min_impressions:,}以上の投稿を出しているアカウント")
    rows = collect(keywords, per_query=per_query)
    print(f"  収集: {len(rows)}投稿")
    if not rows:
        print("\n候補が集まりませんでした。キーワードを見直すか、"
              "X APIのクレジット残高を確認してください。")
        return

    ranked, st = rank(rows, top, min_impressions)
    print(f"  表示回数あり: {st['with_imp']}投稿 / "
          f"基準クリア: {st['hot']}投稿")

    # 他人の投稿の表示回数はAPIの契約次第で取得できないことがある。
    # 全件0のときは「該当なし」ではなく「測れていない」ので、そう伝える
    if st["with_imp"] == 0 and min_impressions > 0:
        print("\n⚠ 収集した投稿の表示回数が1件も取得できませんでした。")
        print("  X APIのプランによっては、他人の投稿の表示回数が返らないことがあります。")
        print("  その場合はこの基準では絞り込めないため、いいね数で見てください:")
        print("    python find_benchmarks.py --min-impressions 0")
        return

    if not ranked:
        print(f"\n表示回数{min_impressions:,}以上の投稿が見つかりませんでした。")
        print("  基準を下げるか、キーワードを見直してください。例:")
        print(f"    python find_benchmarks.py --min-impressions {min_impressions // 2}")
        return

    print(f"\n■ ベンチマーク候補（{len(ranked)}件）")
    print("  アカウント              表示/本   最高表示  該当数  いいね/本   反応率  ヒットしたキーワード")
    print("  " + "-" * 100)
    for r in ranked:
        print(f"  {_pad('@' + r['username'], 22)} {r['imp']:>7,} {r['best_imp']:>10,} "
              f"{r['hits']:>6} {r['likes']:>9} {r['er']:>7}%  {r['keywords']}")

    print("\n■ 候補の詳細")
    for r in ranked:
        print(f"\n  @{r['username']}（{r['name']}）フォロワー{r['followers']:,}")
        if r["bio"]:
            print(f"    プロフィール: {r['bio'][:80]}")
        print(f"    最も読まれた投稿（{r['best_imp']:,}表示）: {r['sample']}")

    weak = [r for r in ranked if r["hits"] < 2]
    if weak:
        print(f"\n  ※ 該当1件だけの候補が{len(weak)}件あります。"
              "たまたま1投稿が伸びただけの可能性があり、")
        print("     手本として妥当かはプロフィールと投稿例で必ず確認してください。")

    print("\n■ 次の手順")
    print("  1. 上の候補から、自社と方向性が合うアカウントを3件選ぶ")
    print("     ・該当数が多いほど、継続して読まれている＝再現性のある型を持っています")
    print("     ・表示は多いが反応率が低いアカウントは、内容ではなく話題性で"
          "伸びただけの可能性があります")
    print("     ・企業の公式アカウントより、個人アカウントの方が学べる型が多いです")
    print("     ・同業の競合より、少し隣接した分野の方が真似だと思われにくいです")
    print("     ・プロフィールを読み、発信テーマが自社と重なるかを必ず確認してください")
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
    ap.add_argument("--min-impressions", type=int, default=5000,
                    help="この表示回数以上の投稿を出したアカウントだけを候補にする（既定: 5000）")
    ap.add_argument("--per-query", type=int, default=30,
                    help="1キーワードあたりに取得する投稿数。10〜100（既定: 30）")
    args = ap.parse_args()
    kws = args.keywords or (_cfg("topics", "keywords_x", default=[]) or [])
    run([str(k) for k in kws][:5], args.top,
        args.min_impressions, max(10, min(100, args.per_query)))


if __name__ == "__main__":
    main()
