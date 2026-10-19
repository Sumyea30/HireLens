"""Smoke tests: every graph compiles and its main, retry and failure paths run
end to end with fakes (no API keys, no cost)."""
import json

from langgraph.types import Command

from app.graphs import (
    Deps,
    build_assessment_graph,
    build_cv_graph,
    build_job_post_graph,
    build_qa_graph,
    initial_assessment_state,
    initial_cv_state,
    initial_job_post_state,
    initial_qa_state,
    memory_checkpointer,
    profile_to_requirements,
)
from app.graphs.fakes import FakeChannel, FakeLLM, FakeRepo, FakeVectorStore

REQS = [
    {"id": "R1", "text": "Python web development with Django REST", "kind": "required", "category": "skill"},
    {"id": "R2", "text": "Docker containers", "kind": "required", "category": "skill"},
]
WEIGHTS = {"technical_skills": 0.6, "experience": 0.4}

CV_TEXT = (
    "Jane Doe\nEmail: jane@example.com\nPhone: +880 1700 000000\nDate of Birth: 1999-01-01\n"
    "Gender: F\n\nExperience\nBuilt REST services in Django for 3 years. "
    "Deployed Python web apps with Docker containers on Linux servers.\n\n"
    "Education\nBSc in Computer Science and Engineering. Completed several backend projects."
)


def _cv_deps(llm, files):
    def extract(path):
        value = files[path]
        if isinstance(value, Exception):
            raise value
        return value

    return Deps(llm=llm, store=FakeVectorStore(), repo=FakeRepo(), extract_text=extract)


def _good_matches(_user):
    return {"matches": [
        {"requirement_id": "R1", "status": "met", "justification": "Django REST work",
         "evidence_refs": ["cv:cand-1:c001"]},
        {"requirement_id": "R2", "status": "met", "justification": "Docker deployments",
         "evidence_refs": ["cv:cand-1:c001"]},
    ]}


def _good_scores(_user):
    return {"scores": [
        {"dimension": "technical_skills", "score": 80, "justification": "Strong stack match",
         "evidence_refs": ["cv:cand-1:c001"]},
        {"dimension": "experience", "score": 70, "justification": "3 years",
         "evidence_refs": ["cv:cand-1:c001"]},
    ]}


def test_cv_graph_happy_path_and_redaction():
    llm = FakeLLM({"ParsedCV": {"skills": ["Python"]}, "MatchesOut": _good_matches,
                   "DimensionsOut": _good_scores})
    deps = _cv_deps(llm, {"a.pdf": CV_TEXT})
    out = build_cv_graph(deps).invoke(
        initial_cv_state(candidate_id="cand-1", job_id="job-1", file_path="a.pdf",
                         job_requirements=REQS, weights=WEIGHTS))
    assert out["status"] == "completed"
    assert out["aggregate_score"] == 76.0   # (80*0.6 + 70*0.4) / 1.0
    saved = deps.repo.load_cv_result("cand-1", "job-1")
    assert "contact" not in saved
    assert "jane@example.com" not in out["clean_text"]
    assert "1999" not in out["clean_text"] and "Gender" not in out["clean_text"]


def test_cv_graph_retries_invalid_output_then_succeeds():
    calls = {"n": 0}

    def flaky_matches(user):
        calls["n"] += 1
        if calls["n"] == 1:  # first attempt cites an unknown evidence ref
            bad = _good_matches(user)
            bad["matches"][0]["evidence_refs"] = ["cv:cand-1:c999"]
            return bad
        return _good_matches(user)

    llm = FakeLLM({"ParsedCV": {}, "MatchesOut": flaky_matches, "DimensionsOut": _good_scores})
    deps = _cv_deps(llm, {"a.pdf": CV_TEXT})
    out = build_cv_graph(deps).invoke(
        initial_cv_state(candidate_id="cand-1", job_id="job-1", file_path="a.pdf",
                         job_requirements=REQS, weights=WEIGHTS))
    assert out["status"] == "completed"
    assert calls["n"] == 2 and out["retry_count"] == 1


def test_cv_graph_fails_safely_when_retries_exhausted():
    def always_bad(user):
        bad = _good_matches(user)
        bad["matches"][0]["evidence_refs"] = ["cv:cand-1:c999"]
        return bad

    llm = FakeLLM({"ParsedCV": {}, "MatchesOut": always_bad, "DimensionsOut": _good_scores})
    deps = _cv_deps(llm, {"a.pdf": CV_TEXT})
    out = build_cv_graph(deps).invoke(
        initial_cv_state(candidate_id="cand-1", job_id="job-1", file_path="a.pdf",
                         job_requirements=REQS, weights=WEIGHTS))
    assert out["status"] == "failed"
    assert deps.repo.load_cv_result("cand-1", "job-1")["status"] == "failed"


