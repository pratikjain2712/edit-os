import os
import pickle

import pytest
import yaml

from edit_os import ai_refs, biblio, iso4, refs_other, refweb, style


class _CachedYaml:
    """`yaml` as edit_os.style sees it in tests. A file is parsed once per test process (keyed by path, mtime and
    size, so an edited or tmp file is parsed afresh) and every caller gets its own copy (a pickle round trip: a deep
    copy, several times faster than copy.deepcopy), so a test that changes `st.settings` never leaks into another.
    Parsing the style files (pure-Python YAML, ~0.1 s per style.load) was most of the suite's run time."""

    def __init__(self):
        self._parsed = {}

    def __getattr__(self, name):
        return getattr(yaml, name)

    def load(self, stream, Loader=None):  # edit_os.style._safe_load (libyaml's CSafeLoader): the same cache
        return self.safe_load(stream)

    def safe_load(self, stream):
        name = getattr(stream, "name", None)
        if not isinstance(name, str) or not os.path.isfile(name):
            return yaml.safe_load(stream)
        st = os.stat(name)
        key = (os.path.realpath(name), st.st_mtime_ns, st.st_size)
        if key not in self._parsed:
            loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
            self._parsed[key] = pickle.dumps(yaml.load(stream, Loader=loader), pickle.HIGHEST_PROTOCOL)
        return pickle.loads(self._parsed[key])


style.yaml = _CachedYaml()


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    """Tests never call PubMed, CrossRef, the other bibliographic databases, Claude or web pages; tests that need them
    patch the functions."""
    monkeypatch.setattr(refweb, "_call_claude", lambda text, n: ([], 0, 0, 0))
    monkeypatch.setattr(refweb, "_fetch_page", lambda url: None)
    monkeypatch.setattr(refweb, "STORE_PATH", str(tmp_path / "web_cache.sqlite"))
    monkeypatch.setattr(biblio._Http, "get", lambda self, url, host_gap, **kw: None)
    # Never the user's real HTTP cache (~/.cache/edit-os/http_cache.sqlite), which parallel workers would share.
    monkeypatch.setattr(biblio, "_http", None)
    monkeypatch.setattr(biblio._Http.__init__, "__defaults__", (str(tmp_path / "http_cache.sqlite"),))
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
