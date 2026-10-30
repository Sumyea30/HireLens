"""Store candidate face crops extracted from CVs. Separate from the CV file (which is deleted after screening)."""
import logging
import pathlib

log = logging.getLogger("hirelens.photos")


class PhotoStorage:
    def __init__(self, root: str):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, candidate_id: str) -> pathlib.Path:
        return self.root / f"{candidate_id}.jpg"

    def save(self, candidate_id: str, jpeg: bytes) -> str:
        path = self.path_for(candidate_id)
        path.write_bytes(jpeg)
        return str(path)

    def read(self, path: str | None) -> bytes | None:
        if not path:
            return None
        p = pathlib.Path(path).resolve()
        if p.parent != self.root.resolve() or not p.is_file():
            return None
        return p.read_bytes()

    def delete(self, path: str | None) -> bool:
        if not path:
            return False
        p = pathlib.Path(path).resolve()
        if p.parent != self.root.resolve():
            log.warning("refused_delete_outside_photo_storage")
            return False
        try:
            p.unlink()
            return True
        except FileNotFoundError:
            return False
