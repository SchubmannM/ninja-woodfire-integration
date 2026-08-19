"""Tests for scripts/deploy.py.

Not integration code, but load-bearing enough to regress noticeably: the
restart call already failed once by treating an expected non-reply as fatal.
"""
from __future__ import annotations

import importlib.util
import io
import pathlib
import sys
import urllib.error

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("nwf_deploy", ROOT / "scripts" / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["nwf_deploy"] = module
    spec.loader.exec_module(module)
    return module


deploy = _load()


def _ha(error: Exception | None):
    ha = deploy.HA("https://example.invalid", "token", dry_run=False)

    def fake_request(*_a, **_k):
        if error is not None:
            raise error
        return {}

    ha._request = fake_request
    return ha


def _restart(ha) -> None:
    """Run restart() with stdout swallowed."""
    buf, real = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        ha.restart()
    finally:
        sys.stdout = real


@pytest.mark.parametrize(
    "error",
    [
        None,
        urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None),
        urllib.error.HTTPError("u", 504, "Gateway Timeout", {}, None),
        urllib.error.HTTPError("u", 500, "Internal Server Error", {}, None),
        urllib.error.URLError("connection reset by peer"),
        OSError("broken pipe"),
        TimeoutError("timed out"),
    ],
)
def test_restart_tolerates_a_missing_reply(error) -> None:
    """A restarting HA usually never answers, and that is success.

    It tears down its HTTP server as part of restarting: directly you get a
    dropped connection, behind a reverse proxy a 502 or 504. Treating that as
    an error aborts the deploy before it ever polls for HA coming back — which
    is exactly what happened the first time this ran for real.
    """
    _restart(_ha(error))  # must not raise


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("u", 401, "Unauthorized", {}, None),
        urllib.error.HTTPError("u", 403, "Forbidden", {}, None),
        urllib.error.HTTPError("u", 404, "Not Found", {}, None),
    ],
)
def test_restart_still_raises_on_client_errors(error) -> None:
    """A 4xx is a bad token or a bad URL. Waiting will not fix it."""
    with pytest.raises(urllib.error.HTTPError):
        _restart(_ha(error))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("fffd2d4", True),
        ("19548e7a1b2c3d4e5f60718293a4b5c6d7e8f901", True),
        ("0.5.0", False),
        ("v0.5.0", False),
        ("", False),
        (None, False),
        ("main", False),
        ("2026.3.1", False),
    ],
)
def test_commit_vs_version_detection(value, expected) -> None:
    """HACS reports a commit when branch-tracking, a version when not.

    Distinguishing them is how the script knows a freshly published release
    will not be seen, rather than reporting a bare "no update pending".
    """
    assert deploy.looks_like_commit(value) is expected
