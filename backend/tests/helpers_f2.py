import io
import json

from docx import Document

from app.core.auth import hash_password
from app.core.config import Settings

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def make_docx(text="Backend developer with Python experience.") -> bytes:
    buf = io.BytesIO()
    d = Document()
    d.add_paragraph(text)
    d.save(buf)
    return buf.getvalue()


USERS = {
    "ivy": {"hash": hash_password("pw-ivy"), "role": "interviewer", "name": "Ivy"},
    "ada": {"hash": hash_password("pw-ada"), "role": "admin", "name": "Ada"},
    "guest": {"hash": hash_password("pw-guest"), "role": "candidate", "name": "Guest"},
}


def settings(**kw) -> Settings:
    return Settings(auth_secret="test-secret", users_json=json.dumps(USERS), apply_email="hiring@example.com", **kw)


def open_job(store, job_id="job-abc12345", owner_id="ivy"):
    store.create_job(job_id, owner_id)
    store.update_job(job_id, title="Backend Developer", profile={"title": "Backend Developer", "required_skills": ["Python", "Docker"],
                     "min_experience_years": 3, "qualifications": ["BSc"]})
    store.update_job(job_id, status="open")
    return job_id
