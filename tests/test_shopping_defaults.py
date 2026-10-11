"""Account shopping preferences are shared defaults, never mandatory filters."""

from pathlib import Path
from typing import Any

from grocery_agent.account_models import SettingsPatch
from grocery_agent.account_preferences import apply_shopping_preferences
from grocery_agent.backend.channels import ChannelRepository
from grocery_agent.backend.user_state import UserStateRepository
from grocery_agent.recipes import RecipeRequest
from tests.application_support import NOW
from tests.test_backend import control_repo as control_repo
from tests.test_bilingual_capabilities import client
from tests.test_channels_backend import capabilities, verify


def test_explicit_choices_override_account_defaults() -> None:
    preferences = {"retailer_ids": ["lidl", "albert"], "max_stores": 2}
    implicit = apply_shopping_preferences(RecipeRequest(), preferences)
    assert implicit.retailer_ids == ("lidl", "albert")
    assert implicit.max_stores == 2
    explicit = RecipeRequest(retailer_ids=(), max_stores=1)
    assert apply_shopping_preferences(explicit, preferences) is explicit
    unchanged = RecipeRequest()
    assert apply_shopping_preferences(unchanged, {}) is unchanged


def test_capabilities_api_and_channels_share_preferences(control_repo: Any, tmp_path: Path) -> None:
    token = control_repo.bootstrap("admin", NOW)
    user = control_repo.authenticate(token, NOW).user_id
    state = UserStateRepository(control_repo)
    state.patch_settings(
        user, SettingsPatch(expected_revision=0, retailer_ids=("lidl", "albert"), max_stores=2)
    )
    app = client(control_repo, tmp_path)
    headers = {"Authorization": f"Bearer {token}"}
    defaults = app.get("/v1/capabilities", headers=headers).json()["defaults"]
    assert defaults["retailer_ids"] == ["lidl", "albert"]
    assert defaults["max_stores"] == 2
    for payload, retailers, stores in (
        ({}, ["lidl", "albert"], 2),
        ({"retailer_ids": [], "max_stores": 1}, [], 1),
    ):
        submitted = app.post("/v1/recipes", headers=headers, json=payload)
        assert submitted.status_code == 202, submitted.text
        effective = app.get(
            f"/v1/jobs/{submitted.json()['job_id']}/request", headers=headers
        ).json()
        assert effective["retailer_ids"] == retailers
        assert effective["max_stores"] == stores
    channels = ChannelRepository(control_repo)
    verify(channels, user)
    seen: list[RecipeRequest] = []

    def validate(request: RecipeRequest) -> RecipeRequest:
        seen.append(request)
        return request

    result = channels.intake(
        "simplex", "defaults", "42", "recipe", NOW, capabilities(), validate, 10
    )
    assert result["status"] == "processed"
    assert seen[0].retailer_ids == ("lidl", "albert")
    assert seen[0].max_stores == 2
    channels.intake(
        "simplex",
        "explicit",
        "42",
        "recipe max_stores=1 retailer=billa",
        NOW,
        capabilities(),
        validate,
        10,
    )
    assert seen[-1].retailer_ids == ("billa",)
    assert seen[-1].max_stores == 1
