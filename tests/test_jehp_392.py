"""Owner, 2026-11-09, jehp_392_26 ("why so many AQ"): fewer, better queries."""
from edit_os import docmodel, queries, refengine
from edit_os.refengine import Parsed

from test_jehp_citations import _refs, make
from test_jehp_review9 import jehp_style


def test_complete_entry_not_queried_as_not_found():
    complete = "Karimi A. A survey and analysis of young people's attitudes toward marriage and childbearing. " \
               "Strategic Studies on Youth and Sports 2021;20:275-92."
    partial = "Enjezab B, Bokaei M, Salmanabad F. Attitude and factors affecting intention of childbearing in " \
              "married women. Journal of Shahid Sadoughi University of Medical Sciences 2023."
    p = Parsed(complete, authors=["Karimi A"], title="A survey and analysis of young people's attitudes", year="2021")
    assert queries.complete_entry(complete, p)
    q = Parsed(partial, authors=["Enjezab B"], title="Attitude and factors affecting intention", year="2023")
    assert not queries.complete_entry(partial, q)


def test_authors_put_in_the_record_order_with_the_authors_spelling():
    p = Parsed("x", authors=["Vatanparast Z", "Peyman N", "Esmaeili H", "Gholian Avval M"])

    class R:
        authors = [("Vatanparast", "Z"), ("Peyman", "N"), ("Gholian Avval", "M"), ("Esmeili", "H")]
    assert refengine.in_record_order(p, R).authors == ["Vatanparast Z", "Peyman N", "Gholian Avval M", "Esmaeili H"]


def test_manufacturer_query_in_the_methods_not_the_abstract(tmp_path):
    path = make(tmp_path, ["Abstract", "Background: Childbearing matters. Methods: Data were analyzed in SPSS version 27. "
                           "Results: Scores rose. Conclusion: It helps.", "Keywords: Childbearing, Women",
                           "Introduction", "Text of the introduction.", "Methods",
                           "Data were analyzed with SPSS version 27 for all outcomes."] + _refs("A A. One. J T 2020;1:1-2."))
    ms = docmodel.load(path)
    qs = [q for q in queries.collect(ms, jehp_style()).queries if q.check == "text.manufacturer"]
    assert len(qs) == 1 and ms.by_id(qs[0].para_id).text.startswith("Data were analyzed with SPSS")
