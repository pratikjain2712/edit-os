"""Web search fallback for unverified references (edit_os/refweb.py). Claude and the web pages are mocked; every
name, title and URL here is invented."""

import pytest

from edit_os import ai_refs, biblio, provenance, queries, refengine, refweb, style

ENTRY = ("Quillfeather T, Marrowby A, Ostendahl P. Peer mentoring and exam anxiety among first-year midwifery "
         "students: a randomized trial. J Educ Health Promot. 2021;10:347.")
TITLE = "Peer mentoring and exam anxiety among first-year midwifery students: a randomized trial"
URL = "https://journal.example.org/article/347"


def page(title=TITLE, year="2021/11/30", volume="10", first="347", authors=("Quillfeather, Tamsin",
                                                                              "Marrowby, Alun R.", "Ostendahl, Pia")):
    metas = [("citation_title", title), ("citation_journal_title", "Journal of Education and Health Promotion"),
             ("citation_publication_date", year), ("citation_volume", volume), ("citation_firstpage", first)]
    metas += [("citation_author", a) for a in authors]
    head = "".join(f'<meta name="{k}" content="{v}">' for k, v in metas)
    return f"<html><head><title>{title}</title>{head}</head><body>Article</body></html>"


@pytest.fixture
def st():
    s = style.load("jehp")
    s.settings["references"]["web_search"] = {"enabled": True, "max_searches_per_ref": 2, "max_per_article": 20,
                                              "cost_cap_usd": 0.30}
    return s


@pytest.fixture
def web(monkeypatch):
    """Databases find nothing; Claude proposes `state["urls"]`; pages come from `state["pages"]`."""
    state = {"urls": [URL], "pages": {URL: {"status": 200, "final_url": URL, "html": page()}}, "calls": [],
             "fetched": []}
    monkeypatch.setattr(ai_refs, "available", lambda: True)
    monkeypatch.setattr(biblio, "pubmed_search", lambda term, retmax=5: [])
    monkeypatch.setattr(biblio, "pubmed_best_match", lambda term, retmax=10: [])
    monkeypatch.setattr(biblio, "pubmed_fetch", lambda ids: [])
    monkeypatch.setattr(biblio, "crossref_search", lambda text, rows=5: [])
    monkeypatch.setattr(biblio, "crossref_doi", lambda doi: None)

    def claude(text, n):
        state["calls"].append(n)
        return [{"url": u, "why": "title and authors"} for u in state["urls"]], 9_000, 300, min(n, 1)

    def fetch(url):
        state["fetched"].append(url)
        return state["pages"].get(url, {"status": 404, "final_url": url, "html": ""})

    monkeypatch.setattr(refweb, "_call_claude", claude)
    monkeypatch.setattr(refweb, "_fetch_page", fetch)
    return state


def resolve(text, st):
    with refweb.article(st):
        return refengine.resolve(text, st)


def test_page_verifies(web, st):
    res = resolve(ENTRY, st)
    assert res.status in (refengine.FORMAT_FIXED, refengine.MATCHED)
    assert res.record.web["source"] == "web" and res.record.web["url"] == URL
    assert set(res.record.web["verified_fields"]) >= {"title", "first author", "year", "volume", "pages"}
    # the record is the page's: its authors and title, not anything Claude wrote
    assert res.record.authors[1] == ("Marrowby", "AR") and res.record.title == TITLE
    assert res.formatted.startswith("Quillfeather T, Marrowby AR, Ostendahl P. Peer mentoring and exam anxiety")
    prov = provenance.reference(res, ENTRY)
    assert prov["kind"] == "web" and prov["label"] == "Verified on journal.example.org" and prov["url"] == URL


def test_page_title_differs_is_rejected(web, st):
    web["pages"][URL]["html"] = page(title="Peer mentoring and sleep quality among nursing students: a cohort study")
    res = resolve(ENTRY, st)
    assert res.status == refengine.QUERY and res.record is None
    assert not any(q[0] == "references.possible_source" for q in res.queries)  # not even close: no hint either


