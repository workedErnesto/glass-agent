"""Шаблонные изображения (Pillow) и парсер документов PDF/DOCX/TXT."""
import pytest
from PIL import Image

from app.parsers.base import JsonlDocumentStore
from app.parsers.file_parser import FileParser, extract_text
from app.services import images


def test_render_template_gradient(tmp_path):
    r = images.render_template(tmp_path / "a.png", title="Энергосберегающее стекло Ultraview для фасадов",
                               subtitle="Снижает теплопотери зимой и нагрев летом", footer="salstek.ru",
                               colors=["#102030", "#405060"])
    im = Image.open(r.path)
    assert im.size == (1080, 1080) and im.format == "PNG"
    assert r.meta["generator"] == "pillow_template" and r.meta["ai_generated"] is False


def test_render_for_company_with_media_and_logo(tmp_path):
    media = tmp_path / "acme" / "media"
    media.mkdir(parents=True)
    Image.new("RGB", (800, 600), "orange").save(media / "bg.jpg")
    Image.new("RGBA", (300, 100), (255, 255, 255, 200)).save(media / "logo.png")
    r = images.render_for_company(tmp_path, "acme", {"brand_colors": ["#000000"], "logo": "logo.png"}, "p1",
                                  title="Заголовок", template="story", background="bg.jpg")
    assert Image.open(r.path).size == (1080, 1920) and r.meta["background"] == "bg.jpg" and r.meta["logo"]
    assert images.list_media(tmp_path, "acme") == ["bg.jpg", "logo.png"]


def test_media_path_traversal_blocked(tmp_path):
    (tmp_path / "acme" / "media").mkdir(parents=True)
    (tmp_path / "secret.png").write_bytes(b"x")
    with pytest.raises(images.ImageError):
        images.safe_media_path(tmp_path, "acme", "../../secret.png")


def test_local_generation_reports_unavailable():
    assert images.local_generation_status(None)["available"] is False


def _docx(path, text, table=None):
    import docx
    d = docx.Document()
    d.add_paragraph(text)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for i, row in enumerate(table):
            for k, v in enumerate(row):
                t.cell(i, k).text = v
    d.save(path)


def _pdf(path, text):
    # минимальный PDF с текстовым слоем (латиница — стандартный шрифт Helvetica)
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    page = w.add_blank_page(300, 300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject(
        {NameObject("/F1"): w._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 250 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = w._add_object(stream)
    with open(path, "wb") as f:
        w.write(f)


async def test_file_parser_sync(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "about.txt").write_text("О компании: производим стекло", encoding="utf-8")
    (docs / "copy.md").write_text("О компании: производим стекло", encoding="utf-8")
    (docs / "win1251.txt").write_bytes("Прайс на триплекс".encode("cp1251"))
    _docx(docs / "spec.docx", "Характеристики", table=[["Толщина", "4 мм"], ["Светопропускание", "88%"]])
    _pdf(docs / "cert.pdf", "Certificate GOST 30698")
    (docs / "image.png").write_bytes(b"x")
    assert "Толщина | 4 мм" in extract_text(docs / "spec.docx")

    store = JsonlDocumentStore(tmp_path / "store")
    r = await FileParser().sync(str(docs), store)
    assert r.new == 4 and r.duplicates == 1 and r.errors == []  # copy.md — то же содержимое
    texts = {d.external_id: d.text for d in store.all()}
    assert "GOST 30698" in texts["cert.pdf"] and "триплекс" in texts["win1251.txt"]

    r2 = await FileParser().sync(str(docs), store)
    assert r2.new == 0 and r2.unchanged == 4
    (docs / "about.txt").write_text("О компании: производим стекло с 1969 года", encoding="utf-8")
    (docs / "spec.docx").unlink()
    r3 = await FileParser().sync(str(docs), store)
    assert r3.updated == 1 and any("Удалено" in n for n in r3.notes)
    assert "spec.docx" not in {d.external_id for d in store.all()}
