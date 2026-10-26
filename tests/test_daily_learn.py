"""tools/learn.py and the offline parts of tools/daily_learn.py, on synthetic metrics (no network, no manuscripts)."""

import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import daily_learn as D  # noqa: E402
import learn as L  # noqa: E402
from edit_os import autopilot  # noqa: E402


def test_hold_key_matches_autopilot_hold():
    for reason, key in [("fig_captions.case", "fig_captions.case"), ("spelling (american)", "spelling"),
                        ("claude.grammar (subject-verb)", "claude.grammar"),
                        ("use abbreviation after its definition", "use abbreviation"),
                        ("front_matter: title", "front_matter"), ("guide.trailing_space", "guide.trailing_space")]:
        assert L.hold_key(reason) == key
        assert autopilot._held(reason, [key])


def test_autopilot_decisions_hold_release_and_owner_holds():
    stats = {
        "guide.bad": [10, 15, 3],        # 40% on 25 -> hold
        "guide.few": [1, 9, 0],          # 10 judged: too few
        "guide.good": [40, 1, 0],        # fine, not held
        "text.recovered": [30, 1, 0],    # a hold this tool added, back at 97% -> release
        "fmt_color": [29, 1, 0],         # an owner hold at 97% -> reported only
        "claude": [5, 30, 0],            # already held (owner) -> nothing
        "ref_year": [5, 30, 0],          # pinned by the owner -> nothing
    }
    hold = ["fmt_color", "claude", "text.recovered"]
    out = L.autopilot_decisions(stats, hold, learner=["text.recovered"], pinned=["ref_year"])
    assert out["add"] == ["guide.bad"]
    assert out["release"] == ["text.recovered"]
    assert out["owner_release"] == ["fmt_color"]
    assert out["evidence"]["guide.bad"]["precision"] == 0.4
    assert L.new_hold(hold, out["add"], out["release"]) == ["fmt_color", "claude", "guide.bad"]


def test_a_sub_rule_of_a_held_rule_is_not_held_again():
    out = L.autopilot_decisions({"claude.grammar": [1, 30, 0]}, ["claude"], [])
    assert out["add"] == []


def test_owner_questions_only_for_stylesheet_rules_nearly_all_editors_skip():
    stats = {"sections_back_matter.added": [1, 24, 0], "claude.style": [0, 40, 0], "guide.x": [10, 12, 0]}
    qs = L.owner_questions(stats, "jehp")
    assert [q["rule"] for q in qs] == ["sections_back_matter.added"]
    assert "24 of 25" in qs[0]["question"]


def test_edit_patterns_missed_applied_kept():
    o = "The mean ( 80 ) and the range ( 20 ) were noted in the trial."
    t = "The mean (80) and the range ( 20 ) were noted in the trial."  # editor fixes one, leaves one
    u = o                                                              # the tool does nothing
    pats = L.paragraph_patterns(o, t, u)
    key = next(k for k in pats if L.pattern_label(k).startswith("([␣ -> ∅]"))
    missed, applied, kept = pats[key]
    assert (missed, applied, kept) == (1, 1, 1)
    # the tool making the same edit: not missed
    assert L.paragraph_patterns(o, t, t)[key][0] == 0


def test_proposals_need_support_and_consistency():
    key = L.SEP.join(["(", " ", "", "9"])
    good = {"patterns": {key: [2, 2, 0, "x"]}, "format_editor": {"bold-@heading": [3, 0]},
            "missed_bigrams": {"et al": 2}}
    weak = {"patterns": {key: [2, 2, 8, None]}}
    props = L.proposals([good, good, good], "ijo")
    kinds = {p["kind"] for p in props}
    assert kinds == {"text", "format", "bigram"}
    assert all(p["articles"] == 3 for p in props)
    assert not [p for p in L.proposals([weak, weak, weak], "ijo") if p["kind"] == "text"]  # editors keep it mostly
    assert not L.proposals([good, good], "ijo")  # two articles only


def _res(dOT, dUT, right, wrong, passed=True, crash=False):
    return {"style": {"dOT": dOT, "dUT": dUT}, "bigrams": {"text": {"orig": 100, "ours": 100 - dOT + dUT}},
            "rules_autopilot": {"r": [right, wrong, 0]}, "refs": {"refs": 10, "exact_after": 5, "dOT": 10, "dUT": 5},
            "front": {"title": [1, 1, 1.0]}, "qc": {"passed": passed, "failed": {} if passed else {"content.deleted": "error"}},
            "crashes": [{"step": "x", "error": "E"}] if crash else []}


def test_journal_metrics_and_regressions():
    before = L.journal_metrics([_res(100, 50, 9, 1), _res(100, 50, 9, 1)])
    assert before["style_closure"] == 0.5 and before["rule_precision"] == 0.9 and before["refs_identical"] == 0.5
    after = L.journal_metrics([_res(100, 60, 9, 1), _res(100, 50, 9, 1, passed=False)])
    assert after["qc_blocked"] == 1 and after["qc_failed"] == {"content.deleted": 1}
    regs = L.regressions(after, before, "ijo")
    assert {r["kind"] for r in regs} == {"metric", "qc"}
    assert any(r.get("metric") == "style_closure" for r in regs)
    # within noise, or a different held-out set: no metric regression
    assert not L.regressions(L.journal_metrics([_res(100, 50.5, 9, 1)]), before, "ijo")
    assert not L.regressions(after, before, "ijo", same_set=False)
    crashed = L.journal_metrics([_res(100, 50, 9, 1, crash=True)])
    assert [r["kind"] for r in L.regressions(crashed, None, "ijo")] == ["crash"]


