"""Statistics run together are corrected, not queried (owner, 2026-11-09, jehp_1499_26)."""
from edit_os import docmodel, rules, rules_guide as g

from test_jehp_citations import _refs, make
from test_jehp_review9 import jehp_style

T = "(Path a:B=-0.5009, SE=0.1295,t=-3.866, p<0.001,95% CI-0.7574-0.2443)"


def _apply(text, finder):
    for s, e, new in sorted(finder(text, {}), reverse=True):
        text = text[:s] + new + text[e:]
    return text


def test_ci_bounds_read_from_estimate_and_p_value():
    assert _apply("B = -0.30 (95% CI -0.50-0.20, P = 0.01)", g.find_ci_range) == "B = -0.30 (95% CI −0.50 to −0.20, P = 0.01)"
    assert _apply("B = -0.30 (95% CI -0.50-0.20, P = 0.40)", g.find_ci_range) == "B = -0.30 (95% CI −0.50 to 0.20, P = 0.40)"
    assert _apply("B = -0.30 (95% CI -0.50-0.20)", g.find_ci_range) == "B = -0.30 (95% CI -0.50-0.20)"  # cannot tell
    assert _apply("OR = 1.8 (95% CI 1.2-2.6)", g.find_ci_range) == "OR = 1.8 (95% CI 1.2 to 2.6)"


def test_jehp_formats_the_run_together_statistics(tmp_path):
    path = make(tmp_path, ["Introduction", "Text.", "Results", f"Anxiety fell {T}, as expected."]
                + _refs("A A. One. J T 2020;1:1-2."))
    ms = docmodel.load(path)
    p = next(p for p in ms.paragraphs if "Path a" in p.text)
    text = p.text
    cs = [c for c in rules.run_all(ms, jehp_style()).changes if c.para_id == p.id and c.old != c.new]
    for c in sorted(cs, key=lambda c: (c.start, c.end), reverse=True):
        text = text[:c.start] + c.new + text[c.end:]
    assert "(Path a: B = −0.5009, SE = 0.1295, t = −3.866, P < 0.001, 95% CI −0.7574 to −0.2443)" in text
    assert any(c.reason == "guide.ci_range" for c in cs)


def test_minus_sign_typed_for_a_range_dash():
    # jehp_1728_26: "Moderate (40−80%)" with a minus sign (U+2212) -> en dash; a subtraction stays
    assert _apply("Low (less than 40%), Moderate (40−80%), or High", g.find_range_dash) == \
        "Low (less than 40%), Moderate (40–80%), or High"
    assert _apply("x 5 − 3 = 2", g.find_range_dash) == "x 5 − 3 = 2"
