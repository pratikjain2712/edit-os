"""Claude's fields for an entry that belong to another entry (a neighbour, or the first half of an
entry split over two paragraphs) must not find that entry's paper. Synthetic references only; every
HTTP-facing function is patched."""

import pytest

from edit_os import biblio, refengine, style

PAPER = biblio.Record(
    source="pubmed", pmid="410", doi="10.1000/syn.410",
    title="Nocturnal glare thresholds in amateur cyclists: a cross-sectional survey.",
    authors=[("Varga", "T"), ("Lindqvist", "M")], journal="J Synth Vis", year="2019", volume="31", pages="210-8",
)
TEXT = ("Varga T, Lindqvist M. Nocturnal glare thresholds in amateur cyclists: a cross-sectional survey. "
        "J Synth Vis. 2019;31:210-8.")
FIELDS = {"kind": "journal_article", "authors": ["Varga T", "Lindqvist M"],
          "title": "Nocturnal glare thresholds in amateur cyclists: a cross-sectional survey",
          "year": "2019", "doi": "", "pmid": ""}
# The next entry in the list; its paper is not in the database.
NEIGHBOUR = "Bramblewood K, Osei A. Tear film drift after prolonged screen reading. J Synth Vis. 2020;32:44-9."


@pytest.fixture
def db(monkeypatch):
    def ids(term):
        term = term.lower()
        return ["410"] if "glare" in term or PAPER.doi in term else []

    monkeypatch.setattr(biblio, "pubmed_search", lambda term, retmax=5: ids(term))
    monkeypatch.setattr(biblio, "pubmed_best_match", lambda term, retmax=10: ids(term))
    monkeypatch.setattr(biblio, "pubmed_fetch", lambda pmids: [PAPER] if "410" in pmids else [])
    monkeypatch.setattr(biblio, "crossref_search", lambda text, rows=5: [PAPER] if "glare" in text.lower() else [])
    monkeypatch.setattr(biblio, "crossref_doi", lambda doi: PAPER if doi == PAPER.doi else None)


def _resolve_all(monkeypatch, texts, fields):
    monkeypatch.setattr(refengine.ai_refs, "available", lambda: True)
    monkeypatch.setattr(refengine.ai_refs, "parse_batch", lambda t: fields)
    return refengine.resolve_all(texts, style.load("ijo"))


def test_own_fields_find_the_paper(db, monkeypatch):
    [res] = _resolve_all(monkeypatch, [TEXT], [FIELDS])
    assert res.record is PAPER and res.parsed.title == FIELDS["title"]


def test_neighbours_fields_are_not_used(db, monkeypatch):
    first, second = _resolve_all(monkeypatch, [TEXT, NEIGHBOUR], [FIELDS, FIELDS])  # shifted by one
    assert first.record is PAPER
    assert second.status == refengine.QUERY and second.record is None
    assert second.parsed.title == "Tear film drift after prolonged screen reading"  # the regex split
    assert second.parsed.first_surname == "Bramblewood" and second.parsed.year == "2020"


@pytest.mark.parametrize("fragment", ["doi: 10.1000/syn.410", "Epub 2019 Aug 2."])
def test_trailing_fragment_with_previous_entrys_fields_is_queried(db, monkeypatch, fragment):
    # The entry was split over two paragraphs; Claude gave the second half the first half's fields.
    [res] = _resolve_all(monkeypatch, [fragment], [FIELDS])
    assert res.status == refengine.QUERY and res.record is None
    assert res.parsed.title == "" and res.parsed.authors == []


def test_fields_written_with_slips_are_kept():
    text = ("Varga T,Lindqvist M. Nocturnal  glare thres- holds in amateur cyclists : a cross sectional survey. "
            "J Synth Vis 2019;31:210-8")
    p = refengine.from_fields(text, FIELDS)
    assert p.title == FIELDS["title"] and p.authors == FIELDS["authors"] and p.year == "2019"


def test_compound_and_group_authors_are_found_in_the_text():
    p = refengine.from_fields("Di Martino E, Varga T. A synthetic title about something. J Synth Vis. 2021;3:1-2.",
                              {"authors": ["Di Martino E", "Varga T"], "title": "A synthetic title about something"})
    assert p.authors == ["Di Martino E", "Varga T"]
    p = refengine.from_fields("Synthetic Vision Study Group. Ten-year follow-up of a made-up cohort. "
                              "J Synth Vis. 2022;4:5-9.", {"authors": ["Synthetic Vision Study Group"]})
    assert p.authors == ["Synthetic Vision Study Group"]


def test_year_not_in_the_text_is_dropped():
    p = refengine.from_fields(NEIGHBOUR, {"year": "2019"})
    assert p.year == "2020"
