"""Additive discovery and localized-client diagnostics, without model or retailer calls."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from grocery_agent.backend.api import create_app
from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.repository import ControlRepository
from grocery_agent.config import Settings
from tests.application_support import NOW
from tests.test_backend import control_repo as control_repo

ROOT = Path(__file__).resolve().parents[1]


def client(repo: ControlRepository, tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'offers.db'}",
        meal_config=ROOT / "config/meals.toml",
    )
    return TestClient(
        create_app(settings, BackendSettings(), engine=repo.sessions.kw["bind"], clock=lambda: NOW)
    )


def test_capabilities_publish_effective_defaults_and_translated_labels(
    control_repo: ControlRepository,
    tmp_path: Path,
) -> None:
    app = client(control_repo, tmp_path)
    token = control_repo.bootstrap("admin", NOW)
    response = app.get("/v1/capabilities", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["languages"] == ["cs", "en"]
    assert data["defaults"]["min_protein_g"] == "70"
    assert data["defaults"]["max_cost_per_serving_czk"] == "100"
    assert data["defaults"]["cache_policy"] == "cache-only"
    assert "request_id" not in data["defaults"]
    assert data["defaults"]["source_ids"] == ["kupi"]
    assert "billa" in data["retailer_ids"]
    assert set(data["ingredient_localized_labels"]) == set(data["ingredients"])
    assert (
        data["ingredient_localized_labels"]["rice"]["cs"]
        != data["ingredient_localized_labels"]["rice"]["en"]
    )
    assert data["read_only_policy"]["currency"] == "CZK"
    assert data["read_only_policy"]["provider_timeout_seconds"] == 120
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/v1/me/settings", "/v1/me/pantry", "/v1/me/presets"])
def test_private_settings_require_authentication(
    control_repo: ControlRepository,
    tmp_path: Path,
    path: str,
) -> None:
    response = client(control_repo, tmp_path).get(path)
    assert response.status_code == 401
    assert response.json()["code"] == "session-required"
    assert response.json()["detail"] == "Bearer session required"


def test_field_errors_are_coded_without_reflecting_input(
    control_repo: ControlRepository,
    tmp_path: Path,
) -> None:
    token = control_repo.bootstrap("admin", NOW)
    response = client(control_repo, tmp_path).post(
        "/v1/recipes",
        headers={"Authorization": f"Bearer {token}"},
        json={"servings": "private-invalid-input", "language": "unsupported"},
    )
    assert response.status_code == 422
    data: dict[str, Any] = response.json()
    assert data["code"] == "validation-error"
    assert data["fields"] == data["detail"]
    assert all("input" not in field for field in data["fields"])
    assert "private-invalid-input" not in str(data)
