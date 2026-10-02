"""Optional real-browser acceptance against local fixture offers and the actual backend."""

import asyncio
import shutil
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import uvicorn

from grocery_agent.backend.api import create_app
from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.recipes import RecipeExecutor
from grocery_agent.backend.repository import ControlRepository
from grocery_agent.backend.worker import Worker
from grocery_agent.catalogue.profiles import AcquisitionProfile, Coverage
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.config import Settings
from grocery_agent.persistence.control.schema import JobRow
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import upgrade_database
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from tests.application_support import NOW, complete_batch
from tests.test_meals import groceries
from tests.test_profile_catalogue import ProfiledFixtureRepository

playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.browser


def test_password_cookie_session_resumes_and_signout_revokes(engine: Any, tmp_path: Path) -> None:
    executable = shutil.which("chromium")
    if executable is None:
        pytest.skip("install Chromium to run fixture-backed browser acceptance")
    control = create_database_engine(f"sqlite:///{tmp_path / 'cookie-control.db'}")
    upgrade_database(control, "control")
    repo = ControlRepository(control)
    repo.bootstrap("admin", NOW, password="a long fixture password")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = Settings(database_url=str(engine.url), meal_config=ROOT / "config/meals.toml")
    backend = BackendSettings(
        providers=("template",),
        password_login_enabled=True,
        cookie_sessions_enabled=True,
        cookie_secure=False,
        trusted_origin=origin,
    )
    app = create_app(settings, backend, engine=control, clock=lambda: NOW)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(executable_path=executable, headless=True)
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if urlparse(route.request.url).hostname == "127.0.0.1"
                    else route.abort()
                ),
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin)
            playwright.expect(page.locator("#password-form")).to_be_visible()
            page.locator("#login-username").fill("admin")
            page.locator("#login-password").fill("a long fixture password")
            page.get_by_role("button", name="Sign in with password", exact=True).click()
            playwright.expect(page.locator("#workspace")).to_be_visible()
            assert page.evaluate("localStorage.length + sessionStorage.length") == 0
            assert page.locator("#login-password").input_value() == ""
            cookies = context.cookies()
            assert any(
                cookie["name"] == "grocery_session"
                and cookie["httpOnly"]
                and cookie["sameSite"] == "Strict"
                for cookie in cookies
            )
            page.reload()
            playwright.expect(page.locator("#workspace")).to_be_visible()
            page.get_by_role("button", name="Sign out", exact=True).click()
            playwright.expect(page.locator("#signin-panel")).to_be_visible()
            page.reload()
            playwright.expect(page.locator("#password-form")).to_be_visible()
            playwright.expect(page.locator("#workspace")).to_be_hidden()
            assert not any(cookie["name"] == "grocery_session" for cookie in context.cookies())
            assert not errors
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        control.dispose()


