"""Read a stored CV (PDF or DOCX) into text for Graph A.

The graph calls extract_text first. Scanned PDFs often have no text layer, so
ocr_cv_text is the fallback. It needs the Tesseract binary (installed in the
backend image). DOCX has no pages to scan.
"""
import pathlib


def extract_cv_text(path: str) -> str:
    p = pathlib.Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        import fitz
        with fitz.open(p) as doc:
            return "\n\n".join(page.get_text() for page in doc)
    if suffix == ".docx":
        from docx import Document
        return "\n\n".join(para.text for para in Document(str(p)).paragraphs if para.text.strip())
    raise ValueError("unsupported file type")


def ocr_cv_text(path: str) -> str:
    """OCR a PDF whose text layer is missing or too short. Raises if Tesseract is unavailable."""
    p = pathlib.Path(path)
    if p.suffix.lower() != ".pdf":
        return ""
    import fitz
    parts: list[str] = []
    with fitz.open(p) as doc:
        for page in doc:
            textpage = page.get_textpage_ocr(language="eng", dpi=200, full=True)
            parts.append(page.get_text(textpage=textpage) or "")
    return "\n\n".join(parts)
