"""Where each suggestion comes from (`provenance` on Change / insert records) and the stylesheet line behind
each rule (edit_os/rule_sources.py). All text is synthetic; the stylesheet and guide are made up here."""

import json

import pytest
from docx import Document

from edit_os import biblio, docmodel, frontmatter as fm, pipeline, provenance, refengine, rule_sources, rules, style
from edit_os.changes import Change, ChangeSet, Layer, Source

from test_back_matter import BODY, FIRST_PAGE, REFS, load as bm_load, make as bm_make
from test_frontmatter import PORTAL_XML, TITLE_PAGE, claude_fields


def make(tmp_path, paras, name="m.docx"):
    doc = Document()
    for t in paras:
        doc.add_paragraph(t)
    path = tmp_path / name
    doc.save(path)
    return str(path)


@pytest.fixture
def ijo():
    st = style.load("ijo")
    st.settings["references"]["lookup"] = False
    st.settings["sections"]["keywords"]["add_missing"] = False
    return st


# --- records -----------------------------------------------------------------------------------

def test_old_records_load_without_provenance(tmp_path):
    old = {"id": "r1", "para_id": "p0001", "start": 0, "end": 1, "old": "a", "new": "b", "layer": "house_style",
           "source": "rule", "reason": "citations", "status": "suggested", "author": "Edit OS", "format": None}
    path = tmp_path / "c.json"
    path.write_text(json.dumps([old]))
    c = ChangeSet.load(str(path)).changes[0]
    assert c.provenance is None
    c.provenance = provenance.make("checker", "LanguageTool X", "msg")
    ChangeSet([c]).dump(str(path))
    assert ChangeSet.load(str(path)).changes[0].provenance == {"kind": "checker", "label": "LanguageTool X",
                                                                "detail": "msg"}
    ins = pipeline.insert_from_dict({"id": "fi1", "anchor": "p0001", "where": "before", "runs": [["x", {}]],
                                     "role": "title"})
    assert ins.provenance is None and "provenance" in ins.to_dict()


# --- front matter --------------------------------------------------------------------------------

def test_front_matter_provenance(tmp_path, monkeypatch, ijo):
    # The title page is a separate file; the manuscript starts with the abstract.
    path = make(tmp_path, ["Abstract", "Purpose: To test."])
    monkeypatch.setattr(fm, "split", lambda text: {**claude_fields(()), "paragraphs": []})
    ms = docmodel.load(path)
    src = fm.Sources(first_page=TITLE_PAGE, meta=fm.parse_xml(PORTAL_XML), first_page_name="Title page.docx",
                     xml_name="ms_1.xml")
    pl = fm.plan(ms, ijo, src)
    by_role = {}
    for n in pl.inserts:
        by_role.setdefault(n.role, n.provenance)
    # the XML's article type wins (editors follow it)
    assert by_role["article_type"]["kind"] == "xml" and by_role["article_type"]["label"] == "XML: article type"
    assert by_role["title"] == {"kind": "title_page", "label": "Title page", "detail": TITLE_PAGE[1],
                                "file": "Title page.docx", "field": "title"}
    assert by_role["running_head"]["kind"] == "title_page" and by_role["running_head"]["detail"] == TITLE_PAGE[2]
    assert by_role["authors"]["detail"] == TITLE_PAGE[3]
    assert by_role["affiliations"]["kind"] == "title_page"
    assert by_role["dates"]["label"] == "XML: history dates" and by_role["dates"]["file"] == "ms_1.xml"
    assert by_role["cite"]["kind"] == "journal_style"
    emails = [n for n in pl.inserts if n.role == "correspondence" and "@" in n.text]
    assert emails and "asha@example.org" in emails[0].provenance["detail"]
    # saved and loaded with the records
    d = pl.inserts[0].to_dict()
    assert pipeline.insert_from_dict(d).provenance == pl.inserts[0].provenance


