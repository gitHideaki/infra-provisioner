"""
SQLAlchemy ORM モデル定義。

ORM（Object-Relational Mapper）とは:
  Python のクラス（オブジェクト）と DB のテーブルを対応付ける仕組み。
  SQL を直接書かなくても Python コードで DB を操作できる。

このファイルで定義するテーブル:
  - users      : プロビジョニング管理対象ユーザー
  - sync_logs  : 同期処理の実行ログ
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    """タイムゾーン付き UTC 現在時刻を返すヘルパー。"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """
    全モデルの基底クラス。

    SQLAlchemy 2.0 では DeclarativeBase を継承したクラスを作り、
    そこからさらに各モデルを継承するパターンが推奨されている。
    """
    pass


class User(Base):
    """
    Netbird/Proxmox へのプロビジョニング状態を管理するテーブル。

    カラムは「基本情報」→「Netbird 状態」→「Proxmox 状態」→「削除状態」→「タイムスタンプ」の順。

    Mapped[型] という書き方は SQLAlchemy 2.0 の型アノテーション構文。
    mapped_column() でデフォルト値や制約を指定する。
    """

    __tablename__ = "users"

    # ── 基本情報 ────────────────────────────────────────────────────────
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # EntraID の UPN（例: taro@example.com）。全体でユニーク
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    # Netbird から取得した表示名
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)

    # Netbird が内部で使うユーザー UUID
    netbird_user_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)

    # ── Netbird プロビジョニング状態 ──────────────────────────────────────
    # Netbird グループへの追加が完了したか
    netbird_provisioned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    netbird_provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Proxmox プロビジョニング状態 ──────────────────────────────────────
    # ユーザー作成・Pool・VNet・ACL 全ての設定が完了したか
    proxmox_provisioned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    proxmox_provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Proxmox 上のユーザーID（例: taro@pve）
    proxmox_user_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Proxmox Resource Pool の ID（例: pool-taro）
    proxmox_pool_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Proxmox SDN VNet の ID（例: vnettaro）
    proxmox_vnet_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # VNet に設定したサブネット CIDR（例: 10.0.0.0/24）
    proxmox_subnet: Mapped[str | None] = mapped_column(String, nullable=True)

    # ── 削除・デプロビジョニング状態 ─────────────────────────────────────
    # True = 有効ユーザー / False = 削除済みユーザー
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # クリーンアップが完了した日時
    deprovisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 削除検知の理由
    #   "blocked"   : Netbird の is_blocked=true を検知（EntraID 削除の代理シグナル）
    #   "not_found" : Netbird API のユーザーリストから消えた
    #   "manual"    : CLI による手動デプロビジョニング
    deprovision_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    # ── タイムスタンプ ────────────────────────────────────────────────────
    # DB に初めて登録された日時
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    # 最後に同期処理が実行された日時（5分ごとに更新）
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<User id={self.id} email={self.email!r} "
            f"netbird={self.netbird_provisioned} proxmox={self.proxmox_provisioned} "
            f"active={self.is_active}>"
        )


class TokenStore(Base):
    """
    ローテーション済みトークンを保持するテーブル。

    アプリは起動時にこのテーブルを確認し、アクティブなトークンがあれば
    .env の値より優先して使用する。
    ローテーションは rotator.py が定期実行し、新旧の切り替えもここで管理する。

    service:
      "netbird" → Netbird PAT（Personal Access Token）
      "proxmox" → Proxmox API トークンのシークレット
    """

    __tablename__ = "token_store"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # "netbird" または "proxmox"
    service: Mapped[str] = mapped_column(String, nullable=False)

    # Netbird: PAT の UUID / Proxmox: tokenname（例: provisioner）
    # 旧トークン削除時にこの ID を使う
    token_id: Mapped[str] = mapped_column(String, nullable=False)

    # 実際のシークレット文字列（DB はローカルのため平文保存）
    token_secret: Mapped[str] = mapped_column(String, nullable=False)

    # トークンの有効期限（UTC）
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # このレコードが現在使用中かどうか（最新の1件だけ True になる）
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ローテーション実行日時
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<TokenStore id={self.id} service={self.service!r} "
            f"token_id={self.token_id!r} active={self.is_active} "
            f"expires_at={self.expires_at}>"
        )


class SyncLog(Base):
    """
    同期処理の実行結果を記録するテーブル。

    5分ごとの処理が何件成功・失敗したかをここに残すことで、
    運用中の問題把握やデバッグに役立てる。
    """

    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 同期処理の開始・終了日時
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 今回の同期で新しく DB に追加されたユーザー数
    users_added: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 今回の同期でプロビジョニングに成功したユーザー数
    users_provisioned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 今回の同期でデプロビジョニングに成功したユーザー数
    users_deprovisioned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # エラーが発生した場合の詳細（JSON 文字列として保存）
    # 例: '[{"email": "a@b.com", "error": "connection timeout"}]'
    errors: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 処理全体のステータス
    #   "success" : エラーなし
    #   "partial" : 一部エラーあり
    #   "error"   : 処理全体が失敗
    status: Mapped[str] = mapped_column(String, default="success", nullable=False)

    def __repr__(self) -> str:
        return (
            f"<SyncLog id={self.id} status={self.status!r} "
            f"added={self.users_added} provisioned={self.users_provisioned} "
            f"deprovisioned={self.users_deprovisioned}>"
        )
