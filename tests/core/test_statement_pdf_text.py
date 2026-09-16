"""Getting text out of a statement PDF (spec §5.2).

`pdftotext -layout` reads the bytes from stdin, so no file is written. AEGIS
holds no statement passwords: an encrypted statement is `StatementLocked` and
the caller reports the account.
"""

from io import BytesIO

import pikepdf
import pytest
from aegis.services import statements
from aegis.services.statements import StatementError, StatementLocked, pdf_text

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


def test_an_encrypted_statement_is_locked_and_never_extracted(captured):
    with pytest.raises(StatementLocked) as caught:
        pdf_text(_pdf(password=PASSWORD))
    assert PASSWORD not in str(caught.value)
    assert captured == []  # nothing was extracted from a file we could not open


def test_bytes_that_are_not_a_pdf_are_a_statement_error(captured):
    with pytest.raises(StatementError):
        pdf_text(b"not a pdf at all")
    assert captured == []
