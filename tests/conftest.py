import pytest

from edit_os import ai_refs, biblio, refs_other, refweb


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    """Tests never call PubMed, CrossRef, the other bibliographic databases, Claude or web pages; tests that need them
    patch the functions."""
    monkeypatch.setattr(refweb, "_call_claude", lambda text, n: ([], 0, 0, 0))
    monkeypatch.setattr(refweb, "_fetch_page", lambda url: None)
    monkeypatch.setattr(refweb, "STORE_PATH", str(tmp_path / "web_cache.sqlite"))
    monkeypatch.setattr(biblio._Http, "get", lambda self, url, host_gap, **kw: None)
    monkeypatch.setattr(ai_refs, "available", lambda: False)
    monkeypatch.setattr(refs_other, "_fetch_link", lambda url: None)
