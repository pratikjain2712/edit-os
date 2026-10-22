"""APA-style entries (JEHP authors: "Author, A. A., & Author, B. B. (Year). Title. Journal, vol(issue), pages.")
and records the author shortened or CrossRef split: parsed, matched only on first author + title + year
(+ journal, volume), and formatted in the journal's Vancouver style. Synthetic references; HTTP is patched."""

import json

import pytest

from edit_os import biblio, journals, refengine, refs_other, style


def rec(title, authors=(("Okoro", "A"), ("Lindqvist", "B")), journal="Journal of Midwifery Learning", year="2020",
        volume="12", pages="40-56", source="crossref", **kw):
    return biblio.Record(source=source, title=title, authors=list(authors), journal=journal, year=year,
                         volume=volume, pages=pages, doi=kw.pop("doi", "10.9999/jml.1"), **kw)


APA = ("Okoro, A., Lindqvist, B. B., & Haddad, C. (2020). Virtual reality in midwifery training. "
       "Journal of Midwifery Learning, 12(3), 40–56. https://doi.org/10.9999/abc.123")


@pytest.fixture
def db(monkeypatch):
    """CrossRef's search returns `records`; PubMed finds nothing; no NLM directory."""
    records = []
    monkeypatch.setattr(biblio, "pubmed_search", lambda term, retmax=5: [])
    monkeypatch.setattr(biblio, "pubmed_best_match", lambda term, retmax=10: [])
    monkeypatch.setattr(biblio, "pubmed_fetch", lambda ids: [])
    monkeypatch.setattr(biblio, "crossref_search", lambda text, rows=5: list(records))
    monkeypatch.setattr(biblio, "crossref_doi", lambda doi: None)
    monkeypatch.setattr(journals, "directory", lambda: None)
    return records


def test_apa_entry_is_parsed():
    p = refengine.parse(APA)
    assert p.authors == ["Okoro A", "Lindqvist BB", "Haddad C"]
    assert p.title == "Virtual reality in midwifery training" and p.year == "2020"
    assert p.doi == "10.9999/abc.123"
    assert refengine.citation(APA) == ("2020", "12", "3", "40–56")
    assert refengine.looks_like_journal_article(APA.split(" https")[0])


def test_endnote_numbered_citation():
    text = "Okoro, A., et al., A title here. Journal of Research, 2020. 10(1): p. 59-66."
    assert refengine.citation(text) == ("2020", "10", "1", "59-66")


def test_apa_entry_without_subtitle_matches_the_record(db):
    db.append(rec("Virtual reality in midwifery training: A review of outcomes"))
    st = style.load("jehp")
    res = refengine.resolve(APA.split(" https")[0], st)
    assert res.status == refengine.FORMAT_FIXED
    assert res.formatted == ("Okoro A, Lindqvist BB, Haddad C. Virtual reality in midwifery training: A review of outcomes. "
                             "Journal of Midwifery Learning 2020;12:40-56.")


@pytest.mark.parametrize("record", [
    rec("Virtual reality in midwifery training: A review of outcomes", authors=[("Tanaka", "M")]),  # other author
    rec("Virtual reality in midwifery training: A review of outcomes", year="2018"),                 # other year
    rec("Virtual reality in midwifery training: A review of outcomes", volume="14"),                 # other volume
    rec("Virtual reality in midwifery training: A review of outcomes", journal="Nursing Times"),     # other journal
    rec("Virtual reality in midwifery training programmes for rural hospitals and clinics", pages="60-71"),  # no break
])
def test_a_longer_title_needs_author_year_journal_and_volume(db, record):
    db.append(record)
    res = refengine.resolve(APA.split(" https")[0], style.load("jehp"))
    assert res.status == refengine.QUERY and res.record is None


def test_same_volume_and_pages_allow_a_word_changed(db):
    text = ("Okoro, A., & Lindqvist, B. (2020). A trust scheme for environmental health monitoring in schools. "
            "Journal of Midwifery Learning, 12, 40–56.")
    db.append(rec("A trust scheme for environmental health/accessibility monitoring in schools"))
    assert refengine.resolve(text, style.load("jehp")).status == refengine.FORMAT_FIXED
    db[0] = rec("A trust scheme for environmental health/accessibility monitoring in schools", pages="60-71")
    assert refengine.resolve(text, style.load("jehp")).status == refengine.QUERY
    # same citation, but a word of the entry's title is not in the record's: another paper
    db[0] = rec("A trust scheme for environmental health/accessibility monitoring in homes")
    assert refengine.resolve(text, style.load("jehp")).status == refengine.QUERY


