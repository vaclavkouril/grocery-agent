from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine import make_url


def create_database_engine(
    url: str, *, create_parent: bool = False, read_only: bool = False
) -> Engine:
    """Build an engine without bootstrapping or migrating either database."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
        path = Path(parsed.database)
        if read_only:
            # mode=ro also prevents a lazy connection from creating a missing database.
            parsed = parsed.set(
                database="file:" + quote(str(path.resolve()), safe="/"),
                query={**parsed.query, "mode": "ro", "uri": "true"},
            )
        if create_parent:
            if read_only:
                raise ValueError("read-only database engines cannot create directories")
            path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(parsed)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            if read_only:
                cursor.execute("PRAGMA query_only=ON")
            else:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        @event.listens_for(engine, "begin")
        def begin_transaction(connection: Connection) -> None:
            # Explicit BEGIN makes SQLite reads repeatable and DDL transactional.
            connection.exec_driver_sql("BEGIN")

    elif read_only and engine.dialect.name == "postgresql":

        @event.listens_for(engine, "begin")
        def begin_read_only_transaction(connection: Connection) -> None:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    return engine


def open_database(url: str) -> Engine:
    """Compatibility helper for offer writers; control storage is never opened."""
    from grocery_agent.persistence.migrations import upgrade_database

    engine = create_database_engine(url, create_parent=True)
    try:
        upgrade_database(engine, "offers")
    except BaseException:
        engine.dispose()
        raise
    return engine
