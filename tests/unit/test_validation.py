import io
import zipfile

import pymupdf
import pytest
from pydantic import ValidationError

from job_hire_ai.errors import ATSError
from job_hire_ai.schemas import VacancyInput


@pytest.mark.parametrize("title,description", [("", "text"), ("  ", "text"), ("Title", " ")])
def test_required_vacancy_fields(title, description):
    with pytest.raises(ValidationError):
        VacancyInput(title=title, description=description)


@pytest.mark.parametrize(
    "filename,data",
    [
        ("cv.pdf", b"not a pdf"),
        ("cv.docx", b"not a docx"),
        ("cv.txt", b"text"),
        ("../cv.pdf", b"x"),
        ("..\\cv.pdf", b"x"),
        ("C:\\cv.pdf", b"x"),
        ("cv.pdf:evil", b"x"),
        ("cv.pdf", b""),
    ],
)
def test_invalid_documents(ats, filename, data):
    with pytest.raises(ATSError):
        ats.documents.validator.validate(filename, data)


def test_size_limit(ats):
    with pytest.raises(ATSError, match="limit"):
        ats.documents.validator.validate("cv.pdf", b"x" * (10 * 1024**2 + 1))


def test_real_content_validation(ats, pdf, docx):
    assert ats.documents.validator.validate("cv.PDF", pdf) == ("pdf", "Available")
    assert ats.documents.validator.validate("cv.docx", docx) == ("docx", "Available")
    with pytest.raises(ATSError):
        ats.documents.validator.validate("disguised.pdf", docx)
    with pytest.raises(ATSError):
        ats.documents.validator.validate("disguised.docx", pdf)


def test_scanned_pdf(ats):
    with pymupdf.open() as document:
        document.new_page()
        assert ats.documents.validator.validate("scan.pdf", document.tobytes()) == (
            "pdf",
            "Unavailable",
        )


def test_encrypted_pdf(ats):
    with pymupdf.open() as document:
        document.new_page()
        data = document.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret"
        )
    with pytest.raises(ATSError):
        ats.documents.validator.validate("locked.pdf", data)


def test_docx_expansion_limit(ats):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.xml", b"0" * (51 * 1024**2))
    with pytest.raises(ATSError):
        ats.documents.validator.validate("bomb.docx", buffer.getvalue())


@pytest.mark.parametrize("key", ["../cv.pdf", "C:\\cv.pdf", "arbitrary.pdf"])
def test_storage_path_protection(ats, key):
    with pytest.raises(ATSError):
        ats.storage.retrieve(key)


def test_storage_collision_never_deletes_existing_file(ats, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("job_hire_ai.storage.uuid4", lambda: SimpleNamespace(hex="a" * 32))
    key = ats.storage.save(b"original", "pdf")
    with pytest.raises(FileExistsError):
        ats.storage.save(b"replacement", "pdf")
    assert ats.storage.retrieve(key) == b"original"
