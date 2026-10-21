"""Page view of the manuscript (`docview.document`) and the editor's direct edits (`docview.plan_edit`)."""

import pytest
from docx import Document

from edit_os import docmodel, docview, tracked
from edit_os.changes import Change, ChangeSet, Layer, Source, Status


def make(path):
    doc = Document()
    doc.add_paragraph("Running title: Lens study")
    doc.add_paragraph("Abstract")
    p = doc.add_paragraph("Water is H")
    p.add_run("2").font.subscript = True
    p.add_run("O and cited")
    p.add_run("1").font.superscript = True
    p.add_run(" in ")
    p.add_run("italics").italic = True
    doc.add_heading("Methods", level=2)
    b = doc.add_paragraph()
    b.add_run("Bold side heading").bold = True
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "Group", "n", "A", "12"
    doc.add_paragraph("Table 1. Patients by group")
    doc.add_paragraph("References")
    doc.add_paragraph("1. Lee K. Back pain. Spine. 1997;22:1380-4.")
    doc.save(path)
    return str(path)


def test_document_kinds_and_runs(tmp_path):
    d = docview.document(make(tmp_path / "a.docx"))["paragraphs"]
    by_text = {p["text"]: p for p in d}
    ms = docmodel.load(str(tmp_path / "a.docx"))
    assert [p["id"] for p in d] == [p.id for p in ms.paragraphs]
    for p, m in zip(d, ms.paragraphs):  # runs join to the text view every offset refers to
        assert "".join(r["text"] for r in p["runs"]) == p["text"] == m.text
    assert by_text["Running title: Lens study"]["kind"] == "front"
    assert by_text["Abstract"]["kind"] == "heading" and by_text["Abstract"]["level"] == 1
    assert by_text["Methods"]["kind"] == "heading" and by_text["Methods"]["level"] == 2
    assert by_text["Bold side heading"]["kind"] == "heading"
    runs = by_text["Water is H2O and cited1 in italics"]["runs"]
    assert {"text": "2", "format": {"subscript": True}} in runs
    assert {"text": "1", "format": {"superscript": True}} in runs
    assert {"text": "italics", "format": {"italic": True}} in runs
    cells = [p for p in d if p["kind"] == "table"]
    assert [(c["text"], c["cell"]["row"], c["cell"]["col"]) for c in cells] == \
        [("Group", 0, 0), ("n", 0, 1), ("A", 1, 0), ("12", 1, 1)]
    assert {c["cell"]["table"] for c in cells} == {"t1"}
    assert by_text["Table 1. Patients by group"]["kind"] == "caption"
    assert by_text["References"]["kind"] == "heading"
    assert by_text["1. Lee K. Back pain. Spine. 1997;22:1380-4."]["kind"] == "reference"


SRC = "The colour of the lesion was noted in 40 patients."


def ch(cid, start, end, new, status=Status.SUGGESTED, reason="spelling"):
    return Change(cid, "p1", start, end, SRC[start:end], new, Layer.LANGUAGE, Source.RULE, reason, status)


def test_plan_edit_word_level_against_source():
    plan = docview.plan_edit("p1", SRC, [], SRC, "The colour of the big lesion was seen in 40 patients.")
    got = [(c.old, c.new) for c in plan.changes]
    assert ("", "big ") in got and ("noted", "seen") in got
    assert all(c.status == Status.ACCEPTED and c.source == Source.EDITOR for c in plan.changes)
    assert all(SRC[c.start:c.end] == c.old for c in plan.changes)


def test_plan_edit_keeps_accepted_suggestion_and_maps_offsets():
    acc = ch("s1", 4, 10, "color", Status.ACCEPTED)  # colour -> color
    base = "The color of the lesion was noted in 40 patients."
    plan = docview.plan_edit("p1", SRC, [acc], base, "The color of the lesion was noted in 41 patients.")
    [c] = plan.changes
    assert (c.old, c.new) == ("40", "41") and SRC[c.start:c.end] == "40"
    # Touching the accepted suggestion's words is refused.
    with pytest.raises(docview.EditConflict):
        docview.plan_edit("p1", SRC, [acc], base, "The hue of the lesion was noted in 40 patients.")
    # A stale base is refused.
    with pytest.raises(docview.EditConflict):
        docview.plan_edit("p1", SRC, [acc], SRC, SRC.replace("40", "41"))


def test_plan_edit_appending_to_an_accepted_word_uses_the_boundary():
    acc = ch("s1", 4, 10, "color", Status.ACCEPTED)
    base = "The color of the lesion was noted in 40 patients."
    plan = docview.plan_edit("p1", SRC, [acc], base, "The colors of the lesion was noted in 40 patients.")
    [c] = plan.changes
    assert (c.start, c.end, c.new) == (10, 10, "s")


def test_plan_edit_sets_aside_overlapping_pending_and_replaces_own_edits():
    pending = ch("s2", 30, 35, "recorded")  # noted -> recorded
    other = ch("s3", 4, 10, "color")
    plan = docview.plan_edit("p1", SRC, [pending, other], SRC, SRC.replace("noted", "seen"))
    assert [p.id for p in plan.superseded] == ["s2"]
    first = plan.changes
    # Editing again replaces the first direct edit, computed against the source.
    later = docview.plan_edit("p1", SRC, [pending, other] + first, SRC.replace("noted", "seen"),
                              SRC.replace("noted", "observed"))
    assert [c.id for c in later.replaced] == [c.id for c in first]
    assert [(c.old, c.new) for c in later.changes] == [("noted", "observed")]
    # Reverting to the original text leaves nothing.
    back = docview.plan_edit("p1", SRC, first, SRC.replace("noted", "seen"), SRC)
    assert back.changes == [] and back.replaced == first


def test_plan_edit_rejects_paragraph_breaks():
    with pytest.raises(docview.EditInvalid):
        docview.plan_edit("p1", SRC, [], SRC, SRC + "\nNew line")
    with pytest.raises(docview.EditInvalid):
        docview.plan_edit("p1", SRC, [], SRC, SRC + " x")


def test_direct_edits_render_with_tracked(tmp_path):
    doc = Document()
    doc.add_paragraph(SRC)
    doc.save(tmp_path / "s.docx")
    acc = ch("s1", 4, 10, "color", Status.ACCEPTED)
    acc.para_id = "p0001"
    plan = docview.plan_edit("p0001", SRC, [acc], "The color of the lesion was noted in 40 patients.",
                             "The color of the large lesion was noted in 41 patients.")
    ms = docmodel.load(str(tmp_path / "s.docx"))
    tracked.render(ms, ChangeSet([acc] + plan.changes), tracked=False)
    assert ms.paragraphs[0].element is not None
    assert docmodel.paragraph_text(ms.paragraphs[0].element) == "The color of the large lesion was noted in 41 patients."