def test_cv_graph_unreadable_file_marks_failed_without_crashing():
    llm = FakeLLM({})
    deps = _cv_deps(llm, {"bad.pdf": ValueError("corrupt")})
    out = build_cv_graph(deps).invoke(
        initial_cv_state(candidate_id="cand-2", job_id="job-1", file_path="bad.pdf",
                         job_requirements=REQS, weights=WEIGHTS))
    assert out["status"] == "failed"
    assert llm.calls == []   # no LLM call was made for an unreadable CV


TURNS = [
    {"turn_id": "t1", "speaker": "interviewer", "start_ms": 0, "end_ms": 1000,
     "text": "Tell me about building REST services in Django."},
    {"turn_id": "t2", "speaker": "candidate", "start_ms": 1100, "end_ms": 9000,
     "text": "I built Django REST services with Python for three years."},
    {"turn_id": "t3", "speaker": "interviewer", "start_ms": 9100, "end_ms": 10000,
     "text": "How do you test them?"},
    {"turn_id": "t4", "speaker": "candidate", "start_ms": 10100, "end_ms": 15000,
     "text": "With pytest and Django REST test clients."},
]


def _qa_llm():
    return FakeLLM({
        "LabelsOut": {"labels": {"t1": "question", "t2": "answer", "t3": "follow_up", "t4": "answer"}},
        "PairsOut": {"pairs": [
            {"question_turn_ids": ["t1"], "answer_turn_ids": ["t2"]},
            {"question_turn_ids": ["t3"], "answer_turn_ids": ["t4"], "is_follow_up": True,
             "parent_index": 1}]},
        "EvaluationOut": {"relevance": 8, "accuracy": 7, "completeness": 7, "quality": 8,
                          "justification": "Relevant and specific.",
                          "evidence_refs": ["tr:s1:t2"]},
    })


def test_qa_graph_pairs_evaluates_and_finds_gaps():
    repo = FakeRepo()
    repo.transcripts["s1"] = TURNS
    deps = Deps(llm=_qa_llm(), store=FakeVectorStore(), repo=repo)
    out = build_qa_graph(deps).invoke(
        initial_qa_state(session_id="s1", candidate_id="cand-1", job_id="job-1", job_requirements=REQS))
    assert out["status"] == "completed"
    assert len(out["qa_pairs"]) == 2 and out["qa_pairs"][1]["parent_pair_id"] == "p001"
    assert "R2" in out["uncovered_requirement_ids"]   # Docker was never discussed
    assert repo.load_qa_result("s1")["status"] == "completed"


def test_qa_graph_empty_transcript_fails_safely():
    repo = FakeRepo()
    deps = Deps(llm=_qa_llm(), store=FakeVectorStore(), repo=repo)
    out = build_qa_graph(deps).invoke(
        initial_qa_state(session_id="none", candidate_id="c", job_id="job-1", job_requirements=REQS))
    assert out["status"] == "failed"
    assert repo.load_qa_result("none")["uncovered_requirement_ids"] == ["R1", "R2"]


def _assessment_repo():
    repo = FakeRepo()
    repo.save_cv_result("cand-1", "job-1", {
        "status": "completed", "aggregate_score": 76.0,
        "matches": _good_matches(None)["matches"], "dimension_scores": _good_scores(None)["scores"]})
    repo.save_qa_result("s1", {
        "status": "completed", "uncovered_requirement_ids": ["R2"],
        "evaluations": [{"pair_id": "p001", "relevance": 8, "accuracy": 7, "completeness": 7,
                         "quality": 8, "justification": "ok", "evidence_refs": ["tr:s1:t2"]}]})
    return repo


def test_assessment_drops_untraceable_findings_after_retries():
    llm = FakeLLM({"FindingsOut": {
        "strengths": [{"statement": "Strong Django background", "evidence_refs": ["tr:s1:t2"]},
                      {"statement": "Unsupported claim", "evidence_refs": []}],
        "evidence_gaps": [{"statement": "Docker never discussed", "evidence_refs": ["req:R2"]}]}})
    repo = _assessment_repo()
    out = build_assessment_graph(Deps(llm=llm, store=FakeVectorStore(), repo=repo)).invoke(
        initial_assessment_state(candidate_id="cand-1", session_id="s1", job_id="job-1",
                                 job_requirements=REQS))
    statements = [f["statement"] for f in out["report"]["strengths"]]
    assert statements == ["Strong Django background"]
    assert out["report"]["component_scores"]["interview_score"] == 75.0
    assert out["status"] == "completed"
    assert repo.assessments[("cand-1", "s1")]["status"] == "completed"


