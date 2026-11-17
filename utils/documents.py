"""Text out of document files that are not plain text (PDF, Word).

Shared by the tools that read files and the memory layer that imports documents, which may not import
each other.
"""

from __future__ import annotations

import io
from pathlib import Path


def file_text(path: Path) -> str:
    """The text in a file: a PDF's text layer, a Word document's paragraphs, or a text file as it is.

    Raises ValueError for a PDF with no text layer and for any other binary file, so a
    caller says so instead of printing nothing or bytes.
    """
    content = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = pdf_text(content)
        if not text:
            raise ValueError(f"{path} has no text layer (a scan?), so it cannot be read as text.")
        return text
    if suffix == ".docx":
        return docx_text(content)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path} is a binary file, not text.") from None


def pdf_text(content: bytes) -> str:
    """The text of a PDF, page by page. An empty string for a PDF with no text layer (a scan)."""
    from pypdf import PdfReader  # type: ignore[import-untyped]

    reader = PdfReader(io.BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


def docx_text(content: bytes) -> str:
    """The text of a Word document, one paragraph per line."""
    from docx import Document  # type: ignore[import-untyped]

    document = Document(io.BytesIO(content))
    return "\n".join(p.text for p in document.paragraphs if p.text.strip()).strip()
