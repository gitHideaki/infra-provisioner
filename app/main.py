"""
アプリのエントリポイント。

このファイルの役割:
  1. ロギングの初期設定
  2. DB の初期化（テーブルがなければ作成）
  3. 起動時に DB からトークンを復元（ローテーション済みトークンを優先）
  4. スケジューラーの起動（5分ごとに sync.run_sync()、N日ごとにトークンローテーション）
  5. CLI オプションのハンドリング（--deprovision による手動デプロビジョニング）

起動方法:
  通常起動（スケジューラー）:
    python main.py

  手動デプロビジョニング:
    python main.py --deprovision user@example.com
"""

import argparse
import logging
import sys
import time

import schedule

from config import settings
from database import init_db
from rotator import init_tokens_from_db, run_rotation
from sync import manual_deprovision, run_sync


def setup_logging() -> None:
    """
    ロギングの設定。

    logging モジュールの基本概念:
      - Logger   : ログを発行する主体（各モジュールで `logging.getLogger(__name__)` で取得）
      - Handler  : ログの出力先（ここでは標準出力 = StreamHandler）
      - Formatter: ログの書式（日時・レベル・モジュール名・メッセージ）
      - Level    : DEBUG < INFO < WARNING < ERROR < CRITICAL

    basicConfig() で一括設定する最も簡単な方法を採用。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        # datefmt で日時フォーマットを指定（ISO 8601 形式）
        datefmt="%Y-%m-%dT%H:%M:%S",
        # sys.stdout に出力（Docker の `docker compose logs` で確認できる）
        stream=sys.stdout,
    )


def parse_args() -> argparse.Namespace:
    """
    コマンドライン引数をパースする。

    argparse はPython 標準ライブラリのCLI引数解析モジュール。
    --deprovision オプションを指定すると手動デプロビジョニングモードになる。
    """
    parser = argparse.ArgumentParser(
        description="Netbird / Proxmox ユーザー自動プロビジョニングアプリ",
    )
    parser.add_argument(
        "--deprovision",
        metavar="EMAIL",
        help="指定メールアドレスのユーザーを即時デプロビジョニングする",
    )
    return parser.parse_args()


def main() -> None:
    """メイン処理。"""
    setup_logging()
    logger = logging.getLogger(__name__)

    args = parse_args()

    # ── DB 初期化 ──────────────────────────────────────────────────────────
    # アプリ起動時に一度だけ呼び出し、テーブルが存在しなければ作成する。
    # すでに存在するテーブルはスキップされる（冪等）。
    logger.info("DB を初期化: %s", settings.db_path)
    init_db()

    # ── トークン復元 ────────────────────────────────────────────────────────
    # DB にローテーション済みのトークンがある場合、.env の初期値より優先して使用する。
    # コンテナ再起動時もローテーション済みトークンが引き継がれるため、
    # 古い .env の値でリクエストが失敗するのを防ぐ。
    init_tokens_from_db()

    # ── 手動デプロビジョニングモード ────────────────────────────────────────
    if args.deprovision:
        # --deprovision オプションが指定された場合はスケジューラーを起動せず
        # 指定ユーザーのデプロビジョニングだけ実行して終了する
        logger.info("手動デプロビジョニングモードで起動")
        manual_deprovision(args.deprovision)
        sys.exit(0)

    # ── スケジューラー起動 ──────────────────────────────────────────────────
    logger.info(
        "スケジューラー起動: %d 秒間隔で同期を実行します",
        settings.sync_interval_seconds,
    )

    # 起動直後に1回すぐ実行する（最初の同期をスキップしないため）
    logger.info("初回同期を実行...")
    run_sync()

    # schedule ライブラリで定期実行を登録する
    # schedule.every(N).seconds.do(func) : N 秒ごとに func() を呼ぶジョブを登録
    schedule.every(settings.sync_interval_seconds).seconds.do(run_sync)

    # トークンローテーションジョブ: TOKEN_ROTATION_DAYS 日ごとに早朝 03:00 に実行
    # 03:00 を選ぶのは利用が少ない時間帯であり、削除→再作成の数秒間のAPIエラーを最小化するため。
    # at("03:00") はホスト（コンテナ）のローカルタイムに依存するため、
    # TZ 環境変数またはコンテナの timezone 設定で UTC+9 など適切なタイムゾーンを指定すること。
    schedule.every(settings.token_rotation_days).days.at("03:00").do(run_rotation)
    logger.info(
        "トークンローテーションを %d 日ごと 03:00 に実行するよう登録しました",
        settings.token_rotation_days,
    )

    logger.info("スケジューラー起動完了。Ctrl+C で停止します。")

    # メインループ: schedule が登録されたジョブを実行時刻になったら呼び出す
    # schedule.run_pending() は「実行時刻が来たジョブ」を実行する（来ていない場合は何もしない）
    # time.sleep(1) で 1 秒ごとにチェックすることで CPU 使用率を抑える
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        # Ctrl+C または Docker の `docker compose down` による SIGINT を受け取った場合
        logger.info("停止シグナルを受信しました。アプリを終了します。")
        sys.exit(0)


if __name__ == "__main__":
    main()