def test_dataset_of_another_year_loses_to_the_article(db):
    db.append(rec("Virtual reality in midwifery training", year="2011", other_years=["2020"], volume="", pages=""))
    db.append(rec("Virtual reality in midwifery training"))
    res = refengine.resolve(APA.split(" https")[0], style.load("jehp"))
    assert res.status == refengine.FORMAT_FIXED and res.record.year == "2020"


def test_online_year_counts_for_a_doi_match(monkeypatch):
    p = refengine.parse(APA)
    r = rec("Virtual reality in midwifery training", year="2022", other_years=["2020"])
    assert refengine.accept(p, r, by_id=True)
    assert not refengine.accept(p, rec("Virtual reality in midwifery training", year="2022"), by_id=True)


def test_capitals_surname_from_crossref_written_as_the_author_does():
    p = refengine.parse("VanLehn, K., & Bloom, B. S. (2011). A title. Journal, 1, 1-2.")
    r = rec("A title", authors=[("VanLEHN", "K"), ("BLOOM", "BS"), ("SMITH", "J"), ("WHO", "")])
    assert refengine._usable_authors(r, p) == ["VanLehn K", "Bloom BS", "Smith J", "WHO"]


def test_non_indexed_journal_full_title(monkeypatch):
    class Empty:
        def lookup(self, name):
            return None
    monkeypatch.setattr(journals, "directory", lambda: Empty())
    r = rec("A title", journal="ITSE", journal_full="Interactive Technology and Smart Education")
    assert refengine.journal_name(r, style.load("jehp")) == "Interactive Technology and Smart Education"


def test_crossref_subtitle_joined():
    item = {"title": ["Effectiveness of intelligent tutoring systems"], "subtitle": ["A meta-analytic review"],
            "container-title": ["Review of Educational Research"], "short-container-title": ["Rev Educ Res"]}
    r = biblio._crossref_record(item)
    assert r.title == "Effectiveness of intelligent tutoring systems: A meta-analytic review"
    assert r.journal == "Rev Educ Res" and r.journal_full == "Review of Educational Research"


# --- books: Open Library finds the main title only ------------------------------------------------

@pytest.fixture
def ol(monkeypatch, tmp_path):
    monkeypatch.setattr(biblio, "_http", biblio._Http(str(tmp_path / "cache.sqlite")))
    bodies = {}

    def get(self, url, host_gap):
        if "openlibrary.org" in url:
            return bodies.get("ol:" + ("main" if "title=Patterns+of+care&" in url else "full"))
        return None
    monkeypatch.setattr(biblio._Http, "get", get)
    return bodies


def test_book_found_by_main_title_with_publisher(ol):
    ol["ol:main"] = json.dumps({"docs": [{"title": "Patterns of care", "author_name": ["Ada Quill"],
                                          "publish_year": [1997], "publisher": ["Harbour House"]}]})
    text = "Quill, A. (1997). Patterns of care: A field guide. Harbour House."
    assert refs_other.verify_book("Patterns of care: A field guide", ["Quill"], "1997", "", text).verified
    # another subtitle and another publisher: not this book
    other = "Quill, A. (1997). Patterns of care: Cooking for one. Penguin."
    assert not refs_other.verify_book("Patterns of care: Cooking for one", ["Quill"], "1997", "", other).verified
    # the year must agree
    assert not refs_other.verify_book("Patterns of care: A field guide", ["Quill"], "2005", "", text).verified


def test_conference_paper_verified_in_crossref(monkeypatch, tmp_path):
    monkeypatch.setattr(biblio, "_http", biblio._Http(str(tmp_path / "cache.sqlite")))
    item = {"type": "proceedings-article", "title": ["Gradient trees for ward staffing"],
            "author": [{"family": "Okoro", "given": "Ada"}], "issued": {"date-parts": [[2016, 8]]},
            "container-title": ["Proceedings of the 22nd Conference on Hospital Data"]}
    monkeypatch.setattr(biblio._Http, "get", lambda self, url, gap: json.dumps({"message": {"items": [item]}}))
    assert refs_other.verify_paper("Gradient trees for ward staffing", ["Okoro"], "2016")
    assert not refs_other.verify_paper("Gradient trees for ward staffing", ["Tanaka"], "2016")
    assert not refs_other.verify_paper("Gradient trees for ward staffing", ["Okoro"], "2019")
    text = ("Okoro A. Gradient trees for ward staffing. In: Proceedings of the 22nd Conference on Hospital Data. "
            "New York: ACM; 2016. p. 785-94.")
    res = refengine.resolve(text, style.load("jehp"), {"kind": "conference", "title": "Gradient trees for ward staffing",
                                                         "authors": ["Okoro A"], "year": "2016"})
    assert res.status == refs_other.OTHER_VERIFIED and "references.unverified" not in [q for q, _ in res.queries]
