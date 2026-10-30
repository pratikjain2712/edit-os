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
    assert package_fix.fix(p) == {"charts_copied": 1, "drawing_ids_renumbered": 1}
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
    assert package_fix.fix(p) == {"charts_copied": 0, "drawing_ids_renumbered": 0}  # idempotent


def test_a_file_without_shared_charts_is_left_as_it_is(tmp_path):
    p = str(tmp_path / "t.docx")
    make(p, 1)
    before = open(p, "rb").read()
    assert package_fix.fix(p) == {"charts_copied": 0, "drawing_ids_renumbered": 0}
    assert open(p, "rb").read() == before
