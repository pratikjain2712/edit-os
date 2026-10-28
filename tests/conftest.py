import pytest

from edit_os import ai_refs, biblio, iso4, refs_other, refweb


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
    monkeypatch.setattr(iso4, "table", lambda: LTWA_SAMPLE)  # never the downloaded list


# A few LTWA rows (the ISSN Centre's format), enough for the tests.
LTWA_SAMPLE = iso4.LTWA("""\ufeff"WORDS";"ABBREVIATIONS";"LANGUAGES"
"journal";"j.";"fre, eng"
"interactive";"interact.";"eng"
"internation-";"int.";"fre, eng"
"computer";"comput.";"eng"
"artificial";"artif.";"eng"
"intelligen-";"intell.";"eng"
"perspective";"perspect.";"eng"
"midwifery";"n.a.";"eng"
"public";"n.a.";"eng"
"technolog-";"technol.";"mul"
"educa-";"educ.";"eng, spa, fre"
"learn-";"learn.";"eng"
"scienc-";"sci.";"fre, eng"
"quarterly";"q.";"eng"
"nursing";"nurs.";"eng"
"teaching";"teach.";"eng"
"research-";"res.";"eng"
"health";"n.a.";"eng"
"medical";"méd.";"eng, ukr, fre"
"caring";"n.a.";"eng"
"United States";"U. S.";"eng"
"-band (book)";"-bd.";"ger"
"ergebnis-";"erg.";"ger"
""")
