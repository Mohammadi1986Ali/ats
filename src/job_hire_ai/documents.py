import io
import re
import zipfile
from pathlib import PurePosixPath

import pymupdf
from docx import Document

from job_hire_ai.errors import ATSError
from job_hire_ai.storage import LocalStorage


class FileValidator:
    def __init__(self, storage: LocalStorage):
        self.storage = storage

    def validate(self, filename: str, data: bytes) -> tuple[str, str]:
        if (
            not filename
            or len(filename) > 255
            or filename.strip() != filename
            or re.search(r"[\\/:<>|?*\x00-\x1f\x7f]", filename)
            or filename in {".", ".."}
            or filename.endswith(".")
        ):
            raise ATSError("Unsafe filename: paths and reserved characters are not allowed.")
        extension = PurePosixPath(filename).suffix.lower().lstrip(".")
        if extension not in {"pdf", "docx"}:
            raise ATSError("Unsupported file type. Upload PDF or DOCX documents.")
        self.storage.validate_size(data)
        if extension == "pdf":
            status = self._pdf(data)
        else:
            status = self._docx(data)
        return extension, status

    @staticmethod
    def _pdf(data: bytes) -> str:
        try:
            if not data.startswith(b"%PDF-"):
                raise ValueError("Invalid PDF signature")
            with pymupdf.open(stream=data, filetype="pdf") as document:
                if document.needs_pass or document.is_repaired or document.page_count == 0:
                    raise ValueError("Encrypted, repaired or empty PDF")
                has_text = False
                for page in document:
                    has_text = bool(page.get_text().strip()) or has_text
                return "Available" if has_text else "Unavailable"
        except Exception as exc:
            raise ATSError("Invalid, corrupted or password-protected PDF.") from exc

    @staticmethod
    def _docx(data: bytes) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 2000 or sum(x.file_size for x in entries) > 50 * 1024 * 1024:
                    raise ValueError("Archive expansion limit exceeded")
                for entry in entries:
                    if entry.flag_bits & 1:
                        raise ValueError("Encrypted archive")
                    name = entry.filename.lower()
                    if name.endswith("vbaproject.bin"):
                        raise ValueError("Macro content")
                if archive.testzip() is not None:
                    raise ValueError("Corrupted archive")
            document = Document(io.BytesIO(data))
            has_text = any(p.text.strip() for p in document.paragraphs)
            has_text = has_text or any(
                cell.text.strip()
                for table in document.tables
                for row in table.rows
                for cell in row.cells
            )
            return "Available" if has_text else "Unavailable"
        except Exception as exc:
            raise ATSError("Invalid or corrupted DOCX, or unsafe document archive.") from exc
