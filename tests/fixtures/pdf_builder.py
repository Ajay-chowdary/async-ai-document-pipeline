"""Builds genuinely valid PDFs for tests.

Hand-written PDF literals are tempting but wrong: without a correct cross
reference table ``pypdf`` refuses the file, so a test using one would exercise
the parse-failure path while claiming to test the happy path. This assembles
the objects and computes real byte offsets, which keeps the fixtures honest
without pulling in a PDF-generation dependency.
"""

HEADER = b"%PDF-1.4\n"


def _assemble(objects: list[bytes]) -> bytes:
    """Serialise numbered objects with a correct xref table and trailer."""
    out = bytearray(HEADER)
    offsets: list[int] = []

    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"

    xref_offset = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset

    out += b"trailer\n<</Size %d /Root 1 0 R>>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_offset
    return bytes(out)


def build_pdf(lines: list[str]) -> bytes:
    """Return a one-page PDF whose text layer contains ``lines``."""
    drawn = " ".join(f"({line}) Tj 0 -16 Td" for line in lines)
    content = f"BT /F1 12 Tf 72 720 Td {drawn} ET".encode()

    return _assemble(
        [
            b"<</Type /Catalog /Pages 2 0 R>>",
            b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
            b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources <</Font <</F1 5 0 R>>>> /Contents 4 0 R>>",
            b"<</Length %d>>\nstream\n" % len(content) + content + b"\nendstream",
            b"<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>",
        ]
    )


def build_pdf_without_text_layer() -> bytes:
    """Return a valid one-page PDF with no text, as a scanned document would be."""
    return _assemble(
        [
            b"<</Type /Catalog /Pages 2 0 R>>",
            b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
            b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]>>",
        ]
    )
