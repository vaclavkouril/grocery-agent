"""Owned account state and reusable recipe presets with serialized revisions."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from grocery_agent.account_models import (
    PantryPut,
    PantryView,
    PresetCreate,
    PresetPage,
    PresetUpdate,
    PresetView,
    SavedRecipeCreate,
    SavedRecipeDetail,
    SavedRecipePage,
    SavedRecipeView,
    SettingsPatch,
    SettingsView,
)
from grocery_agent.contracts import Principal
from grocery_agent.persistence.control.schema import (
    AccountStateRow,
    JobRow,
    ProfileRow,
    SavedRecipeRow,
    UserRow,
)
from grocery_agent.recipes import RecipeRequest

from .repository import ControlRepository

TRANSIENT_PARAMETERS = {"pantry", "use_first", "request_id", "condiments"}


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
                pantry_condiments=[],
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
                "retailer_ids": row.preferred_retailers,
                "max_stores": row.preferred_max_stores,
            }
        )

    def patch_settings(self, user_id: str, payload: SettingsPatch) -> SettingsView:
        with self.sessions.begin() as session:
            self._lock_owner(session, user_id)
            row = self._state(session, user_id)
            check_revision(row.settings_revision, payload.expected_revision)
            values = payload.model_dump(exclude={"expected_revision"}, exclude_unset=True)
            if "retailer_ids" in values:
                selected = values.pop("retailer_ids")
                values["preferred_retailers"] = list(selected) if selected is not None else None
            if "max_stores" in values:
                values["preferred_max_stores"] = values.pop("max_stores")
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
                PantryView(
                    revision=row.pantry_revision,
                    items=row.pantry_items,
                    condiments=tuple(row.pantry_condiments),
                )
                if row
                else PantryView()
            )

    def put_pantry(self, user_id: str, payload: PantryPut) -> PantryView:
        with self.sessions.begin() as session:
            self._lock_owner(session, user_id)
            row = self._state(session, user_id)
            check_revision(row.pantry_revision, payload.expected_revision)
            values = {
                "pantry_items": payload.model_dump(mode="json")["items"],
                "pantry_revision": payload.expected_revision + 1,
            }
            if "condiments" in payload.model_fields_set:
                values["pantry_condiments"] = list(payload.condiments)
            session.flush()
            changed = session.scalar(
                update(AccountStateRow)
                .where(
                    AccountStateRow.user_id == user_id,
                    AccountStateRow.pantry_revision == payload.expected_revision,
                )
                .values(**values)
                .returning(AccountStateRow)
            )
            if changed is None:
                raise HTTPException(409, "Revision conflict; reload current state")
            return PantryView(
                revision=changed.pantry_revision,
                items=changed.pantry_items,
                condiments=tuple(changed.pantry_condiments),
            )

    def _saved_job(self, session: Session, user_id: str, job_id: str) -> JobRow:
        job = session.scalar(select(JobRow).where(JobRow.id == job_id, JobRow.user_id == user_id))
        if job is None:
            raise HTTPException(404, "Recipe job not found")
        return job

    def _saved_view(self, row: SavedRecipeRow, job: JobRow) -> SavedRecipeDetail:
        result = job.result or {}
        return SavedRecipeDetail(
            id=row.id,
            job_id=row.job_id,
            meal_index=row.meal_index,
            created_at=row.created_at,
            meal=result["report"]["meals"][row.meal_index],
            request=job.request,
            result=result,
        )

    def save_recipe(
        self, user_id: str, payload: SavedRecipeCreate, now: datetime
    ) -> tuple[SavedRecipeDetail, bool]:
        with self.sessions.begin() as session:
            self._lock_owner(session, user_id)
            job = self._saved_job(session, user_id, payload.job_id)
            if job.kind != "recipe" or job.state != "succeeded":
                raise HTTPException(422, "Only succeeded recipe jobs can be bookmarked")
            report = (job.result or {}).get("report")
            meals = report.get("meals") if isinstance(report, dict) else None
            if (
                not isinstance(meals, list)
                or not 0 <= payload.meal_index < len(meals)
                or not isinstance(meals[payload.meal_index], dict)
            ):
                raise HTTPException(422, "Invalid report meal index")
            existing = session.scalar(
                select(SavedRecipeRow).where(
                    SavedRecipeRow.user_id == user_id,
                    SavedRecipeRow.job_id == payload.job_id,
                    SavedRecipeRow.meal_index == payload.meal_index,
                )
            )
            if existing is not None:
                return self._saved_view(existing, job), False
            count = session.scalar(
                select(func.count())
                .select_from(SavedRecipeRow)
                .where(SavedRecipeRow.user_id == user_id)
            )
            if count is not None and count >= 500:
                raise HTTPException(409, "Saved recipe limit reached (500)")
            row = SavedRecipeRow(
                id=str(uuid4()),
                user_id=user_id,
                job_id=job.id,
                meal_index=payload.meal_index,
                created_at=now,
            )
            session.add(row)
            return self._saved_view(row, job), True

    def saved_recipes(self, user_id: str, *, offset: int = 0, limit: int = 20) -> SavedRecipePage:
        with self.sessions() as session:
            owned = SavedRecipeRow.user_id == user_id
            total = session.scalar(select(func.count()).select_from(SavedRecipeRow).where(owned))
            rows = session.execute(
                select(SavedRecipeRow, JobRow)
                .join(JobRow, SavedRecipeRow.job_id == JobRow.id)
                .where(owned, JobRow.user_id == user_id)
                .order_by(SavedRecipeRow.created_at.desc(), SavedRecipeRow.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            return SavedRecipePage(
                items=[
                    SavedRecipeView.model_validate(
                        self._saved_view(row, job).model_dump(exclude={"request", "result"})
                    )
                    for row, job in rows
                ],
                total=total or 0,
                offset=offset,
                limit=limit,
            )

    def saved_recipe(self, user_id: str, saved_id: str) -> SavedRecipeDetail:
        with self.sessions() as session:
            row = session.scalar(
                select(SavedRecipeRow).where(
                    SavedRecipeRow.user_id == user_id, SavedRecipeRow.id == saved_id
                )
            )
            if row is None:
                raise HTTPException(404, "Saved recipe not found")
            return self._saved_view(row, self._saved_job(session, user_id, row.job_id))

    def delete_saved_recipe(self, user_id: str, saved_id: str) -> None:
        with self.sessions.begin() as session:
            self._lock_owner(session, user_id)
            removed = session.scalar(
                delete(SavedRecipeRow)
                .where(SavedRecipeRow.user_id == user_id, SavedRecipeRow.id == saved_id)
                .returning(SavedRecipeRow.id)
            )
            if removed is None:
                raise HTTPException(404, "Saved recipe not found")

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
        if payload.model_fields_set & {"retailer_ids", "max_stores"}:
            # Reuse configured defaults without acquisition or provider work. The
            # capabilities endpoint is already mounted by the application.
            defaults: dict[str, Any] = {}
            for route in app.routes:
                if isinstance(route, APIRoute) and route.path == "/v1/capabilities":
                    defaults = dict(route.endpoint(user).defaults)
                    break
            changes = {
                key: value if value is not None else getattr(RecipeRequest(), key)
                for key, value in payload.model_dump(exclude_unset=True).items()
                if key in {"retailer_ids", "max_stores"}
            }
            try:
                validate(
                    RecipeRequest.model_validate(
                        {
                            key: value
                            for key, value in defaults.items()
                            if key in RecipeRequest.model_fields
                        }
                    ).model_copy(update=changes)
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        return repo.patch_settings(user.user_id, payload)

    @app.post("/v1/me/saved-recipes", status_code=201, response_model=SavedRecipeDetail)
    def save_recipe(
        payload: SavedRecipeCreate,
        response: Response,
        user: Annotated[Principal, Depends(principal)],
    ) -> SavedRecipeDetail:
        saved, created = repo.save_recipe(user.user_id, payload, clock())
        response.status_code = 201 if created else 200
        return saved

    @app.get("/v1/me/saved-recipes", response_model=SavedRecipePage)
    def saved_recipes(
        user: Annotated[Principal, Depends(principal)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> SavedRecipePage:
        return repo.saved_recipes(user.user_id, offset=offset, limit=limit)

    @app.get("/v1/me/saved-recipes/{saved_id}", response_model=SavedRecipeDetail)
    def saved_recipe(
        saved_id: str, user: Annotated[Principal, Depends(principal)]
    ) -> SavedRecipeDetail:
        return repo.saved_recipe(user.user_id, saved_id)

    @app.delete("/v1/me/saved-recipes/{saved_id}", status_code=204)
    def delete_saved_recipe(
        saved_id: str, user: Annotated[Principal, Depends(principal)]
    ) -> Response:
        repo.delete_saved_recipe(user.user_id, saved_id)
        return Response(status_code=204)

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
