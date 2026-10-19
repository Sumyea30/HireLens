import { Badge, Bar } from "./ui";
import { scoreTone, titleCase } from "../lib/format";
import type { InterviewBrief } from "../lib/types";

/** Interviewer-only snapshot. The candidate page never renders this. */
export function CandidateBrief({ brief }: { brief: InterviewBrief | null }) {
  if (!brief) return <aside className="room-brief" aria-label="Candidate"><p className="muted">Loading the candidate...</p></aside>;
  const met = brief.matches.filter((m) => m.status === "met").length;
  return (
    <aside className="room-brief" aria-label="Candidate">
      <div>
        <strong>{brief.name}</strong>
        <p className="muted small">{brief.role}</p>
      </div>
      <div>
        <p className="muted small">Overall score</p>
        <div className="score-cell"><strong>{brief.aggregate_score ?? "-"}</strong><Bar value={brief.aggregate_score ?? 0} tone={scoreTone(brief.aggregate_score ?? 0)} /></div>
      </div>
      <div>
        <strong>Dimensions</strong>
        {brief.dimension_scores.length === 0 && <p className="muted small">No dimension scores yet.</p>}
        {brief.dimension_scores.map((d) => (
          <div key={d.dimension}>
            <div className="row between"><span>{titleCase(d.dimension)}</span><strong>{d.score}</strong></div>
            <Bar value={d.score} tone={scoreTone(d.score)} />
          </div>
        ))}
      </div>
      <div>
        <strong>Requirements</strong>
        <p className="muted small">{met} met · {brief.matches.filter((m) => m.status === "partial").length} partial · {brief.matches.filter((m) => m.status === "unmet").length} unmet</p>
        {brief.matches.map((m) => (
          <div key={m.requirement_id} className="cov">
            <Badge tone={m.status === "met" ? "good" : m.status === "partial" ? "warn" : "bad"}>{titleCase(m.status)}</Badge>
            <span>{m.requirement_text}</span>
          </div>
        ))}
      </div>
      <div>
        <strong>Gaps to probe</strong>
        {brief.gaps.length === 0 ? <p className="muted small">Nothing unmet or partial on the CV.</p> : brief.gaps.map((m) => <p key={m.requirement_id}>{m.requirement_text}</p>)}
      </div>
    </aside>
  );
}
