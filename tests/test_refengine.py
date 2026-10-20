import pytest
from docx import Document

from edit_os import biblio, docmodel, refengine, rules, style, tracked
from edit_os.changes import Status

SHIELDS = biblio.Record(
    source="pubmed", pmid="8694700", doi="10.1001/archopht.1996.01100140164007",
    title="Diffuse choroidal melanoma. Clinical features predictive of metastasis.",
    authors=[("Shields", "CL"), ("Shields", "JA"), ("De Potter", "P"), ("Cater", "J"), ("Tardio", "D"),
             ("Barrett", "J"), ("Smith", "A")],
    journal="Arch Ophthalmol", year="1996", volume="114", issue="8", pages="956-63",
)
OTHER = biblio.Record(source="pubmed", pmid="1", title="Something else entirely about cataract surgery outcomes",
                      authors=[("Shields", "CL")], journal="Ophthalmology", year="1996", volume="1", pages="1-2")


@pytest.fixture
def fake_pubmed(monkeypatch):
    calls = []

    def search(term, retmax=5):
        calls.append(term)
        return ["1", "8694700"] if "choroidal" in term.lower() else []

    def fetch(ids):
        return [r for r in (OTHER, SHIELDS) if r.pmid in ids]

    monkeypatch.setattr(biblio, "pubmed_search", search)
    monkeypatch.setattr(biblio, "pubmed_fetch", fetch)
    monkeypatch.setattr(biblio, "crossref_search", lambda text, rows=5: [])
    monkeypatch.setattr(biblio, "crossref_doi", lambda doi: None)
    return calls


def test_parse():
    p = refengine.parse("Shields CL, Shields JA, De Potter P. Diffuse choroidal melanoma. Clinical features predictive "
                        "of metastasis. Arch Ophthalmol. 1996;114(8):956-63. doi: 10.1001/archopht.1996.01100140164007.")
    assert p.authors == ["Shields CL", "Shields JA", "De Potter P"]
    assert p.first_surname == "Shields" and p.year == "1996"
    assert p.doi == "10.1001/archopht.1996.01100140164007"


def test_match_and_format(fake_pubmed):
    text = ("Shields CL, Shields JA, De Potter P, Cater J, Tardio D, Barrett J. Diffuse choroidal melanoma. "
            "Clinical features predictive of metastasis. Arch Ophthalmol. 1996;114(8):95-63.")
    res = refengine.resolve(text, style.load("ijo"))
    assert res.status == refengine.FORMAT_FIXED and res.record.pmid == "8694700"
    assert res.formatted == ("Shields CL, Shields JA, De Potter P, Cater J, Tardio D, Barrett J, et al. Diffuse choroidal "
                             "melanoma. Clinical features predictive of metastasis. Arch Ophthalmol 1996;114:956-63.")


def test_wrong_paper_becomes_query(fake_pubmed):
    res = refengine.resolve("Shields CL. Uveal melanoma in children. Ophthalmology. 1996;1:1-2.", style.load("ijo"))
    assert res.status == refengine.QUERY  # OTHER shares author and year but not the title: never swapped in


def test_doi_pointing_elsewhere_is_queried(fake_pubmed, monkeypatch):
    monkeypatch.setattr(biblio, "pubmed_search", lambda term, retmax=5: ["1"] if "[aid]" in term else [])
    res = refengine.resolve("Rao S. Timing of surgery in children with cataract. J Pediatr. 2019;5:1-2. "
                            "doi:10.1000/xyz", style.load("ijo"))
    assert res.status == refengine.QUERY and "points to a different article" in res.message


def test_books_are_left_alone(fake_pubmed):
    res = refengine.resolve("Ringsven MK, Bond D. Gerontology and Leadership Skills for Nurses. 2nd ed. Albany, NY: "
                            "Delmar Publishers; 1996.", style.load("ijo"))
    assert res.status == refengine.NOT_JOURNAL and not fake_pubmed


def test_lookup_rule_writes_word_level_changes(fake_pubmed, tmp_path):
    doc = Document()
    doc.add_paragraph("References")
    doc.add_paragraph("1. Shields CL, Shields JA, De Potter P, Cater J, Tardio D, Barrett J. Diffuse choroidal melanoma. "
                      "Clinical features predictive of metastasis. Arch Ophthalmol. 1996;114(8):95-63.")
    path = tmp_path / "m.docx"
    doc.save(path)
    cs = rules.run_all(docmodel.load(str(path)), style.load("ijo"))
    entry = [c for c in cs.changes if c.para_id == "p0002"]
    assert entry and all(c.reason == "ref_lookup" for c in entry)  # small reference rules step aside
    assert all(len(c.old) < 20 for c in entry)  # small edits, not one whole-entry replacement
    for c in cs.changes:
        c.status = Status.ACCEPTED
    out = tmp_path / "o.docx"
    tracked.render(docmodel.load(str(path)), cs, tracked=False).save(out)
    assert docmodel.load(str(out)).paragraphs[1].text.endswith("et al. Diffuse choroidal melanoma. Clinical features "
                                                                "predictive of metastasis. Arch Ophthalmol 1996;114:956-63.")


def test_claude_fields_drive_the_search(fake_pubmed, monkeypatch):
    # The regex parser would cut this title at "melanoma."; Claude's split keeps it whole.
    text = ("Shields CL , Shields JA,De Potter P, Cater J, Tardio D, Barrett J. Diffuse choroidal melanoma. Clinical "
            "features predictive of metastasis Arch Ophthalmol 1996 114 956")
    fields = {"kind": "journal_article", "authors": ["Shields CL", "Shields JA", "De Potter P"],
              "title": "Diffuse choroidal melanoma. Clinical features predictive of metastasis", "journal": "Arch Ophthalmol",
              "year": "1996", "volume": "114", "issue": "", "pages": "956", "doi": "10.9999/made.up", "pmid": ""}
    monkeypatch.setattr(refengine.ai_refs, "available", lambda: True)
    monkeypatch.setattr(refengine.ai_refs, "parse_batch", lambda texts: [fields])
    [res] = refengine.resolve_all([text], style.load("ijo"))
    assert res.status == refengine.FORMAT_FIXED and res.record.pmid == "8694700"
    assert res.parsed.doi == ""  # a DOI that is not in the text is ignored, not looked up
    assert res.formatted.endswith("Arch Ophthalmol 1996;114:956-63.")


def test_claude_kind_marks_books(fake_pubmed, monkeypatch):
    monkeypatch.setattr(refengine.ai_refs, "available", lambda: True)
    monkeypatch.setattr(refengine.ai_refs, "parse_batch", lambda texts: [{"kind": "book", "title": "Gerontology"}])
    [res] = refengine.resolve_all(["Ringsven MK. Gerontology. Delmar; 1996;3:4-5."], style.load("ijo"))
    assert res.status == refengine.NOT_JOURNAL and not fake_pubmed
