import pytest
from docx import Document

from edit_os import docmodel, qc, rules, style, tracked
from edit_os.changes import Status


@pytest.fixture
def manuscript(tmp_path):
    doc = Document()
    for text in [
        "Abstract",
        "Key words: retina, cataract, amblyopia",
        "Introduction",
        "Dermoids are rare (1). Earlier series reported similar results (2–3), and others (2, 3).",
        "Mean age was 45 (12) years and p=0.03.",
        "Methods:",
        "Results",
        "Conclusion",
        "References",
        "1. Mohammad AEA, Kroosh SS. Huge corneal dermoid: a case report. Orbit Amst Neth. 2002;21(4):295-299.",
        "2. Duncan JL, Golabi M, Fredrick DR, Hoyt CS, Hwang DG, Kramer SG, Smith A. Complex limbal choristomas. "
        "Ophthalmology. 1998;105(8):1459-65.",
        "3. Lindqvist P., Hale R. Timing of surgery. J Med Genet 1987;24:616-20.",
    ]:
        doc.add_paragraph(text)
    path = tmp_path / "m.docx"
    doc.save(path)
    return str(path)


def apply(path, st, tmp_path):
    cs = rules.run_all(docmodel.load(path), st)
    for c in cs.changes:
        c.status = Status.ACCEPTED
    out = tmp_path / "out.docx"
    tracked.render(docmodel.load(path), cs, tracked=False).save(out)
    return [p.text for p in docmodel.load(str(out)).paragraphs]


def test_ijo_rules(manuscript, tmp_path):
    t = apply(manuscript, style.load("ijo"), tmp_path)
    assert t[0] == "Abstract" and t[2] == ""  # IJO does not tag "Abstract" and drops "Introduction"
    assert t[1] == "Keywords: Amblyopia, cataract, retina"
    assert t[7:12] == ["<H1>Conclusion", "<H2>Financial support and sponsorship", "Nil.", "<H2>Conflicts of interest",
                       "There are no conflicts of interest."]
    del t[8:12]
    assert t[3] == "Dermoids are rare.[1] Earlier series reported similar results,[2,3] and others.[2,3]"
    assert t[4] == "Mean age was 45 (12) years and p=0.03."  # statistics, not a citation; spacing off for IJO
    assert t[5] == "<H1>Methods" and t[6] == "<H1>Results" and t[8] == "<H1>References"
    assert t[9] == "1. Mohammad AEA, Kroosh SS. Huge corneal dermoid: A case report. Orbit Amst Neth 2002;21:295-9."
    assert t[10] == ("2. Duncan JL, Golabi M, Fredrick DR, Hoyt CS, Hwang DG, Kramer SG, et al. "
                     "Complex limbal choristomas. Ophthalmology 1998;105:1459-65.")
    assert t[11] == "3. Lindqvist P, Hale R. Timing of surgery. J Med Genet 1987;24:616-20."


def test_base_rules_space_operators_and_tag_all_h1(manuscript, tmp_path):
    t = apply(manuscript, style.load("base"), tmp_path)
    del t[8:12]  # back matter added before the references
    assert t[0] == "<H1>Abstract" and t[2] == "<H1>Introduction"
    assert t[4] == "Mean age was 45 (12) years and p = 0.03."
    assert t[9].endswith("2002;21:295-9.")


def test_qc_blocks_unedited_file(manuscript):
    report = qc.check(docmodel.load(manuscript), style.load("ijo"))
    checks = {i.check for i in report.issues}
    assert not report.passed
    assert {"citations.brackets", "sections.required", "references.authors", "keywords.order"} <= checks


def test_qc_after_rules_passes(manuscript, tmp_path):
    t = apply(manuscript, style.load("ijo"), tmp_path)
    doc = Document()
    for text in t:
        doc.add_paragraph(text)
    path = tmp_path / "edited.docx"
    doc.save(path)
    errors = {i.check for i in qc.check(docmodel.load(str(path)), style.load("ijo")).issues if i.severity == qc.ERROR}
    assert not errors  # the section rules add the back matter and sort the key words


def test_superscript_citations(tmp_path):
    doc = Document()
    p = doc.add_paragraph("Colorectal cancer is common worldwide.¹ Rash is frequent⁴⁻⁶, and dry skin too")
    r = p.add_run("7,8")
    r.font.superscript = True
    p.add_run(". Leukocytes were 5 × 10")
    p.add_run("9").font.superscript = True
    p.add_run("/L in a 2 cm")
    p.add_run("2").font.superscript = True
    p.add_run(" area, 3 mm³ deep.")
    for t in ["References"] + [f"{n}. Author A. Title {n}. J Test 2001;{n}:1-2." for n in range(1, 9)]:
        doc.add_paragraph(t)
    path = tmp_path / "s.docx"
    doc.save(path)
    st = style.load("ijo")
    st.settings["references"]["lookup"] = False
    t = apply(str(path), st, tmp_path)
    assert t[0] == ("Colorectal cancer is common worldwide.[1] Rash is frequent,[4-6] and dry skin too.[7,8] "
                    "Leukocytes were 5 × 109/L in a 2 cm2 area, 3 mm³ deep.")  # powers untouched
    out = Document(tmp_path / "out.docx")
    sup = [r.text for r in out.paragraphs[0].runs if r.font.superscript]
    assert "".join(sup) == "[1][4-6][7,8]92"  # citations superscript (guide); exponents stay superscript too
