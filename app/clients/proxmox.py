"""
Proxmox VE REST API のクライアントモジュール。

Proxmox API の認証方式（API トークン）:
  Authorization: PVEAPIToken=<PROXMOX_TOKEN_ID>=<PROXMOX_TOKEN_SECRET>
  例: PVEAPIToken=root@pam!provisioner=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

  API トークンは Proxmox UI の「Datacenter > Permissions > API Tokens」から発行。
  トークンに付与するロールは「必要最低限」の権限のみにする（最小権限の原則）。

セルフホスト環境の注意:
  自己署名証明書を使っているケースが多いため、
  SSL 検証を verify=False で無効にしている。
  本番では適切な証明書を導入することが望ましい。

Proxmox API リファレンス: https://<your-proxmox>/pve-docs/api-viewer/
"""

import logging
from typing import Any

import requests
import urllib3

from config import settings

# 自己署名証明書の警告を抑制（verify=False を使う際の副作用）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class ProxmoxClient:
    """
    Proxmox VE REST API へのリクエストをまとめたクラス。

    Proxmox の API レスポンスは常に以下の構造:
        {"data": <実際のデータ>, "errors": {...}}

    このクラスでは data フィールドを取り出して返す。
    """

    def __init__(self) -> None:
        # API トークン認証ヘッダー
        self._headers = {
            "Authorization": (
                f"PVEAPIToken={settings.proxmox_token_id}={settings.proxmox_token_secret}"
            ),
            "Content-Type": "application/json",
        }
        self._base_url = settings.proxmox_host

    def _extract_data(self, response: requests.Response) -> Any:
        """
        Proxmox API レスポンスから data フィールドを取り出す共通処理。

        Proxmox は成功時も {"data": null} を返すことがあるため、
        None の場合もエラーとして扱わず None をそのまま返す。
        """
        response.raise_for_status()
        result = response.json()
        return result.get("data")

    def _get(self, path: str, params: dict | None = None) -> Any:
        """GET リクエスト。verify=False でセルフホスト SSL を許容。"""
        url = f"{self._base_url}{path}"
        response = requests.get(url, headers=self._headers, params=params, timeout=30, verify=False)
        return self._extract_data(response)

    def _post(self, path: str, body: dict | None = None) -> Any:
        """POST リクエスト。新規リソースの作成に使う。"""
        url = f"{self._base_url}{path}"
        response = requests.post(url, headers=self._headers, json=body or {}, timeout=30, verify=False)
        return self._extract_data(response)

    def _put(self, path: str, body: dict | None = None) -> Any:
        """PUT リクエスト。既存リソースの更新に使う。"""
        url = f"{self._base_url}{path}"
        response = requests.put(url, headers=self._headers, json=body or {}, timeout=30, verify=False)
        return self._extract_data(response)

    def _delete(self, path: str) -> Any:
        """DELETE リクエスト。リソースの削除に使う。"""
        url = f"{self._base_url}{path}"
        response = requests.delete(url, headers=self._headers, timeout=30, verify=False)
        return self._extract_data(response)

    # ── ユーザー操作 ───────────────────────────────────────────────────────

    def create_user(self, userid: str, comment: str = "") -> None:
        """
        Proxmox にユーザーを作成する。

        Args:
            userid: ユーザーID。形式は "{name}@{realm}"（例: taro@pve）
            comment: ユーザーの説明文（任意）

        Proxmox のユーザーレルム:
            pve  : Proxmox 内部認証（パスワード不要、このアプリで作る際に適切）
            pam  : Linux PAM 認証（OS ユーザーと連動）
            ldap : LDAP/AD 認証
        """
        logger.info("Proxmox: ユーザーを作成 userid=%s", userid)
        self._post(
            "/api2/json/access/users",
            {"userid": userid, "comment": comment, "enable": 1},
        )

    def delete_user(self, userid: str) -> None:
        """Proxmox からユーザーを削除する。"""
        logger.info("Proxmox: ユーザーを削除 userid=%s", userid)
        self._delete(f"/api2/json/access/users/{userid}")

    # ── ACL（アクセス制御）操作 ────────────────────────────────────────────

    def set_acl(self, path: str, userid: str, role: str) -> None:
        """
        指定パスに対してユーザーへのロールを付与する（ACL 設定）。

        Proxmox の権限モデル:
            「パス」に対して「ユーザー」に「ロール」を割り当てる。
            パスの例:
                /pool/{poolid}  : 特定のリソースプールのみ
                /vms/{vmid}     : 特定の VM のみ
                /               : Datacenter 全体（強すぎるので注意）

        Args:
            path: 権限を付与するパス（例: /pool/pool-taro）
            userid: 対象ユーザー（例: taro@pve）
            role: 付与するロール（例: PVEVMUser）
        """
        logger.info("Proxmox: ACL を設定 path=%s userid=%s role=%s", path, userid, role)
        self._put(
            "/api2/json/access/acl",
            {"path": path, "users": userid, "roles": role, "delete": 0},
        )

    def delete_acl(self, path: str, userid: str, role: str) -> None:
        """
        指定パスのユーザーロール割り当てを削除する。

        Proxmox の ACL 削除は DELETE エンドポイントではなく、
        PUT に delete=1 を付けることで行う（Proxmox API の仕様）。
        """
        logger.info("Proxmox: ACL を削除 path=%s userid=%s role=%s", path, userid, role)
        self._put(
            "/api2/json/access/acl",
            {"path": path, "users": userid, "roles": role, "delete": 1},
        )

    # ── Resource Pool 操作 ────────────────────────────────────────────────

    def create_pool(self, poolid: str, comment: str = "") -> None:
        """
        Resource Pool を作成する。

        Resource Pool はリソース（VM・ストレージなど）をグループ化する概念。
        ユーザーに Pool レベルで権限を付与することで、
        Pool 内のリソースにのみアクセスできるようにする。

        Args:
            poolid: Pool の ID（英数字とハイフンのみ使用可。例: pool-taro）
        """
        logger.info("Proxmox: Pool を作成 poolid=%s", poolid)
        self._post("/api2/json/pools", {"poolid": poolid, "comment": comment})

    def delete_pool(self, poolid: str) -> None:
        """
        Resource Pool を削除する。

        注意: Pool 内に VM が残っている場合は削除できない。
        Pool を削除する前に VM を他の Pool に移動するか削除する必要がある。
        """
        logger.info("Proxmox: Pool を削除 poolid=%s", poolid)
        self._delete(f"/api2/json/pools/{poolid}")

    # ── SDN（Software Defined Network）操作 ──────────────────────────────

    def create_vnet(self, vnet: str, zone: str, tag: int | None = None) -> None:
        """
        SDN VNet（仮想ネットワーク）を作成する。

        VNet は特定の SDN Zone 内に作成される論理ネットワーク。
        ユーザーごとに独立した VNet を作ることで、
        ユーザー間のネットワーク分離を実現する。

        重要な制約:
            VNet の ID は英数字のみ・最大 8 文字（Proxmox の制限）
            例: "vnettaro" (taro ユーザー用)

        Args:
            vnet: VNet の ID（最大8文字、英数字のみ）
            zone: VNet を作成する SDN Zone の ID（例: vxlan1）
            tag: VXLAN タグ番号（指定しない場合は自動割り当て）
        """
        body: dict = {"vnet": vnet, "zone": zone}
        if tag is not None:
            body["tag"] = tag

        logger.info("Proxmox: VNet を作成 vnet=%s zone=%s", vnet, zone)
        self._post("/api2/json/sdn/vnets", body)

    def delete_vnet(self, vnet: str) -> None:
        """SDN VNet を削除する。"""
        logger.info("Proxmox: VNet を削除 vnet=%s", vnet)
        self._delete(f"/api2/json/sdn/vnets/{vnet}")

    def create_subnet(self, vnet: str, subnet: str) -> None:
        """
        VNet にサブネットを作成する。

        Proxmox SDN のサブネット機能により、VNet 内の IP レンジを定義する。
        VXLAN Zone では複数の VNet が同一の CIDR を持っても、
        VNet ごとに L2 が分離されているため通信は相互に干渉しない。

        Args:
            vnet: 対象 VNet の ID（例: vnettaro）
            subnet: CIDR 形式のサブネット（例: 10.0.0.0/24）
        """
        logger.info("Proxmox: Subnet を作成 vnet=%s subnet=%s", vnet, subnet)
        self._post(
            f"/api2/json/sdn/vnets/{vnet}/subnets",
            {"subnet": subnet, "type": "subnet"},
        )

    def delete_subnet(self, vnet: str, subnet: str) -> None:
        """
        VNet のサブネットを削除する。

        Proxmox API の仕様: URL パスの subnet パラメータは CIDR の "/" を "-" に
        置換した形式を使う（例: 10.0.0.0/24 → 10.0.0.0-24）。

        Args:
            vnet: 対象 VNet の ID（例: vnettaro）
            subnet: CIDR 形式のサブネット（例: 10.0.0.0/24）
        """
        # "/" を "-" に置換して URL に埋め込む（Proxmox API の仕様）
        subnet_id = subnet.replace("/", "-")
        logger.info("Proxmox: Subnet を削除 vnet=%s subnet=%s", vnet, subnet)
        self._delete(f"/api2/json/sdn/vnets/{vnet}/subnets/{subnet_id}")

    # ── API トークン操作 ──────────────────────────────────────────────────

    def create_api_token(self, userid: str, tokenname: str, expire: int = 0) -> str:
        """
        Proxmox API トークンを作成する。

        同名のトークンがすでに存在する場合は事前に delete_api_token() で削除すること
        （Proxmox は同名トークンの上書き作成を許可しない）。

        Args:
            userid: トークンを作成するユーザー（例: root@pam）
            tokenname: トークン名（例: provisioner）
            expire: 有効期限（Unix タイムスタンプ。0 = 無期限）

        Returns:
            新しいトークンのシークレット文字列（1回だけ返される）
        """
        logger.info("Proxmox: API トークンを作成 userid=%s tokenname=%s", userid, tokenname)
        body: dict = {}
        if expire:
            body["expire"] = expire
        result = self._post(f"/api2/json/access/users/{userid}/token/{tokenname}", body)
        # レスポンス例: {"full-tokenid": "root@pam!provisioner", "info": {...}, "value": "secret"}
        return result["value"]

    def delete_api_token(self, userid: str, tokenname: str) -> None:
        """
        Proxmox API トークンを削除する。

        同名で再作成することでシークレットをローテーションできる。
        トークンに紐付いた ACL エントリは tokenname で管理されており、
        トークン削除後も ACL は保持される（再作成後も同じ権限が引き継がれる）。

        Args:
            userid: トークンを保持するユーザー（例: root@pam）
            tokenname: 削除するトークン名（例: provisioner）
        """
        logger.info("Proxmox: API トークンを削除 userid=%s tokenname=%s", userid, tokenname)
        self._delete(f"/api2/json/access/users/{userid}/token/{tokenname}")

    def update_api_token(self, token_id: str, token_secret: str) -> None:
        """
        インメモリの API トークン認証ヘッダーを更新する。

        ローテーション後に呼び出し、以降のリクエストで新シークレットを使用させる。
        コンテナ再起動なしにトークンを切り替えられる。

        Args:
            token_id: PROXMOX_TOKEN_ID（例: root@pam!provisioner）
            token_secret: 新しいシークレット文字列
        """
        self._headers["Authorization"] = f"PVEAPIToken={token_id}={token_secret}"
        logger.debug("Proxmox: インメモリトークンを更新しました")

    def apply_sdn(self) -> None:
        """
        SDN の設定変更を実際のネットワークに反映させる。

        Proxmox の SDN は「設定を変更した後に apply を実行」しないと
        実際のネットワークに反映されない。
        VNet の作成・削除後は必ずこのメソッドを呼ぶ必要がある。

        PUT /api2/json/sdn に何も body を送らないと apply が実行される。
        """
        logger.info("Proxmox: SDN 設定を反映（apply）")
        self._put("/api2/json/sdn")


# モジュールレベルのシングルトンインスタンス
proxmox_client = ProxmoxClient()
