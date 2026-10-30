"""Funnel Doctor (F11): aggregate requirement funnel over stored screening results.

Pure arithmetic on persisted matches. Does not call an LLM and does not change
official scores or ranking. Coverage is an estimate only.
"""
from __future__ import annotations

STATUS_VALUE = {"met": 1.0, "partial": 0.5, "unmet": 0.0}
LOW_SAMPLE = 10


def _float_env(name: str, default: float) -> float:
    import os
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    import os
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def funnel_thresholds() -> dict:
    return {
        "bottleneck_max": _float_env("FUNNEL_BOTTLENECK_MAX", 0.2),
        "flat_min": _float_env("FUNNEL_FLAT_MIN", 0.9),
        "shortlist_n": max(1, _int_env("FUNNEL_SHORTLIST_N", 5)),
    }


def label_requirement(met_share: float, *, bottleneck_max: float, flat_min: float) -> str:
    if met_share < bottleneck_max:
        return "bottleneck"
    if met_share > flat_min:
        return "non_discriminating"
    return "balanced"


def coverage_estimate(statuses: list[str]) -> float:
    """Weighted share of requirements: met=1, partial=0.5, unmet=0. Equal weight per requirement."""
    if not statuses:
        return 0.0
    total = sum(STATUS_VALUE.get(s, 0.0) for s in statuses)
    return round(total / len(statuses), 4)


def _display_name(candidate: dict) -> str:
    from app.api.pii import display_name
    return display_name(candidate, reveal=False)


def _load_screened(store, job_id: str) -> list[dict]:
    """Candidates with a completed screening result and a score, plus their matches."""
    rows = []
    for row in store.list_candidates(job_id):
        result = store.load_cv_result(row["id"], job_id)
        if not result or result.get("status") != "completed" or result.get("aggregate_score") is None:
            continue
        matches = {m["requirement_id"]: m for m in (result.get("matches") or [])}
        rows.append({
            "id": row["id"],
            "name": _display_name(row),
            "aggregate_score": float(result["aggregate_score"]),
            "matches": matches,
        })
    rows.sort(key=lambda c: (-c["aggregate_score"], c["id"]))
    return rows


def _statuses_for(candidate: dict, req_ids: list[str], *, flip: str | None = None) -> list[str]:
    out = []
    for rid in req_ids:
        if flip and rid == flip:
            out.append("met")
            continue
        m = candidate["matches"].get(rid)
        out.append((m or {}).get("status") or "unmet")
    return out


def _shortlist_ids(ranked: list[dict], n: int) -> list[str]:
    return [c["id"] for c in ranked[:n]]


def _rank_by_coverage(candidates: list[dict], req_ids: list[str], *, flip_all: str | None = None,
                      flip_one: tuple[str, str] | None = None) -> list[dict]:
    """Sort by coverage estimate (desc), then official score, then id. Does not mutate scores."""
    scored = []
    for c in candidates:
        flip = None
        if flip_all:
            flip = flip_all
        elif flip_one and flip_one[0] == c["id"]:
            flip = flip_one[1]
        cov = coverage_estimate(_statuses_for(c, req_ids, flip=flip))
        scored.append({**c, "coverage_estimate": cov})
    scored.sort(key=lambda c: (-c["coverage_estimate"], -c["aggregate_score"], c["id"]))
    return scored


