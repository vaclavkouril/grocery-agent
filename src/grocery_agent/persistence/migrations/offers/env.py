from grocery_agent.catalogue import schema as catalogue_schema
from grocery_agent.persistence.migrations.environment import run_migrations
from grocery_agent.persistence.schema import Base

assert catalogue_schema.CatalogueHeadRow.metadata is Base.metadata
run_migrations(Base.metadata, "offers_schema_version")
