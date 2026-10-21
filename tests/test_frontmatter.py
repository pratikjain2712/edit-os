import zipfile

import pytest
from docx import Document

from edit_os import docmodel, frontmatter as fm, queries, rules, style, tracked
from edit_os.changes import ChangeSet, Status

PORTAL_XML = b"""<?xml version="1.0" encoding="UTF-8"?><manuscript>
<title>Effect of Valsalva maneuver on intraocular pressure</title>
<articletype>Original Article</articletype>
<submission_date>04-Jan-26</submission_date><revision_date> </revision_date><Accepted>01-Oct-26</Accepted>
<authors>
<author><title>Dr</title><fname>Asha</fname><mname> </mname><lname>Kumar</lname><email>asha@example.org</email>
<department>Ophthalmology</department><institute>City Eye Hospital</institute><city>Chennai</city>
<state>Tamil Nadu</state><country> </country><is_corresponding>Y</is_corresponding></author>
<author><title>Mr</title><fname>ravi</fname><mname>shankar</mname><lname>rao</lname><email>ravi@example.org</email>
<department>Ophthalmology</department><institute>City Eye Hospital</institute><city>Chennai</city>
<state>Tamil Nadu</state><country> </country><is_corresponding>N</is_corresponding></author>
</authors></manuscript>"""

