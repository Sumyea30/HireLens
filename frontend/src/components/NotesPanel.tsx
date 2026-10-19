import { Button } from "./ui";
import { formatClock } from "../lib/format";

export function NotesPanel({
  body, markers, saving, onChange, onMark,
}: {
  body: string;
  markers: { offset_ms: number; at: string }[];
  saving: boolean;
  onChange: (value: string) => void;
  onMark: () => void;
}) {
  return (
    <div className="stack">
      <label className="field" htmlFor="room-notes">Private notes
        <textarea id="room-notes" rows={12} maxLength={5000} value={body} onChange={(e) => onChange(e.target.value)} placeholder="Notes for this interview" />
      </label>
      <div className="row between">
        <Button onClick={onMark}>Mark this moment</Button>
        <span className="muted small">{saving ? "Saving..." : `${body.length}/5000`}</span>
      </div>
      {markers.length > 0 && (
        <ul className="list">{markers.map((m) => <li key={m.at}>Marked at {formatClock(m.offset_ms)}</li>)}</ul>
      )}
    </div>
  );
}
