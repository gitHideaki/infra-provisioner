"""
トークン自動ローテーションモジュール。

Netbird PAT と Proxmox API トークンを定期的にローテーションする。
ローテーションの流れ:
  1. 新トークンを API で作成
  2. 動作確認（テストリクエスト）
  3. DB に保存
  4. インメモリのクライアントを新トークンで更新（再起動不要）
  5. 旧トークンを API で削除
  6. DB の旧レコードを非アクティブに更新

起動時の流れ（init_tokens_from_db）:
  - DB にアクティブなトークンがあれば .env の値より優先して使用する
  - .env のトークンは初回起動時やローテーション失敗時のフォールバックとして機能する

エラーハンドリング方針:
  - ローテーション失敗時は旧トークンのまま継続（クライアントの headers は変更前に戻す）
  - 次回のローテーションサイクルで再試行される
  - ログにエラーを記録し、運用者が把握できるようにする
"""

import logging
from datetime import datetime, timedelta, timezone

from clients.netbird import netbird_client
from clients.proxmox import proxmox_client
from config import settings
from database import get_db
from models import TokenStore

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def init_tokens_from_db() -> None:
    """
    起動時に DB のアクティブトークンをクライアントに適用する。

    ローテーション済みのトークンが DB にある場合、.env の初期値より優先する。
    これにより、コンテナを再起動してもローテーション済みトークンが引き続き使われる。
    DB にトークンがない場合（初回起動時）は .env の値をそのまま使用する。
    """
    with get_db() as db:
        # Netbird
        nb = db.query(TokenStore).filter_by(service="netbird", is_active=True).first()
        if nb:
            netbird_client.update_bearer_token(nb.token_secret)
            logger.info(
                "Netbird: DB からトークンを復元 (expires_at=%s)",
                nb.expires_at.strftime("%Y-%m-%d"),
            )
        else:
            logger.info("Netbird: DB にトークンなし → .env の値を使用")

        # Proxmox
        px = db.query(TokenStore).filter_by(service="proxmox", is_active=True).first()
        if px:
            proxmox_client.update_api_token(settings.proxmox_token_id, px.token_secret)
            logger.info(
                "Proxmox: DB からトークンを復元 (expires_at=%s)",
                px.expires_at.strftime("%Y-%m-%d"),
            )
        else:
            logger.info("Proxmox: DB にトークンなし → .env の値を使用")


def rotate_netbird_token() -> bool:
    """
    Netbird PAT をローテーションする。

    新しい PAT を作成してからクライアントを更新し、旧 PAT を削除する。
    初回ローテーション時は DB に旧トークン ID がないため、
    旧 PAT の削除はスキップする（.env のトークンは有効期限が来たら自然に無効化される）。

    Returns:
        True = 成功 / False = 失敗（旧トークンを継続使用）
    """
    logger.info("Netbird: トークンローテーション開始")

    # 有効期限 = ローテーション間隔 + 1日（次のローテーションまでの余裕）
    expires_in = settings.token_rotation_days + 1
    new_name = f"provisioner-{_utcnow().strftime('%Y%m%d')}"

    try:
        # ── Step 1: 新 PAT を作成 ──────────────────────────────────────────
        result = netbird_client.create_token(
            user_id=settings.netbird_service_user_id,
            name=new_name,
            expires_in=expires_in,
        )
        new_token_id = result["id"]
        new_token_secret = result["plain_token"]
        logger.debug("Netbird: 新 PAT 作成完了 id=%s", new_token_id)

        # ── Step 2: 動作確認（新トークンでAPIコール）─────────────────────
        # 一時的にヘッダーを差し替えてテストコール
        netbird_client.update_bearer_token(new_token_secret)
        netbird_client.list_users()  # 疎通確認
        logger.debug("Netbird: 新トークンの動作確認完了")

        # ── Step 3: DB への保存と旧トークンの削除 ─────────────────────────
        with get_db() as db:
            old = db.query(TokenStore).filter_by(service="netbird", is_active=True).first()

            # 新トークンを DB に登録
            db.add(TokenStore(
                service="netbird",
                token_id=new_token_id,
                token_secret=new_token_secret,
                expires_at=_utcnow() + timedelta(days=expires_in),
            ))

            # 旧トークンを Netbird から削除（DB に ID が記録されている場合のみ）
            if old:
                try:
                    netbird_client.delete_token(settings.netbird_service_user_id, old.token_id)
                    logger.debug("Netbird: 旧トークン削除完了 id=%s", old.token_id)
                except Exception as e:
                    # 削除失敗は警告のみ（旧トークンは有効期限が来れば自動失効）
                    logger.warning("Netbird: 旧トークン削除失敗（期限切れで自動失効します） id=%s - %s", old.token_id, e)
                old.is_active = False

            db.commit()

        logger.info(
            "Netbird: トークンローテーション完了 (次回有効期限: %s)",
            (_utcnow() + timedelta(days=expires_in)).strftime("%Y-%m-%d"),
        )
        return True

    except Exception as e:
        logger.error("Netbird: トークンローテーション失敗 - %s", e, exc_info=True)
        # 失敗時はインメモリトークンを元に戻す
        _restore_netbird_token()
        return False


