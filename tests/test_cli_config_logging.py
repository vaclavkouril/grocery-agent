import json
import logging
from pathlib import Path

import pytest

from grocery_agent.cli.main import main
from grocery_agent.config import Settings
from grocery_agent.logging import JSONFormatter


@pytest.fixture(autouse=True)
def preserve_pytest_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    # CLI owns logging in a process; in-process tests must retain pytest's capture handlers.
    monkeypatch.setattr("grocery_agent.cli.main.configure_logging", lambda level: None)


def test_store_listing(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["stores"]) == 0
    assert capsys.readouterr().out.strip() == "mock"


@pytest.mark.parametrize(
    "args",
    [
        ["scrape"],
        ["scrape", "mock", "--all"],
        ["scrape", "albert"],
        ["runs", "--limit", "0"],
    ],
)
def test_cli_argument_errors(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2


def test_cli_mock_scrape_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["scrape", "mock"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["accepted"] == 5 and first["changed"] == 5
    assert main(["scrape", "--all"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["accepted"] == 5 and second["changed"] == 0
    assert main(["runs", "--limit", "2"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GROCERY_HTTP_TIMEOUT_SECONDS=10\nGROCERY_LOG_LEVEL=DEBUG\n")
    monkeypatch.setenv("GROCERY_HTTP_TIMEOUT_SECONDS", "20")
    settings = Settings()
    assert settings.http_timeout_seconds == 20 and settings.log_level == "DEBUG"


def test_invalid_configuration_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROCERY_HTTP_TIMEOUT_SECONDS", "-1")
    assert main(["scrape", "mock"]) == 1


def test_structured_log_format() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "scrape_finished", (), None)
    record.fields = {"store_id": "mock", "accepted": 5}
    parsed = json.loads(JSONFormatter().format(record))
    assert parsed["event"] == "scrape_finished" and parsed["store_id"] == "mock"
    assert parsed["accepted"] == 5 and parsed["timestamp"] and parsed["level"] == "INFO"
