import pytest

from edit_os import biblio


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never call PubMed / CrossRef; tests that need records patch biblio's functions."""
    monkeypatch.setattr(biblio._Http, "get", lambda self, url, host_gap: None)