def funnel_report(store, job_id: str) -> dict | None:
    """Build the Funnel Doctor report for a job. Returns None if the job is missing."""
    job = store.get_job(job_id)
    if not job:
        return None
    thr = funnel_thresholds()
    reqs = list(job.get("requirements") or [])
    req_ids = [r["id"] for r in reqs]
    candidates = _load_screened(store, job_id)
    n_cand = len(candidates)
    shortlist_n = thr["shortlist_n"]
    shortlist = _shortlist_ids(candidates, shortlist_n)
    shortlist_set = set(shortlist)

    # Per-candidate coverage (estimate only).
    for c in candidates:
        c["coverage_estimate"] = coverage_estimate(_statuses_for(c, req_ids))

    requirements_out = []
    for req in reqs:
        rid = req["id"]
        mandatory = req.get("kind") == "required"
        counts = {"met": 0, "partial": 0, "unmet": 0}
        for c in candidates:
            status = (c["matches"].get(rid) or {}).get("status") or "unmet"
            if status not in counts:
                status = "unmet"
            counts[status] += 1
        met_share = (counts["met"] / n_cand) if n_cand else 0.0
        pass_rate = round(met_share, 4)
        requirements_out.append({
            "requirement_id": rid,
            "text": req.get("text") or "",
            "kind": req.get("kind") or "required",
            "mandatory": mandatory,
            "met": counts["met"],
            "partial": counts["partial"],
            "unmet": counts["unmet"],
            "pass_rate": pass_rate,
            "label": label_requirement(met_share, bottleneck_max=thr["bottleneck_max"], flat_min=thr["flat_min"]),
        })

    # What-if: preferred only. Treat R as met for everyone, re-rank by coverage, take top N.
    what_if = []
    for req in reqs:
        if req.get("kind") == "required":
            continue
        rid = req["id"]
        alt = _rank_by_coverage(candidates, req_ids, flip_all=rid)
        alt_ids = _shortlist_ids(alt, shortlist_n)
        entering = [
            {"id": c["id"], "name": c["name"], "coverage_estimate": c["coverage_estimate"]}
            for c in alt if c["id"] in set(alt_ids) and c["id"] not in shortlist_set
        ]
        what_if.append({
            "requirement_id": rid,
            "text": req.get("text") or "",
            "shortlist_size": len(alt_ids),
            "entering": entering,
        })

    # Near-miss: outside shortlist; exactly one preferred relaxation puts them in coverage top-N.
    near_miss = []
    preferred_ids = [r["id"] for r in reqs if r.get("kind") != "required"]
    for c in candidates:
        if c["id"] in shortlist_set:
            continue
        workable: list[tuple[str, list]] = []
        for rid in preferred_ids:
            status = (c["matches"].get(rid) or {}).get("status") or "unmet"
            if status == "met":
                continue
            alt = _rank_by_coverage(candidates, req_ids, flip_one=(c["id"], rid))
            if c["id"] in set(_shortlist_ids(alt, shortlist_n)):
                refs = list((c["matches"].get(rid) or {}).get("evidence_refs") or [])
                workable.append((rid, refs))
        if len(workable) == 1:
            rid, refs = workable[0]
            req = next((r for r in reqs if r["id"] == rid), None)
            near_miss.append({
                "candidate_id": c["id"],
                "name": c["name"],
                "requirement_id": rid,
                "requirement_text": (req or {}).get("text") or "",
                "evidence_refs": refs,
                "coverage_estimate": c["coverage_estimate"],
                "aggregate_score": c["aggregate_score"],
            })

    return {
        "job_id": job_id,
        "job_title": job.get("title") or "Job",
        "candidate_count": n_cand,
        "low_sample": n_cand < LOW_SAMPLE,
        "shortlist_n": shortlist_n,
        "shortlist": [
            {
                "id": c["id"],
                "name": c["name"],
                "aggregate_score": c["aggregate_score"],
                "coverage_estimate": c["coverage_estimate"],
            }
            for c in candidates if c["id"] in shortlist_set
        ],
        "candidates": [
            {
                "id": c["id"],
                "name": c["name"],
                "aggregate_score": c["aggregate_score"],
                "coverage_estimate": c["coverage_estimate"],
                "in_shortlist": c["id"] in shortlist_set,
            }
            for c in candidates
        ],
        "requirements": requirements_out,
        "what_if": what_if,
        "near_miss": near_miss,
        "thresholds": thr,
        "coverage_note": "Coverage is an estimate (met=1, partial=0.5, unmet=0). It is never the official score.",
    }


def aggregates_for_llm(report: dict, post_text: str | None) -> dict:
    """Payload for the revision LLM: aggregates, requirement text, and the post only."""
    return {
        "job_title": report.get("job_title"),
        "candidate_count": report.get("candidate_count"),
        "shortlist_n": report.get("shortlist_n"),
        "requirements": [
            {
                "text": r["text"],
                "kind": r["kind"],
                "mandatory": r["mandatory"],
                "met": r["met"],
                "partial": r["partial"],
                "unmet": r["unmet"],
                "pass_rate": r["pass_rate"],
                "label": r["label"],
            }
            for r in report.get("requirements") or []
        ],
        "what_if_summary": [
            {
                "text": w["text"],
                "shortlist_size_if_met": w["shortlist_size"],
                "entering_count": len(w.get("entering") or []),
            }
            for w in report.get("what_if") or []
        ],
        "current_post": post_text or "",
    }
