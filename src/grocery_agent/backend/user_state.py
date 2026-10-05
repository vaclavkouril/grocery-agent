"""Owned account state and reusable recipe presets with serialized revisions."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from grocery_agent.account_models import (
    PantryPut,
    PantryView,
    PresetCreate,
    PresetPage,
    PresetUpdate,
    PresetView,
    SettingsPatch,
    SettingsView,
)
from grocery_agent.contracts import Principal
from grocery_agent.persistence.control.schema import AccountStateRow, JobRow, ProfileRow, UserRow
from grocery_agent.recipes import RecipeRequest

from .repository import ControlRepository

TRANSIENT_PARAMETERS = {"pantry", "use_first", "request_id"}


def preset_view(
    row: ProfileRow, validate: Callable[[RecipeRequest], RecipeRequest] | None = None
) -> PresetView:
    stale = bool(TRANSIENT_PARAMETERS & row.parameters.keys())
    try:
        request = RecipeRequest.model_validate(row.parameters)
        if validate is not None:
            validate(request)
    except (ValueError, HTTPException):
        stale = True
    return PresetView(
        id=row.id,
        name=row.name,
        revision=row.revision,
        parameters=row.parameters,
        stale=stale,
    )


def check_revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise HTTPException(409, "Revision conflict; reload current state")


class UserStateRepository:
    def __init__(
        self,
        control: ControlRepository,
        validate: Callable[[RecipeRequest], RecipeRequest] | None = None,
    ) -> None:
        self.sessions = control.sessions
        self.validate = validate

    def _lock_owner(self, session: Session, user_id: str) -> None:
        # The first statement acquires SQLite's writer lock or a PostgreSQL row lock.
        # This also serializes initial state creation and the per-owner preset quota.
        found = session.scalar(
            update(UserRow).where(UserRow.id == user_id).values(id=UserRow.id).returning(UserRow.id)
        )
        if found is None:
            raise HTTPException(404, "Account not found")

    def _state(self, session: Session, user_id: str) -> AccountStateRow:
        row = session.get(AccountStateRow, user_id)
        if row is None:
            row = AccountStateRow(
                user_id=user_id,
                settings_revision=0,
                pantry_revision=0,
                pantry_items={},
                ui_language=None,
                recipe_language=None,
            )
            session.add(row)
        return row

    def settings(self, user_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(AccountStateRow, user_id)
            return (self._settings_view(row) if row else SettingsView()).model_dump(mode="json")

    def _settings_view(self, row: AccountStateRow) -> SettingsView:
        return SettingsView.model_validate(
            {
                "revision": row.settings_revision,
                "ui_language": row.ui_language,
                "recipe_language": row.recipe_language,
            }
        )

    def patch_settings(self, user_id: str, payload: SettingsPatch) -> SettingsView:
        with self.sessions.begin() as session:
            self._lock_owner(session, user_id)
            row = self._state(session, user_id)
            check_revision(row.settings_revision, payload.expected_revision)
            values = payload.model_dump(exclude={"expected_revision"}, exclude_unset=True)
            session.flush()
            changed = session.scalar(
                update(AccountStateRow)
                .where(
                    AccountStateRow.user_id == user_id,
                    AccountStateRow.settings_revision == payload.expected_revision,
                )
                .values(**values, settings_revision=payload.expected_revision + 1)
                .returning(AccountStateRow)
            )
            if changed is None:
                raise HTTPException(409, "Revision conflict; reload current state")
            return self._settings_view(changed)

    def pantry(self, user_id: str) -> PantryView:
        with self.sessions() as session:
            row = session.get(AccountStateRow, user_id)
            return (
                PantryView(revision=row.pantry_revision, items=row.pantry_items)
                if row
                else PantryView()
            )

    def put_pantry(self, user_id: str, payload: PantryPut) -> PantryView:
        with self.sessions.begin() as session:
            self._lock_owner(session, user_id)
            row = self._state(session, user_id)
            check_revision(row.pantry_revision, payload.expected_revision)
            session.flush()
            changed = session.scalar(
                update(AccountStateRow)
                .where(
                    AccountStateRow.user_id == user_id,
                    AccountStateRow.pantry_revision == payload.expected_revision,
                )
                .values(
                    pantry_items=payload.model_dump(mode="json")["items"],
                    pantry_revision=payload.expected_revision + 1,
                )
                .returning(AccountStateRow)
            )
            if changed is None:
                raise HTTPException(409, "Revision conflict; reload current state")
            return PantryView(revision=changed.pantry_revision, items=changed.pantry_items)

    def presets(self, user_id: str) -> PresetPage:
        with self.sessions() as session:
            rows = session.scalars(
                select(ProfileRow)
                .where(ProfileRow.user_id == user_id)
                .order_by(ProfileRow.name, ProfileRow.id)
            ).all()
            return PresetPage(
                items=[preset_view(row, self.validate) for row in rows], total=len(rows)
            )

    def _preset(self, session: Session, user_id: str, preset_id: str) -> ProfileRow:
        row = session.scalar(
            select(ProfileRow).where(ProfileRow.user_id == user_id, ProfileRow.id == preset_id)
        )
        if row is None:
            raise HTTPException(404, "Preset not found")
        return row

    def preset(self, user_id: str, preset_id: str) -> PresetView:
        with self.sessions() as session:
            return preset_view(self._preset(session, user_id, preset_id), self.validate)

    def check_preset_revision(self, user_id: str, preset_id: str, expected_revision: int) -> None:
        with self.sessions() as session:
            row = self._preset(session, user_id, preset_id)
            check_revision(row.revision, expected_revision)

    def _unique_name(
        self, session: Session, user_id: str, name: str, preset_id: str | None = None
    ) -> None:
        query = select(ProfileRow.id).where(
            ProfileRow.user_id == user_id,
            ProfileRow.name == name,
        )
        if preset_id is not None:
            query = query.where(ProfileRow.id != preset_id)
        duplicate = session.scalar(query)
        if duplicate is not None:
            raise HTTPException(409, "A preset with that name already exists")

    def create_preset(
        self, user_id: str, name: str, parameters: dict[str, Any], now: datetime
    ) -> PresetView:
        with self.sessions.begin() as session:
            self._lock_owner(session, user_id)
            rows = session.scalars(select(ProfileRow.id).where(ProfileRow.user_id == user_id)).all()
            if len(rows) >= 100:
                raise HTTPException(409, "Preset limit reached (100)")
            self._unique_name(session, user_id, name)
            row = ProfileRow(
                id=str(uuid4()),
                user_id=user_id,
                name=name,
                parameters=parameters,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            return preset_view(row, self.validate)

    def update_preset(
        self,
        user_id: str,
        preset_id: str,
        payload: PresetUpdate,
        parameters: dict[str, Any] | None,
        now: datetime,
    ) -> PresetView:
        with self.sessions.begin() as session:
            self._lock_owner(session, user_id)
            row = self._preset(session, user_id, preset_id)
            check_revision(row.revision, payload.expected_revision)
            values: dict[str, Any] = {
                "revision": payload.expected_revision + 1,
                "updated_at": now,
            }
            if payload.name is not None:
                self._unique_name(session, user_id, payload.name, preset_id)
                values["name"] = payload.name
            if parameters is not None:
                values["parameters"] = parameters
            changed = session.scalar(
                update(ProfileRow)
                .where(
                    ProfileRow.user_id == user_id,
                    ProfileRow.id == preset_id,
                    ProfileRow.revision == payload.expected_revision,
                )
                .values(**values)
                .returning(ProfileRow)
            )
            if changed is None:
                raise HTTPException(409, "Revision conflict; reload current state")
            return preset_view(changed, self.validate)

    def delete_preset(self, user_id: str, preset_id: str, expected_revision: int) -> None:
        with self.sessions.begin() as session:
            self._lock_owner(session, user_id)
            row = self._preset(session, user_id, preset_id)
            check_revision(row.revision, expected_revision)
            changed = session.scalar(
                delete(ProfileRow)
                .where(
                    ProfileRow.user_id == user_id,
                    ProfileRow.id == preset_id,
                    ProfileRow.revision == expected_revision,
                )
                .returning(ProfileRow.id)
            )
            if changed is None:
                raise HTTPException(409, "Revision conflict; reload current state")

    def job_request(self, user_id: str, job_id: str) -> RecipeRequest:
        with self.sessions() as session:
            row = session.scalar(
                select(JobRow).where(
                    JobRow.user_id == user_id, JobRow.id == job_id, JobRow.kind == "recipe"
                )
            )
            if row is None:
                raise HTTPException(404, "Recipe job not found")
            return RecipeRequest.model_validate(row.request)


def mount_user_state(
    app: FastAPI,
    control: ControlRepository,
    principal: Callable[..., Principal],
    clock: Callable[[], datetime],
    validate: Callable[[RecipeRequest], RecipeRequest],
) -> None:
    repo = UserStateRepository(control, validate)

    def parameters(value: dict[str, Any]) -> dict[str, Any]:
        try:
            request = validate(RecipeRequest.model_validate(value))
        except ValidationError as exc:
            raise HTTPException(422, "Invalid recipe preset parameters") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return request.model_dump(mode="json", exclude=TRANSIENT_PARAMETERS)

    @app.get("/v1/me/settings", response_model=SettingsView)
    def settings(user: Annotated[Principal, Depends(principal)]) -> SettingsView:
        return SettingsView.model_validate(repo.settings(user.user_id))

    @app.patch("/v1/me/settings", response_model=SettingsView)
    def patch_settings(
        payload: SettingsPatch, user: Annotated[Principal, Depends(principal)]
    ) -> SettingsView:
        return repo.patch_settings(user.user_id, payload)

    @app.get("/v1/me/pantry", response_model=PantryView)
    def pantry(user: Annotated[Principal, Depends(principal)]) -> PantryView:
        return repo.pantry(user.user_id)

    @app.put("/v1/me/pantry", response_model=PantryView)
    def put_pantry(
        payload: PantryPut, user: Annotated[Principal, Depends(principal)]
    ) -> PantryView:
        return repo.put_pantry(user.user_id, payload)

    @app.get("/v1/me/presets", response_model=PresetPage)
    def presets(user: Annotated[Principal, Depends(principal)]) -> PresetPage:
        return repo.presets(user.user_id)

    @app.post("/v1/me/presets", status_code=201, response_model=PresetView)
    def create_preset(
        payload: PresetCreate, user: Annotated[Principal, Depends(principal)]
    ) -> PresetView:
        return repo.create_preset(
            user.user_id, payload.name, parameters(payload.parameters), clock()
        )

    @app.get("/v1/me/presets/{preset_id}", response_model=PresetView)
    def preset(preset_id: str, user: Annotated[Principal, Depends(principal)]) -> PresetView:
        return repo.preset(user.user_id, preset_id)

    @app.patch("/v1/me/presets/{preset_id}", response_model=PresetView)
    def update_preset(
        preset_id: str, payload: PresetUpdate, user: Annotated[Principal, Depends(principal)]
    ) -> PresetView:
        repo.check_preset_revision(user.user_id, preset_id, payload.expected_revision)
        normalized = parameters(payload.parameters) if payload.parameters is not None else None
        # update_preset rechecks under the owner lock and uses a revision SQL predicate:
        # a write racing with validation must still fail rather than overwrite state.
        return repo.update_preset(user.user_id, preset_id, payload, normalized, clock())

    @app.delete("/v1/me/presets/{preset_id}", status_code=204)
    def delete_preset(
        preset_id: str,
        expected_revision: Annotated[int, Query(ge=0)],
        user: Annotated[Principal, Depends(principal)],
    ) -> Response:
        repo.delete_preset(user.user_id, preset_id, expected_revision)
        return Response(status_code=204)

    @app.get("/v1/jobs/{job_id}/request", response_model=RecipeRequest)
    def job_request(job_id: str, user: Annotated[Principal, Depends(principal)]) -> RecipeRequest:
        return repo.job_request(user.user_id, job_id)
