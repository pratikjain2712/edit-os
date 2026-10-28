"""Author queries the IJO / JEHP editors raise on the front matter, the first-author block, headings and books, in
their wording (front_matter.queries, first_author.photo_query, headings.rename_query, references.other.edition_query).
All names, places and texts are invented."""

from datetime import datetime

import pytest
from docx import Document

from edit_os import docmodel, first_author, frontmatter as fm, queries, refs_other, rules_sections, style

XML = b"""<?xml version="1.0" encoding="UTF-8"?><manuscript>
<title>Effect of reading light on eye strain</title>
<articletype>Original Article</articletype>
<authors>
<author><title>Dr</title><fname>Asha</fname><mname>Lakshmi</mname><lname>Kumar</lname><email>asha@example.org</email>
<department>Ophthalmology</department><institute>City Eye Hospital</institute><city>Chennai</city>
<state>Tamil Nadu</state><country>India</country><is_corresponding>Y</is_corresponding></author>
</authors></manuscript>"""

BODY = ["Original Article", "Introduction", "Text.", "Discussion", "More text.", "References",
        "1. Lee K. Back pain in adolescents. Spine. 1997;22:1380-4.", "Figure Legends", "Figure 1: A lesion"]


def postal_on(st):
    st.settings["front_matter"].setdefault("queries", {})["postal_code"] = True
    return st


def load(code, **front):
    st = style.load(code)
    st.settings["references"]["lookup"] = False
    for k, v in front.items():
        st.settings["front_matter"][k] = v
    return st


def blank():
    return docmodel.Manuscript(path="", document=None, paragraphs=[])


def fields(affs, corr=None, authors=(("Asha", "Kumar", [1], ""),)):
    f = fm.Fields(title="Reading light and eye strain")
    f.authors = [fm.Author(g, s, a, corresponding=True, written=w) for g, s, a, w in authors]
    f.affiliations = affs
    f.correspondence = corr or {}
    return f


def checks(qs):
    return [c for c, _ in qs]


def make(tmp_path, paras, name="m.docx"):
    doc = Document()
    for t in paras:
        doc.add_paragraph(t)
    doc.save(tmp_path / name)
    return str(tmp_path / name)


# --- department ---------------------------------------------------------------------------------------------------

def test_department_missing_is_queried_for_ijo_only():
    affs = {1: dict(department="", institution="Lakeview Eye Hospital", city="Riverton", country="Freedonia"),
            2: dict(department="Ophthalmology", institution="Hill Medical College", city="Riverton", country="Freedonia"),
            3: dict(department="Retina Services", institution="Hill Eye Institute", city="Riverton", country="Freedonia"),
            4: dict(department="Division of Allergy", institution="Hill Hospital", city="Riverton", country="Freedonia")}
    f = fields(affs, authors=[("Asha", "Kumar", [1], ""), ("Ravi", "Rao", [2], ""), ("Mina", "Das", [3], ""),
                              ("Tara", "Sen", [4], "")])
    _, qs = fm.build(blank(), f, fm.Meta(), load("ijo"))
    # one query on the affiliation line (its first such affiliation), not one per affiliation
    assert [(c, fmt["_quote"]) for c, fmt in qs if c.startswith("front.department")] == [
        ("front.departments", "Lakeview Eye Hospital, Riverton")]
    assert queries.TEXT["front.departments"] == "Kindly provide department names."
    f.affiliations[3]["department"] = "Retina Unit"
    _, qs = fm.build(blank(), f, fm.Meta(), load("ijo"))
    assert [c for c, _ in qs if c.startswith("front.department")] == ["front.department"]
    assert queries.TEXT["front.department"] == "Kindly provide department name."
    _, qs = fm.build(blank(), f, fm.Meta(), load("jehp"))
    assert not [c for c in checks(qs) if c.startswith("front.department")]


def test_no_department_reads_units():
    assert fm._no_department({"department": "", "institution": "Lakeview Eye Hospital"})
    assert fm._no_department({"department": "Cornea Services"})
    assert fm._no_department({"department": "Hill Retina Institute"})
    assert not fm._no_department({"department": "Ophthalmology"})  # "Department of Ophthalmology"
    assert not fm._no_department({"department": "Endoscopy Unit"})
    assert not fm._no_department({"department": "Clinical Research Centre"})


# --- history dates ------------------------------------------------------------------------------------------------

