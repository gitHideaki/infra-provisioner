"""
Proxmox プロビジョニング・デプロビジョニングのロジックモジュール。

このモジュールの役割:
  - clients/proxmox.py の低レベル API 呼び出しを組み合わせて
    「1ユーザー分のプロビジョニング/デプロビジョニング」を実現する
  - DB の更新もここで行う

プロビジョニング内容（順番が重要）:
  1. ユーザーアカウント作成
  2. Resource Pool 作成
  3. SDN VNet 作成
  4. Subnet 作成（gateway + DHCP range 付き）
  5. Pool への ACL 設定（PVEVMUser）
  6. VNet への ACL 設定（PVESDNUser）
  7. SDN 設定の反映（apply）

デプロビジョニング内容（プロビジョニングの逆順）:
  1. Pool ACL 削除
  2. VNet ACL 削除
  3. Pool 削除
  4. Subnet 削除
  5. VNet 削除 + apply
  6. ユーザーアカウント削除

命名規則:
  - Pool ID: "pool-{email_local}"  （例: pool-taro）
  - VNet ID: "vnet{email_local}" の最初の8文字（例: vnettaro）
             ※ Proxmox の VNet ID は英数字のみ・最大 8 文字の制限あり
  - Proxmox user ID: "{email_local}@{realm}"（例: taro@pve）
"""

import logging
import re
from datetime import datetime, timezone

from clients.proxmox import proxmox_client
from config import settings
from models import User

logger = logging.getLogger(__name__)


def _sanitize_email_local(email: str) -> str:
    """
    メールアドレスのローカル部分（@ より前）を Proxmox リソース名に使える形に変換する。

    変換ルール:
      - @ より前だけ取り出す（例: taro.yamada@example.com → taro.yamada）
      - 英数字以外を削除（ドット・ハイフンなども除去）
      - 小文字に変換

    例: "taro.yamada@example.com" → "taroyamada"
        "john+test@example.com" → "johntest"
    """
    local = email.split("@")[0]
    # 正規表現 [^a-z0-9] は「英小文字・数字以外の全文字」にマッチするので、それを削除
    return re.sub(r"[^a-z0-9]", "", local.lower())


def _make_pool_id(email: str) -> str:
    """Pool ID を生成する。例: "taro@example.com" → "pool-taro" """
    return f"pool-{_sanitize_email_local(email)}"


def _make_vnet_id(email: str) -> str:
    """
    VNet ID を生成する。

    Proxmox の VNet ID は最大 8 文字の制限がある。
    "vnet" の 4 文字 + メールローカル部分の最初の 4 文字 = 最大 8 文字。

    例: "taro@example.com" → "vnettaro"
        "yamada@example.com" → "vnetyama"
    """
    local = _sanitize_email_local(email)
    # "vnet" が 4 文字なので、残り 4 文字分だけローカル部を使う
    return f"vnet{local[:4]}"


def _make_proxmox_userid(email: str) -> str:
    """Proxmox ユーザー ID を生成する。例: "taro@example.com" → "taro@pve" """
    local = _sanitize_email_local(email)
    return f"{local}@{settings.proxmox_user_realm}"


def _build_subnet(user_id: int, base: str) -> tuple[str, str, str, str]:
    """
    DB の User.id と PROXMOX_SUBNET_BASE から、そのユーザー専用のサブネット情報を生成する。

    採番ルール: id=N → {base}.N.0/24
    例: user_id=3, base="10.0" → cidr="10.0.3.0/24", gateway="10.0.3.1",
                                  dhcp_start="10.0.3.100", dhcp_end="10.0.3.200"

    User.id は DB の auto-increment のため再利用されず、削除済みユーザーのサブネットが
    新ユーザーに割り当てられることはない（空き番号は生じるが問題ない）。

    Returns:
        (cidr, gateway, dhcp_start, dhcp_end) のタプル
    """
    cidr = f"{base}.{user_id}.0/24"
    gateway = f"{base}.{user_id}.1"
    dhcp_start = f"{base}.{user_id}.100"
    dhcp_end = f"{base}.{user_id}.200"
    return cidr, gateway, dhcp_start, dhcp_end


