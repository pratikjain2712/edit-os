import pytest
from docx import Document

from edit_os import docmodel, journals, rules, style

NLM = """--------------------------------------------------------
JrId: 1
JournalTitle: Indian journal of ophthalmology
MedAbbr: Indian J Ophthalmol
ISSN (Print): 0301-4738
ISSN (Online): 1998-3689
IsoAbbr: Indian J Ophthalmol
NlmId: 0405376
--------------------------------------------------------
JrId: 2
JournalTitle: Indian journal of ophthalmology
MedAbbr: Indian J Ophthalmol (Poona City)
ISSN (Print): 
ISSN (Online): 
IsoAbbr: 
NlmId: 9999999
--------------------------------------------------------
JrId: 3
JournalTitle: Spine
MedAbbr: Spine (Phila Pa 1976)
ISSN (Print): 0362-2436
ISSN (Online): 1528-1159
IsoAbbr: Spine
NlmId: 7610646
--------------------------------------------------------
JrId: 4
JournalTitle: Journal of clinical and diagnostic research : JCDR
MedAbbr: J Clin Diagn Res
ISSN (Print): 2249-782X
ISSN (Online): 0973-709X
IsoAbbr: J Clin Diagn Res
NlmId: 101488993
--------------------------------------------------------
"""


@pytest.fixture
def directory(tmp_path, monkeypatch):
    db = str(tmp_path / "j.sqlite")
    journals.build(journals.parse_nlm(NLM), db)
    d = journals.Directory(db)
    monkeypatch.setattr(journals, "_directory", d)
    return d


def test_lookup(directory):
    assert directory.lookup("Indian J. Ophthalmol.").med_abbr == "Indian J Ophthalmol"
    assert directory.lookup("Indian journal of ophthalmology").nlm_id == "0405376"  # not the Poona City one
    assert directory.lookup("Journal of Clinical and Diagnostic Research").med_abbr == "J Clin Diagn Res"
    assert directory.lookup("0973-709X").med_abbr == "J Clin Diagn Res"
    assert directory.lookup("Spine").med_abbr == "Spine (Phila Pa 1976)"
    assert directory.lookup("Journal of Unknown Things") is None


def test_reference_journal_rule(directory, tmp_path):
    doc = Document()
    for t in ["References",
              "1. Rao S, Mehta P. Timing of surgery in children. Indian Journal of Ophthalmology. 2019;67(3):12-18.",
              "2. Lee K. Back pain in adolescents. Spine. 1997;22:1380-4.",
              "3. Kim J. A study. Journal of Unknown Things 2001;3:4-5."]:
        doc.add_paragraph(t)
    path = tmp_path / "r.docx"
    doc.save(path)
    ms = docmodel.load(str(path))
    found = {(c.old, c.new) for c in rules.rule_references(ms, style.load("ijo")) if c.reason == "ref_journal"}
    # IJO keeps NLM's qualifier, as its editors do ("Eye (Lond)", "Clinics (Sao Paulo)"); unknown left alone.
    assert found == {("Indian Journal of Ophthalmology", "Indian J Ophthalmol"), ("Spine", "Spine (Phila Pa 1976)")}
    st = style.load("ija")  # every journal keeps it now (S: "abbreviated according to the style used in Index Medicus")
    assert {(c.old, c.new) for c in rules.rule_references(ms, st) if c.reason == "ref_journal"} == found
    st.settings["references"]["journal_drop_qualifier"] = True
    found = {(c.old, c.new) for c in rules.rule_references(ms, st) if c.reason == "ref_journal"}
    assert found == {("Indian Journal of Ophthalmology", "Indian J Ophthalmol")}  # the switch drops it