def test_missing_revised_date_queried_on_its_place():
    meta = fm.Meta(dates={"received": datetime(2026, 1, 5), "accepted": datetime(2026, 3, 9)})
    f = fields({1: dict(department="Ophthalmology", institution="Hill College", city="Riverton", country="Freedonia")})
    blocks, qs = fm.build(blank(), f, meta, load("ijo"))
    dates = next(b.text for b in blocks if b.role == "dates")
    assert dates == "Submitted: 05-Jan-2026\tRevised: ???\tAccepted: 09-Mar-2026"
    q = [fmt for c, fmt in qs if c == "front.missing_date"]
    assert q == [{"what": "revised", "_block": "dates", "_quote": "Revised: ???"}]
    assert queries.TEXT["front.missing_date"].format(what="revised") == "Kindly provide missing revised date."
    full = fm.Meta(dates={"received": datetime(2026, 1, 5), "revised": datetime(2026, 2, 1),
                          "accepted": datetime(2026, 3, 9)})
    assert "front.missing_date" not in checks(fm.build(blank(), f, full, load("ijo"))[1])
    assert "front.missing_date" not in checks(fm.build(blank(), f, meta, load("jehp"))[1])


# --- postal code --------------------------------------------------------------------------------------------------

AFF = {1: dict(department="Department of Nursing", institution="Hill University", city="Riverton", state="North State",
               country="Freedonia")}


def corr(postal="", parts=("Department of Nursing", "Hill University")):
    return dict(given_names="Asha", surname="Kumar", prefix="Dr", address_parts=list(parts), city="Riverton",
                state="North State", postal_code=postal, country="Freedonia", email="asha@example.org")


def address(blocks):
    return " ".join(b.text for b in blocks if b.role == "correspondence")


def test_jehp_keeps_the_correspondence_postal_code_before_the_country():
    blocks, qs = fm.build(blank(), fields(AFF, corr("50123")), fm.Meta(), load("jehp"))
    assert "Hill University, Riverton, North State, 50123, Freedonia." in address(blocks)
    assert "50123" not in next(b.text for b in blocks if b.role == "affiliations")  # S: delete pin/zip (affiliation)
    assert "front.postal_code" not in checks(qs)


def test_jehp_queries_a_missing_postal_code_on_the_country():
    # built for JEHP, off since the owner's 2026-10-28 "no queries the editors would not add" (24 asked vs 8)
    blocks, qs = fm.build(blank(), fields(AFF, corr("")), fm.Meta(), postal_on(load("jehp")))
    q = [fmt for c, fmt in qs if c == "front.postal_code"]
    assert q == [{"_block": "correspondence", "_quote": "Freedonia"}]
    assert queries.TEXT["front.postal_code"] == "Kindly check and provide postal code."
    # one written into the address is a postal code too
    _, qs = fm.build(blank(), fields(AFF, corr("", ("Hill University", "Lake Road 4", "Riverton 50123"))), fm.Meta(),
                     load("jehp"))
    assert "front.postal_code" not in checks(qs)


def test_ijo_keeps_the_correspondence_postal_code_and_never_asks():
    aff = {1: dict(department="Ophthalmology", institution="Hill Eye Hospital", city="Chennai", state="Tamil Nadu",
                   country="India")}
    c = dict(corr("600123"), city="Chennai", state="Tamil Nadu", country="India",
             address_parts=["Hill Eye Hospital"])
    blocks, qs = fm.build(blank(), fields(aff, c), fm.Meta(), load("ijo"))
    assert "Hill Eye Hospital, Chennai - 600 123, Tamil Nadu, India." in address(blocks)
    _, qs = fm.build(blank(), fields(aff, dict(c, postal_code="")), fm.Meta(), load("ijo"))
    assert "front.postal_code" not in checks(qs)


def test_postal_settings_recorded():
    for code in ("ijo", "jehp"):
        st = style.load(code)
        assert st.get("front_matter.delete_postal_codes") is True  # affiliations (S)
        assert st.get("front_matter.correspondence_postal_code") is True  # owner, 2026-10-28: follow the editors
    assert style.load("jehp").get("front_matter.queries.postal_code") is False  # owner, 2026-10-28
    assert not style.load("ijo").get("front_matter.queries.postal_code")
    assert style.load("base").get("front_matter.correspondence_postal_code") is False


# --- corresponding author's designator ----------------------------------------------------------------------------

