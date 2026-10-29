"""Update check against a fake GitHub API (the tests never use the network)."""

import io
import json
import urllib.error

import pytest

from p6mcp import updates


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_for(payload):
    def opener(req, timeout):
        assert req.full_url == updates.LATEST_API and timeout == updates.TIMEOUT
        return Resp(json.dumps(payload).encode())
    return opener


@pytest.mark.parametrize("a,b", [("1.0.0", "1.0.1"), ("1.0.0", "v1.1.0"), ("1.0.0rc1", "1.0.0"),
                                 ("1.0.0.dev1", "1.0.0rc1"), ("1.0.0-rc.1", "1.0.0-rc.2"), ("1.9.9", "2.0.0")])
def test_version_order(a, b):
    assert updates.parse(a) < updates.parse(b)


def test_newer_release():
    r = updates.check("1.0.0", opener_for({"tag_name": "v1.2.0", "html_url": "https://example.test/r/1.2.0"}))
    assert r["newer"] and r["latest"] == "1.2.0" and r["url"].endswith("1.2.0") and r["error"] is None


def test_same_or_older_release():
    assert not updates.check("1.2.0", opener_for({"tag_name": "v1.2.0"}))["newer"]
    assert not updates.check("1.3.0", opener_for({"tag_name": "v1.2.0"}))["newer"]


def test_falls_back_to_the_old_repository_name():
    """2.0 ships before the repository is renamed: the new name answers 404 until then."""
    seen = []

    def opener(req, timeout):
        seen.append(req.full_url)
        if req.full_url == updates.LATEST_API:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        return Resp(json.dumps({"tag_name": "v2.0.1"}).encode())
    r = updates.check("2.0.0", opener)
    assert r["newer"] and r["error"] is None
    assert seen == [updates.LATEST_API, "https://api.github.com/repos/nizarhamza/p6-mcp/releases/latest"]

    def rate_limited(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "rate limit", {}, None)
    assert "403" in updates.check("2.0.0", rate_limited)["error"]  # only a 404 tries the old name


def test_failures_are_reported_not_raised():
    def offline(req, timeout):
        raise urllib.error.URLError("no network")
    r = updates.check("1.0.0", offline)
    assert not r["newer"] and "no network" in r["error"]
    assert updates.check("1.0.0", opener_for({"message": "Not Found"}))["newer"] is False
