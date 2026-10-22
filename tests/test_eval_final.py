"""tools/eval_final.py on synthetic text: rule precision and bigram closure against a final file."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import eval_final as E  # noqa: E402
from edit_os.changes import Change, Layer, Source, Status  # noqa: E402


def _c(i, text, old, new, reason, status=Status.ACCEPTED):
    s = text.index(old)
    return Change(f"c{i}", "p1", s, s + len(old), old, new, Layer.HOUSE_STYLE, Source.RULE, reason, status)


def test_rule_precision_right_wrong_unclear():
    text = "The mean age was 46-55 years in the first group of the study ( 80 ) and the colour was red."
    ce = ["The mean age was 46–55 years in the first group of the study ( 80 ) and the colour was red."]
    changes = [
        _c(1, text, "46-55", "46–55", "guide.range_dash: numeric range"),        # CE did it: right
        _c(2, text, "( 80 )", "(80)", "guide.bracket_inner_space: space"),        # CE kept it: wrong
        _c(3, text, "colour", "color", "spelling: US", Status.SUGGESTED),         # not accepted: ignored
    ]
    stat = E.rule_precision(changes, {"p1": text}, ce)
    assert stat["guide.range_dash"] == [1, 0, 0]
    assert stat["guide.bracket_inner_space"] == [0, 1, 0]
    assert "spelling" not in stat
    # a period added at a paragraph's end is looked up there, not inside a longer sentence
    cap = "Table 1: Characteristics of participants"
    end = Change("e", "p2", len(cap), len(cap), "", ".", Layer.HOUSE_STYLE, Source.RULE, "fig_captions.period_added",
                 Status.ACCEPTED)
    assert E.rule_precision([end], {"p2": cap}, [cap, "Next."])["fig_captions.period_added"] == [0, 1, 0]


def test_rule_name():
    assert E.rule_name("ref_year: 2019 -> 2020") == "ref_year"
    assert E.rule_name("claude.grammar (certain)") == "claude.grammar"


def test_bigram_closure_text_and_references():
    orig = ["Introduction", "Patients was seen.", "References", "1. Smith A. Title. J 2001;1:1-9."]
    ce = ["Introduction", "Patients were seen.", "References", "1. Smith A. Title. J 2001;1:1-9."]
    ours = list(ce)
    b = E.bigram_scores(orig, ours, ce)
    assert b["text"]["orig"] == 4 and b["text"]["ours"] == 0 and b["text"]["te"] is None
    assert b["references"]["orig"] == 0
    assert E.closure(b["text"]["orig"], b["text"]["ours"]) == 1.0
    # a wrong edit makes it worse than the original
    b = E.bigram_scores(orig, ["Introduction", "Patient was seen .", "References"], ce, te=ce)
    assert b["text"]["ours"] > 0 and b["text"]["te"] == 0


def test_split_refs_keeps_captions_in_text():
    paras = ["Intro", "References", "1. A", "Table 1: Caption"]
    sp = E.split_refs(paras)
    assert sp["references"] == ["References", "1. A"]
    assert sp["text"] == ["Intro", "Table 1: Caption"]


def test_changes_are_judged_in_our_edited_text():
    text = "Table 1: Baseline Clinical Characteristics of the Participants"
    ce = ["Table 1: Baseline clinical characteristics of the participants"]
    changes = [_c(1, text, "Cl", "cl", "fig_captions.case"), _c(2, text, "Ch", "ch", "fig_captions.case"),
               _c(3, text, "Pa", "pa", "fig_captions.case")]
    assert E.rule_precision(changes, {"p1": text}, ce)["fig_captions.case"] == [3, 0, 0]
    # one word at a time the final text matches neither side
    assert E.rule_precision(changes, {"p1": text}, ce, single=True)["fig_captions.case"][0] == 0
