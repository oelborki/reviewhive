"""What the logging configuration has to get right.

The bug this replaced was not a crash. `warning` and `exception` already reached
stderr through Python's fallback handler, so the service looked like it was
logging; only the `info` lines that explain *why* a delivery was ignored were
discarded. These tests are written against that failure: the first one asserts an
INFO record survives, because that is the thing that did not.

Every test installs the handler onto a `StringIO` and restores the root logger
afterwards. `configure_logging` mutates process-wide state by design, and a test
that left it mutated would change what every later test sees.
"""

from __future__ import annotations

import io
import logging

import pytest

from reviewhive.logging_setup import PACKAGE_LOGGER, configure_logging


@pytest.fixture(autouse=True)
def _restore_logging():
    """Put the root logger and `reviewhive` back exactly as they were."""
    root = logging.getLogger()
    package = logging.getLogger(PACKAGE_LOGGER)
    saved = (root.handlers[:], root.level, package.level)
    yield
    root.handlers[:], root.level, package.level = saved


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


class TestWhatGetsThrough:
    def test_an_info_line_from_the_package_is_emitted(self, stream) -> None:
        """The whole point. This is the level that was being dropped, and these
        are the lines that say why a webhook returned 200 and did nothing."""
        configure_logging("INFO", stream=stream)

        logging.getLogger("reviewhive.api.webhook").info("delivery %s already recorded", "d-1")

        assert "delivery d-1 already recorded" in stream.getvalue()

    def test_third_party_info_is_not_emitted(self, stream) -> None:
        """The reason the root logger is not simply lowered. httpx logs a line
        per request at INFO; letting those through buries the handful of lines
        this exists to surface."""
        configure_logging("INFO", stream=stream)

        logging.getLogger("httpx").info("HTTP Request: POST /reviews 200 OK")

        assert stream.getvalue() == ""

    def test_third_party_warnings_still_get_through(self, stream) -> None:
        """Quieting libraries at INFO must not also silence them when something
        is actually wrong — the handler is on the root logger for this reason."""
        configure_logging("INFO", stream=stream)

        logging.getLogger("httpx").warning("connection pool is full")

        assert "connection pool is full" in stream.getvalue()

    def test_the_level_is_honoured(self, stream) -> None:
        configure_logging("WARNING", stream=stream)
        package = logging.getLogger("reviewhive.jobs")

        package.info("this should not appear")
        package.warning("this should")

        assert "this should not appear" not in stream.getvalue()
        assert "this should" in stream.getvalue()

    def test_debug_reaches_the_package_without_reaching_anyone_else(self, stream) -> None:
        """`log_level=DEBUG` is a request to hear more from this project, not to
        turn on every dependency's internal chatter."""
        configure_logging("DEBUG", stream=stream)

        logging.getLogger("reviewhive.api.webhook").debug("ignoring our own comment")
        logging.getLogger("httpx").debug("send_request_headers.started")

        assert "ignoring our own comment" in stream.getvalue()
        assert "send_request_headers" not in stream.getvalue()

    def test_an_exception_carries_its_traceback(self, stream) -> None:
        """`logger.exception` is used in eleven places for failures nobody
        predicted; a formatter that dropped the traceback would make each of them
        a one-line mystery."""
        configure_logging("INFO", stream=stream)

        try:
            raise ValueError("could not fetch the diff")
        except ValueError:
            logging.getLogger("reviewhive.jobs").exception("review failed")

        written = stream.getvalue()
        assert "review failed" in written
        assert "ValueError: could not fetch the diff" in written
        assert "Traceback" in written


class TestRepeatedCalls:
    def test_calling_twice_does_not_double_every_line(self, stream) -> None:
        """The entry points are not mutually exclusive — a script, a test and a
        lifespan can each call this in one process. Stacking handlers would print
        every line once per call, which reads as the service doing the work twice.
        """
        configure_logging("INFO", stream=stream)
        configure_logging("INFO", stream=stream)

        logging.getLogger("reviewhive.jobs").info("only once")

        assert stream.getvalue().count("only once") == 1

    def test_a_handler_installed_by_someone_else_is_left_alone(self, stream) -> None:
        """Only handlers this module named are replaced. uvicorn, pytest's
        caplog and anything else on the root logger keep working."""
        other = logging.StreamHandler(stream)
        other.name = "not-ours"
        logging.getLogger().addHandler(other)

        configure_logging("INFO", stream=io.StringIO())

        assert other in logging.getLogger().handlers


class TestSettingsIntegration:
    def test_the_default_level_shows_the_explanatory_lines(self) -> None:
        """A default of WARNING would ship the exact bug this fixes."""
        from reviewhive.config import Settings

        assert Settings(anthropic_api_key="test").log_level == "INFO"

    @pytest.mark.parametrize("given, expected", [("debug", "DEBUG"), (" Warning ", "WARNING")])
    def test_a_level_name_is_normalised(self, given, expected) -> None:
        from reviewhive.config import Settings

        assert Settings(anthropic_api_key="test", log_level=given).log_level == expected

    def test_an_unknown_level_is_rejected_at_load(self) -> None:
        """Rather than at the `setLevel` call inside lifespan, where the
        traceback points at the logging module instead of at the typo."""
        from pydantic import ValidationError

        from reviewhive.config import Settings

        with pytest.raises(ValidationError, match="log_level must be"):
            Settings(anthropic_api_key="test", log_level="VERBOSE")
