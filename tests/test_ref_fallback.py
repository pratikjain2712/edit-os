"""Fallback searches (biblio.fallback_candidates) behind refengine.find. Synthetic references only;
every HTTP-facing function is patched."""

import pytest

from edit_os import biblio, refengine, style

# A full-title PubMed search misses this one: the author wrote "every day", PubMed has "everyday".
PAPER = biblio.Record(
    source="pubmed", pmid="111", doi="10.1000/demo.111",
    title="Grading of acne lesions: a simple system for everyday dermatological practice.",
    authors=[("Larsen", "B"), ("Okafor", "C")], journal="Dermatol Pract", year="2004", volume="12", pages="136-41",
)
LOOKALIKE = biblio.Record(  # same title words and journal, different paper
    source="pubmed", pmid="222", title="Grading of acne lesions in adolescents: a cohort study.",
    authors=[("Moreau", "P")], journal="Dermatol Pract", year="2004", volume="12", pages="200-5",
)
TEXT = ("Larsen B, Okafor C. Grading of acne lesions: a simple system for every day dermatological practice. "
        "Dermatol Pract. 2004;12:136-41.")


@pytest.fixture
def db(monkeypatch):
    calls = []
    records = {r.pmid: r for r in (PAPER, LOOKALIKE)}

    def search(term, retmax=5):
        calls.append(("search", term))
        if "[aid]" in term:
            return [p for p, r in records.items() if r.doi and r.doi in term]
        return []  # the plain full-title searches find nothing

    def best_match(term, retmax=10):
        calls.append(("best_match", term))
        return ["222", "111"] if "grading" in term and "lesions" in term else []

    def fetch(ids):
        calls.append(("fetch", tuple(ids)))
        return [records[i] for i in ids if i in records]

    def crossref(text, rows=5):
        calls.append(("crossref", text))
        return []

    monkeypatch.setattr(biblio, "pubmed_search", search)
    monkeypatch.setattr(biblio, "pubmed_best_match", best_match)
    monkeypatch.setattr(biblio, "pubmed_fetch", fetch)
    monkeypatch.setattr(biblio, "crossref_search", crossref)
    monkeypatch.setattr(biblio, "crossref_doi", lambda doi: calls.append(("crossref_doi", doi)))
    return calls


def test_title_words_keep_distinctive_words():
    words = biblio.title_words("The colour of COVID-19 care: a randomised trial of IL-6 in children's wards")
    assert words == ["care", "trial", "wards"]  # stopwords, variants, acronyms, digits, hyphens, possessives out


def test_first_author_surname():
    assert biblio.first_author_surname("Di Martino E") == "Di Martino"
    assert biblio.first_author_surname("Shields CL") == "Shields"
    assert biblio.first_author_surname("Rahayu, W. R.") == "Rahayu"
    assert biblio.first_author_surname("M") == ""
    assert biblio.first_author_surname("GBD 2019 Collaborators") == ""


def test_partial_title_query_is_held_to_author_year_journal():
    q = biblio.partial_title_query("Grading of acne lesions: a simple system for everyday dermatological practice",
                                   "Larsen", "2004", "Dermatol Pract")
    clauses = q.split(" OR ")
    assert len(clauses) == 3
    assert '"Larsen"[1au]' in clauses[0] and "2004[dp]" not in clauses[0]
    assert '"Larsen"[1au]' in clauses[1] and "2004[dp]" in clauses[1]
    assert '"Dermatol Pract"[ta]' in clauses[2] and "2004[dp]" in clauses[2]
    assert biblio.partial_title_query("Acne", "Larsen", "2004") == ""  # too few words to search on


def test_journal_hint():
    assert biblio.journal_hint(TEXT, "Grading of acne lesions: a simple system for every day dermatological "
                                     "practice") == "Dermatol Pract"


def test_partial_title_rescues_typo_and_keeps_strict_acceptance(db):
    res = refengine.resolve(TEXT, style.load("ijo"))
    assert res.status == refengine.FORMAT_FIXED and res.record.pmid == "111"  # not the look-alike listed first
    assert res.formatted.endswith("Dermatol Pract 2004;12:136-41.")


def test_lookalike_alone_is_never_accepted(db, monkeypatch):
    monkeypatch.setattr(biblio, "pubmed_fetch", lambda ids: [LOOKALIKE] if "222" in ids else [])
    res = refengine.resolve(TEXT, style.load("ijo"))
    assert res.status == refengine.QUERY and res.record is None


def test_record_without_volume_is_completed_by_doi(db, monkeypatch):
    deposit = biblio.Record(source="crossref", title=PAPER.title, authors=PAPER.authors, journal="DPract",
                            year="2004", doi=PAPER.doi)
    monkeypatch.setattr(biblio, "crossref_search", lambda text, rows=5: [deposit])
    monkeypatch.setattr(biblio, "pubmed_best_match", lambda term, retmax=10: [])
    res = refengine.resolve(TEXT, style.load("ijo"))
    assert res.status == refengine.FORMAT_FIXED and res.record.pmid == "111" and res.record.volume == "12"


def test_no_fallback_when_plain_search_succeeds(db, monkeypatch):
    monkeypatch.setattr(biblio, "pubmed_search", lambda term, retmax=5: ["111"] if "[au]" in term else [])
    res = refengine.resolve(TEXT, style.load("ijo"))
    assert res.record.pmid == "111" and not any(c[0] in ("best_match", "crossref_doi") for c in db)


def test_fallback_request_budget(db):
    known = biblio.Record(source="crossref", title="x", doi="10.1000/none")
    list(biblio.fallback_candidates("Grading of acne lesions: a simple system for everyday practice",
                                    "Larsen B", "2004", TEXT, known))
    assert len(db) <= biblio.MAX_FALLBACK_REQUESTS
    db.clear()
    list(biblio.fallback_candidates("Grading of acne lesions: a simple system for everyday practice",
                                    "Larsen B", "2004", TEXT))
    assert [c[0] for c in db] == ["best_match", "fetch", "crossref"]
    assert db[2][1] == "Grading of acne lesions: a simple system for everyday practice Larsen 2004"


def test_shorter_title_by_another_author_is_not_taken(db, monkeypatch):
    # "Grading of acne lesions: a simple system" is contained in the entry, same year, but the first author
    # differs: neither `accept` nor a fallback search takes it.
    short = biblio.Record(source="pubmed", pmid="333", title="Grading of acne lesions: a simple system.", authors=[("Moreau", "P")],
                          journal="Skin", year="2004", volume="3", pages="1-2")
    monkeypatch.setattr(biblio, "pubmed_fetch", lambda ids: [short] if ids else [])
    p = refengine.parse(TEXT)
    assert not refengine.accept(p, short)
    assert refengine.find(p)[0] is None


def test_fields_from_another_entry_do_not_confirm_a_record(db, monkeypatch):
    # Claude's split handed this entry the fields of a neighbouring one: the record matches those
    # fields, but its title is not in the entry, so the fallback does not take it.
    text = "Okafor C, et al. Endoscopic findings in adults with dyspepsia. Gut Dig. 2012;4(3):146-50."
    fields = {"kind": "journal_article", "authors": ["Larsen B", "Okafor C"], "year": "2004",
              "title": "Grading of acne lesions: a simple system for everyday dermatological practice"}
    res = refengine.resolve(text, style.load("ijo"), fields)
    assert res.status == refengine.QUERY and res.record is None