def test_front_matter_in_place_changes_carry_role(tmp_path, monkeypatch, ijo):
    path = make(tmp_path, TITLE_PAGE + ["Abstract", "Purpose: To test."])
    monkeypatch.setattr(fm, "split", lambda text: claude_fields())
    ms = docmodel.load(path)
    ms.front_sources = fm.Sources(meta=fm.parse_xml(PORTAL_XML))
    pl = fm.plan(ms, ijo)
    title = [c for c in pl.changes if c.para_id == "p0002"]
    assert title and all(c.provenance["kind"] == "manuscript" and c.provenance["role"] == "title" for c in title)
    assert rule_sources.key_for("front_matter", title[0].provenance) == "front_matter:title"
    label = [c for c in pl.changes if c.para_id == "p0003"]  # "Running title: ..." line is rewritten or removed
    assert label and all(c.provenance for c in label)


# --- references ----------------------------------------------------------------------------------

def test_reference_provenance(tmp_path, monkeypatch):
    rec = biblio.Record(source="pubmed", title="Virtual reality in midwifery training: a review of outcomes.",
                        authors=[("Okoro", "A"), ("Lindqvist", "B")], journal="J Midwif Educ", year="2020",
                        volume="12", pages="40-6", pmid="31234567", doi="10.1000/vr.1")
    e1 = "1. Okoro A., Lindqvist B. Virtual reality in midwifery training. J Midwif Educ. 2020;12(3):40-46."
    e2 = "2. Tanaka M. Sutures versus staples. Surg Today. 2017;4(2):1-9."
    styled = "Okoro A, Lindqvist B. Virtual reality in midwifery training: A review of outcomes. J Midwif Educ 2020;12:40-6."

    def fake(texts, st):
        return [refengine.Resolution(refengine.FORMAT_FIXED, refengine.parse(texts[0]), rec, styled),
                refengine.Resolution(refengine.QUERY, refengine.parse(texts[1]), message="Not found in PubMed or CrossRef")]

    monkeypatch.setattr(refengine, "resolve_all", fake)
    path = make(tmp_path, ["Body text.[1,2]", "References", e1, e2])
    st = style.load("ijo")
    cs = rules.run_all(docmodel.load(path), st, only=["ref_lookup", "references"])
    verified = [c for c in cs.changes if c.para_id == "p0003"]
    assert verified and all(c.reason == "ref_lookup" for c in verified)
    p = verified[0].provenance
    assert p["kind"] == "pubmed" and p["label"] == "PubMed 31234567"
    assert p["url"] == "https://pubmed.ncbi.nlm.nih.gov/31234567/"
    assert p["original"] == e1[3:] and p["styled"] == styled
    assert p["record"]["title"] == rec.title and p["record"]["pages"] == "40-6" and p["record"]["doi"] == "10.1000/vr.1"
    other = [c for c in cs.changes if c.para_id == "p0004"]
    assert other, "the cleanup rules still fix the unverified entry"
    assert all(c.provenance["kind"] == "unverified" and "Not found" in c.provenance["detail"] for c in other)


def test_crossref_provenance_links_the_doi():
    rec = biblio.Record(source="crossref", title="T", authors=[("A", "B")], doi="10.5/x")
    res = refengine.Resolution(refengine.FORMAT_FIXED, refengine.parse("A B. T."), rec, "A B. T. J 2020;1:1-2.")
    p = provenance.reference(res, "A B. T.")
    assert p["kind"] == "crossref" and p["label"] == "CrossRef 10.5/x" and p["url"] == "https://doi.org/10.5/x"


# --- back matter ---------------------------------------------------------------------------------

