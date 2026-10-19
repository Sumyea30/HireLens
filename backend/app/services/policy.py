"""Company policy knowledge base for the job assistant (RAG).

Upload -> extract text -> chunk -> embed -> store. A new upload with the same title becomes
the active version and supersedes (and removes the chunks of) the old one.
Use synthetic policy documents only.

The job-description template is one of those documents. A user template replaces it.
Until one is saved, the built-in template is stored in the same collection.
"""
import io
import logging
import uuid
from datetime import datetime, timezone

from app.graphs.common import chunk_text

log = logging.getLogger("hirelens.policy")
ALLOWED = (".txt", ".md", ".pdf", ".docx")
MAX_BYTES = 5 * 1024 * 1024
COLLECTION = "policy"
TEMPLATE_TITLE = "Job description template"
DEFAULT_JOB_TEMPLATE = """\
Job description template. This is the job post wording to follow.

# Job Opening: {{title}}

**Company:** {{company}}
**Location:** {{location_or_mode}}
**Employment Type:** {{employment_type}}
**Experience Level:** {{experience_level}}
**Salary Range:** {{remuneration}}
**Application Deadline:** {{application_deadline}}

## About the Role

{{about_role}}

## Key Responsibilities

{{responsibilities}}

## Required Qualifications

* Education: {{qualifications}}
* Experience: At least {{min_experience_years}} years of relevant experience.
* Technical Skills: {{required_skills}}
* Communication: {{communication}}
* Other Requirements: {{other_criteria}}

## Preferred Qualifications

{{preferred_skills}}

## What We Offer

{{offers}}

## How to Apply

Email your CV to {{contact_email}} by **{{application_deadline}}**.

**Equal Opportunity Statement:** We welcome applications from qualified candidates and evaluate applicants based on job-related qualifications and evidence of relevant skills.

**Contact:** {{contact_email}}
"""


class PolicyError(ValueError):
    """Raised with a message that is safe to show to the user."""


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if not name.endswith(ALLOWED):
        raise PolicyError("Only TXT, MD, PDF or DOCX files are accepted.")
    if len(data) > MAX_BYTES:
        raise PolicyError("The file is larger than 5 MB.")
    try:
        if name.endswith((".txt", ".md")):
            return data.decode("utf-8", errors="replace")
        if name.endswith(".pdf"):
            import fitz  # PyMuPDF
            with fitz.open(stream=data, filetype="pdf") as doc:
                return "\n\n".join(page.get_text() for page in doc)
        from docx import Document
        return "\n\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs if p.text.strip())
    except Exception:
        raise PolicyError("The file could not be read. It may be damaged or protected.") from None


def _next_version(old: str | None) -> str:
    try:
        return f"{int(float(old)) + 1}.0" if old else "1.0"
    except ValueError:
        return "1.0"


class PolicyService:
    def __init__(self, store):
        self.store = store

    def ingest(self, filename: str, data: bytes) -> dict:
        text = extract_text(filename, data).strip()
        title = filename.rsplit(".", 1)[0][:120]
        return self.ingest_text(title, text, empty_message="The file has no readable text.")

    def ingest_text(self, title: str, text: str, *, empty_message: str = "The template is empty.") -> dict:
        chunks = chunk_text((text or "").strip(), max_chars=700)
        if not chunks:
            raise PolicyError(empty_message)
        title = (title or "policy").strip()[:120]
        doc_id = f"pol-{uuid.uuid4().hex[:8]}"
        old = self.store.find_active_policy(title)
        policy = {
            "id": doc_id, "title": title, "version": _next_version(old["version"] if old else None),
            "effective_date": datetime.now(timezone.utc).isoformat(), "status": "active", "chunks": len(chunks),
        }
        # Embedding happens before anything changes, so a provider failure leaves the old version active.
        self.store.publish_policy(policy, [{"id": f"{doc_id}:{c['id']}", "text": c["text"]} for c in chunks],
                                  supersede_id=old["id"] if old else None)
        return policy

    def template_text(self) -> str:
        """The template Gemini should follow. The stored document wins; otherwise the built-in text."""
        find = getattr(self.store, "find_active_policy", None)
        chunks = getattr(self.store, "policy_chunks", None)
        if not find or not chunks:
            return DEFAULT_JOB_TEMPLATE
        doc = find(TEMPLATE_TITLE)
        if not doc:
            return DEFAULT_JOB_TEMPLATE
        text = "\n\n".join(part["text"] for part in chunks(doc["id"]) if part.get("text", "").strip())
        return text or DEFAULT_JOB_TEMPLATE

    def ensure_default_template(self) -> None:
        """Put the built-in template into the policy collection when the user has not saved one."""
        doc = self.store.find_active_policy(TEMPLATE_TITLE)
        if doc:
            text = self.template_text()
            outdated = "{{apply_documents}}" in text or (
                "Key Responsibilities" not in text and (
                    "{{start_date}}" in text or "About the company" in text or "A short introduction" in text))
            if not outdated:
                return
        try:
            self.ingest_text(TEMPLATE_TITLE, DEFAULT_JOB_TEMPLATE)
        except Exception:
            log.warning("default_template_not_stored")

    def list(self) -> list[dict]:
        return self.store.list_policies()

    def delete(self, policy_id: str) -> None:
        self.store.delete_policy(policy_id)
