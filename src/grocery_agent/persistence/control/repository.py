"""Private profile persistence; authentication is composed by future callers."""

from typing import Annotated, Protocol
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import sessionmaker

from grocery_agent.application.parameters import MealParameters
from grocery_agent.models.common import DomainModel, utc_now
from grocery_agent.persistence.control.schema import ProfileRow


class StoredProfile(DomainModel):
    id: UUID
    user_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=100)]
    revision: Annotated[int, Field(ge=1)]
    parameters: MealParameters


class ProfileRepository(Protocol):
    def get(self, user_id: UUID, profile_id: UUID) -> StoredProfile | None: ...

    def create(self, user_id: UUID, name: str, parameters: MealParameters) -> StoredProfile: ...

    def update(
        self, user_id: UUID, profile_id: UUID, parameters: MealParameters, expected_revision: int
    ) -> StoredProfile: ...


def as_profile(row: ProfileRow) -> StoredProfile:
    return StoredProfile(
        id=UUID(row.id),
        user_id=UUID(row.user_id),
        name=row.name,
        revision=row.revision,
        parameters=MealParameters.model_validate(row.parameters),
    )


class SQLAlchemyProfileRepository:
    def __init__(self, engine: Engine) -> None:
        self.sessions = sessionmaker(engine)

    def get(self, user_id: UUID, profile_id: UUID) -> StoredProfile | None:
        with self.sessions() as session:
            row = session.scalar(
                select(ProfileRow).where(
                    ProfileRow.id == str(profile_id), ProfileRow.user_id == str(user_id)
                )
            )
            return as_profile(row) if row is not None else None

    def create(self, user_id: UUID, name: str, parameters: MealParameters) -> StoredProfile:
        profile = StoredProfile(
            id=uuid4(), user_id=user_id, name=name, revision=1, parameters=parameters
        )
        now = utc_now()
        with self.sessions.begin() as session:
            session.add(
                ProfileRow(
                    id=str(profile.id),
                    user_id=str(user_id),
                    name=profile.name,
                    revision=1,
                    parameters=parameters.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
            )
        return profile

    def update(
        self, user_id: UUID, profile_id: UUID, parameters: MealParameters, expected_revision: int
    ) -> StoredProfile:
        with self.sessions.begin() as session:
            changed_id = session.scalar(
                update(ProfileRow)
                .where(
                    ProfileRow.id == str(profile_id),
                    ProfileRow.user_id == str(user_id),
                    ProfileRow.revision == expected_revision,
                )
                .values(
                    parameters=parameters.model_dump(mode="json"),
                    revision=expected_revision + 1,
                    updated_at=utc_now(),
                )
                .returning(ProfileRow.id)
            )
            if changed_id is None:
                raise ValueError("profile is unavailable or was changed by another request")
            row = session.get(ProfileRow, str(profile_id))
            assert row is not None
            return as_profile(row)
