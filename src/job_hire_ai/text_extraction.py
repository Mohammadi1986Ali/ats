"""Loss-conscious document text extraction and bounded, source-aware chunking."""

import io
import re
import unicodedata
from dataclasses import asdict, dataclass

import pymupdf
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from job_hire_ai.documents import FileValidator
from job_hire_ai.errors import ATSError

EXTRACTOR_VERSION = "document-text-v1"


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C"))
    return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines()).strip()


@dataclass
class TextSegment:
    reference: str
    text: str
    original_text: str
    page_number: int | None
    kind: str
    character_start: int = 0


@dataclass
class ExtractedText:
    segments: list[TextSegment]
    warnings: list[str]

    def serialize(self) -> dict:
        return {"segments": [asdict(s) for s in self.segments], "warnings": self.warnings}

    @classmethod
    def deserialize(cls, value: dict) -> "ExtractedText":
        return cls([TextSegment(**s) for s in value["segments"]], value["warnings"])


class TextExtractor:
    def __init__(self, validator: FileValidator, max_characters: int = 250000):
        self.validator, self.max_characters = validator, max_characters

    def extract(self, filename: str, data: bytes) -> ExtractedText:
        kind, _ = self.validator.validate(filename, data)
        segments, warnings = [], []

        def add(reference, raw, page=None, kind="paragraph"):
            normalized = normalize_text(raw)
            if normalized:
                segments.append(TextSegment(reference, normalized, raw, page, kind))
            if sum(len(s.text) for s in segments) > self.max_characters:
                raise ATSError(
                    "Document exceeds the text processing limit; no content was truncated."
                )

        if kind == "pdf":
            with pymupdf.open(stream=data, filetype="pdf") as document:
                if document.page_count > 200:
                    raise ATSError(
                        "PDF exceeds the 200-page processing limit; no text was omitted."
                    )
                for page in document:
                    before_page = len(segments)
                    for index, block in enumerate(page.get_text("blocks", sort=True), 1):
                        if block[6] == 0:
                            raw = block[4]
                            heading = len(raw.strip()) < 100 and raw.strip().isupper()
                            add(
                                f"pdf:p{page.number + 1}:block{index}",
                                raw,
                                page.number + 1,
                                "heading" if heading else "paragraph",
                            )
                    try:
                        for table_index, table in enumerate(page.find_tables().tables, 1):
                            for row_index, row in enumerate(table.extract(), 1):
                                add(
                                    f"pdf:p{page.number + 1}:table{table_index}:row{row_index}",
                                    " | ".join(cell or "" for cell in row),
                                    page.number + 1,
                                    "table",
                                )
                    except Exception:
                        warnings.append(
                            f"PDF page {page.number + 1}: table layout detection unavailable; "
                            "page text blocks retained."
                        )
                    if len(segments) == before_page:
                        warnings.append(
                            f"PDF page {page.number + 1}: no readable text; images need OCR."
                        )
        else:
            document = Document(io.BytesIO(data))
            paragraph_index, table_index = 0, 0
            for item in document.iter_inner_content():
                if isinstance(item, Paragraph):
                    paragraph_index += 1
                    heading = bool(item.style and item.style.name.startswith("Heading"))
                    add(
                        f"docx:paragraph{paragraph_index}",
                        item.text,
                        kind="heading" if heading else "paragraph",
                    )
                elif isinstance(item, Table):
                    table_index += 1
                    for row_index, row in enumerate(item.rows, 1):
                        add(
                            f"docx:table{table_index}:row{row_index}",
                            " | ".join(cell.text for cell in row.cells),
                            kind="table",
                        )
            warnings.append(
                "DOCX text boxes, images, nested tables, headers/footers and embedded objects "
                "are not extracted; review the original CV for omitted content."
            )
        if not segments or not any(any(c.isalnum() for c in s.text) for s in segments):
            raise ATSError("No readable text. Scanned/image-only CVs require future OCR support.")
        offset = 0
        for segment in segments:
            segment.character_start = offset
            offset += len(segment.text) + 2
        if any("\ufffd" in s.text for s in segments):
            warnings.append(
                "Replacement characters in source text: inspect possible encoding loss."
            )
        return ExtractedText(segments, list(dict.fromkeys(warnings)))


def chunk_document(
    source: ExtractedText, max_characters: int, max_chunks: int = 100
) -> list[list[dict]]:
    if max_characters < 100:
        raise ATSError(
            "CV context budget is too small. Increase OLLAMA_NUM_CTX or reduce output tokens."
        )
    units = []
    structured_segments = []
    headings = {
        "experience",
        "employment",
        "employment history",
        "work experience",
        "professional experience",
        "skills",
        "technical skills",
        "education",
        "qualifications",
        "certifications",
        "certificates",
        "projects",
        "languages",
        "language skills",
        "availability",
        "location",
        "domain experience",
    }
    for segment in source.segments:
        # PDFs often place several section headings and paragraphs in one text block.
        # Split on recognized full-line headings without changing source reference/offsets.
        pending = []
        for line in segment.text.splitlines():
            if line.strip().rstrip(":").casefold() in headings:
                if pending:
                    text = "\n".join(pending)
                    structured_segments.append(
                        TextSegment(segment.reference, text, text, segment.page_number, "paragraph")
                    )
                    pending = []
                structured_segments.append(
                    TextSegment(segment.reference, line, line, segment.page_number, "heading")
                )
            else:
                pending.append(line)
        if pending:
            text = "\n".join(pending)
            structured_segments.append(
                TextSegment(segment.reference, text, text, segment.page_number, segment.kind)
            )
    current_section = ""
    for segment in structured_segments:
        if segment.kind == "heading":
            current_section = segment.text.strip().rstrip(":").casefold()
        # Prefer real paragraphs/lines, then sentence/word boundaries for oversized units.
        text = segment.text
        part_limit = max_characters - len(segment.reference) - 100
        if part_limit < 50:
            raise ATSError("Source location metadata exceeds the configured chunk budget.")
        while text:
            cut = len(text)
            if cut > part_limit:
                cut = max(text.rfind("\n", 0, part_limit), text.rfind(". ", 0, part_limit))
                if cut < part_limit // 3:
                    cut = text.rfind(" ", 0, part_limit)
                if cut < 1:
                    raise ATSError(
                        "An unbroken text span exceeds the context limit; no text truncated."
                    )
            part, text = text[:cut], text[cut:]
            if part.strip():
                units.append(
                    {
                        "reference": segment.reference,
                        "page_number": segment.page_number,
                        "kind": segment.kind,
                        "text": part.strip(),
                        "section": current_section,
                    }
                )
    chunks, current, length = [], [], 0
    for unit in units:
        size = len(unit["text"]) + len(unit["reference"]) + 80
        if current and (unit["kind"] == "heading" or length + size > max_characters):
            chunks.append(current)
            current, length = [], 0
        current.append(unit)
        length += size
    if current:
        chunks.append(current)
    if len(chunks) > max_chunks:
        raise ATSError("Document requires too many chunks; processing stopped without truncation.")
    return chunks