def test_fabricated_url_is_rejected(web, st):
    web["urls"] = ["https://journal.example.org/article/does-not-exist"]
    res = resolve(ENTRY, st)
    assert res.status == refengine.QUERY and res.record is None
    assert web["fetched"] == ["https://journal.example.org/article/does-not-exist"]
    assert "no web page confirmed it" in res.message


def test_challenge_page_is_not_metadata(web, st):
    web["pages"][URL] = {"status": 200, "final_url": URL,
                         "html": "<html><head><title>Just a moment...</title></head></html>"}
    assert resolve(ENTRY, st).record is None


def test_wrong_author_or_year_rejected_with_possible_source(web, st):
    web["pages"][URL]["html"] = page(year="2019/01/02")
    res = resolve(ENTRY, st)
    assert res.record is None
    assert ("references.possible_source", {"url": URL}) in res.queries
    assert queries.TEXT["references.possible_source"].format(url=URL) == f"Possible source: {URL} — kindly check."
    web["pages"][URL]["html"] = page(authors=("Brindlecombe, Hugo",))
    res = resolve(ENTRY.replace("2021", "2021 "), st)  # another text: not the cached answer
    assert res.record is None and ("references.possible_source", {"url": URL}) in res.queries


def test_volume_conflict_rejected(web, st):
    web["pages"][URL]["html"] = page(volume="12", first="12")
    assert resolve(ENTRY, st).record is None


def test_cost_cap_respected(web, st):
    st.settings["references"]["web_search"]["cost_cap_usd"] = 0.015  # one search's worst case, not two
    entries = [ENTRY.replace("347", str(n)) for n in range(300, 306)]
    with refweb.article(st) as budget:
        for e in entries:
            refengine.resolve(e, st)
    # the first call may make one search (two could pass the cap); after it, even one more could pass it
    assert web["calls"] == [1] and budget.cost_usd <= 0.015

    st.settings["references"]["web_search"]["cost_cap_usd"] = 0.001  # below one search: no call at all
    web["calls"].clear()
    with refweb.article(st):
        refengine.resolve(ENTRY.replace("347", "999"), st)
    assert web["calls"] == []


def test_article_search_limit(web, st):
    st.settings["references"]["web_search"].update(max_searches_per_ref=2, max_per_article=3)
    with refweb.article(st) as budget:
        for n in range(10):
            refengine.resolve(ENTRY.replace("347", str(400 + n)), st)
    assert budget.searches <= 3 and web["calls"][0] == 2


def test_cached_by_reference_text(web, st):
    resolve(ENTRY, st)
    resolve(ENTRY, st)
    assert len(web["calls"]) == 1


def test_off_by_default_and_outside_an_article(web):
    base = style.load("idoj")
    assert not refweb.settings(base)["enabled"]
    with refweb.article(base):
        assert refengine.resolve(ENTRY, base).record is None
    on = style.load("ijo")
    assert refweb.settings(on)["enabled"] and refweb.settings(style.load("jehp"))["enabled"]
    assert refengine.resolve(ENTRY, on).record is None  # no article context: no calls
    assert web["calls"] == []


def test_pubmed_and_doi_links_read_through_the_databases(web, st, monkeypatch):
    rec = biblio.Record(source="crossref", title=TITLE, authors=[("Quillfeather", "T"), ("Marrowby", "AR")],
                        journal="J Educ Health Promot", year="2021", volume="10", pages="347",
                        doi="10.9999/jehp.example.347")
    monkeypatch.setattr(biblio, "crossref_doi", lambda doi: rec if doi == "10.9999/jehp.example.347" else None)
    web["urls"] = ["https://doi.org/10.9999/jehp.example.347"]
    web["pages"] = {}  # the publisher page itself is not reachable: the DOI in the link is enough
    res = resolve(ENTRY, st)
    assert res.record is not None and res.record.web["via"] == "crossref" and res.record.source == "crossref"

    pm = biblio.Record(source="pubmed", pmid="40000001", title=TITLE, authors=[("Quillfeather", "T")],
                       journal="J Educ Health Promot", year="2021", volume="10", pages="347")
    monkeypatch.setattr(biblio, "pubmed_fetch", lambda ids: [pm] if ids == ["40000001"] else [])
    web["urls"] = ["https://pubmed.ncbi.nlm.nih.gov/40000001/"]
    web["fetched"].clear()
    res = resolve(ENTRY.replace("Promot.", "Promot"), st)
    assert res.record.web["via"] == "pubmed" and res.record.pmid == "40000001"
    assert web["fetched"] == []  # PubMed's page is never scraped