def test_snippets_are_short_and_masked():
    s = L.change_snippet("as reported by Sharma et al. in 2019 the mean age was 45 years in all the groups studied",
                         "as reported by Sharma et al. in 2019, the mean age was 45 years in all the groups studied")
    old, new = s.split(" -> ")
    assert len(old.split()) <= L.MAX_WORDS and len(new.split()) <= L.MAX_WORDS
    assert "Sharma" not in s and "2019" not in s
    assert L.snippet("write to a.b@x.org now") == "write to <email> now"


def test_render_markdown_has_the_sections():
    rep = {"date": "2026-10-26", "commit": "abcdef1234567", "usage": {"calls": 3, "cost": 0.12}, "new_count": 2,
           "journals": {"ijo": {"priority": 0, "heldout": L.journal_metrics([_res(100, 50, 9, 1)]),
                                "heldout_previous": L.journal_metrics([_res(100, 40, 9, 1)]),
                                "new": L.journal_metrics([_res(100, 50, 9, 1)])}},
           "autopilot": {"ijo": {"add": ["guide.bad"], "release": [], "owner_release": [],
                                 "evidence": {"guide.bad": {"right": 1, "wrong": 30, "why": "w",
                                                            "examples": {"wrong": ["a -> b"]}}}}},
           "proposals": [{"kind": "text", "journal": "ijo", "pattern": "([␣ -> ∅]9", "missed": 9,
                          "articles": 4, "of": 5, "consistency": 0.9, "example": None}],
           "owner_questions": [{"question": "ijo: Q?"}], "regressions": [{"what": "ijo held-out style: 60% -> 50%"}]}
    md = L.render_markdown(rep)
    for part in ("## Scores per journal", "| ijo | heldout | 1 | 50.0% (-10.0)", "## Regressions",
                 "hold `guide.bad`", "## New rule proposals", "ijo: Q?"):
        assert part in md


STYLE = """name: Test journal
# a comment that stays
autopilot:
  # owner notes stay
  hold: [fmt_color, figure_insert,
         claude]
  ai_judge: true
other: 1
"""


def test_patch_hold_yaml_keeps_comments_and_marks_learner_holds():
    new = L.patch_hold_yaml(STYLE, ["fmt_color", "claude", "guide.bad"], ["guide.bad"])
    data = yaml.safe_load(new)
    assert data["autopilot"]["hold"] == ["fmt_color", "claude", "guide.bad"]
    assert data["autopilot"]["ai_judge"] is True and data["other"] == 1
    assert "# owner notes stay" in new and "# a comment that stays" in new
    assert L.learner_holds(new) == ["guide.bad"]
    back = L.patch_hold_yaml(new, ["fmt_color", "claude"], [])
    assert yaml.safe_load(back)["autopilot"]["hold"] == ["fmt_color", "claude"]
    assert L.learner_holds(back) == [] and L.LEARNER_MARK not in back
    added = L.patch_hold_yaml("name: X\n", ["a", "b"], ["b"])
    assert yaml.safe_load(added)["autopilot"]["hold"] == ["a", "b"] and L.learner_holds(added) == ["b"]


def test_apply_actions_releases_only_its_own_holds(tmp_path):
    (tmp_path / "base.yaml").write_text("autopilot:\n  hold: [fmt_color, figure_insert]\n")
    (tmp_path / "x.yaml").write_text(L.patch_hold_yaml(STYLE, ["fmt_color", "figure_insert", "claude", "mine"],
                                                       ["mine"]))
    (tmp_path / "y.yaml").write_text("name: Y\n")
    changed = D.apply_actions({"autopilot": {"x": {"add": ["guide.bad"], "release": ["mine", "claude"]},
                                             "y": {"add": ["guide.z"], "release": []}}}, str(tmp_path))
    assert len(changed) == 2
    x = yaml.safe_load((tmp_path / "x.yaml").read_text())["autopilot"]["hold"]
    assert x == ["fmt_color", "figure_insert", "claude", "guide.bad"]  # the owner's "claude" hold stays
    assert L.learner_holds((tmp_path / "x.yaml").read_text()) == ["guide.bad"]
    y = yaml.safe_load((tmp_path / "y.yaml").read_text())["autopilot"]["hold"]
    assert y == ["fmt_color", "figure_insert", "guide.z"]  # the journal starts from the base list


def test_heldout_pick_is_stable_and_priority_order():
    ids = [f"IJO_{i}_26" for i in range(20)]
    assert D.pick_heldout(ids, 5) == D.pick_heldout(list(reversed(ids)), 5)
    assert len(D.pick_heldout(ids, 5)) == 5
    order = ["ijo", "jehp", "mjdrdypu"]
    assert sorted(["abr", "mjdrdypu", "ijo"], key=lambda j: D.priority(j, order)) == ["ijo", "mjdrdypu", "abr"]
    assert sorted(["2026-10-01", "2026-10-20"], key=D._neg) == ["2026-10-20", "2026-10-01"]


def test_local_source_lists_folders(tmp_path):
    d = tmp_path / "IJO_1_26"
    d.mkdir()
    (d / "te.docx").write_bytes(b"x")
    (d / "original.zip").write_bytes(b"x")
    rec = D.LocalSource(str(tmp_path)).overview()[0]
    assert rec["article_id"] == "IJO_1_26" and rec["te_upload_link"] and not rec["ce_update_link"]
    assert D.journal_of("IJO_1_26") == "ijo" and D.journal_of("DSHMJ_49_26") == "ijn_dshmj"
