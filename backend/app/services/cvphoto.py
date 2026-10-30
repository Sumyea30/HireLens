"""Pull candidate portrait images out of PDF / DOCX CV files."""
from __future__ import annotations

import io
import logging
import zipfile

log = logging.getLogger("hirelens.cvphoto")


def _to_jpeg(raw: bytes) -> bytes | None:
    try:
        import fitz  # PyMuPDF can decode many image formats via pixmaps
        # Prefer Pillow when present for non-PDF image bytes.
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(raw))
            img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=90)
            return out.getvalue()
        except Exception:
            pass
        doc = fitz.open(stream=raw, filetype="png")
        if doc.page_count < 1:
            doc = fitz.open(stream=raw, filetype="jpeg")
        if doc.page_count < 1:
            return None
        pix = doc[0].get_pixmap()
        return pix.tobytes("jpeg")
    except Exception:
        if raw[:3] == b"\xff\xd8\xff":
            return raw
        return None


def images_from_pdf(path: str) -> list[bytes]:
    import fitz

    out: list[bytes] = []
    doc = fitz.open(path)
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                data = doc.extract_image(xref)
                jpeg = _to_jpeg(data["image"])
                if jpeg:
                    out.append(jpeg)
            except Exception:
                continue
    return out


def images_from_docx(path: str) -> list[bytes]:
    out: list[bytes] = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.startswith("word/media/"):
                continue
            try:
                jpeg = _to_jpeg(z.read(name))
                if jpeg:
                    out.append(jpeg)
            except Exception:
                continue
    return out


def images_from_cv(path: str) -> list[bytes]:
    lower = path.lower()
    try:
        if lower.endswith(".pdf"):
            return images_from_pdf(path)
        if lower.endswith(".docx"):
            return images_from_docx(path)
    except Exception:
        log.error("cv_image_extract_failed")
    return []


def pick_face_jpeg(path: str, face_service) -> tuple[bytes | None, list[float] | None]:
    """Return (jpeg_bytes, embedding) for the best face found in the CV, or (None, None)."""
    best: tuple[bytes, list[float], int] | None = None
    for jpeg in images_from_cv(path):
        emb = face_service.embed(jpeg)
        if emb is None:
            continue
        score = len(jpeg)
        if best is None or score > best[2]:
            best = (jpeg, emb, score)
    if not best:
        return None, None
    return best[0], best[1]