def test_page_metadata_formats():
    dc = ('<html><head><meta name="DC.title" content="A study of things"><meta name="DC.creator" content="Doe, Jane">'
          '<meta name="DC.date" content="2020-05-01"><meta name="DC.identifier" content="doi:10.1234/abc.5">'
          '<meta name="prism.volume" content="7"><meta name="prism.startingPage" content="11"></head></html>')
    r = refweb.page_metadata(dc)[0]
    assert (r.title, r.authors, r.year, r.volume, r.pages, r.doi) == ("A study of things", [("Doe", "J")], "2020",
                                                                      "7", "11", "10.1234/abc.5")
    ld = ('<html><head><script type="application/ld+json">{"@context": "https://schema.org", "@graph": [{"@type": '
          '"ScholarlyArticle", "headline": "Another study", "author": [{"@type": "Person", "familyName": "Roe", '
          '"givenName": "Ann Beth"}], "datePublished": "2019-02-03", "pageStart": "5", "pageEnd": "9", "isPartOf": '
          '{"@type": "PublicationIssue", "issueNumber": "2", "isPartOf": {"@type": "PublicationVolume", '
          '"volumeNumber": "4", "isPartOf": {"@type": "Periodical", "name": "Journal of Examples"}}}}]}'
          '</script></head></html>')
    r = refweb.page_metadata(ld)[0]
    assert (r.title, r.authors, r.year, r.volume, r.issue, r.pages) == ("Another study", [("Roe", "AB")], "2019",
                                                                        "4", "2", "5-9")
    assert refweb.page_metadata("") == []
    assert refweb._name("Elizabeth A. van der Berg") == ("van der Berg", "EA")
    assert refweb._name("Kinchen EA") == ("Kinchen", "EA")


def test_page_quirks():
    html = ('<html><head><meta name="citation_title" content="A review of jasmine">'
            '<meta name="citation_author" content="P. R. Bharathi">'
            '<meta name="citation_author" content="S. K. Sripathi  and A. N. Lakshmi">'
            '<meta name="citation_volume" content="Volume 11 (2020)"><meta name="citation_publication_date" content="2020/05/01">'
            '<script type="application/ld+json">{"@type": "ScholarlyArticle", "name": "A review of jasmine", '
            '"datePublished": "2020", "pageStart": "1994", "pageEnd": "2010"}</script></head></html>')
    r = refweb.page_metadata(html)[0]
    assert r.authors == [("Bharathi", "PR"), ("Sripathi", "SK"), ("Lakshmi", "AN")]
    assert r.volume == "11" and r.pages == "1994-2010"  # the pages from the same article's JSON-LD


def test_page_with_fewer_authors_than_the_entry_rejected(web, st):
    web["pages"][URL]["html"] = page(authors=("Quillfeather, Tamsin",))
    res = resolve(ENTRY, st)
    assert res.record is None and ("references.possible_source", {"url": URL}) in res.queries


def test_entry_year_kept_when_the_page_gives_it(web, st):
    html = page(year="2020/12/01").replace("</head>", '<meta name="citation_date" content="2021"></head>')
    web["pages"][URL]["html"] = html
    res = resolve(ENTRY, st)
    assert res.record.year == "2021" and "2021;10:347" in res.formatted
