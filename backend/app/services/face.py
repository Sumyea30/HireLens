"""Local face embeddings for CV photos and live interview frames. No third-party face API."""
from __future__ import annotations

import hashlib
import logging
import math
from typing import Protocol

log = logging.getLogger("hirelens.face")

# Cosine similarity above this (configurable) counts as a match.
DEFAULT_THRESHOLD = 0.35
EMBED_DIM = 128


class FaceEngine(Protocol):
    def embed_jpeg(self, jpeg: bytes) -> list[float] | None:
        """Return a face embedding, or None when no face is found."""
        ...


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


class HashFaceEngine:
    """Deterministic stand-in for tests and environments without InsightFace.

    Any JPEG/PNG of at least 64 bytes is treated as a face. Identical bytes
    produce identical embeddings; different images produce different ones.
    """

    min_bytes: int = 64

    def embed_jpeg(self, jpeg: bytes) -> list[float] | None:
        if not jpeg or len(jpeg) < self.min_bytes:
            return None
        digest = hashlib.sha256(jpeg).digest()
        # Stretch digest into a fixed-length unit-ish vector.
        vals: list[float] = []
        seed = digest
        while len(vals) < EMBED_DIM:
            seed = hashlib.sha256(seed).digest()
            for byte in seed:
                vals.append((byte / 127.5) - 1.0)
                if len(vals) >= EMBED_DIM:
                    break
        return vals


class InsightFaceEngine:
    """CPU InsightFace buffalo_l embeddings when the package is installed."""

    def __init__(self):
        import numpy as np
        from insightface.app import FaceAnalysis

        self._np = np
        self._app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        self._app.prepare(ctx_id=-1, det_size=(640, 640))

    def embed_jpeg(self, jpeg: bytes) -> list[float] | None:
        import cv2

        arr = self._np.frombuffer(jpeg, dtype=self._np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        faces = self._app.get(img)
        if not faces:
            return None
        face = max(faces, key=lambda f: float((f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])))
        emb = getattr(face, "normed_embedding", None)
        if emb is None:
            emb = face.embedding
            norm = self._np.linalg.norm(emb)
            emb = emb / norm if norm else emb
        return [float(x) for x in emb]


def build_face_engine(provider: str = "auto") -> FaceEngine:
    provider = (provider or "auto").strip().lower()
    if provider == "hash":
        return HashFaceEngine()
    if provider == "insightface":
        return InsightFaceEngine()
    if provider == "auto":
        try:
            return InsightFaceEngine()
        except Exception as exc:
            log.warning("insightface_unavailable", extra={"error_type": type(exc).__name__})
            return HashFaceEngine()
    raise ValueError(f"Unknown FACE_PROVIDER: {provider}")


class FaceService:
    def __init__(self, engine: FaceEngine, threshold: float = DEFAULT_THRESHOLD):
        self.engine = engine
        self.threshold = threshold

    def embed(self, jpeg: bytes) -> list[float] | None:
        try:
            return self.engine.embed_jpeg(jpeg)
        except Exception:
            log.error("face_embed_failed")
            return None

    def match(self, stored: list[float] | None, live_jpeg: bytes) -> dict:
        live = self.embed(live_jpeg)
        if not stored or live is None:
            return {"match": False, "score": 0.0, "reason": "no_face"}
        score = cosine(stored, live)
        return {"match": score >= self.threshold, "score": round(score, 4), "reason": "ok"}