def test_back_matter_provenance(tmp_path):
    path = bm_make(tmp_path, ["Case Report", BODY, "Discussion", BODY] + REFS)
    ms = bm_load(path, first_page=FIRST_PAGE)
    ms.front_sources.first_page_name = "FirstPage.docx"
    cs = rules.run_all(ms, style.load("ijo"), only=["sections_back_matter"])
    added = [c for c in cs.changes if "contributions" in c.new]
    assert added
    p = added[0].provenance
    assert p["kind"] == "multiple"  # contributions from the title page + the journal's standard sections
    kinds = {x["kind"]: x for x in p["parts"]}
    assert kinds["title_page"]["file"] == "FirstPage.docx" and kinds["title_page"]["detail"] == FIRST_PAGE[5]
    assert "journal_style" in kinds

    form = ["Author Contributions", "A.B.: Conceptualization, Methodology, Writing – Original Draft."]
    path = bm_make(tmp_path, ["Discussion", BODY, "<H2>Financial support and sponsorship", "Nil.",
                              "<H2>Conflicts of interest", "None."] + REFS, "f.docx")
    ms = bm_load(path, forms=[form])
    ms.front_sources.contribution_form_names = ["Contribution form.docx"]
    items, origin = __import__("edit_os.rules_sections", fromlist=["x"]).contributions_with_origin(ms)
    assert items == form[1:] and origin["kind"] == "contribution_form" and origin["file"] == "Contribution form.docx"

    letter = ["Cover Letter", "6. Author Contributions:", "Dr A Rao – Concept, Design, Manuscript Review.",
              "Dr B Pillai – Literature Search, Data Acquisition.", "Thank you for considering our work."]
    path = bm_make(tmp_path, letter + ["Discussion", BODY] + REFS, "c.docx")
    ms = bm_load(path)
    cs = rules.run_all(ms, style.load("ijo"), only=["sections_back_matter"])
    added = next(c for c in cs.changes if "contributions" in c.new)
    labels = [added.provenance["label"]] + [x["label"] for x in added.provenance.get("parts", [])]
    assert "Cover letter" in labels


# --- renumbering, checkers ----------------------------------------------------------------------

def test_renumber_provenance(tmp_path):
    path = make(tmp_path, ["First finding.[2] Second finding.[1]", "References",
                           "1. Smith A, Jones B. A study. Ophthalmology 2001;12:1-9.",
                           "2. Rao C, Das D. Another study. Ophthalmology 2002;13:1-9."])
    st = style.load("ijo")
    st.settings["references"]["lookup"] = False
    cs = rules.run_all(docmodel.load(path), st, only=["renumber"])
    cites = [c for c in cs.changes if c.para_id == "p0001"]
    assert {c.provenance["label"] for c in cites} == {"Citation 2 → 1", "Citation 1 → 2"}
    entries = [c for c in cs.changes if c.para_id in ("p0003", "p0004")]
    assert entries and {c.provenance["label"] for c in entries} == {"Reference 2 → 1", "Reference 1 → 2"}


def test_spelling_provenance(tmp_path):
    from edit_os.copyedit import spelling

    path = make(tmp_path, ["The colour of the lesion was noted in the patients and the colour changed over time."])
    st = style.load("ijo")
    st.settings.setdefault("copyedit", {}).setdefault("spelling", {})["convention"] = "american"
    out = spelling.check(docmodel.load(path), st)
    if out:  # the word list is present in this install
        assert out[0].provenance["label"] == "Spelling: American convention"


# --- rule quotes ---------------------------------------------------------------------------------

SHEET = """SAMPLE
Running Title | Surname of the first author, a colon, then a brief title in sentence case
Article title | Bold, flush left, sentence case, no final full stop
Reference citation | Citation numbers go in square brackets, superscript, placed after the punctuation.
Reference List | Drop the repeated leading digits of the closing page number
Keywords | Sorted A to Z
"""

GUIDE = """INSTRUCTIONS
Paragraphs
Strip space characters at the beginning or end of a paragraph.
1 | p | P | The probability symbol takes an upper-case
letter set in italics.
2 | 5kg | 5<space>kg | Add space between each value and / its unit.
"""


@pytest.fixture
def files(tmp_path):
    d = tmp_path / "sheets"
    d.mkdir()
    (d / "StyleSheet_Sample_IJO.txt").write_text(SHEET)
    g = tmp_path / "guide.txt"
    g.write_text(GUIDE)
    return str(d), str(g)