def _restore_netbird_token() -> None:
    """ローテーション失敗時に直前の有効なトークンを復元する。"""
    try:
        with get_db() as db:
            active = db.query(TokenStore).filter_by(service="netbird", is_active=True).first()
        if active:
            netbird_client.update_bearer_token(active.token_secret)
            logger.info("Netbird: 旧トークンにロールバックしました")
        else:
            # DB にもない場合は .env の初期値にリセット
            from config import settings as s
            netbird_client.update_bearer_token(s.netbird_service_token)
            logger.info("Netbird: .env の初期トークンにロールバックしました")
    except Exception as e:
        logger.error("Netbird: ロールバックも失敗しました - %s", e)


def rotate_proxmox_token() -> bool:
    """
    Proxmox API トークンをローテーションする。

    PROXMOX_TOKEN_ID を解析して userid と tokenname を取得し、
    同じ tokenname で削除→再作成することでシークレットを更新する。
    ACL は tokenname に紐付いているためシークレット更新後も継続して有効。

    Proxmox トークン削除から再作成までの数秒間、API 呼び出しが失敗する可能性があるが、
    この処理は同期サイクル外で実行されるため影響は最小限。

    Returns:
        True = 成功 / False = 失敗（旧シークレットを継続使用）
    """
    logger.info("Proxmox: トークンローテーション開始")

    # "root@pam!provisioner" → userid="root@pam", tokenname="provisioner"
    try:
        userid, tokenname = settings.proxmox_token_id.rsplit("!", 1)
    except ValueError:
        logger.error(
            "Proxmox: PROXMOX_TOKEN_ID の形式が不正です（'user@realm!tokenname' 形式が必要）: %s",
            settings.proxmox_token_id,
        )
        return False

    expire_days = settings.token_rotation_days + 1
    # Proxmox の expire はUnixタイムスタンプ（0 = 無期限）
    expire_ts = int((_utcnow() + timedelta(days=expire_days)).timestamp())

    # ロールバック用に現在の DB トークンを保持
    current_secret: str | None = None
    try:
        with get_db() as db:
            active = db.query(TokenStore).filter_by(service="proxmox", is_active=True).first()
            if active:
                current_secret = active.token_secret
    except Exception:
        pass

    try:
        # ── Step 1: 旧トークンを削除 ──────────────────────────────────────
        # 削除失敗は警告のみ（存在しない場合もある）
        try:
            proxmox_client.delete_api_token(userid, tokenname)
            logger.debug("Proxmox: 旧トークン削除完了")
        except Exception as e:
            logger.warning("Proxmox: 旧トークン削除失敗（初回または存在しない） - %s", e)

        # ── Step 2: 同じ tokenname で新トークンを作成 ─────────────────────
        new_secret = proxmox_client.create_api_token(userid, tokenname, expire=expire_ts)
        logger.debug("Proxmox: 新トークン作成完了")

        # ── Step 3: 動作確認 ──────────────────────────────────────────────
        proxmox_client.update_api_token(settings.proxmox_token_id, new_secret)
        proxmox_client._get("/api2/json/version")  # 疎通確認
        logger.debug("Proxmox: 新トークンの動作確認完了")

        # ── Step 4: DB を更新 ─────────────────────────────────────────────
        with get_db() as db:
            db.query(TokenStore).filter_by(service="proxmox", is_active=True).update(
                {"is_active": False}
            )
            db.add(TokenStore(
                service="proxmox",
                token_id=tokenname,
                token_secret=new_secret,
                expires_at=_utcnow() + timedelta(days=expire_days),
            ))
            db.commit()

        logger.info(
            "Proxmox: トークンローテーション完了 (次回有効期限: %s)",
            (_utcnow() + timedelta(days=expire_days)).strftime("%Y-%m-%d"),
        )
        return True

    except Exception as e:
        logger.error("Proxmox: トークンローテーション失敗 - %s", e, exc_info=True)
        # 失敗時はインメモリトークンを元に戻す
        if current_secret:
            proxmox_client.update_api_token(settings.proxmox_token_id, current_secret)
            logger.info("Proxmox: 旧シークレットにロールバックしました")
        return False


def run_rotation() -> None:
    """
    両サービスのトークンをローテーションする。
    main.py のスケジューラーから TOKEN_ROTATION_DAYS 日ごとに呼び出される。
    """
    logger.info("=== トークンローテーション開始 ===")

    nb_ok = rotate_netbird_token()
    px_ok = rotate_proxmox_token()

    if nb_ok and px_ok:
        logger.info("=== トークンローテーション完了（両サービス成功）===")
    else:
        logger.warning(
            "=== トークンローテーション一部失敗 === Netbird=%s / Proxmox=%s",
            "OK" if nb_ok else "FAILED",
            "OK" if px_ok else "FAILED",
        )