def test_designator_missing_queried_for_jehp():
    c = dict(corr("50123"), prefix="")
    meta = fm.Meta(authors=[dict(given="Asha", surname="Kumar", prefix="Ms", corresponding=True)])
    blocks, qs = fm.build(blank(), fields(AFF, c), meta, load("jehp"))
    assert "Address for correspondence: Asha Kumar," in address(blocks)
    q = [fmt for k, fmt in qs if k == "front.designator"]
    assert q == [{"_block": "correspondence", "_quote": "Asha Kumar"}]
    assert "front.designator" not in checks(fm.build(blank(), fields(AFF, corr("50123")), meta, load("jehp"))[1])
    assert "front.designator" not in checks(fm.build(blank(), fields(AFF, c), meta, load("ijo"))[1])


# --- author name shortened where the XML has it in full ----------------------------------------------------------

def test_xml_fuller_name():
    xml = [dict(given="Dewa Sari", surname="Wirana"), dict(given="Tanvi", surname="Rajan"),
           dict(given="Moran", surname="Moran"), dict(given="lakshmi", surname="pandiyan"), dict(given="mira", surname="dc")]
    assert fm.xml_fuller_name("Dewa S. Wirana", xml)  # an initial the XML spells out
    assert fm.xml_fuller_name("Tanvi", xml)  # a single name the XML adds a surname to
    assert fm.xml_fuller_name("Lakshmi P", xml)
    assert not fm.xml_fuller_name("Moran", xml)  # the XML only repeats it
    assert not fm.xml_fuller_name("Mira D.C", xml)  # the same initials
    assert not fm.xml_fuller_name("Dewa Sari Wirana", xml)
    assert not fm.xml_fuller_name("Ravi Menon", xml)


def test_xml_name_query_replaces_the_full_name_query():
    meta = fm.Meta(authors=[dict(given="Lakshmi", surname="Pandiyan", prefix="Dr", corresponding=True)])
    f = fields(AFF, corr("50123"), authors=[("Lakshmi", "P", [1], "Lakshmi P")])
    _, qs = fm.build(blank(), f, meta, load("jehp"))
    q = [fmt for c, fmt in qs if c == "front.xml_name"]
    assert q == [{"name": "Lakshmi P", "_block": "authors", "_quote": "Lakshmi P"}]
    assert "front.full_name" not in checks(qs)
    assert queries.TEXT["front.xml_name"].format(name="Lakshmi P") == \
        "Kindly note the Author name provided in XML as “Lakshmi P”. Please check and confirm."
    _, qs = fm.build(blank(), f, meta, load("ijo"))
    assert "front.xml_name" not in checks(qs)
    # expanded from the XML when printed: no note
    f2 = fields(AFF, corr("50123"), authors=[("Lakshmi", "Pandiyan", [1], "L. Pandiyan")])
    assert "front.xml_name" not in checks(fm.build(blank(), f2, meta, load("jehp"))[1])


# --- article type source (off in both journals; the setting works) ----------------------------------------------

def test_type_source_note_names_the_title_page_or_the_xml():
    st = load("jehp", queries={"type_source": True})
    ms = blank()
    ms.front_sources = fm.Sources(first_page=["Type of article: Original Article"])
    f = fields(AFF)
    f.article_type = "Original Article"
    _, qs = fm.build(ms, f, fm.Meta(article_type="Original Article"), st)
    assert [fmt for c, fmt in qs if c == "front.type_source"] == [{"source": "FP", "_block": "article_type"}]
    f.article_type = ""
    _, qs = fm.build(blank(), f, fm.Meta(article_type="Review Article"), st)
    assert [fmt["source"] for c, fmt in qs if c == "front.type_source"] == ["XML"]
    assert queries.TEXT["front.type_source"].format(source="XML") == \
        "Kindly note that the “Article type” provided in XML file has been followed."
    assert "front.type_source" not in checks(fm.build(blank(), f, fm.Meta(article_type="Review Article"),
                                                      load("jehp"))[1])


# --- anchors ----------------------------------------------------------------------------------------------------

def test_front_query_sits_on_the_block_that_holds_its_quote(tmp_path, monkeypatch):
    raw = {"article_type": "", "title": "Reading light", "running_title": "Reading light",
           "authors": [dict(given_names="Asha", surname="Kumar", prefix="Dr", affiliations=[1], corresponding=True)],
           "affiliations": [dict(id=1, department="Department of Nursing", institution="Hill University",
                                 city="Riverton", state="", country="Freedonia")],
           "correspondence": dict(prefix="Dr", given_names="Asha", surname="Kumar",
                                  address_parts=["Department of Nursing", "Hill University"], city="Riverton",
                                  state="", postal_code="", country="Freedonia", email="asha@example.org"),
           "proper_nouns": [], "paragraphs": []}
    monkeypatch.setattr(fm, "split", lambda text: raw)
    ms = docmodel.load(make(tmp_path, ["Abstract", "Background: A study."]))
    ms.front_sources = fm.Sources(first_page=["Reading light", "Asha Kumar", "Hill University, Riverton, Freedonia"],
                                  meta=fm.Meta(dates={"received": datetime(2026, 1, 5)}))
    st = postal_on(load("jehp", correspondence_one_line=False))
    pl = fm.plan(ms, st)
    q = next(q for q in pl.queries if q.check == "front.postal_code")
    ins = next(n for n in pl.inserts if n.id == q.insert_id)
    assert ins.role == "correspondence" and q.quote == "Freedonia" and ins.text[q.start:q.end] == "Freedonia"


