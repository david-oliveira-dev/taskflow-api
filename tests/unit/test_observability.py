"""Structured logging, offline.

What is checked here is not that logging works — it is that the log line says what it must
and nothing it must not. A log stream is a place secrets go to be retained, forwarded, and
read by people who should not see them.
"""

import json
import logging
from typing import Any

import pytest
import structlog

from taskflow_api.config import Settings
from taskflow_api.observability import add_correlation_id, configure_logging


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "environment": "local",
        "database_url": "postgresql+asyncpg://user:pass@localhost:5432/taskflow",
        "redis_url": "redis://localhost:6379/0",
        "jwt_secret": "a-test-signing-key-long-enough-for-hs256",
    }
    base.update(overrides)
    return Settings(**base)


class TestCorrelationProcessor:
    def test_outside_a_request_no_id_is_added(self) -> None:
        """A log line from a worker or a script must not invent one."""
        assert add_correlation_id(None, "info", {"event": "x"}) == {"event": "x"}

    def test_inside_a_request_the_id_is_attached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A processor, not a `bind()` at the edge: a line written deep in a service is
        correlated without that service knowing a request exists.
        """
        monkeypatch.setattr("taskflow_api.observability.correlation_id", lambda: "trace-1")

        assert add_correlation_id(None, "info", {"event": "x"}) == {
            "event": "x",
            "correlation_id": "trace-1",
        }


class TestFormat:
    def test_deployed_environments_get_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(_settings(environment="production"))

        structlog.get_logger("t").info("hello", extra_field=7)

        line = capsys.readouterr().out.strip().splitlines()[-1]
        parsed = json.loads(line)
        assert parsed["event"] == "hello"
        assert parsed["extra_field"] == 7
        assert parsed["level"] == "info"
        assert parsed["timestamp"].endswith("Z")

    def test_local_gets_something_a_person_can_read(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A developer reading a terminal and a shipper parsing a stream want opposites."""
        configure_logging(_settings(environment="local"))

        structlog.get_logger("t").info("hello")

        out = capsys.readouterr().out
        assert "hello" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out.strip().splitlines()[-1])

    def test_standard_library_logs_come_out_in_the_same_shape(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """SQLAlchemy and friends use `logging`. Half a stream in JSON and half in prose is
        a stream nothing can parse.
        """
        configure_logging(_settings(environment="ci"))

        logging.getLogger("sqlalchemy.engine").warning("a plain warning")

        parsed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert parsed["event"] == "a plain warning"
        assert parsed["level"] == "warning"
        assert parsed["logger"] == "sqlalchemy.engine"

    def test_configuring_twice_does_not_duplicate_output(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The tests build applications constantly; handlers must not stack."""
        configure_logging(_settings(environment="ci"))
        configure_logging(_settings(environment="ci"))

        logging.getLogger("t").warning("once")

        assert len(capsys.readouterr().out.strip().splitlines()) == 1

    def test_the_level_is_honoured(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(_settings(environment="ci", log_level="WARNING"))

        logging.getLogger("t").info("should not appear")

        assert capsys.readouterr().out.strip() == ""
