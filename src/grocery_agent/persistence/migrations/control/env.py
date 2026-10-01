from grocery_agent.persistence.control.schema import ControlBase
from grocery_agent.persistence.migrations.environment import run_migrations

run_migrations(ControlBase.metadata, "control_schema_version")
