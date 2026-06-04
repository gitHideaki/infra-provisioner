"""
アプリ全体の設定管理モジュール。

環境変数（または .env ファイル）から設定値を読み込み、
dataclass として型安全に提供する。

使い方:
    from config import settings
    print(settings.netbird_api_url)
"""

import os
from dataclasses import dataclass

# python-dotenv ライブラリを使って .env ファイルを読み込む。
# Docker 環境では env_file: .env で注入されるため実質的には開発時専用。
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    """
    全設定値を保持する dataclass。

    dataclass を使うメリット:
    - 型ヒントが付くため IDE の補完が効く
    - インスタンス生成時に必須フィールドの存在チェックができる
    - frozen=True にすると不変オブジェクトにできる（ここでは省略）
    """

    # ── Netbird ──────────────────────────────────────────────────────────
    # Netbird Management API のベース URL（末尾スラッシュなし）
    netbird_api_url: str

    # Service Account から発行した Personal Access Token
    # 形式: nbp_xxxxxxxxxxxxxxxxxxxxxxxxxx
    netbird_service_token: str

    # プロビジョニング後にユーザーを追加する Netbird グループの ID
    netbird_provisioning_group_id: str

    # ── Proxmox ──────────────────────────────────────────────────────────
    # Proxmox API エンドポイント（例: https://192.168.1.10:8006）
    proxmox_host: str

    # API トークンの識別子。形式: user@realm!tokenname
    # 例: root@pam!provisioner
    proxmox_token_id: str

    # API トークンのシークレット文字列
    proxmox_token_secret: str

    # 操作対象の Proxmox ノード名（例: pve）
    proxmox_node: str

    # ユーザーごとの VNet を作成する VXLAN Zone の ID（例: vxlan1）
    proxmox_vxlan_zone: str

    # 作成するユーザーのレルム。"pve"（Proxmox 内部）または "pam"（Linux PAM）
    proxmox_user_realm: str

    # ユーザーに付与する Proxmox ロール（Pool への ACL）
    proxmox_user_role: str

    # SDN VNet に付与するロール（VNet への ACL）
    proxmox_sdn_role: str

    # VNet に設定するサブネット CIDR（全ユーザー共通）
    # VXLAN では VNet ごとに L2 が分離されるため、複数ユーザーで同一 CIDR を使用可能
    proxmox_subnet: str

    # ── アプリ設定 ────────────────────────────────────────────────────────
    # SQLite データベースファイルのパス（Docker では /data/app.db）
    db_path: str

    # 同期処理の実行間隔（秒）。デフォルト 300 秒 = 5 分
    sync_interval_seconds: int

    # ── トークンローテーション設定 ──────────────────────────────────────────
    # Netbird Service Account のユーザー ID（PAT 操作に必要）
    # Netbird UI: Settings > Service Accounts でユーザー ID を確認する
    netbird_service_user_id: str

    # トークンを何日ごとにローテーションするか。
    # 発行するトークンの有効期限はこの値 + 1 日（余裕を持たせるため）。
    # デフォルト 6 日 → 有効期限 7 日のトークンを 6 日ごとに切り替える。
    token_rotation_days: int


def _require_env(key: str) -> str:
    """
    環境変数を取得し、未設定の場合は ValueError を送出するヘルパー関数。

    os.environ.get() は未設定時に None を返すだけだが、
    この関数は起動時に明示的にエラーを出すことで「設定漏れ」を早期に検知できる。
    """
    value = os.environ.get(key)
    if not value:
        raise ValueError(f"必須の環境変数 '{key}' が設定されていません。.env を確認してください。")
    return value


def _load_settings() -> Settings:
    """
    環境変数から Settings オブジェクトを構築して返す。

    モジュールレベルで一度だけ呼ばれ、その結果を settings 変数に束縛する
    （シングルトンパターン）。
    """
    return Settings(
        # Netbird
        netbird_api_url=_require_env("NETBIRD_API_URL").rstrip("/"),
        netbird_service_token=_require_env("NETBIRD_SERVICE_TOKEN"),
        netbird_provisioning_group_id=_require_env("NETBIRD_PROVISIONING_GROUP_ID"),
        # Proxmox
        proxmox_host=_require_env("PROXMOX_HOST").rstrip("/"),
        proxmox_token_id=_require_env("PROXMOX_TOKEN_ID"),
        proxmox_token_secret=_require_env("PROXMOX_TOKEN_SECRET"),
        proxmox_node=_require_env("PROXMOX_NODE"),
        proxmox_vxlan_zone=_require_env("PROXMOX_VXLAN_ZONE"),
        proxmox_user_realm=os.environ.get("PROXMOX_USER_REALM", "pve"),
        proxmox_user_role=os.environ.get("PROXMOX_USER_ROLE", "PVEVMUser"),
        proxmox_sdn_role=os.environ.get("PROXMOX_SDN_ROLE", "PVESDNUser"),
        proxmox_subnet=os.environ.get("PROXMOX_SUBNET", "10.0.0.0/24"),
        # App
        db_path=os.environ.get("DB_PATH", "/data/app.db"),
        sync_interval_seconds=int(os.environ.get("SYNC_INTERVAL_SECONDS", "300")),
        # Token rotation
        netbird_service_user_id=_require_env("NETBIRD_SERVICE_USER_ID"),
        token_rotation_days=int(os.environ.get("TOKEN_ROTATION_DAYS", "6")),
    )


# モジュールインポート時に一度だけ設定を読み込む。
# 他のモジュールは `from config import settings` で参照するだけでよい。
settings = _load_settings()
