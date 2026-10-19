import uuid

import pytest

from p6mcp import credentials


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("P6MCP_SECRET_SERVICE", f"P6MCP-test-{uuid.uuid4().hex[:8]}")
    try:
        credentials.get("probe")
    except credentials.SecretStoreError as e:
        pytest.skip(f"no usable credential store here: {e}")
    yield credentials
    for name in ("x", credentials.HTTP_TOKEN, credentials.db_key("demo")):
        try:
            credentials.delete(name)
        except credentials.SecretStoreError:
            pass


def test_set_get_delete(store):
    assert store.get("x") is None
    store.set("x", "value-1")
    assert store.get("x") == "value-1"
    assert store.delete("x") is True
    assert store.get("x") is None
    assert store.delete("x") is False


def test_http_token_created_once_and_regenerated(store):
    t1 = store.http_token()
    assert len(t1) >= 40 and store.http_token() == t1
    t2 = store.regenerate_http_token()
    assert t2 != t1 and store.http_token() == t2


def test_database_password_key_and_empty_refused(store):
    store.set(store.db_key("demo"), "pw")
    assert store.get("db:demo") == "pw"
    with pytest.raises(ValueError):
        store.set("x", "")


def test_mask():
    assert credentials.mask("abcdef123456").endswith("3456") and "abcdef" not in credentials.mask("abcdef123456")
    assert credentials.mask(None) == ""
