

def test_qc_skips_enumerations_and_unary_signs():
    from edit_os import qc
    assert qc._enumeration("The aims were: (1) describing use; (2) assessing cost; and (3) estimating effect.")
    assert not qc._enumeration("as reported earlier (3) and later (5).")
    import re
    t = "Most patients showed >90 percent recovery while p >0.05 here."
    ms = [m for m in re.finditer(r"(?<=[A-Za-z0-9)]) [=<>≤≥](?=\d)", t)]
    assert [qc._unary_after_word(t, m) for m in ms] == [True, False]


def test_qc_unary_after_short_word():
    import re
    from edit_os import qc
    t = "The rate is <30% and sizes up to ≤10 cm; OR >2 here."
    ms = list(re.finditer(r"(?<=[A-Za-z0-9)]) [=<>≤≥](?=\d)", t))
    assert [qc._unary_after_word(t, m) for m in ms] == [True, True, False]
