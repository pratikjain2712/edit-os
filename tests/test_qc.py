

def test_qc_skips_unary_signs():
    from edit_os import qc
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
