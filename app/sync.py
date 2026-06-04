"""
差分検出とプロビジョニング制御のメインロジック。

このモジュールが全体の司令塔。
5分ごとに run_sync() が呼ばれ、以下を行う:

  [プロビジョニング]
  1. Netbird から「Peerが存在するユーザー」を取得
  2. DB と照合して新規ユーザーを登録
  3. 未プロビジョニングのユーザーを順番に処理

  [デプロビジョニング]
  4. is_blocked=true または API から消えたユーザーを検知
  5. 対象ユーザーのリソースをクリーンアップ

また、手動デプロビジョニング用の manual_deprovision() も提供する。
"""

import json
import logging
from datetime import datetime, timezone

from clients.netbird import netbird_client
from database import get_db
from models import SyncLog, User
from provisioners import netbird as netbird_provisioner
from provisioners import proxmox as proxmox_provisioner

logger = logging.getLogger(__name__)


def run_sync() -> None:
    """
    メインの同期処理。スケジューラーから 5 分ごとに呼び出される。

    処理全体を try/except で囲み、予期しないエラーが発生しても
    次のサイクルで再実行できるようにしている。
    """
    logger.info("=== 同期処理開始 ===")
    started_at = datetime.now(timezone.utc)

    # 今回の同期結果をカウントする変数
    users_added = 0
    users_provisioned = 0
    users_deprovisioned = 0
    errors: list[dict] = []

    try:
        # ── Step 1: Netbird から全ユーザーと Peer 情報を取得 ─────────────
        all_netbird_users = netbird_client.list_users()
        peer_user_ids = netbird_client.get_peer_user_ids()

        # Netbird の全ユーザーを user_id をキーにした辞書に変換
        # → O(1) で検索できるため、後の処理が高速になる
        netbird_users_by_id: dict[str, dict] = {u["id"]: u for u in all_netbird_users}

        # サービスアカウント（is_service_user=true）は除外する
        # サービスアカウントはアプリが使う内部アカウントなのでプロビジョニング不要
        #
        # また "issued": "integration" は EntraID OIDC 経由でインポートされたユーザーを示す。
        # Netbird 管理画面や API で手動作成されたユーザー（"issued": "api" 等）は
        # EntraID と連携していないためプロビジョニング対象外とする。
        human_users = [
            u for u in all_netbird_users
            if not u.get("is_service_user", False)
            and u.get("issued") == "integration"
        ]

        logger.info(
            "Netbird: 全ユーザー %d 件 / Peer 接続済み %d 件",
            len(human_users),
            len(peer_user_ids),
        )

        # ── Step 2: デプロビジョニング対象の検出 ─────────────────────────
        # Netbird の現在のユーザー ID セット（サービスアカウント含めた全ユーザー）
        all_netbird_user_ids = {u["id"] for u in all_netbird_users}

        # is_blocked=true のユーザー ID セット
        blocked_user_ids = {u["id"] for u in human_users if u.get("is_blocked", False)}

        with get_db() as db:
            # DB 内のアクティブユーザー（= 既にプロビジョニング済みで削除されていない）
            active_db_users = db.query(User).filter(User.is_active == True).all()

            for db_user in active_db_users:
                if db_user.netbird_user_id is None:
                    continue

                reason: str | None = None

                # シグナル①: is_blocked=true の検知
                if db_user.netbird_user_id in blocked_user_ids:
                    reason = "blocked"
                    logger.info(
                        "削除検知 (blocked): %s", db_user.email
                    )

                # シグナル②: Netbird のユーザーリストから消えた
                elif db_user.netbird_user_id not in all_netbird_user_ids:
                    reason = "not_found"
                    logger.info(
                        "削除検知 (not_found): %s", db_user.email
                    )

                if reason:
                    success = _deprovision_user(db_user, db, reason)
                    if success:
                        users_deprovisioned += 1
                    else:
                        errors.append({"email": db_user.email, "action": "deprovision", "error": "一部失敗"})

        # ── Step 3: プロビジョニング対象の検出と処理 ─────────────────────
        # Peer が存在するユーザー（= 初回ログイン済み）のみ対象
        users_with_peers = [u for u in human_users if u["id"] in peer_user_ids]
        logger.info("プロビジョニング候補: %d 件", len(users_with_peers))

        with get_db() as db:
            for nb_user in users_with_peers:
                # DB に存在するか確認
                db_user = db.query(User).filter(
                    User.netbird_user_id == nb_user["id"]
                ).first()

                if db_user is None:
                    # DB に未登録の新規ユーザー → DB に追加
                    db_user = _register_new_user(db, nb_user)
                    users_added += 1
                    logger.info("新規ユーザー登録: %s", nb_user.get("email"))
                else:
                    # 既存ユーザー: 表示名を最新の値に更新しておく
                    db_user.display_name = nb_user.get("name")
                    db_user.last_synced_at = datetime.now(timezone.utc)
                    db.commit()

                # 削除済みユーザーはスキップ
                if not db_user.is_active:
                    continue

                # ── Netbird プロビジョニング ──────────────────────────────
                if not db_user.netbird_provisioned:
                    success = netbird_provisioner.provision_user(db_user, db)
                    if not success:
                        errors.append({"email": db_user.email, "action": "netbird_provision"})
                        continue  # Netbird が失敗したら Proxmox もスキップ

                # ── Proxmox プロビジョニング ──────────────────────────────
                if not db_user.proxmox_provisioned:
                    success = proxmox_provisioner.provision_user(db_user, db)
                    if success:
                        users_provisioned += 1
                    else:
                        errors.append({"email": db_user.email, "action": "proxmox_provision"})

    except Exception as e:
        # 予期しないエラー（API 接続失敗など）をキャッチしてログに残す
        logger.error("同期処理で予期しないエラーが発生しました: %s", e, exc_info=True)
        errors.append({"error": str(e), "action": "sync_main"})

    finally:
        # エラーの有無に関わらず実行ログを DB に記録する
        _write_sync_log(
            started_at=started_at,
            users_added=users_added,
            users_provisioned=users_provisioned,
            users_deprovisioned=users_deprovisioned,
            errors=errors,
        )

    logger.info(
        "=== 同期処理完了 === 新規: %d 件 / プロビジョニング: %d 件 / デプロビジョニング: %d 件 / エラー: %d 件",
        users_added,
        users_provisioned,
        users_deprovisioned,
        len(errors),
    )


