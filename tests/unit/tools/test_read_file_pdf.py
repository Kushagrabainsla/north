"""read_file reads the text of a PDF: a resume is the file a job application starts from."""

from __future__ import annotations

from pathlib import Path

from tools.models import ToolInput
from tools.universal.read_file import ReadFileTool


def _pdf_with_text(path: Path, text: str) -> None:
    """A minimal one-page PDF whose content stream draws *text*."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


async def test_a_pdf_is_read_as_its_text(tmp_path) -> None:
    resume = tmp_path / "resume.pdf"
    _pdf_with_text(resume, "Jane Doe - Software Engineer")

    out = await ReadFileTool().run(ToolInput(params={"path": str(resume), "workspace": str(tmp_path)}))

    assert out.success, out.error
    assert "Jane Doe - Software Engineer" in out.data["content"]


async def test_a_pdf_with_no_text_says_so(tmp_path) -> None:
    scan = tmp_path / "scan.pdf"
    _pdf_with_text(scan, "")

    out = await ReadFileTool().run(ToolInput(params={"path": str(scan), "workspace": str(tmp_path)}))

    assert not out.success and "no text layer" in out.error