# --- first author block --------------------------------------------------------------------------------------------

def first_author_plan(tmp_path, files=()):
    st = style.load("ijo")
    st.settings["references"]["lookup"] = False
    ms = docmodel.load(make(tmp_path, BODY))
    ms.front_sources = fm.Sources(meta=fm.parse_xml(XML), file_names=list(files))
    return first_author.plan(ms, st, fm.plan(ms, st))


def test_first_author_photo_and_designation_on_the_label(tmp_path):
    pl = first_author_plan(tmp_path, ["Manuscript.docx", "Figure 1.jpg", "photo.jpg"])
    label = pl.inserts[0]
    assert [(q.text, q.insert_id, q.quote) for q in pl.insert_queries] == [
        ("Kindly provide photo and designation for the first author.", label.id, "First author:")]
    assert style.load("ijo").get("sections.back_matter.first_author")["photo_query"] is True


def test_first_author_designation_only_with_the_authors_photograph(tmp_path):
    pl = first_author_plan(tmp_path, ["Manuscript.docx", "First author photograph.jpg"])
    assert [q.text for q in pl.insert_queries] == ["Kindly provide designation for the first author."]


def test_first_author_photo_query_off_without_the_setting(tmp_path):
    st = style.load("ijo")
    st.settings["references"]["lookup"] = False
    st.settings["sections"]["back_matter"]["first_author"]["photo_query"] = False
    ms = docmodel.load(make(tmp_path, BODY))
    ms.front_sources = fm.Sources(meta=fm.parse_xml(XML))
    assert first_author.plan(ms, st, fm.plan(ms, st)).insert_queries == []


# --- heading renamed ---------------------------------------------------------------------------------------------

def test_heading_renamed_per_journal_is_queried_for_ijo(tmp_path):
    paras = ["Introduction", "Text.", "Materials and Methods", "We did it.", "Results", "It worked.", "Method",
             "Discussion", "Fine."]
    for code, want in (("ijo", ["Heading has been changed to Methods as per journal specification."]), ("jehp", [])):
        st = style.load(code)
        ms = docmodel.load(make(tmp_path, paras, f"{code}.docx"))
        qs = queries.QuerySet()
        rules_sections.add_queries(ms, st, qs)
        got = [q for q in qs.queries if q.check == "headings.renamed"]
        assert [q.text for q in got] == want
        if got:
            assert ms.by_id(got[0].para_id).text == "Materials and Methods"


# --- books without edition and pages -------------------------------------------------------------------------------

def test_book_without_edition_and_pages():
    s = refs_other.lacks_edition_pages("Rowan K, Dale P. Grouping Things: A Guide. Riverton: Hill Press; 1998.")
    assert s is not None
    assert refs_other.lacks_edition_pages("Rowan K. Grouping Things. 2nd ed. Riverton: Hill Press; 1998.") is None
    assert refs_other.lacks_edition_pages("Rowan K. Grouping. In: Dale P, editor. Things. Riverton: Hill Press; 1998. "
                                          "p. 12-9.") is None
    assert refs_other.lacks_edition_pages("Rowan K, Dale P. Grouping things. J Things. 1998;12:4-9.") is None


def test_book_query_follows_the_setting(tmp_path):
    entry = "Rowan K, Dale P. Grouping Things: A Guide. Riverton: Hill Press; 1998."
    ms = docmodel.load(make(tmp_path, ["Introduction", "Text [1].", "References", "1. " + entry]))
    for on, n in ((False, 0), (True, 1)):
        st = style.load("jehp")
        st.settings["references"]["other"]["edition_query"] = on
        qs = queries.QuerySet()
        refs_other.add_edition_queries(ms, st, qs)
        assert len(qs.queries) == n
    q = qs.queries[0]
    assert q.text == "Kindly check and provide edition and page number details."
    assert ms.by_id(q.para_id).text[q.start:q.end] == "1998"
    assert style.load("jehp").get("references.other.edition_query") is False
