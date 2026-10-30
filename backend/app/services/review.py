"""Explain a job's CV scores from the judgments Gemini already stored.

The wording is built from those stored matches, so opening the review again, or
uploading the same file again, does not ask Gemini for a new opinion.
"""


def _shown(score) -> str:
    number = round(float(score), 2)
    return str(int(number)) if number == int(number) else f"{number:.2f}"


def _name(candidate: dict) -> str:
    from app.api.pii import display_name
    return display_name(candidate, reveal=False)


def explain_candidate(name: str, score, matches: list[dict]) -> str:
    """One stable paragraph: the score, then met, partially met, and not met, each with Gemini's reason."""
    met = [m for m in matches if m.get("status") == "met"]
    partial = [m for m in matches if m.get("status") == "partial"]
    unmet = [m for m in matches if m.get("status") == "unmet"]

    def clause(rows: list[dict], label: str) -> str:
        if not rows:
            return ""
        reasons = " ".join(f"{m.get('requirement_text') or 'Requirement'}: {m.get('justification') or ''}".strip() for m in rows)
        return f"{label}: {reasons}"

    parts = [
        f"{name} scored {_shown(score)}.",
        f"Gemini marked {len(met)} met, {len(partial)} partially met and {len(unmet)} not met.",
    ]
    parts.extend(bit for bit in (
        clause(met, "Met"),
        clause(partial, "Partially met"),
        clause(unmet, "Not met"),
    ) if bit)
    return " ".join(parts)


def why_these(ranked: list[dict]) -> str:
    if not ranked:
        return "No CV has been scored for this job yet."
    lead = ranked[0]
    lines = [
        "Gemini scored every CV against the same requirements and the same weights. "
        "Uploading the same file again reuses that judgment, so the score stays the same.",
        f"Gemini ranked {lead['name']} first. {lead['why']}",
    ]
    for candidate in ranked[1:]:
        lines.append(f"{candidate['name']} is next. {candidate['why']}")
    return "\n\n".join(lines)


def build_review(store, job_id: str) -> dict:
    job = store.get_job(job_id) or {}
    order = {r["id"]: i for i, r in enumerate(job.get("requirements") or [])}
    texts = {r["id"]: r["text"] for r in job.get("requirements") or []}
    ranked = []
    unscored = []
    for row in store.list_candidates(job_id):
        full = store.get_candidate(row["id"]) or row
        name = _name(full)
        result = store.load_cv_result(full["id"], job_id)
        if not result or result.get("status") != "completed" or result.get("aggregate_score") is None:
            unscored.append({
                "id": full["id"],
                "name": name,
                "status": full.get("status"),
                "reason": full.get("failure_reason") or "Scoring has not finished.",
            })
            continue
        matches = []
        for match in result.get("matches") or []:
            matches.append({
                "requirement_id": match["requirement_id"],
                "requirement_text": texts.get(match["requirement_id"], ""),
                "status": match["status"],
                "justification": match.get("justification") or "",
            })
        matches.sort(key=lambda m: order.get(m["requirement_id"], 999))
        score = round(float(result["aggregate_score"]), 2)
        dimensions = []
        for score_row in result.get("dimension_scores") or []:
            dimensions.append({
                "dimension": score_row["dimension"],
                "score": round(float(score_row["score"]), 2),
                "justification": score_row.get("justification") or "",
            })
        ranked.append({
            "id": full["id"],
            "name": name,
            "aggregate_score": score,
            "why": explain_candidate(name, score, matches),
            "met": sum(1 for m in matches if m["status"] == "met"),
            "partial": sum(1 for m in matches if m["status"] == "partial"),
            "unmet": sum(1 for m in matches if m["status"] == "unmet"),
            "matches": matches,
            "dimensions": dimensions,
        })
    ranked.sort(key=lambda c: (-c["aggregate_score"], c["id"]))
    for index, candidate in enumerate(ranked, start=1):
        candidate["rank"] = index
    return {
        "job_id": job_id,
        "job_title": job.get("title") or "Job",
        "why_these": why_these(ranked),
        "candidates": ranked,
        "unscored": unscored,
    }
