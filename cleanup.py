"""
承認待ちの棚卸し（古い投稿案の一括却下）

承認が滞ると「承認待ち」が数百件たまる。中身は当時のニュースが元ネタなので
時間が経つと使えなくなるが、残っているとレポートの集計に混ざり、
一括承認してしまうと大量投稿の事故につながる。

指定した日数より古い「承認待ち」を「却下」に変え、直近ぶんだけ残す。

使い方:
  python sns_scheduler/cleanup.py --older-than 14            # 確認だけ（既定）
  python sns_scheduler/cleanup.py --older-than 14 --apply    # 実際に変更する

安全側の設計:
  - 既定は確認のみ。--apply を付けたときだけ書き込む
  - 対象は「承認待ち」だけ。未投稿・投稿済・エラーには触れない
  - 投稿日時が新しいものは残す（--older-than で境界を指定）
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()
JST = timezone(timedelta(hours=9))

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


NOTION_TOKEN       = os.environ["NOTION_TOKEN"].strip()
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"].strip()

PROP_TEXT     = _cfg("notion", "properties", "text",     default="投稿文")
PROP_DATETIME = _cfg("notion", "properties", "datetime", default="投稿日時")
PROP_STATUS   = _cfg("notion", "properties", "status",   default="ステータス")

STATUS_PENDING_APPROVAL = _cfg("notion", "status", "pending",  default="承認待ち")
STATUS_REJECTED         = _cfg("notion", "status", "rejected", default="却下")


def _title(page: dict) -> str:
    return "".join(t.get("plain_text", "")
                   for t in page["properties"].get(PROP_TEXT, {}).get("title", []))


def _dt(page: dict):
    d = page["properties"].get(PROP_DATETIME, {}).get("date") or {}
    if not d.get("start"):
        return None
    try:
        return datetime.fromisoformat(d["start"]).astimezone(JST)
    except ValueError:
        return None


def fetch_old_pending(notion: Client, before: datetime) -> list[dict]:
    """指定日時より前の「承認待ち」を全件取得する"""
    results, cursor = [], None
    while True:
        kwargs = {
            "database_id": NOTION_DATABASE_ID,
            "filter": {"and": [
                {"property": PROP_STATUS,
                 "multi_select": {"contains": STATUS_PENDING_APPROVAL}},
                {"property": PROP_DATETIME, "date": {"before": before.isoformat()}},
            ]},
            "sorts": [{"property": PROP_DATETIME, "direction": "ascending"}],
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            return results
        cursor = resp.get("next_cursor")


def reject(notion: Client, pages: list[dict]) -> int:
    """ステータスを「却下」に書き換える。失敗した行は飛ばして続行する"""
    done = 0
    for i, page in enumerate(pages, start=1):
        try:
            notion.pages.update(
                page_id=page["id"],
                properties={PROP_STATUS:
                            {"multi_select": [{"name": STATUS_REJECTED}]}},
            )
            done += 1
        except Exception as e:
            print(f"  [{i}/{len(pages)}] 失敗: {e}")
        if i % 20 == 0:
            print(f"  {i}/{len(pages)} 件…")
            time.sleep(1)      # Notion APIのレート制限に配慮
    return done


def run(older_than: int, apply: bool) -> None:
    now    = datetime.now(JST)
    before = now - timedelta(days=older_than)

    print(f"[{now.strftime('%Y-%m-%d %H:%M')} JST] 承認待ちの棚卸し")
    print(f"  対象: 投稿日時が {before.strftime('%Y-%m-%d %H:%M')} より前の「{STATUS_PENDING_APPROVAL}」")
    print(f"  モード: {'実行（却下に変更します）' if apply else '確認のみ（変更しません）'}")

    notion = Client(auth=NOTION_TOKEN)
    try:
        pages = fetch_old_pending(notion, before)
    except Exception as e:
        print(f"Notionの取得に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)

    if not pages:
        print("\n対象はありませんでした。")
        return

    dts = [d for d in (_dt(p) for p in pages) if d]
    print(f"\n  対象: {len(pages)}件")
    if dts:
        print(f"  期間: {min(dts).date()} 〜 {max(dts).date()}")

    print("\n  【最も古い3件】")
    for p in pages[:3]:
        d = _dt(p)
        print(f"    {d.date() if d else '日付なし'}  {_title(p)[:44]}")
    if len(pages) > 3:
        print("  【最も新しい3件】")
        for p in pages[-3:]:
            d = _dt(p)
            print(f"    {d.date() if d else '日付なし'}  {_title(p)[:44]}")

    if not apply:
        print(f"\n  ※ 確認のみのため変更していません。")
        print(f"     実行する場合は apply を true にして再実行してください。")
        print(f"     残したい投稿がある場合は、日数を短くして対象を絞ってください。")
        return

    print(f"\n  {len(pages)}件を「{STATUS_REJECTED}」に変更します…")
    done = reject(notion, pages)
    print(f"\n完了 — {done}件を却下しました（失敗 {len(pages) - done}件）")
    if done:
        print("  ※ 却下した投稿は配信されません。Notion上には残ります")


def main() -> None:
    ap = argparse.ArgumentParser(description="承認待ちの棚卸し（古い投稿案の一括却下）")
    ap.add_argument("--older-than", type=int, default=14,
                    help="この日数より古い承認待ちを対象にする（既定: 14）")
    ap.add_argument("--apply", action="store_true",
                    help="実際に却下する。付けない場合は確認のみ")
    args = ap.parse_args()
    if args.older_than < 1:
        print("--older-than は1以上を指定してください（直近の投稿案まで消える恐れがあります）")
        sys.exit(1)
    run(args.older_than, args.apply)


if __name__ == "__main__":
    main()
