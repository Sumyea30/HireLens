"""CV identity extraction, incompleteness mail loop, PII gating, and local face match."""
from email.message import EmailMessage
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient

from app.graphs import Deps, build_job_post_graph, memory_checkpointer
from app.graphs.common import name_from_text
from app.graphs.fakes import FakeLLM
from app.main import Services, create_app
from app.services.completion import completion_subject, find_completion_token
from app.services.face import FaceService, HashFaceEngine, cosine
from app.services.mail import MailProcessor
from app.services.memory_store import MemoryStore
from app.services.policy import PolicyService
from tests.helpers_f2 import FACE_JPEG, FACE_JPEG_B, make_cv_docx, make_docx, open_job, settings, wired_intake


def make_client(tmp_path):
    store = MemoryStore("hiring@example.com")
    deps = Deps(llm=FakeLLM({}), store=store, repo=store)
    s = settings(photo_dir=str(tmp_path / "photos"))
    intake, photos, face = wired_intake(tmp_path, store, settings_obj=s)
    svc = Services(s, store, deps, build_job_post_graph(deps, memory_checkpointer()), PolicyService(store), intake)
    svc.photos, svc.face = photos, face
    return TestClient(create_app(svc)), svc


def login(c, user="ivy"):
    return {"Authorization": "Bearer " + c.post("/auth/login", json={"username": user, "password": f"pw-{user}"}).json()["token"]}


def test_name_from_text_prefers_label_and_header():
    assert name_from_text("Name: Ada Lovelace\nEmail: a@b.com\n") == "Ada Lovelace"
    assert name_from_text("Jane Doe\nEmail: jane@example.com\nExperience\n") == "Jane Doe"


def test_incomplete_without_email_or_photo(tmp_path):
    _, svc = make_client(tmp_path)
    job = open_job(svc.store)
    no_mail = make_docx("Jane Doe\nPython developer", photo=FACE_JPEG)
    no_photo = make_docx("Jane Doe\nEmail: jane@example.com\nPython", photo=None)
    a = svc.intake.accept(job_id=job, filename="a.docx", data=no_mail, source="upload")
    b = svc.intake.accept(job_id=job, filename="b.docx", data=no_photo, source="upload")
    assert a["status"] == "incomplete" and b["status"] == "incomplete"


def test_completion_mail_uses_sender_then_stores_cv_email(tmp_path):
    store = MemoryStore()
    open_job(store)
    intake, _, _ = wired_intake(tmp_path, store)
    sent = []

    def fake_send(settings, to, job_id, token):
        sent.append({"to": to, "job_id": job_id, "token": token})
        return f"<thread-{token}@hirelens.completion>"

    with patch("app.services.intake.send_completion_request", side_effect=fake_send):
        incomplete = make_docx("Jane Doe\nPython only", photo=None)
        res = intake.accept(
            job_id="job-abc12345", filename="cv.docx", data=incomplete, source="email",
            sender_email="sender@example.org", message_id_hash="h-incomplete",
        )
    assert res["status"] == "incomplete"
    assert sent == [{"to": "sender@example.org", "job_id": "job-abc12345",
                     "token": store.get_candidate(res["candidate_id"])["completion_token"]}]
    cand = store.get_candidate(res["candidate_id"])
    assert cand["email"] is None and cand["completion_requests"] == 1

    complete = make_cv_docx(email="fromcv@example.com")
    with patch("app.services.intake.send_completion_request", side_effect=fake_send):
        out = intake.replace_cv(
            candidate_id=res["candidate_id"], filename="cv.docx", data=complete,
            sender_email="sender@example.org", message_id_hash="h-reply-1",
        )
    assert out["status"] == "queued"
    assert store.get_candidate(res["candidate_id"])["email"] == "fromcv@example.com"
    assert len(sent) == 1  # no further request once complete


def test_completion_requests_capped_at_two(tmp_path):
    store = MemoryStore()
    open_job(store)
    intake, _, _ = wired_intake(tmp_path, store)
    sent = []

    def fake_send(settings, to, job_id, token):
        sent.append(token)
        return f"<thread-{len(sent)}@hirelens.completion>"

    with patch("app.services.intake.send_completion_request", side_effect=fake_send):
        res = intake.accept(
            job_id="job-abc12345", filename="cv.docx",
            data=make_docx("No photo", photo=None), source="email",
            sender_email="sender@example.org", message_id_hash="h1",
        )
        cid = res["candidate_id"]
        intake.replace_cv(candidate_id=cid, filename="cv.docx",
                          data=make_docx("Still no photo Email: a@b.com", photo=None),
                          sender_email="sender@example.org", message_id_hash="h2")
        intake.replace_cv(candidate_id=cid, filename="cv.docx",
                          data=make_docx("Third try Email: a@b.com", photo=None),
                          sender_email="sender@example.org", message_id_hash="h3")
    assert len(sent) == 2
    assert store.get_candidate(cid)["completion_requests"] == 2
    assert store.get_candidate(cid)["status"] == "incomplete"