def provision_user(db_user: User, db) -> bool:
    """
    Proxmox に対してユーザーをプロビジョニングする。

    各ステップが独立した API 呼び出しなので、途中で失敗した場合は
    成功済みのリソースが残る可能性がある。
    その場合は次回の同期サイクルで再試行される（proxmox_provisioned=False のまま）。

    Args:
        db_user: DB から取得した User オブジェクト
        db: SQLAlchemy のセッション

    Returns:
        True = 全ステップ成功 / False = いずれかのステップで失敗
    """
    logger.info("Proxmox プロビジョニング開始: %s", db_user.email)

    # リソース名を事前に計算
    userid = _make_proxmox_userid(db_user.email)
    pool_id = _make_pool_id(db_user.email)
    vnet_id = _make_vnet_id(db_user.email)
    pool_path = f"/pool/{pool_id}"  # ACL で使うパス形式

    try:
        # ── Step 1: ユーザーアカウント作成 ─────────────────────────────────
        # Proxmox ユーザーを作成。comment にメールアドレスを記録しておくと
        # Proxmox UI から誰のアカウントか識別しやすい。
        proxmox_client.create_user(
            userid=userid,
            comment=f"Provisioned by infra-provisioner from {db_user.email}",
        )
        logger.debug("Proxmox: ユーザー作成完了 userid=%s", userid)

        # ── Step 2: Resource Pool 作成 ─────────────────────────────────────
        proxmox_client.create_pool(
            poolid=pool_id,
            comment=f"Pool for {db_user.email}",
        )
        logger.debug("Proxmox: Pool 作成完了 poolid=%s", pool_id)

        # ── Step 3: SDN VNet 作成 ──────────────────────────────────────────
        proxmox_client.create_vnet(
            vnet=vnet_id,
            zone=settings.proxmox_vxlan_zone,
        )
        logger.debug("Proxmox: VNet 作成完了 vnet=%s", vnet_id)

        # ── Step 4: Subnet 作成（DHCP・ゲートウェイ付き）─────────────────
        # DB の user_id を使ってユーザー専用の /24 を決定論的に採番する。
        # gateway を設定すると EVPN/Simple ゾーンが Anycast GW として機能し、
        # dhcp-range を設定すると dnsmasq が DHCP サーバーとして動作する。
        cidr, gw, dhcp_start, dhcp_end = _build_subnet(
            db_user.id, settings.proxmox_subnet_base
        )
        proxmox_client.create_subnet(
            vnet=vnet_id,
            subnet=cidr,
            gateway=gw,
            dhcp_start=dhcp_start,
            dhcp_end=dhcp_end,
        )
        logger.debug(
            "Proxmox: Subnet 作成完了 vnet=%s subnet=%s gateway=%s", vnet_id, cidr, gw
        )

        # ── Step 5: ACL 設定（Pool への PVEVMUser ロール付与）─────────────
        # "/pool/{pool_id}" パスに対してユーザーに PVEVMUser ロールを付与する。
        # これにより、ユーザーは自分の Pool 内のリソースのみ操作できるようになる。
        proxmox_client.set_acl(
            path=pool_path,
            userid=userid,
            role=settings.proxmox_user_role,
        )
        logger.debug("Proxmox: ACL 設定完了 path=%s userid=%s", pool_path, userid)

        # ── Step 6: ACL 設定（SDN VNet への PVESDNUser ロール付与）──────────
        # "/sdn/zones/{zone}/{vnet}" パスに対して PVESDNUser ロールを付与する。
        # PVEVMUser だけでは SDN ネットワークを VM に割り当てられないため、
        # VNet を使うにはこのロールが必要。
        sdn_path = f"/sdn/zones/{settings.proxmox_vxlan_zone}/{vnet_id}"
        proxmox_client.set_acl(
            path=sdn_path,
            userid=userid,
            role=settings.proxmox_sdn_role,
        )
        logger.debug("Proxmox: SDN ACL 設定完了 path=%s userid=%s", sdn_path, userid)

        # ── Step 7: SDN 設定を反映 ──────────────────────────────────────────
        # VNet・Subnet を作成しただけでは実際のネットワークに反映されない。
        # apply を呼ぶことで Proxmox ノードに設定が配布される。
        proxmox_client.apply_sdn()
        logger.debug("Proxmox: SDN apply 完了")

        # ── DB 更新 ────────────────────────────────────────────────────────
        db_user.proxmox_provisioned = True
        db_user.proxmox_provisioned_at = datetime.now(timezone.utc)
        db_user.proxmox_user_id = userid
        db_user.proxmox_pool_id = pool_id
        db_user.proxmox_vnet_id = vnet_id
        db_user.proxmox_subnet = cidr
        db.commit()

        logger.info(
            "Proxmox プロビジョニング完了: %s (user=%s pool=%s vnet=%s subnet=%s gw=%s)",
            db_user.email,
            userid,
            pool_id,
            vnet_id,
            cidr,
            gw,
        )
        return True

    except Exception as e:
        db.rollback()
        logger.error("Proxmox プロビジョニング失敗: %s - %s", db_user.email, e)
        return False


