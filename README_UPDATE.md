# sns-scheduler 更新パッケージ

このフォルダの中身を sns-scheduler リポジトリのルートにそのまま配置してください。

## ファイル一覧と配置先

| ファイル | 配置先 | 状態 |
|---|---|---|
| `daily_generator.py` | ルート | 置き換え（プロンプト大幅改善・実績学習・重複防止・曜日カレンダー） |
| `poster.py` | ルート | 置き換え（Threads文面・セルフリプライ・文字数ガード・エラー通知） |
| `infographic.py` | ルート | 置き換え（ブランドカラー・ウォーターマーク対応） |
| `notify.py` | ルート | **新規**（LINE通知の共通モジュール） |
| `analytics.py` | ルート | **新規**（毎日22時のいいね・インプ計測） |
| `config.yaml` | ルート | **新規**（全設定。会社名・カラー・曜日カレンダーなど） |
| `requirements.txt` | ルート | 置き換え（PyYAML追加） |
| `.github/workflows/daily_generate.yml` | 同パス | 置き換え |
| `.github/workflows/auto_poster.yml` | 同パス | **新規**（毎時の自動投稿） |
| `.github/workflows/analytics.yml` | 同パス | **新規**（毎日22時の計測） |

## 更新後の確認手順

1. すべてのファイルをコミット
2. Actions → Daily Post Generator → Run workflow で手動実行
3. ログで以下を確認:
   - `[config] config.yaml 読み込み完了`
   - `重複防止: 直近N件のテーマを回避対象に設定`
   - `レンダラー: playwright (高解像度)`
4. Notionに投稿が届き、「Threads用文面」「リプライ文面」「投稿タイプ」が埋まっていればOK

## Notion側の前提（作成済みのはず）

- Threads用文面（テキスト）
- リプライ文面（テキスト）
- 投稿タイプ（セレクト）
- X投稿ID（テキスト）
- いいね数 / RT数 / リプライ数 / インプレッション（数値）
- 計測日時（日時）

## 注意

- APIキー・トークンはこのパッケージに含まれていません（GitHub Secretsをそのまま使用）
- X Premiumアカウント前提の設定です（config.yaml の x_char_limit: 0）
