"""
Netbird Management API のクライアントモジュール。

このクライアントの役割:
  - HTTP 通信の詳細（ヘッダー設定・エラーハンドリング）をここに閉じ込める
  - 呼び出し元（provisioners/）はビジネスロジックに集中できる
  - ステートレスな設計：インスタンスは1つ作ればどこからでも使い回せる

Netbird API 認証:
  Service Account から発行した PAT（Personal Access Token）を
  Authorization: Bearer <token> ヘッダーで送る。

API リファレンス: https://<your-netbird-host>/api/swagger
"""

import logging
from typing import Any

import requests

from config import settings

logger = logging.getLogger(__name__)


class NetbirdClient:
    """
    Netbird Management API へのリクエストをまとめたクラス。

    各メソッドは HTTP レスポンスをそのまま返さず、
    必要なデータだけを Python の dict / list 型に変換して返す。
    """

    def __init__(self) -> None:
        # 全リクエストに共通するヘッダーを事前に組み立てる
        self._headers = {
            "Authorization": f"Bearer {settings.netbird_service_token}",
            "Content-Type": "application/json",
        }
        self._base_url = settings.netbird_api_url

    def _get(self, path: str) -> Any:
        """
        GET リクエストの共通処理。

        requests.get() は HTTP レスポンスを Response オブジェクトとして返す。
        raise_for_status() は 4xx/5xx のときに例外（HTTPError）を送出する。
        .json() はレスポンスボディを JSON としてパースして dict/list で返す。
        """
        url = f"{self._base_url}{path}"
        response = requests.get(url, headers=self._headers, timeout=30)
        response.raise_for_status()
        return response.json()

    def _put(self, path: str, body: dict) -> Any:
        """PUT リクエストの共通処理。body は JSON にシリアライズして送信。"""
        url = f"{self._base_url}{path}"
        response = requests.put(url, headers=self._headers, json=body, timeout=30)
        response.raise_for_status()
        return response.json()

    def _delete(self, path: str) -> None:
        """
        DELETE リクエストの共通処理。

        Netbird の DELETE エンドポイントはボディなし・レスポンスも空のケースが多い。
        200/204 いずれも成功扱いとする。
        """
        url = f"{self._base_url}{path}"
        response = requests.delete(url, headers=self._headers, timeout=30)
        response.raise_for_status()

    # ── ユーザー操作 ───────────────────────────────────────────────────────

    def list_users(self) -> list[dict]:
        """
        全ユーザーの一覧を返す。

        レスポンス例（1ユーザー分）:
        {
            "id": "abc123",
            "email": "taro@example.com",
            "name": "Yamada Taro",
            "role": "user",
            "status": "active",
            "is_service_user": false,
            "is_blocked": false,       ← EntraID 削除検知に使う重要フィールド
            "issued": "integration",   ← "integration" = OIDC 経由でインポートされたユーザー
            "last_login": "2024-01-01T00:00:00Z"
        }
        """
        logger.debug("Netbird: ユーザー一覧を取得")
        return self._get("/api/users")

    def delete_user(self, user_id: str) -> None:
        """
        指定ユーザーを Netbird から削除する。

        デプロビジョニング時の最後のステップ。
        削除後は Netbird にログインできなくなる。
        """
        logger.info("Netbird: ユーザーを削除 user_id=%s", user_id)
        self._delete(f"/api/users/{user_id}")

    # ── Peer（接続端末）操作 ──────────────────────────────────────────────

    def list_peers(self) -> list[dict]:
        """
        全 Peer（Netbird クライアントで接続された端末）の一覧を返す。

        Peer に user_id フィールドが含まれており、
        「どのユーザーが接続したか」を判定するために使う。

        レスポンス例（1 Peer 分の主要フィールド）:
        {
            "id": "peer-xyz",
            "user_id": "abc123",   ← list_users() の id と対応
            "name": "my-laptop",
            "connected": true,
            "last_seen": "2024-01-01T12:00:00Z"
        }
        """
        logger.debug("Netbird: Peer 一覧を取得")
        return self._get("/api/peers")

    def get_peer_user_ids(self) -> set[str]:
        """
        Peer が存在するユーザーの user_id セットを返す。

        この関数が返すセットに含まれる user_id = 「少なくとも1回ログインしたことがある」
        = 初回プロビジョニングの対象。

        set() を使うのは重複を自動排除するため（同じユーザーが複数端末で接続可能）。
        """
        peers = self.list_peers()
        # Peer に user_id が設定されている（= ユーザーに紐付いている）ものだけ抽出
        return {p["user_id"] for p in peers if p.get("user_id")}

    # ── PAT（トークン）操作 ───────────────────────────────────────────────

    def create_token(self, user_id: str, name: str, expires_in: int) -> dict:
        """
        Service Account の PAT（Personal Access Token）を新規作成する。

        注意: レスポンスの "plain_token" フィールドは作成時の1回だけ返される。
        必ず DB に保存すること。

        Args:
            user_id: Service Account のユーザー ID（NETBIRD_SERVICE_USER_ID）
            name: トークンの識別名（例: "provisioner-20250601"）
            expires_in: 有効日数（例: 7 → 7日後に失効）

        Returns:
            {"id": "token-uuid", "name": "...", "plain_token": "nbp_xxx...", ...}
        """
        logger.info("Netbird: PAT を作成 user_id=%s name=%s expires_in=%d日", user_id, name, expires_in)
        return self._post(
            f"/api/users/{user_id}/tokens",
            {"name": name, "expires_in": expires_in},
        )

    def delete_token(self, user_id: str, token_id: str) -> None:
        """
        Service Account の PAT を削除する。

        Args:
            user_id: Service Account のユーザー ID
            token_id: 削除するトークンの UUID（create_token レスポンスの "id"）
        """
        logger.info("Netbird: PAT を削除 user_id=%s token_id=%s", user_id, token_id)
        self._delete(f"/api/users/{user_id}/tokens/{token_id}")

    def update_bearer_token(self, new_token: str) -> None:
        """
        インメモリの Bearer トークンを更新する。

        ローテーション後に呼び出し、以降のリクエストで新トークンを使用させる。
        コンテナ再起動なしにトークンを切り替えられる。

        Args:
            new_token: 新しい PAT の plain_token 文字列
        """
        self._headers["Authorization"] = f"Bearer {new_token}"
        logger.debug("Netbird: インメモリトークンを更新しました")

    # ── グループ操作 ───────────────────────────────────────────────────────

    def get_group(self, group_id: str) -> dict:
        """
        指定グループの情報を返す。

        グループにはどのユーザーが属しているかのリストが含まれる。
        PUT でグループを更新する前に現在のメンバーリストを取得するために使う。

        レスポンス例:
        {
            "id": "group-id",
            "name": "Infra Users",
            "peers": [
                {"id": "peer-xyz", "name": "my-laptop"},
                ...
            ]
        }
        """
        logger.debug("Netbird: グループ情報を取得 group_id=%s", group_id)
        return self._get(f"/api/groups/{group_id}")

    def add_user_peers_to_group(self, group_id: str, user_peer_ids: list[str]) -> None:
        """
        指定ユーザーの Peer をグループに追加する。

        重要: Netbird のグループ更新は「置き換え（replace）方式」。
        つまり PUT のボディに既存メンバー + 新しいメンバーの全リストを渡さないと、
        既存メンバーが削除されてしまう。

        手順:
          1. 現在のグループ情報を取得（既存の Peer ID を保持するため）
          2. 既存の Peer ID リスト + 新しい Peer ID を合算
          3. 合算したリストで PUT

        Args:
            group_id: 追加先グループの ID
            user_peer_ids: 追加する Peer の ID リスト
        """
        # Step 1: 現在のメンバーリストを取得
        group = self.get_group(group_id)
        existing_peer_ids = [p["id"] for p in group.get("peers", [])]

        # Step 2: 既存 + 新規をマージ（set で重複排除、list に戻して送信）
        all_peer_ids = list(set(existing_peer_ids + user_peer_ids))

        logger.info(
            "Netbird: グループにPeerを追加 group_id=%s 追加数=%d",
            group_id,
            len(user_peer_ids),
        )

        # Step 3: 全メンバーリストで更新
        self._put(f"/api/groups/{group_id}", {"peers": [{"id": pid} for pid in all_peer_ids]})

    def remove_user_peers_from_group(self, group_id: str, user_peer_ids: list[str]) -> None:
        """
        指定ユーザーの Peer をグループから除外する。

        add_user_peers_to_group と同様に置き換え方式のため、
        既存メンバーから対象ユーザーの Peer を除いたリストを PUT する。

        Args:
            group_id: 対象グループの ID
            user_peer_ids: 除外する Peer の ID リスト
        """
        group = self.get_group(group_id)
        existing_peer_ids = [p["id"] for p in group.get("peers", [])]

        # 除外対象を set で引き算して除く
        remove_set = set(user_peer_ids)
        remaining_peer_ids = [pid for pid in existing_peer_ids if pid not in remove_set]

        logger.info(
            "Netbird: グループからPeerを削除 group_id=%s 削除数=%d",
            group_id,
            len(user_peer_ids),
        )
        self._put(f"/api/groups/{group_id}", {"peers": [{"id": pid} for pid in remaining_peer_ids]})

    def get_user_peer_ids(self, user_id: str) -> list[str]:
        """
        指定ユーザーに紐付く全 Peer の ID リストを返す。

        デプロビジョニング時にグループから特定ユーザーの Peer を除外するために使う。
        """
        peers = self.list_peers()
        return [p["id"] for p in peers if p.get("user_id") == user_id]


# モジュールレベルのシングルトンインスタンス。
# 他モジュールは `from clients.netbird import netbird_client` でインポートして使う。
netbird_client = NetbirdClient()
