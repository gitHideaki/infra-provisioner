# CLAUDE.md

このファイルは、Claude Code (claude.ai/code) がこのリポジトリで作業する際のガイダンスを提供します。

## このアプリについて

Netbird の API を5分ごとにポーリングし、Peer 接続済み（= EntraID OIDC 経由で初回ログイン済み）のユーザーを検出する。新規ユーザーを発見したら、Netbird（グループ追加）と Proxmox（ユーザーアカウント・Resource Pool・SDN VNet・PVEVMUser ロール）の両方に自動プロビジョニングする。状態は SQLite で管理しており、リトライを行っても冪等に動作する。

**本プロジェクトは Python / SQLAlchemy / Alembic の学習を兼ねているため、コードには処理の意図・ライブラリの動作・設計上の判断を説明するコメントを丁寧に残すこと。**

## コマンド

```bash
# ローカル開発（Docker なし）
pip install -r requirements.txt
cp .env.example .env   # 実際の値を設定する
cd app && python main.py

# スケジューラーを起動せず1回だけ同期実行
cd app && python -c "from sync import run_sync; run_sync()"

# Docker
docker compose up --build      # ビルドして起動
docker compose logs -f         # ログをリアルタイム表示
docker compose down

# DB 確認（SQLite）
sqlite3 data/app.db ".tables"
sqlite3 data/app.db "SELECT * FROM users;"
sqlite3 data/app.db "SELECT * FROM sync_logs ORDER BY started_at DESC LIMIT 5;"

# Alembic マイグレーション
cd app
alembic upgrade head                              # 未適用マイグレーションをすべて適用
alembic revision --autogenerate -m "説明"         # モデル変更からマイグレーションを自動生成
alembic history                                   # マイグレーション履歴を表示
```

## アーキテクチャ

アプリのコードはすべて `app/` 配下にある。エントリポイントの `main.py` が `schedule` ライブラリで `SYNC_INTERVAL_SECONDS` 間隔のジョブを登録し、`sync.run_sync()` を繰り返し呼び出す。

**`sync.run_sync()` のデータフロー:**

1. `clients/netbird.py` → 全ユーザーと全 Peer を取得
2. Peer が1件以上存在するユーザー（= ログイン済み）に絞り込む
3. SQLAlchemy で `users` テーブルに upsert（新規登録 or 最終同期日時を更新）
4. `netbird_provisioned=False` の行 → `provisioners/netbird.py` を呼び出す
5. `proxmox_provisioned=False` の行 → `provisioners/proxmox.py` を呼び出す
6. 実行結果を `sync_logs` テーブルに記録

**各レイヤーの責務:**

- `clients/` — ステートレスな HTTP ラッパー。2xx 以外はすべて例外を raise する。ビジネスロジックは持たない。
- `provisioners/` — 1ユーザー分の複数 API 呼び出しを順番に実行し、成功したら DB フラグを更新する。
- `models.py` — SQLAlchemy ORM（`User`・`SyncLog`）。タイムスタンプはすべて UTC。
- `database.py` — engine・`SessionLocal` ファクトリ・`get_db()` コンテキストマネージャーを提供する。
- `config.py` — `os.environ` / `.env` から読み込む `dataclass`。シングルトン `settings` としてインポートして使う。

**Proxmox リソースの命名規則:**

- Pool ID: `pool-{email_local}`（メールのローカル部分、英数字のみ）
- VNet ID: `vnet-{email_local}`（Proxmox の VNet ID は最大8文字制限あり）
- Proxmox ユーザー: `{email_local}@{PROXMOX_USER_REALM}`

## 外部 API の注意事項

**Netbird** — `NETBIRD_SERVICE_TOKEN` を Bearer トークンとして使用。グループへのユーザー追加は `PUT /api/groups/{id}` にメンバー全員のリストを渡す「置き換え」方式（追記ではない）のため、既存メンバーを取得してから新規ユーザーを加えたリストを送る必要がある。

**Proxmox** — `Authorization: PVEAPIToken={PROXMOX_TOKEN_ID}={PROXMOX_TOKEN_SECRET}` ヘッダーで認証。セルフホスト環境のため SSL 検証はクライアント側でデフォルト無効（`verify=False`）。SDN の変更は最後に `PUT /api2/json/sdn` を呼び出して設定を反映させる必要がある。

## 環境変数

`.env.example` を `.env` にコピーして実際の値を設定する。`data/` ディレクトリはコンテナ内の `/data` にバインドマウントされ `app.db` を保持する。Docker 起動前にディレクトリが存在している必要がある。