def test_assessment_without_interview_is_partial():
    llm = FakeLLM({"FindingsOut": {"strengths": [
        {"statement": "Strong CV match", "evidence_refs": ["cv:cand-1:c001"]}]}})
    repo = FakeRepo()
    repo.save_cv_result("cand-1", "job-1", {
        "status": "completed", "aggregate_score": 76.0,
        "matches": _good_matches(None)["matches"], "dimension_scores": _good_scores(None)["scores"]})
    out = build_assessment_graph(Deps(llm=llm, store=FakeVectorStore(), repo=repo)).invoke(
        initial_assessment_state(candidate_id="cand-1", session_id="none", job_id="job-1",
                                 job_requirements=REQS))
    assert out["status"] == "partial" and out["report"]["has_interview"] is False


def _opening_facts() -> str:
    return """\
Company: HireLens
Location: Dhaka, Bangladesh
Employment type: Full-time
Experience level: Mid-level
Salary range: 80000 BDT per month
Application deadline: 1 November 2026
About the role: We are looking for a backend developer to build hiring tools.
Responsibilities: Build APIs; Review code
Education: BSc in CSE
Experience: 3 years
Technical skills: Python, Django
Contact: hiring@example.com
"""


def _job_llm():
    return FakeLLM({
        "DraftOut": {"post": "Backend Developer. We are an equal opportunity employer.",
                     "policy_refs": ["pol:p1:c001"], "unresolved_items": []},
        "ComplianceOut": {"findings": [{"severity": "info", "message": "ok",
                                        "policy_refs": ["pol:p1:c001", "pol:invented"]}]},
    })


def _job_setup(channel):
    store = FakeVectorStore()
    store.index("policy", [{"id": "p1:c001", "text": "Job posts must use approved titles and wording"}])
    repo = FakeRepo()
    deps = Deps(llm=_job_llm(), store=store, repo=repo, channels={"careers_page": channel})
    graph = build_job_post_graph(deps, memory_checkpointer())
    config = {"configurable": {"thread_id": "job-1"}}
    return graph, config, repo


def _first_interrupt(graph, config):
    state = graph.get_state(config)
    return state.next, state.tasks[0].interrupts[0].value


def test_incomplete_profile_does_not_call_the_model():
    llm = _job_llm()
    store = FakeVectorStore()
    deps = Deps(llm=llm, store=store, repo=FakeRepo(), channels={"careers_page": FakeChannel("careers_page")})
    graph = build_job_post_graph(deps, memory_checkpointer())
    config = {"configurable": {"thread_id": "job-fields"}}
    graph.invoke(initial_job_post_state(job_id="job-fields", first_message="backend developer"), config)
    graph.invoke(Command(resume="Python and Django, 3 years, BSc in CSE"), config)
    assert llm.calls == []
    graph.invoke(Command(resume="hiring@example.com"), config)
    assert llm.calls == []
    graph.invoke(Command(resume=_opening_facts()), config)
    assert llm.calls[0] == "DraftOut"


def test_an_invalid_contact_email_does_not_finish_the_profile():
    llm = _job_llm()
    deps = Deps(llm=llm, store=FakeVectorStore(), repo=FakeRepo(), channels={"careers_page": FakeChannel("careers_page")})
    graph = build_job_post_graph(deps, memory_checkpointer())
    config = {"configurable": {"thread_id": "job-bad-email"}}
    graph.invoke(initial_job_post_state(job_id="job-bad-email", first_message="We need a backend developer. Python, 3 years, BSc in CSE. nope@gmail"), config)
    nxt, payload = _first_interrupt(graph, config)
    assert nxt == ("await_user_reply",)
    assert "not valid" in payload["assistant_message"].lower()
    assert llm.calls == []


