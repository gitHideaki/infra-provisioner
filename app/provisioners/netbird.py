"""
Netbird プロビジョニング・デプロビジョニングのロジックモジュール。

このモジュールの役割:
  - clients/netbird.py の低レベル API 呼び出しを組み合わせて
    「1ユーザー分のプロビジョニング/デプロビジョニング」を実現する
  - DB の更新もここで行う

プロビジョニング内容:
  - ユーザーが接続した Peer を、設定済みの Netbird グループに追加する
    → そのグループに紐付く Policy により Proxmox への VPN アクセスが開通する

デプロビジョニング内容:
  - グループから Peer を除外する
  - Netbird ユーザーアカウントを削除する
"""

import logging
from datetime import datetime, timezone

from clients.netbird import netbird_client
from config import settings
from models import User

logger = logging.getLogger(__name__)


def provision_user(db_user: User, db) -> bool:
    """
    Netbird に対してユーザーをプロビジョニングする。

    処理内容:
      1. そのユーザーの Peer ID リストを取得
      2. 設定済みグループ（NETBIRD_PROVISIONING_GROUP_ID）に Peer を追加
      3. DB フラグを更新

    Args:
        db_user: DB から取得した User オブジェクト
        db: SQLAlchemy のセッション（DB 更新のために使う）

    Returns:
        True = 成功 / False = 失敗
    """
    logger.info("Netbird プロビジョニング開始: %s", db_user.email)

    try:
        # このユーザーに紐付く全 Peer の ID を取得する
        # （1ユーザーが複数端末でログインすることがあるため list で返る）
        peer_ids = netbird_client.get_user_peer_ids(db_user.netbird_user_id)

        if not peer_ids:
            # Peer がゼロの場合は「まだ接続していない」ということなのでスキップ
            # ※ このメソッドは「Peer が存在するユーザー」のみ呼ばれるはずなので
            #   通常はここに来ない（念のため）
            logger.warning("Netbird: Peer が見つかりません user_id=%s", db_user.netbird_user_id)
            return False

        # 取得した全 Peer をプロビジョニンググループに追加
        netbird_client.add_user_peers_to_group(
            group_id=settings.netbird_provisioning_group_id,
            user_peer_ids=peer_ids,
        )

        # DB フラグを更新: プロビジョニング完了
        db_user.netbird_provisioned = True
        db_user.netbird_provisioned_at = datetime.now(timezone.utc)
        db.commit()

        logger.info("Netbird プロビジョニング完了: %s (Peer %d 件)", db_user.email, len(peer_ids))
        return True

    except Exception as e:
        # 例外が発生した場合は DB をロールバックしてエラーを伝播
        # commit 前なので DB には変更が残らない
        db.rollback()
        logger.error("Netbird プロビジョニング失敗: %s - %s", db_user.email, e)
        return False


def deprovision_user(db_user: User, db) -> bool:
    """
    Netbird からユーザーをデプロビジョニングする。

    処理内容:
      1. ユーザーの Peer をグループから除外
      2. Netbird ユーザーアカウントを削除
      3. DB フラグは呼び出し元（sync.py）でまとめて更新するため、ここでは更新しない

    Args:
        db_user: DB から取得した User オブジェクト
        db: SQLAlchemy のセッション

    Returns:
        True = 成功 / False = 失敗（部分的な成功も False とする）
    """
    logger.info("Netbird デプロビジョニング開始: %s", db_user.email)

    if not db_user.netbird_user_id:
        # Netbird ユーザーID が DB に記録されていない場合はスキップ
        logger.warning("Netbird: user_id が DB に記録されていません email=%s", db_user.email)
        return True  # スキップするが失敗ではない

    try:
        # Step 1: グループから Peer を除外
        if db_user.netbird_provisioned:
            peer_ids = netbird_client.get_user_peer_ids(db_user.netbird_user_id)
            if peer_ids:
                netbird_client.remove_user_peers_from_group(
                    group_id=settings.netbird_provisioning_group_id,
                    user_peer_ids=peer_ids,
                )
                logger.info(
                    "Netbird: グループから Peer を除外 email=%s peer_count=%d",
                    db_user.email,
                    len(peer_ids),
                )

        # Step 2: Netbird ユーザーを削除
        netbird_client.delete_user(db_user.netbird_user_id)
        logger.info("Netbird デプロビジョニング完了: %s", db_user.email)
        return True

    except Exception as e:
        logger.error("Netbird デプロビジョニング失敗: %s - %s", db_user.email, e)
        return False
