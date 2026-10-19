import { useState } from "react";
import { Button } from "./ui";
import type { CopilotQuestion } from "../lib/types";

export function CopilotPanel({
  remaining, limit, questions, busy, onAsk,
}: {
  remaining: number;
  limit: number;
  questions: CopilotQuestion[];
  busy: boolean;
  onAsk: (message: string) => void;
}) {
  const [text, setText] = useState("");
  const blocked = remaining <= 0;
  return (
    <div className="stack">
      <p className="muted small" aria-live="polite">{remaining} of {limit} questions left</p>
      <label className="field" htmlFor="copilot-q">Ask the co-pilot
        <textarea id="copilot-q" rows={3} value={text} maxLength={1000} disabled={blocked || busy}
          placeholder="What should I ask next?" onChange={(e) => setText(e.target.value)} />
      </label>
      <Button variant="primary" disabled={blocked || !text.trim()} loading={busy} onClick={() => { onAsk(text.trim()); setText(""); }}>Ask</Button>
      {questions.map((q) => (
        <div key={q.question} className="card">
          <strong>{q.question}</strong>
          <p className="muted small">Requirement {q.requirement_id}</p>
          <p>{q.reference_answer}</p>
        </div>
      ))}
    </div>
  );
}
