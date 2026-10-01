from alembic import context
from sqlalchemy import Connection, MetaData


def run_migrations(metadata: MetaData, version_table: str) -> None:
    connection = context.config.attributes.get("connection")
    if not isinstance(connection, Connection):
        raise ValueError("use grocery-agent db commands to supply the database connection")
    context.configure(
        connection=connection,
        target_metadata=metadata,
        version_table=version_table,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