def test_desktop_mobile_recipe_offers_reports_relogin_and_isolation(
    engine: Any,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    tmp_path: Path,
) -> None:
    executable = shutil.which("chromium")
    if executable is None:
        pytest.skip("install Chromium to run fixture-backed browser acceptance")
    run = complete_batch(
        repository, snapshots, [row.offer for row in groceries(candidate)], source="kupi"
    )
    SQLAlchemyCatalogueRepository(engine).publish(run.run_id)
    profile = AcquisitionProfile(source_id="kupi", name="browser-fixture")
    selected = complete_batch(
        ProfiledFixtureRepository(engine, profile),
        snapshots,
        [row.offer for row in groceries(candidate)],
        source="kupi",
    )
    catalogue = SQLAlchemyCatalogueRepository(engine)
    catalogue.record_profile(
        selected.run_id,
        profile,
        Coverage(
            profile_fingerprint=profile.fingerprint, observed=selected.accepted, complete=True
        ),
    )
    catalogue.publish(selected.run_id)
    secondary_profile = AcquisitionProfile(source_id="tesco", name="browser-online")
    from grocery_agent.models.offer import Offer

    secondary = complete_batch(
        ProfiledFixtureRepository(engine, secondary_profile),
        snapshots,
        [
            Offer.model_validate(
                {
                    **row.offer.model_dump(exclude_computed_fields=True),
                    "scope": "tesco:online:anonymous",
                }
            )
            for row in groceries(candidate)
        ],
        source="tesco",
    )
    catalogue.record_profile(
        secondary.run_id,
        secondary_profile,
        Coverage(
            profile_fingerprint=secondary_profile.fingerprint,
            observed=secondary.accepted,
            complete=True,
        ),
    )
    catalogue.publish(secondary.run_id)
    control = create_database_engine(f"sqlite:///{tmp_path / 'browser-control.db'}")
    upgrade_database(control, "control")
    repo = ControlRepository(control)
    token = repo.bootstrap("admin", NOW)
    bob_token = repo.accept_invite(repo.invite("bob", NOW), NOW, 24)
    settings = Settings(
        database_url=str(engine.url), meal_config=ROOT / "config/meals.toml", _env_file=None
    )
    backend = BackendSettings(
        providers=("template",),
        source_scopes={
            "kupi": ("kupi:locality:praha",),
            "tesco": ("tesco:online:anonymous",),
        },
    )
    app = create_app(settings, backend, engine=control, clock=lambda: NOW)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "fixture API did not start"
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(executable_path=executable, headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000}, accept_downloads=True
            )
            # No retailer, model or other external network requests are permitted.
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if urlparse(route.request.url).hostname == "127.0.0.1"
                    else route.abort()
                ),
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}/")
            page.get_by_label("Session token", exact=True).fill(token)
            page.get_by_role("button", name="Sign in", exact=True).click()
            playwright.expect(page.locator("#workspace")).to_be_visible()
            playwright.expect(page.locator("#recipe-profile option")).to_have_count(2)
            page.locator("#recipe-sources").select_option(["kupi", "tesco"])
            page.locator("#recipe-profile").select_option(profile.fingerprint)
            page.locator("#extra-source-profiles select").select_option(
                secondary_profile.fingerprint
            )
            page.locator("#pantry-rows select").select_option("rice")
            page.locator("#pantry-rows input").fill("500g")
            page.get_by_label("Budget per serving").fill("80.0000")
            page.get_by_role("button", name="Find my next meal").click()
            playwright.expect(page.locator("#job-progress")).to_contain_text("queued")
            worker = Worker(
                repo, RecipeExecutor(settings, backend, lambda: NOW), settings, backend, lambda: NOW
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(lambda: asyncio.run(worker.once())).result(timeout=10)
            page.get_by_role("button", name="Check progress").click()
            playwright.expect(page.locator("#job-progress")).to_contain_text("succeeded")
            playwright.expect(page.locator(".recipe-card").first).to_be_visible()
            with repo.sessions() as session:
                from sqlalchemy import select

                stored = session.scalar(select(JobRow))
                assert stored.inputs["catalog"]["pantry"]["items"]["rice"]["grams"] == "500"
                assert stored.inputs["profile_fingerprint"] == profile.fingerprint
                assert stored.inputs["run_id"] == selected.run_id
                assert stored.inputs["catalogue_snapshot"]["run_ids"] == [
                    selected.run_id,
                    secondary.run_id,
                ]
            with page.expect_download() as downloaded:
                page.get_by_role("button", name="Download report", exact=True).click()
            assert downloaded.value.suggested_filename.endswith(".html")
            page.get_by_label("Comparable unit").select_option("kg")
            page.locator("#offer-profile").select_option(profile.fingerprint)
            page.get_by_role("button", name="Search offers").click()
            playwright.expect(page.locator("#offer-rows tr")).to_have_count(6)
            page.locator("#offer-source").select_option("tesco")
            page.locator("#offer-profile").select_option(secondary_profile.fingerprint)
            page.get_by_role("button", name="Search offers").click()
            playwright.expect(page.locator("#offer-rows tr")).to_have_count(6)
            assert (
                page.evaluate(
                    "Object.keys(localStorage).length + Object.keys(sessionStorage).length"
                )
                == 0
            )
            page.screenshot(path=str(tmp_path / "website-desktop.png"), full_page=True)
            page.reload()
            playwright.expect(page.locator("#signin-panel")).to_be_visible()
            page.get_by_label("Session token", exact=True).fill(token)
            page.get_by_role("button", name="Sign in", exact=True).click()
            playwright.expect(page.locator("#job-list li")).to_have_count(1)
            page.locator("#job-list").get_by_role("button", name="Open").click()
            playwright.expect(page.locator(".recipe-card").first).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(tmp_path / "website-mobile.png"), full_page=True)
            page.get_by_label("Invite username").fill("charlie")
            page.get_by_role("button", name="Create invitation").click()
            playwright.expect(page.locator("#new-invite-token")).not_to_be_empty()
            invitation = page.locator("#new-invite-token").inner_text()
            page.get_by_role("button", name="Sign out").click()
            playwright.expect(page.locator("#signin-panel")).to_be_visible()
            page.get_by_label("Session token", exact=True).fill(bob_token)
            page.get_by_role("button", name="Sign in", exact=True).click()
            playwright.expect(page.locator("#workspace")).to_be_visible()
            playwright.expect(page.locator("#job-list li")).to_have_count(0)
            playwright.expect(page.locator("#admin")).to_be_hidden()
            page.get_by_role("button", name="Find my next meal").click()
            playwright.expect(page.locator("#job-progress")).to_contain_text("queued")
            page.get_by_role("button", name="Cancel request").click()
            playwright.expect(page.locator("#job-progress")).to_contain_text("cancelled")
            page.get_by_role("button", name="Sign out").click()
            page.get_by_text("Have an invitation?", exact=True).click()
            page.get_by_label("Invitation token", exact=True).fill(invitation)
            page.get_by_role("button", name="Accept invitation").click()
            playwright.expect(page.locator("#workspace")).to_be_visible()
            playwright.expect(page.locator("#username")).to_have_text("charlie")
            playwright.expect(page.locator("#job-list li")).to_have_count(0)
            assert not errors, errors
            print(f"Browser screenshots: {tmp_path}")
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        control.dispose()
