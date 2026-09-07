"""Getting text out of a statement PDF (spec §5.2).

`pikepdf` decrypts in memory and `pdftotext -layout` reads the bytes from stdin,
so no decrypted file is written and no password ever reaches argv, where
anything that can read `/proc` could see it.
"""

import shutil
from io import BytesIO

import pikepdf
import pytest
from aegis.services import statements
from aegis.services.statements import StatementLocked, pdf_text

PASSWORD = "SPEC0704"


def _pdf(text: str = "HELLO", password: str | None = None) -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 200))
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.Contents = pdf.make_stream(f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode())
    buf = BytesIO()
    if password is None:
        pdf.save(buf)
    else:
        pdf.save(buf, encryption=pikepdf.Encryption(user=password, owner=password))
    return buf.getvalue()


@pytest.fixture
def captured(monkeypatch):
    """Stand in for poppler and record exactly what bytes it was handed."""
    seen: list[bytes] = []

    def fake(data: bytes) -> str:
        seen.append(data)
        return "EXTRACTED"

    monkeypatch.setattr(statements, "_pdftotext", fake)
    return seen


def test_an_unencrypted_pdf_is_passed_through_untouched(captured):
    data = _pdf()
    assert pdf_text(data) == "EXTRACTED"
    assert captured == [data]


def test_the_candidates_are_tried_in_order_until_one_opens_the_file(captured):
    data = _pdf(password=PASSWORD)
    assert pdf_text(data, ["WRONGPASS", PASSWORD]) == "EXTRACTED"
    # Poppler was handed decrypted bytes, once, and never the original.
    assert len(captured) == 1
    assert captured[0] != data
    with pikepdf.open(BytesIO(captured[0])) as opened:  # opens with no password
        assert len(opened.pages) == 1


def test_a_statement_no_candidate_opens_raises_without_naming_the_attempt(captured):
    with pytest.raises(StatementLocked) as caught:
        pdf_text(_pdf(password=PASSWORD), ["WRONGPASS", "ALSOWRONG"])
    assert PASSWORD not in str(caught.value)
    assert "WRONGPASS" not in str(caught.value)
    assert captured == []  # nothing was extracted from a file we could not open


def test_an_encrypted_statement_with_no_candidates_at_all_is_locked(captured):
    with pytest.raises(StatementLocked):
        pdf_text(_pdf(password=PASSWORD))


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler-utils not installed")
def test_end_to_end_a_password_protected_pdf_becomes_text():
    assert "HELLO" in pdf_text(_pdf("HELLO", password=PASSWORD), ["NOPE", PASSWORD])


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler-utils not installed")
def test_pdftotext_is_never_given_the_password_on_the_command_line(monkeypatch):
    seen: list[list[str]] = []
    real_run = statements.subprocess.run

    def spy(argv, **kwargs):
        seen.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(statements.subprocess, "run", spy)
    pdf_text(_pdf(password=PASSWORD), [PASSWORD])
    assert seen == [["pdftotext", "-layout", "-", "-"]]
