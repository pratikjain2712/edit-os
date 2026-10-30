import re
import zipfile

from edit_os import package_fix

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CHART = ('<w:r><w:drawing><wp:inline><wp:docPr id="{id}" name="Chart"/><a:graphic><a:graphicData '
         'uri="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart r:id="rId5"/></a:graphicData>'
         '</a:graphic></wp:inline></w:drawing></w:r>')
CT = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="xml" ContentType="application/xml"/>'
      '<Override PartName="/word/document.xml" ContentType="doc"/>'
      '<Override PartName="/word/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>'
      '<Override PartName="/word/embeddings/book1.xlsx" ContentType="xlsx"/></Types>')


def make(path, n_uses):
    body = "".join(f"<w:p>{CHART.format(id=1)}</w:p>" for _ in range(n_uses))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
        z.writestr("word/_rels/document.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId5" Type="http://x/chart" Target="charts/chart1.xml"/></Relationships>')
        z.writestr("word/charts/chart1.xml", "<c:chartSpace/>")
        z.writestr("word/charts/_rels/chart1.xml.rels",
                   '<Relationships><Relationship Id="rId1" Type="http://x/package" Target="../embeddings/book1.xlsx"/>'
                   '</Relationships>')
        z.writestr("word/embeddings/book1.xlsx", b"PK-xlsx")


def test_a_chart_used_twice_gets_its_own_copy(tmp_path):
    # a figure moved as a tracked change: the deleted original and the inserted copy pointed at one chart, and
    # Word refused the whole file (JEHP_73_26)
    p = str(tmp_path / "t.docx")
    make(p, 2)
    assert package_fix.fix(p) == {"charts_copied": 1, "drawing_ids_renumbered": 1, "local_links_removed": 0, "tables_separated": 0}
    z = zipfile.ZipFile(p)
    doc = z.read("word/document.xml").decode()
    rids = re.findall(r'c:chart r:id="([^"]+)"', doc)
    assert rids[0] == "rId5" and rids[1] != "rId5"
    assert len(set(re.findall(r'docPr id="(\d+)"', doc))) == 2
    rels = z.read("word/_rels/document.xml.rels").decode()
    assert f'Id="{rids[1]}" Type="http://x/chart" Target="charts/chart2.xml"' in rels
    assert z.read("word/charts/chart2.xml") == b"<c:chartSpace/>"
    assert 'Target="../embeddings/book2.xlsx"' in z.read("word/charts/_rels/chart2.xml.rels").decode()
    assert z.read("word/embeddings/book2.xlsx") == b"PK-xlsx"
    ct = z.read("[Content_Types].xml").decode()
    assert 'PartName="/word/charts/chart2.xml"' in ct and 'PartName="/word/embeddings/book2.xlsx"' in ct
    assert package_fix.fix(p) == {"charts_copied": 0, "drawing_ids_renumbered": 0, "local_links_removed": 0, "tables_separated": 0}


def test_a_file_without_shared_charts_is_left_as_it_is(tmp_path):
    p = str(tmp_path / "t.docx")
    make(p, 1)
    before = open(p, "rb").read()
    assert package_fix.fix(p) == {"charts_copied": 0, "drawing_ids_renumbered": 0, "local_links_removed": 0, "tables_separated": 0}
    assert open(p, "rb").read() == before


def test_chart_link_to_the_authors_computer_removed(tmp_path):
    # Protected View refused a file whose chart linked to "file:///C:/Users/.../data.xlsx" (JEHP_73_26)
    p = str(tmp_path / "t.docx")
    make(p, 1)
    with zipfile.ZipFile(p) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    parts["word/charts/chart1.xml"] = (b'<c:chartSpace><c:chart/><c:externalData r:id="rId2"><c:autoUpdate val="0"/>'
                                       b'</c:externalData></c:chartSpace>')
    parts["word/charts/_rels/chart1.xml.rels"] = (
        b'<Relationships><Relationship Id="rId2" Type="http://x/oleObject" '
        b'Target="file:///C:\\Users\\U\\data.xlsx" TargetMode="External"/></Relationships>')
    with zipfile.ZipFile(p, "w") as z:
        for n, b in parts.items():
            z.writestr(n, b)
    assert package_fix.fix(p)["local_links_removed"] == 1
    z = zipfile.ZipFile(p)
    assert z.read("word/charts/chart1.xml") == b"<c:chartSpace><c:chart/></c:chartSpace>"
    assert b"data.xlsx" not in z.read("word/charts/_rels/chart1.xml.rels")


def _doc_with(path, body):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
        z.writestr("word/_rels/document.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')


TBL = "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>x</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"


def test_touching_tables_are_separated(tmp_path):
    # Word shows tables with no paragraph between them as one table (tables moved to the end one after another)
    p = str(tmp_path / "clean.docx")
    _doc_with(p, TBL + TBL + "<w:p/>" + TBL)
    assert package_fix.fix(p)["tables_separated"] == 1
    doc = zipfile.ZipFile(p).read("word/document.xml").decode()
    assert "</w:tbl><w:p/><w:tbl>" in doc and "</w:tbl></w:tbl>" not in doc
    p = str(tmp_path / "tracked.docx")
    _doc_with(p, '<w:p><w:ins w:id="7" w:author="A"><w:r><w:t>y</w:t></w:r></w:ins></w:p>' + TBL + TBL)
    assert package_fix.fix(p)["tables_separated"] == 1
    doc = zipfile.ZipFile(p).read("word/document.xml").decode()
    assert re.search(r'</w:tbl><w:p><w:pPr><w:rPr><w:ins w:id="8" w:author="Edit OS"', doc)
