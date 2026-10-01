"""Independent migration histories with conservative legacy database adoption."""

from io import StringIO
from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, MetaData, UniqueConstraint, inspect

from grocery_agent.persistence.migrations.baselines import control_baseline, offer_baseline

DatabaseTarget = Literal["offers", "control"]


def migration_config(target: DatabaseTarget) -> Config:
    if target not in {"offers", "control"}:
        raise ValueError("database target must be offers or control")
    config = Config(stdout=StringIO())
    config.set_main_option(
        "script_location", str(Path(__file__).parent / target).replace("%", "%%")
    )
    return config


def schema_version(engine: Engine, target: DatabaseTarget) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(
            connection, opts={"version_table": f"{target}_schema_version"}
        ).get_current_revision()


def validate_baseline(connection: Connection, metadata: MetaData) -> None:
    inspector = inspect(connection)
    actual = set(inspector.get_table_names())
    if actual != metadata.tables.keys():
        raise ValueError("unversioned database does not match the complete baseline schema")
    for name, table in metadata.tables.items():
        columns = {column["name"]: column for column in inspector.get_columns(name)}
        if set(columns) != set(table.columns.keys()):
            raise ValueError(f"legacy schema column mismatch in {name}")
        for expected in table.columns:
            actual_column = columns[expected.name]
            wanted_type = str(expected.type.compile(dialect=connection.dialect)).upper()
            actual_type = str(actual_column["type"].compile(dialect=connection.dialect)).upper()
            if actual_type != wanted_type or actual_column["nullable"] != expected.nullable:
                raise ValueError(
                    f"legacy schema type/nullability mismatch in {name}.{expected.name}"
                )
        actual_pk = set(inspector.get_pk_constraint(name)["constrained_columns"])
        if actual_pk != {column.name for column in table.primary_key}:
            raise ValueError(f"legacy schema primary key mismatch in {name}")
        actual_unique = {
            tuple(item["column_names"]) for item in inspector.get_unique_constraints(name)
        }
        expected_unique = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        if actual_unique != expected_unique:
            raise ValueError(f"legacy schema unique constraint mismatch in {name}")
        actual_fks = {
            (
                tuple(item["constrained_columns"]),
                item["referred_table"],
                tuple(item["referred_columns"]),
            )
            for item in inspector.get_foreign_keys(name)
        }
        expected_fks = {
            (
                tuple(column.name for column in constraint.columns),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
            )
            for constraint in table.foreign_key_constraints
        }
        if actual_fks != expected_fks:
            raise ValueError(f"legacy schema foreign key mismatch in {name}")
        actual_indexes = {
            (tuple(item["column_names"]), bool(item["unique"]))
            for item in inspector.get_indexes(name)
            if not item.get("duplicates_constraint")
        }
        expected_indexes = {
            (tuple(column.name for column in index.columns), bool(index.unique))
            for index in table.indexes
        }
        if actual_indexes != expected_indexes:
            raise ValueError(f"legacy schema index mismatch in {name}")


def upgrade_database(engine: Engine, target: DatabaseTarget) -> str:
    config = migration_config(target)
    baseline = offer_baseline() if target == "offers" else control_baseline()
    version_table = f"{target}_schema_version"
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        other_version = "control_schema_version" if target == "offers" else "offers_schema_version"
        if other_version in tables:
            raise ValueError("offer and control databases must be separate")
        config.attributes["connection"] = connection
        if version_table not in tables and tables:
            validate_baseline(connection, baseline)
            command.stamp(config, f"{target}_0001")
        command.upgrade(config, "head")
    revision = schema_version(engine, target)
    assert revision is not None
    return revision


def require_offer_schema(engine: Engine) -> None:
    revision = schema_version(engine, "offers")
    if revision is None:
        with engine.connect() as connection:
            validate_baseline(connection, offer_baseline())
        return  # The legacy baseline remains readable without writing a version table.
    head = ScriptDirectory.from_config(migration_config("offers")).get_current_head()
    if revision != head:
        raise ValueError("offer schema needs migration; run grocery-agent db upgrade offers")
