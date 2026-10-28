import pytest

from edit_os import ai_refs, biblio, refs_other


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never call PubMed, CrossRef, the other bibliographic databases or Claude; tests that need them patch the functions."""
    monkeypatch.setattr(biblio._Http, "get", lambda self, url, host_gap, **kw: None)
    monkeypatch.setattr(ai_refs, "available", lambda: False)
    monkeypatch.setattr(refs_other, "_fetch_link", lambda url: None)
