"""Подключение к SQLite.

WAL нужен потому, что писать в базу будут одновременно веб-запросы и поток-
супервизор; без него они начнут блокировать друг друга. busy_timeout убирает
случайные «database is locked» на коротких пересечениях.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, scoped_session, sessionmaker

from app.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL,
    future=True,
    # SQLite-соединение шарится между потоками супервизора и веб-запросами;
    # синхронизацию обеспечивают WAL и busy_timeout, а не GIL.
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ARG001
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
ScopedSession = scoped_session(SessionFactory)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Транзакция с гарантированным закрытием.

    В парке уже был долг «sqlite3-соединения без finally» — здесь закрываем
    через контекст-менеджер сразу, чтобы он не появился снова.
    """
    session = SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