def test_mail_reply_matches_completion_token(tmp_path):
    store = MemoryStore()
    open_job(store)
    intake, _, _ = wired_intake(tmp_path, store)
    with patch("app.services.intake.send_completion_request", return_value="<mid-1@hirelens.completion>"):
        res = intake.accept(
            job_id="job-abc12345", filename="cv.docx",
            data=make_docx("Email: a@b.com", photo=None), source="email",
            sender_email="sender@example.org", message_id_hash="orig",
        )
    cid = res["candidate_id"]
    token = store.get_candidate(cid)["completion_token"]
    proc = MailProcessor(intake, store)
    m = EmailMessage()
    m["Subject"] = completion_subject("job-abc12345", token)
    m["From"] = "sender@example.org"
    m["To"] = "hiring@example.com"
    m["Message-ID"] = f"<{uuid.uuid4().hex}@mail.example>"
    m["In-Reply-To"] = "<mid-1@hirelens.completion>"
    m.set_content("here is my updated CV")
    m.add_attachment(make_cv_docx(email="final@example.com"), maintype="application",
                     subtype="octet-stream", filename="cv.docx")
    out = proc.process(m.as_bytes())
    assert out["outcome"] == "accepted" and out["status"] == "queued"
    assert store.get_candidate(cid)["email"] == "final@example.com"
    assert find_completion_token(m["Subject"]) == token


def test_pii_gating_admin_sees_name_photo_interviewer_does_not(tmp_path):
    c, svc = make_client(tmp_path)
    job = open_job(svc.store)
    ivy, ada = login(c, "ivy"), login(c, "ada")
    up = c.post(f"/jobs/{job}/cvs", headers=ivy,
                files=[("files", ("cv.docx", make_cv_docx(name="Secret Name", email="s@example.com"), "application/zip"))])
    assert up.json()[0]["status"] == "queued"
    cid = c.get(f"/candidates?job_id={job}", headers=ivy).json()[0]["id"]
    ivy_detail = c.get(f"/candidates/{cid}", headers=ivy).json()
    ada_detail = c.get(f"/candidates/{cid}", headers=ada).json()
    assert ivy_detail["name"].startswith("Applicant ") and "Secret" not in ivy_detail["name"]
    assert ivy_detail.get("photo_url") is None and "real_name" not in ivy_detail
    assert ada_detail["real_name"] == "Secret Name" and ada_detail["photo_url"]
    photo = c.get(ada_detail["photo_url"], headers=ada)
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    assert c.get(ada_detail["photo_url"], headers=ivy).status_code == 403


def test_face_check_match_and_mismatch(tmp_path):
    c, svc = make_client(tmp_path)
    job = open_job(svc.store)
    ivy = login(c, "ivy")
    res = svc.intake.accept(job_id=job, filename="cv.docx", data=make_cv_docx(photo=FACE_JPEG), source="upload")
    cid = res["candidate_id"]
    svc.store.update_candidate(cid, status="screened", aggregate_score=80)
    created = c.post("/interviews", headers=ivy, json={
        "job_id": job, "candidate_id": cid, "starts_at": "2099-01-01T10:00:00+00:00",
    })
    assert created.status_code == 200, created.text
    iid = created.json()["id"]
    stored_jpeg = svc.photos.read(svc.store.get_candidate(cid)["photo_path"])
    ok = c.post(f"/interviews/{iid}/face-check", headers=ivy, files=[("image", ("f.jpg", stored_jpeg, "image/jpeg"))])
    bad = c.post(f"/interviews/{iid}/face-check", headers=ivy, files=[("image", ("g.jpg", FACE_JPEG_B, "image/jpeg"))])
    assert ok.status_code == 200 and ok.json()["match"] is True
    assert bad.status_code == 200 and bad.json()["match"] is False
    assert "embedding" not in ok.json()
    assert cosine(HashFaceEngine().embed_jpeg(stored_jpeg), HashFaceEngine().embed_jpeg(stored_jpeg)) > 0.99


def test_room_brief_reveals_name_to_interviewer(tmp_path):
    c, svc = make_client(tmp_path)
    job = open_job(svc.store)
    ivy = login(c, "ivy")
    res = svc.intake.accept(job_id=job, filename="cv.docx",
                            data=make_cv_docx(name="Room Name", email="r@example.com"), source="upload")
    cid = res["candidate_id"]
    svc.store.update_candidate(cid, status="screened", aggregate_score=70)
    iid = c.post("/interviews", headers=ivy, json={
        "job_id": job, "candidate_id": cid, "starts_at": "2099-01-01T10:00:00+00:00",
    }).json()["id"]
    brief = c.get(f"/interviews/{iid}/brief", headers=ivy).json()
    listed = c.get("/interviews", headers=ivy).json()
    assert brief["name"] == "Room Name"
    assert all(i["candidate_name"].startswith("Applicant ") for i in listed if i["id"] == iid)
