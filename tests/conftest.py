import pytest

from edit_os import ai_refs, biblio


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never call PubMed, CrossRef or Claude; tests that need them patch the functions."""
    monkeypatch.setattr(biblio._Http, "get", lambda self, url, host_gap: None)
    monkeypatch.setattr(ai_refs, "available", lambda: False)
