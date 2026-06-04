"""
SQLAlchemy のデータベース接続管理モジュール。

このモジュールが担う役割:
  1. SQLite への接続設定（engine の作成）
  2. セッションファクトリ（SessionLocal）の提供
  3. テーブルの初期作成（起動時に1回）
  4. DB セッションのコンテキストマネージャー（get_db）

SQLAlchemy の基本概念:
  - Engine   : DB への接続プール。接続文字列（URL）から作る
  - Session  : 実際の DB 操作の単位。BEGIN/COMMIT/ROLLBACK を管理する
  - sessionmaker : Session を作るファクトリ（設定をまとめたクラス）
"""

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config import settings
from models import Base


# ── Engine の作成 ──────────────────────────────────────────────────────────
# "sqlite:///path/to/file.db" という形式が SQLite の接続 URL。
# connect_args={"check_same_thread": False} は SQLite 特有の設定。
# SQLite はデフォルトで「作成したスレッドからしか使えない」制限があるが、
# この引数を指定することでマルチスレッド環境でも安全に使えるようになる。
engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
    # echo=True にすると実行された SQL が標準出力に表示される（デバッグ用）
    echo=False,
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """
    SQLite 接続時に PRAGMA foreign_keys=ON を実行する。

    SQLite は外部キー制約がデフォルトで無効なので、
    接続のたびに明示的に有効化する必要がある。
    event.listens_for を使うことで、毎回の接続に自動的に適用できる。
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# ── セッションファクトリの作成 ────────────────────────────────────────────
# sessionmaker はセッションの設定をまとめたクラスを生成する。
# 引数:
#   autocommit=False : 明示的に commit() を呼ぶまでは DB に書き込まない
#   autoflush=False  : commit() 前に自動で flush（SQL発行）しない
#   bind=engine      : どの engine（= どの DB）に接続するか
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """
    アプリ起動時にテーブルを作成する。

    Base.metadata.create_all() は models.py で定義した全クラスのテーブルを
    DB に作成する。すでに存在するテーブルはスキップされる（冪等）。

    本番のスキーマ変更には alembic を使うが、初回起動時はこれで十分。
    """
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """
    DB セッションを提供するコンテキストマネージャー。

    使い方:
        with get_db() as db:
            users = db.query(User).all()
            db.add(new_user)
            db.commit()

    contextmanager デコレータの仕組み:
      - yield の前: セッションを開く（= BEGIN 相当）
      - yield: セッションを呼び出し元に渡す
      - yield の後（正常終了）: db.close() でセッションを閉じる
      - 例外発生時: db.rollback() で変更を取り消してから close()

    finally ブロックにより、例外が発生してもセッションは必ず閉じられる。
    """
    db: Session = SessionLocal()
    try:
        yield db
    except Exception:
        # 例外が起きた場合は変更を全て取り消す（ロールバック）
        db.rollback()
        raise
    finally:
        # 正常・異常どちらの場合もセッションを閉じる
        db.close()
