"""Reading an attachment costs a bounded amount of work, however large the file is.

Regression for: the extractors parsed EVERY page/row/paragraph and only then cut the text to MAX_CHARS, so a
10 MB PDF or a million-row sheet could pin the (single-core) server for seconds to keep 8,000 characters."""

import io
from unittest import mock

from django.test import SimpleTestCase

from chat import document_extraction as de


class FieldLike:
    """What the extractors need from a FieldFile: .open("rb") as a context manager."""

    def __init__(self, data):
        self.data = data

    def open(self, mode="rb"):
        return io.BytesIO(self.data)


def pdf_with_pages(count):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=72, height=72)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class PdfBoundsTests(SimpleTestCase):
    def test_a_huge_pdf_stops_once_it_has_enough_text(self):
        data = pdf_with_pages(400)
        with mock.patch("pypdf._page.PageObject.extract_text", return_value="x" * 2000) as extract:
            text = de.extract_text(FieldLike(data), "pdf")
        self.assertLessEqual(extract.call_count, 6)  # 8000 characters are reached on the 5th page
        self.assertTrue(text.endswith("[...truncated...]"))
        self.assertLessEqual(len(text), de.MAX_CHARS + 30)

    def test_a_pdf_with_no_text_is_still_capped_by_page_count(self):
        data = pdf_with_pages(300)
        with mock.patch("pypdf._page.PageObject.extract_text", return_value="") as extract:
            self.assertIsNone(de.extract_text(FieldLike(data), "pdf"))
        self.assertEqual(extract.call_count, de.MAX_PDF_PAGES)

    def test_a_small_pdf_is_read_in_full(self):
        data = pdf_with_pages(3)
        with mock.patch("pypdf._page.PageObject.extract_text", side_effect=["one", "two", "three"]):
            self.assertEqual(de.extract_text(FieldLike(data), "pdf"), "one\n\ntwo\n\nthree")

    def test_a_corrupt_pdf_degrades_to_none(self):
        self.assertIsNone(de.extract_text(FieldLike(b"%PDF-1.4 not really"), "pdf"))


class SpreadsheetAndDocumentBoundsTests(SimpleTestCase):
    def test_a_huge_sheet_is_cut_early_and_bounded(self):
        import openpyxl

        book = openpyxl.Workbook()
        sheet = book.active
        for i in range(30000):
            sheet.append([f"row {i}", "y" * 40, i])
        out = io.BytesIO()
        book.save(out)
        text = de.extract_text(FieldLike(out.getvalue()), "xlsx")
        self.assertLessEqual(len(text), de.MAX_CHARS + 30)
        self.assertLess(len(text.splitlines()), 400)
        self.assertIn("# Sheet:", text)
        self.assertIn("row 0", text)
        self.assertNotIn("row 29999", text)

    def test_a_sheet_of_empty_rows_is_capped_by_row_count(self):
        import openpyxl

        book = openpyxl.Workbook()
        book.active.append(["only", "one"])
        book.active.cell(row=20000, column=1, value="far away")
        out = io.BytesIO()
        book.save(out)
        text = de.extract_text(FieldLike(out.getvalue()), "xlsx")
        self.assertIn("only | one", text)
        self.assertNotIn("far away", text)  # beyond MAX_SHEET_ROWS

    def test_a_huge_document_is_cut_early_and_bounded(self):
        import docx

        document = docx.Document()
        for i in range(6000):
            document.add_paragraph(f"paragraph {i} " + "z" * 30)
        out = io.BytesIO()
        document.save(out)
        text = de.extract_text(FieldLike(out.getvalue()), "docx")
        self.assertLessEqual(len(text), de.MAX_CHARS + 30)
        self.assertIn("paragraph 0 ", text)
        self.assertNotIn("paragraph 5999", text)

    def test_a_small_xlsx_is_readable_at_all(self):
        """Regression: rows were iterated after the file was closed, so EVERY .xlsx came back unreadable."""
        import openpyxl

        book = openpyxl.Workbook()
        book.active.title = "Budget"
        book.active.append(["item", "cost"])
        book.active.append(["licence", 120])
        out = io.BytesIO()
        book.save(out)
        self.assertEqual(
            de.extract_text(FieldLike(out.getvalue()), "xlsx"), "# Sheet: Budget\nitem | cost\nlicence | 120"
        )

    def test_small_files_are_unchanged(self):
        import docx

        document = docx.Document()
        document.add_paragraph("hello world")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "a"
        table.rows[0].cells[1].text = "b"
        out = io.BytesIO()
        document.save(out)
        self.assertEqual(de.extract_text(FieldLike(out.getvalue()), "docx"), "hello world\na | b")

    def test_plain_text_is_still_bounded_to_the_first_bytes(self):
        text = de.extract_text(FieldLike(b"a" * 100_000), "txt")
        self.assertLessEqual(len(text), de.MAX_CHARS + 30)