def deprovision_user(db_user: User, db) -> bool:
    """
    Proxmox からユーザーをデプロビジョニングする。

    削除順序はプロビジョニングの逆順。
    ACL → Pool → VNet → ユーザー の順に削除することで、
    依存関係によるエラーを防ぐ。

    一部のステップが失敗した場合も続行し、最終的に成功・失敗の結果を返す。
    （リソースが一部残った場合は手動での確認が必要）

    Args:
        db_user: DB から取得した User オブジェクト
        db: SQLAlchemy のセッション

    Returns:
        True = 全ステップ成功 / False = いずれかのステップで失敗
    """
    logger.info("Proxmox デプロビジョニング開始: %s", db_user.email)

    if not db_user.proxmox_provisioned:
        # Proxmox プロビジョニングが完了していない場合はスキップ
        logger.info("Proxmox: プロビジョニング未完了のためスキップ email=%s", db_user.email)
        return True

    # DB に記録されたリソース情報を使う（作成時に保存した名前）
    userid = db_user.proxmox_user_id
    pool_id = db_user.proxmox_pool_id
    vnet_id = db_user.proxmox_vnet_id
    subnet = db_user.proxmox_subnet
    pool_path = f"/pool/{pool_id}"
    sdn_path = f"/sdn/zones/{settings.proxmox_vxlan_zone}/{vnet_id}" if vnet_id else None

    success = True

    # ── Step 1: Pool ACL 削除 ─────────────────────────────────────────────
    try:
        if userid and pool_id:
            proxmox_client.delete_acl(
                path=pool_path,
                userid=userid,
                role=settings.proxmox_user_role,
            )
            logger.debug("Proxmox: Pool ACL 削除完了 path=%s", pool_path)
    except Exception as e:
        logger.error("Proxmox: Pool ACL 削除失敗 email=%s - %s", db_user.email, e)
        success = False

    # ── Step 2: SDN VNet ACL 削除 ─────────────────────────────────────────
    try:
        if userid and sdn_path:
            proxmox_client.delete_acl(
                path=sdn_path,
                userid=userid,
                role=settings.proxmox_sdn_role,
            )
            logger.debug("Proxmox: SDN ACL 削除完了 path=%s", sdn_path)
    except Exception as e:
        logger.error("Proxmox: SDN ACL 削除失敗 email=%s - %s", db_user.email, e)
        success = False

    # ── Step 3: Pool 削除 ─────────────────────────────────────────────────
    # 注意: Pool 内に VM が残っている場合は削除できない（Proxmox の制約）
    try:
        if pool_id:
            proxmox_client.delete_pool(pool_id)
            logger.debug("Proxmox: Pool 削除完了 poolid=%s", pool_id)
    except Exception as e:
        logger.error("Proxmox: Pool 削除失敗 email=%s - %s", db_user.email, e)
        success = False

    # ── Step 4: Subnet 削除 ───────────────────────────────────────────────
    try:
        if vnet_id and subnet:
            proxmox_client.delete_subnet(vnet=vnet_id, subnet=subnet)
            logger.debug("Proxmox: Subnet 削除完了 vnet=%s subnet=%s", vnet_id, subnet)
    except Exception as e:
        logger.error("Proxmox: Subnet 削除失敗 email=%s - %s", db_user.email, e)
        success = False

    # ── Step 5: VNet 削除 + SDN 反映 ─────────────────────────────────────
    try:
        if vnet_id:
            proxmox_client.delete_vnet(vnet_id)
            proxmox_client.apply_sdn()
            logger.debug("Proxmox: VNet 削除・SDN apply 完了 vnet=%s", vnet_id)
    except Exception as e:
        logger.error("Proxmox: VNet 削除失敗 email=%s - %s", db_user.email, e)
        success = False

    # ── Step 6: ユーザーアカウント削除 ───────────────────────────────────
    try:
        if userid:
            proxmox_client.delete_user(userid)
            logger.debug("Proxmox: ユーザー削除完了 userid=%s", userid)
    except Exception as e:
        logger.error("Proxmox: ユーザー削除失敗 email=%s - %s", db_user.email, e)
        success = False

    if success:
        logger.info("Proxmox デプロビジョニング完了: %s", db_user.email)
    else:
        logger.warning("Proxmox デプロビジョニング一部失敗: %s（一部リソースが残存する可能性があります）", db_user.email)

    return success