JATS_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<article article-type="Case Report"><front><article-meta>
<article-categories><subj-group subj-group-type="Article Type"><subject>Case Report</subject></subj-group></article-categories>
<title-group><article-title>A rare case</article-title><alt-title alt-title-type="running-head">Rare case</alt-title></title-group>
<contrib-group><contrib contrib-type="author" corresp="yes"><name><surname>Nair.</surname><given-names>Suresh</given-names>
<prefix>Dr</prefix></name><email>s@example.org</email><xref ref-type="aff" rid="aff1"/></contrib></contrib-group>
<aff id="aff1"><institution>SCTIMST, Trivandrum</institution><country>India</country></aff>
<history><date date-type="received"><day>09</day><month>10</month><year>2025</year></date>
<date date-type="rev-recd"><day>01</day><month>02</month><year>2026</year></date>
<date date-type="rev-recd"><day>01</day><month>05</month><year>2026</year></date>
<date date-type="accepted"><day>09</day><month>10</month><year>2026</year></date></history>
</article-meta></front></article>"""

TITLE_PAGE = [
    "ORIGINAL ARTICLE",
    "Title: Effect of Valsalva Maneuver on Intraocular Pressure in OCT Study.",
    "Running title: Valsalva and IOP.",
    "Dr. Asha Kumar MS1, RAVI SHANKAR RAO MD1 and Jan van der Berg PhD2",
    "1Senior Resident, Department of ophthalmology, City Eye Hospital, Chennai, Tamil Nadu. Email: asha@example.org",
    "2Department of Physiology, Erasmus University, Rotterdam, Netherlands",
    "Corresponding author: Dr. Asha Kumar, Department of Ophthalmology, City Eye Hospital, 12 Main Road, "
    "Chennai 600006, Tamil Nadu. Phone +91 99999 99999. Email: asha@example.org",
]


def claude_fields(para_ids=("M1", "M2", "M3", "M4", "M5", "M6", "M7")):
    """What Claude would return for TITLE_PAGE (plus one invented author and affiliation part)."""
    roles = ["article_type", "title", "running_title", "authors", "affiliation", "affiliation", "correspondence"]
    return {
        "article_type": "ORIGINAL ARTICLE",
        "title": "Effect of Valsalva Maneuver on Intraocular Pressure in OCT Study.",
        "running_title": "Valsalva and IOP.",
        "authors": [
            {"given_names": "Asha", "surname": "Kumar", "prefix": "Dr", "affiliations": [1], "corresponding": True},
            {"given_names": "RAVI SHANKAR", "surname": "RAO", "prefix": "", "affiliations": [1], "corresponding": False},
            {"given_names": "Jan", "surname": "van der Berg", "prefix": "", "affiliations": [2], "corresponding": False},
            {"given_names": "Invented", "surname": "Person", "prefix": "", "affiliations": [2], "corresponding": False},
        ],
        "affiliations": [
            {"id": 1, "department": "Senior Resident, Department of ophthalmology", "institution": "City Eye Hospital",
             "city": "Chennai", "state": "Tamil Nadu", "country": ""},
            {"id": 2, "department": "Department of Physiology", "institution": "Erasmus University",
             "city": "Rotterdam", "state": "Zuid-Holland", "country": "Netherlands"},
        ],
        "correspondence": {"prefix": "Dr", "given_names": "Asha", "surname": "Kumar",
                           "address_parts": ["Department of Ophthalmology", "City Eye Hospital", "12 Main Road",
                                             "Phone +91 99999 99999"],
                           "city": "Chennai", "state": "Tamil Nadu", "postal_code": "600006", "country": "",
                           "email": "asha@example.org"},
        "proper_nouns": ["Valsalva", "OCT", "IOP"],
        "paragraphs": [{"id": i, "role": r} for i, r in zip(para_ids, roles)],
    }


def make(tmp_path, paras):
    doc = Document()
    for t in paras:
        doc.add_paragraph(t)
    path = tmp_path / "m.docx"
    doc.save(path)
    return str(path)


@pytest.fixture
def ijo():
    st = style.load("ijo")
    st.settings["references"]["lookup"] = False
    st.settings["sections"]["keywords"]["add_missing"] = False  # front matter only; key words are A4's
    return st


def test_recase():
    keep = ["Valsalva", "OCT"]
    assert fm.recase("Effect of Valsalva Maneuver on IOP: a Study Using OCT", "sentence", keep) == \
        "Effect of Valsalva maneuver on IOP: A study using OCT"
    assert fm.recase("EFFECT OF VALSALVA MANEUVER IN OCT", "sentence", keep) == "Effect of Valsalva maneuver in OCT"
    assert fm.recase("stimulus-sensitive myoclonus in the young – a case report", "title", []) == \
        "Stimulus-Sensitive Myoclonus in the Young – A Case Report"
    assert fm.clean_title("Title:  “Fern like deposits in dystrophy .”") == "Fern like deposits in dystrophy"


def test_names():
    cfg = {"initial_period": True}
    assert fm.format_name(fm.Author("AN", "Swamy"), cfg) == ("A. N. Swamy", False)
    assert fm.format_name(fm.Author("A.N.", "Swamy"), {"initial_period": False}) == ("A N Swamy", False)
    assert fm.format_name(fm.Author("Appulingam Narayana", "Swamy"), cfg, abbreviate=True)[0] == "Appulingam N. Swamy"
    assert fm.format_name(fm.Author("Anjum Syed Khan", "Joad"), cfg, abbreviate=True)[0] == "Anjum S. K. Joad"
    assert fm.format_name(fm.Author("MEERA", "IYER"), cfg)[0] == "Meera Iyer"
    assert fm.format_name(fm.Author("jan", "van der berg"), cfg)[0] == "Jan van der Berg"
    assert fm.cite_name(fm.Author("Asha M. E.", "Kumar")) == "Kumar AME"
    assert fm.strip_designation("Senior resident , Department of ophthalmology") == "Department of ophthalmology"
    assert fm.strip_designation("Consultant cornea and general ophthalmology") == "cornea and general ophthalmology"
    assert fm.strip_designation("Head and Neck Surgery") == "Head and Neck Surgery"


def test_xml_formats():
    m = fm.parse_xml(PORTAL_XML)
    assert m.article_type == "Original Article" and m.dates["received"].year == 2026 and "revised" not in m.dates
    assert m.authors[1]["given"] == "ravi shankar" and m.authors[0]["corresponding"]
    j = fm.parse_xml(JATS_XML)
    assert (j.article_type, j.running_title, j.authors[0]["surname"]) == ("Case Report", "Rare case", "Nair")
    assert j.dates["revised"].month == 5  # the latest revision
    assert j.authors[0]["aff"]["institution"] == "SCTIMST, Trivandrum"


def test_verify_drops_what_is_not_in_the_text():
    f = fm.verify(claude_fields(), "\n".join(TITLE_PAGE), {f"M{i}": f"p{i:04d}" for i in range(1, 8)})
    assert [a.surname for a in f.authors] == ["Kumar", "RAO", "van der Berg"]
    assert any("Invented" in n for n in f.notes)
    assert f.affiliations[1]["department"] == "Department of ophthalmology"  # designation removed by code
    assert f.affiliations[2]["state"] == ""  # "Zuid-Holland" is not in the text
    assert "Phone +91 99999 99999" not in f.correspondence["address_parts"]
    assert f.roles["p0002"] == "title"


def test_front_matter_in_the_manuscript(tmp_path, monkeypatch, ijo):
    path = make(tmp_path, TITLE_PAGE + ["Abstract", "Purpose: To test."])
    monkeypatch.setattr(fm, "split", lambda text: claude_fields())
    ms = docmodel.load(path)
    ms.front_sources = fm.Sources(meta=fm.parse_xml(PORTAL_XML))
    pl = fm.plan(ms, ijo)
    text = {b.role: b.text for b in pl.blocks}
    assert text["running_head"] == "<RH>Kumar, et al.: Valsalva and IOP"
    assert text["article_type"] == "Original Article"
    assert text["title"] == "Effect of Valsalva maneuver on intraocular pressure in OCT study"
    assert text["authors"] == "Asha Kumar, Ravi S Rao, Jan van der Berg1"  # IJO: middle initial, no period
    assert text["affiliations"] == ("Department of Ophthalmology, City Eye Hospital, Chennai, Tamil Nadu, India, "
                                    "1Department of Physiology, Erasmus University, Rotterdam, Netherlands")
    corr = [b.text for b in pl.blocks if b.role == "correspondence"]
    assert corr == ["Correspondence to: Dr. Asha Kumar,",
                    "Department of Ophthalmology, City Eye Hospital, 12 Main Road, Chennai - 600 006, Tamil Nadu, India.",
                    "E-mail: asha@example.org"]
    assert text["dates"] == "Submitted: 04-Jan-2026\tRevised: ???\tAccepted: 01-Oct-2026"
    assert text["cite"] == ("Cite this article as: Kumar A, Rao RS, van der Berg J. Effect of Valsalva maneuver on "
                            "intraocular pressure in OCT study. Indian J Ophthalmol XX;XX:XX.")
    assert not pl.queries  # running head, type and full names all present

    # Through the rules: the existing paragraphs are rewritten in place, the running-title line removed.
    cs = rules.run_all(ms, ijo)
    for c in cs.changes:
        c.status = Status.ACCEPTED
    for n in pl.inserts:
        n.status = Status.ACCEPTED
    out = docmodel.load(path)
    tracked.render(out, cs, tracked=False)
    fm.render_inserts(out, pl.inserts, tracked=False)
    out.save(tmp_path / "clean.docx")
    got = [p.text for p in docmodel.load(str(tmp_path / "clean.docx")).paragraphs if p.text]
    assert got[0] == "<RH>Kumar, et al.: Valsalva and IOP"
    assert got[1:5] == [text["article_type"], text["title"], text["authors"], text["affiliations"]]
    assert got[5:8] == corr and got[8] == text["dates"].replace("\t", "\t") and got[9] == text["cite"]
    assert got[10:] == ["Abstract", "Purpose: To test."]
    assert "Running title" not in " ".join(got)

    # Tracked output: inserted paragraphs are marked, authors are bold with superscript links.
    ms2 = docmodel.load(path)
    tracked.render(ms2, cs, tracked=True)
    fm.render_inserts(ms2, pl.inserts, tracked=True)
    ms2.save(tmp_path / "tracked.docx")
    xml = zipfile.ZipFile(tmp_path / "tracked.docx").read("word/document.xml").decode()
    assert xml.count("<w:pPr><w:rPr><w:ins ") == len(pl.inserts)
    assert '<w:vertAlign w:val="superscript"/>' in xml and "<w:tab/>" in xml


def test_missing_running_head_and_type_are_queried(tmp_path, monkeypatch, ijo):
    raw = claude_fields()
    raw.update(running_title="", article_type="")
    monkeypatch.setattr(fm, "split", lambda text: raw)
    paras = [t for t in TITLE_PAGE if not t.startswith(("Running", "ORIGINAL"))]
    path = make(tmp_path, paras + ["Introduction", "Materials and Methods", "Results", "Discussion", "Conclusion"])
    ms = docmodel.load(path)
    for item in raw["paragraphs"]:
        item["role"] = {"M1": "title", "M2": "authors", "M3": "affiliation", "M4": "affiliation",
                        "M5": "correspondence"}.get(item["id"], "body")
    pl = fm.plan(ms, ijo, fm.Sources())
    assert [q.check for q in pl.queries] == ["front.confirm_type", "front.running_head"]
    assert pl.blocks[0].text == "<RH>Kumar, et al.: ???"
    assert pl.blocks[1].text == "Original Article"
    qs = pl.add_queries(queries.QuerySet(), ms)
    assert [q.text for q in qs.queries] == ["Kindly confirm article type.", "Kindly provide running head."]


def test_xml_only_without_claude(tmp_path, ijo):
    path = make(tmp_path, ["Abstract", "Body text."])
    ms = docmodel.load(path)
    pl = fm.plan(ms, style.load("neurol"), fm.Sources(meta=fm.parse_xml(JATS_XML)))
    text = [b.text for b in pl.blocks]
    assert text[:4] == ["<RH>Nair: Rare case", "Case Report", "A Rare Case", "Suresh Nair"]
    assert "Address for correspondence: Dr. Suresh Nair," in text
    assert text[-4:-1] == ["Received: 09-10-2025", "Revised: 01-05-2026", "Accepted: 09-10-2026"]
    assert text[-1] == "How to cite this article: Nair S. A rare case. Neurol India XX;XX:XX."
    assert not pl.changes and all(n.anchor == "p0001" and n.where == "before" for n in pl.inserts)


def test_no_sources_no_proposals(tmp_path, ijo):
    ms = docmodel.load(make(tmp_path, ["Some title", "Abstract", "Body."]))
    assert fm.plan(ms, ijo).changes == [] and fm.plan(ms, ijo).inserts == []
    assert isinstance(rules.run_all(ms, ijo), ChangeSet)
