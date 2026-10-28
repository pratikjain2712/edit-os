from edit_os import iso4

from conftest import LTWA_SAMPLE


def ab(title):
    return iso4.abbreviate(title, LTWA_SAMPLE)


def test_words_and_stems():
    assert ab("Interactive Technology and Smart Education") == "Interact Technol Smart Educ"
    assert ab("Journal of the Learning Sciences") == "J Learn Sci"  # plural stem, "of the" dropped
    assert ab("Journal of Nursing Teaching Research") == "J Nurs Teach Res"


def test_left_in_full():
    assert ab("Nature") == "Nature"                       # one word
    assert ab("MIS Quarterly") == "MIS Q"                 # acronym kept
    assert ab("Journal of Health Promotion") == "J Health Promotion"  # n.a. and unlisted words stay
    assert ab("Journal of caring sciences") == "J Caring Sci"         # each word capitalised
    assert ab("Journal of e-learning") == "J E-Learn"                 # after a hyphen too (the editors)


def test_ascii_title_takes_no_accents():
    assert ab("Journal of Medical Research") == "J Med Res"


def test_phrase_and_other_languages():
    assert ab("Journal of the United States Learning") == "J U S Learn"
    assert ab("Ergebnisse Journal") == "Ergebnisse J"     # German-only entries are not used


def test_no_list():
    assert iso4.abbreviate("Journal of Things", None) is not None  # conftest's sample stands in for the list