def test_rule_quotes_resolve_against_stylesheet_and_guide(files):
    sheets, guide = files
    r = rule_sources.resolver("ijo", sheets, guide)
    q = r.rule("guide.p_value_capital")
    assert q["where"] == "Pre-editing guide" and q["line"] == 4
    assert q["quote"] == "p → P: The probability symbol takes an upper-case letter set in italics."
    assert r.rule("fmt_p_value")["quote"] == q["quote"]
    c = r.rule(rule_sources.key_for("citations"))
    assert c["where"] == "IJO stylesheet" and c["quote"].startswith("Reference citation | Citation numbers go in square")
    assert r.rule("ref_pages")["quote"].endswith("closing page number")
    assert r.rule("front_matter:title")["quote"].startswith("Article title |")
    assert r.rule("front_matter:running_head")["line"] == 2
    assert r.rule("guide.value_unit_space")["quote"] == "5kg → 5<space>kg: Add space between each value and its unit."
    assert r.rule("guide.trailing_space")["quote"].startswith("Paragraphs | Strip space characters")
    # nothing matches: the plain description only
    nothing = r.rule("fmt_color")
    assert nothing["quote"] is None and nothing["label"] == "Font colour removed" and nothing["description"]
    # cached per journal and file version
    assert rule_sources.resolver("ijo", sheets, guide) is r
    # another journal without a stylesheet: the guide still answers, the sheet does not
    other = rule_sources.resolver("jehp", sheets, guide)
    assert other.rule("citations")["quote"] is None and other.rule("guide.p_value_capital")["quote"]


def test_rule_keys():
    k = rule_sources.key_for
    assert k("claude.grammar (subject-verb agreement)") == "claude.grammar"
    assert rule_sources.locator(k("claude.grammar (x)"))[0] == "claude"
    assert rule_sources.locator(k("languagetool:EN_A_VS_AN"))[0] == "languagetool"
    assert rule_sources.locator(k("spelling (american)"))[0] == "spelling"
    assert k("front_matter: title") == "front_matter:title"
    assert rule_sources.locator("sections_back_matter.consent_added")[0] == "sections_back_matter.consent_added"
    assert rule_sources.locator("submission_cover_letter")[0] == "submission"
    assert rule_sources.locator(k("expand abbreviation at first use"))[0] == "abbreviations"


def test_no_stylesheets_configured(monkeypatch):
    monkeypatch.delenv(rule_sources.ENV_SHEETS, raising=False)
    monkeypatch.delenv(rule_sources.ENV_GUIDE, raising=False)
    q = rule_sources.rule_for("ijo", "citations")
    assert q["quote"] is None and q["label"] == "Citation style"
    assert rule_sources.configured() == {"stylesheets": False, "guide": False}


def test_change_kwargs_unchanged():
    c = Change("x", "p1", 0, 1, "a", "b", Layer.HOUSE_STYLE, Source.RULE, "citations")
    assert c.provenance is None and Change.from_dict(c.to_dict()) == c


# --- API -----------------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch, files):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.config import Settings
    from test_pipeline import offline_style

    sheets, guide = files
    monkeypatch.setenv(rule_sources.ENV_SHEETS, sheets)
    monkeypatch.setenv(rule_sources.ENV_GUIDE, guide)
    orig, st = style.load, offline_style()
    monkeypatch.setattr(style, "load", lambda code: st if code.lower() == "ijo" else orig(code))
    settings = Settings(data_dir=str(tmp_path / "data"), api_keys=["test-key"], workers=1)
    with TestClient(create_app(settings)) as c:
        yield c


def test_api_changes_and_rules(api, tmp_path):
    from test_api import KEY, upload, wait

    job_id = upload(api, tmp_path).json()["id"]
    assert wait(api, job_id)["status"] == "done"
    items = api.get(f"/api/jobs/{job_id}/changes", headers=KEY).json()["items"]
    assert items and all("rule_key" in d and "provenance" in d for d in items)
    data = api.get(f"/api/jobs/{job_id}/rules", headers=KEY).json()
    assert data["configured"] == {"stylesheets": True, "guide": True}
    assert {d["rule_key"] for d in items} <= set(data["rules"])
    assert all({"label", "description", "quote", "where", "line"} <= set(r) for r in data["rules"].values())
    quoted = [r for r in data["rules"].values() if r["quote"]]
    assert quoted and {r["where"] for r in quoted} <= {"IJO stylesheet", "Pre-editing guide"}
    assert api.get(f"/api/jobs/{job_id}/rules").status_code == 401