def _register_new_user(db, nb_user: dict) -> User:
    """
    Netbird API から取得したユーザー情報を DB に新規登録する。

    Args:
        db: SQLAlchemy セッション
        nb_user: Netbird API の users レスポンス（1ユーザー分）

    Returns:
        作成した User オブジェクト
    """
    new_user = User(
        email=nb_user.get("email", ""),
        display_name=nb_user.get("name"),
        netbird_user_id=nb_user["id"],
        last_synced_at=datetime.now(timezone.utc),
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)  # DB が生成した id 等を Python オブジェクトに反映させる
    return new_user


def _deprovision_user(db_user: User, db, reason: str) -> bool:
    """
    削除検知したユーザーをデプロビジョニングし、DB を更新する。

    Args:
        db_user: デプロビジョニング対象の User オブジェクト
        db: SQLAlchemy セッション
        reason: 検知理由 ("blocked" / "not_found" / "manual")

    Returns:
        True = 成功 / False = 失敗
    """
    logger.info("デプロビジョニング開始: %s (reason=%s)", db_user.email, reason)

    # Netbird と Proxmox それぞれのデプロビジョニングを実行
    nb_ok = netbird_provisioner.deprovision_user(db_user, db)
    px_ok = proxmox_provisioner.deprovision_user(db_user, db)

    # 両方成功した場合のみ is_active=False にする
    # 失敗した場合は次のサイクルで再試行できるよう is_active=True のまま残す
    if nb_ok and px_ok:
        db_user.is_active = False
        db_user.deprovisioned_at = datetime.now(timezone.utc)
        db_user.deprovision_reason = reason
        db.commit()
        logger.info("デプロビジョニング完了: %s", db_user.email)
        return True
    else:
        db.rollback()
        logger.error("デプロビジョニング失敗（部分的）: %s", db_user.email)
        return False


def _write_sync_log(
    started_at: datetime,
    users_added: int,
    users_provisioned: int,
    users_deprovisioned: int,
    errors: list[dict],
) -> None:
    """同期処理の実行結果を sync_logs テーブルに書き込む。"""
    status = "success"
    if errors:
        # エラーはあるが一部成功している場合は "partial"
        status = "partial" if (users_provisioned > 0 or users_deprovisioned > 0) else "error"

    try:
        with get_db() as db:
            log = SyncLog(
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                users_added=users_added,
                users_provisioned=users_provisioned,
                users_deprovisioned=users_deprovisioned,
                errors=json.dumps(errors, ensure_ascii=False) if errors else None,
                status=status,
            )
            db.add(log)
            db.commit()
    except Exception as e:
        # ログ書き込み自体が失敗した場合はログ出力だけして無視する
        logger.error("sync_log の書き込みに失敗しました: %s", e)


def manual_deprovision(email: str) -> None:
    """
    管理者による手動デプロビジョニング（CLI から呼び出す）。

    EntraID 削除後に is_blocked が更新されるまでのラグ期間に
    緊急でアクセスを止めたい場合に使用する。

    使い方:
        docker compose exec provisioner python main.py --deprovision user@example.com

    Args:
        email: デプロビジョニング対象ユーザーのメールアドレス
    """
    logger.info("手動デプロビジョニング開始: %s", email)

    with get_db() as db:
        db_user = db.query(User).filter(User.email == email, User.is_active == True).first()

        if db_user is None:
            logger.error("ユーザーが見つかりません、または既に削除済みです: %s", email)
            print(f"エラー: ユーザーが見つかりません: {email}")
            return

        success = _deprovision_user(db_user, db, reason="manual")
        if success:
            print(f"デプロビジョニング完了: {email}")
        else:
            print(f"デプロビジョニング失敗（ログを確認してください）: {email}")