def test_user_template_is_what_the_draft_follows():
    from app.services.memory_store import MemoryStore
    from app.services.policy import DEFAULT_JOB_TEMPLATE, TEMPLATE_TITLE, PolicyService
    channel = FakeChannel("careers_page")
    done = _opening_facts()

    store = MemoryStore()
    policy = PolicyService(store)
    policy.ensure_default_template()
    assert store.find_active_policy(TEMPLATE_TITLE) and "About the Role" in policy.template_text()
    policy.ingest_text(TEMPLATE_TITLE, "Stored company template\n\nWe are an equal opportunity employer.")
    assert policy.template_text().startswith("Stored company template")

    llm = _job_llm()
    graph = build_job_post_graph(Deps(llm=llm, store=store, repo=store, channels={"careers_page": channel}), memory_checkpointer())
    config = {"configurable": {"thread_id": "job-template"}}
    graph.invoke(initial_job_post_state(job_id="job-template", first_message="We need a backend developer"), config)
    graph.update_state(config, {"description_template": "CUSTOM TEMPLATE MARKER\n\nWe are an equal opportunity employer."})
    graph.invoke(Command(resume=done), config)
    draft = json.loads(next(ctx for call, ctx in zip(llm.calls, llm.contexts) if call == "DraftOut"))
    assert draft["description_template"].startswith("CUSTOM TEMPLATE MARKER")
    assert any(item["text"].startswith("Stored company template") for item in draft["policy_evidence"])

    llm2 = _job_llm()
    graph2 = build_job_post_graph(Deps(llm=llm2, store=store, repo=store, channels={"careers_page": channel}), memory_checkpointer())
    config2 = {"configurable": {"thread_id": "job-default-template"}}
    graph2.invoke(initial_job_post_state(job_id="job-default-template", first_message="We need a backend developer"), config2)
    graph2.invoke(Command(resume=done), config2)
    draft2 = json.loads(next(ctx for call, ctx in zip(llm2.calls, llm2.contexts) if call == "DraftOut"))
    assert draft2["description_template"].startswith("Stored company template")
    assert DEFAULT_JOB_TEMPLATE.splitlines()[0] not in draft2["description_template"]


def test_job_post_chat_approval_and_publish():
    channel = FakeChannel("careers_page")
    graph, config, repo = _job_setup(channel)
    graph.invoke(initial_job_post_state(job_id="job-1", first_message="We need a backend developer"), config)
    nxt, payload = _first_interrupt(graph, config)
    assert nxt == ("await_user_reply",) and payload["type"] == "user_message"

    graph.invoke(Command(resume=_opening_facts()), config)
    nxt, payload = _first_interrupt(graph, config)
    assert nxt == ("human_approval",) and payload["type"] == "approval"
    assert channel.published == []                       # nothing published before approval
    assert payload["compliance_findings"][0]["policy_refs"] == ["pol:p1:c001"]  # invented ref dropped

    out = graph.invoke(Command(resume={"action": "approve", "channels": ["careers_page"],
                                       "edited_post": None, "approver_role": "interviewer"}), config)
    assert out["status"] == "open"
    assert "Application job-1" in channel.published[0]["body"]
    assert "job-1" in repo.open_jobs and repo.decisions["job-1"]["action"] == "approve"


def test_job_post_rejection_never_publishes():
    channel = FakeChannel("careers_page")
    graph, config, repo = _job_setup(channel)
    graph.invoke(initial_job_post_state(job_id="job-1", first_message="We need a backend developer"), config)
    graph.invoke(Command(resume=_opening_facts()), config)
    out = graph.invoke(Command(resume={"action": "reject", "approver_role": "interviewer"}), config)
    assert out["status"] == "rejected" and channel.published == []


def test_job_post_blocks_edited_text_that_breaks_mandatory_checks():
    channel = FakeChannel("careers_page")
    graph, config, _ = _job_setup(channel)
    graph.invoke(initial_job_post_state(job_id="job-1", first_message="We need a backend developer"), config)
    graph.invoke(Command(resume=_opening_facts()), config)
    out = graph.invoke(Command(resume={"action": "approve", "channels": ["careers_page"],
                                       "edited_post": "Looking for male candidates only.",
                                       "approver_role": "interviewer"}), config)
    assert out["status"] == "failed" and channel.published == []


def test_profile_to_requirements():
    reqs = profile_to_requirements({"required_skills": ["Python"], "qualifications": ["BSc"],
                                    "min_experience_years": 2})
    assert [r["id"] for r in reqs] == ["R1", "R2", "R3"]


def test_job_post_changes_requested_loops_back_to_draft_then_publishes():
    channel = FakeChannel("careers_page")
    graph, config, _ = _job_setup(channel)
    graph.invoke(initial_job_post_state(job_id="job-1", first_message="We need a backend developer"), config)
    graph.invoke(Command(resume=_opening_facts()), config)
    graph.invoke(Command(resume={"action": "changes", "feedback": "Shorter, please.",
                                 "approver_role": "interviewer"}), config)
    nxt, payload = _first_interrupt(graph, config)
    assert nxt == ("human_approval",)                    # drafted again, waiting again
    out = graph.invoke(Command(resume={"action": "approve", "channels": ["careers_page"],
                                       "approver_role": "interviewer"}), config)
    assert out["status"] == "open" and len(channel.published) == 1


def test_job_post_graph_requires_checkpointer():
    import pytest
    deps = Deps(llm=_job_llm(), store=FakeVectorStore(), repo=FakeRepo())
    with pytest.raises(ValueError):
        build_job_post_graph(deps, None)
