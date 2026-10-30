import { ArrowLeft, Mic, MicOff, PhoneOff, Video, VideoOff } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { CandidateBrief } from "../components/CandidateBrief";
import { CopilotPanel } from "../components/CopilotPanel";
import { NotesPanel } from "../components/NotesPanel";
import { useToast } from "../components/Toast";
import { Badge, Button, Tabs } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { useInterviewRoom } from "../hooks/useInterviewRoom";
import { formatClock, titleCase } from "../lib/format";
import { downloadText } from "../lib/logger";
import type { CopilotQuestion, TranscriptTurn } from "../lib/types";

type StaffTab = "copilot" | "notes" | "transcript";
type RatingState = "idle" | "pending" | "ready" | "failed";

function TranscriptList({
  turns,
  onEdit,
}: {
  turns: TranscriptTurn[];
  onEdit?: (turnId: string, text: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  if (!turns.length) {
    return <p className="muted small">Spoken turns appear here as each person talks. Audio is transcribed on the server and is not stored.</p>;
  }
  return (
    <ul className="transcript-live" aria-live="polite">
      {turns.map((t) => {
        const low = t.confidence != null && t.confidence < 0.35;
        const flagged = !!t.language_mismatch && !t.edited;
        const display = flagged ? "not English, check the transcript" : t.text;
        return (
          <li key={t.turn_id} className={`transcript-turn ${t.speaker}${low || flagged ? " low-confidence" : ""}${flagged ? " language-mismatch" : ""}`}>
            <span className="transcript-speaker">{titleCase(t.speaker)}</span>
            {editing === t.turn_id ? (
              <span className="transcript-text" style={{ display: "grid", gap: 6 }}>
                <textarea
                  rows={2}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  aria-label="Edit transcript turn"
                />
                <span className="row" style={{ gap: 8 }}>
                  <button
                    type="button"
                    className="btn"
                    disabled={saving || !draft.trim()}
                    onClick={async () => {
                      if (!onEdit) return;
                      setSaving(true);
                      try {
                        await onEdit(t.turn_id, draft.trim());
                        setEditing(null);
                      } finally {
                        setSaving(false);
                      }
                    }}
                  >
                    Save
                  </button>
                  <button type="button" className="btn" disabled={saving} onClick={() => setEditing(null)}>Cancel</button>
                </span>
              </span>
            ) : (
              <span className="transcript-text" style={low || flagged ? { opacity: 0.7, fontStyle: flagged ? "italic" : undefined } : undefined}>
                {display}
                {onEdit && (
                  <button
                    type="button"
                    className="btn"
                    style={{ marginLeft: 8, padding: "2px 8px", fontSize: "0.75rem" }}
                    onClick={() => { setEditing(t.turn_id); setDraft(t.text); }}
                  >
                    Edit
                  </button>
                )}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export default function InterviewRoom() {
  const { id = "" } = useParams();
  const { session, role, logout } = useAuth();
  const nav = useNavigate();
  const toast = useToast();
  const isCandidate = role === "candidate";
  const room = useInterviewRoom(id, session?.token ?? "", role ?? "candidate");
  const interview = useAsync(() => api.getInterview(id), [id]);
  const brief = useAsync(async () => (isCandidate ? null : api.getBrief(id)), [id, isCandidate]);
  const [tab, setTab] = useState<StaffTab>("transcript");
  const [notes, setNotes] = useState("");
  const [markers, setMarkers] = useState<{ offset_ms: number; at: string }[]>([]);
  const [saving, setSaving] = useState(false);
  const [quota, setQuota] = useState({ remaining: 15, limit: 15 });
  const [questions, setQuestions] = useState<CopilotQuestion[]>([]);
  const [asking, setAsking] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [busy, setBusy] = useState(false);
  const [finished, setFinished] = useState(false);
  const [rating, setRating] = useState<RatingState>("idle");
  const saved = useRef<string | null>(null);
  const localRef = useRef<HTMLVideoElement>(null);
  const remoteRef = useRef<HTMLVideoElement>(null);
  const [needsTap, setNeedsTap] = useState(false);

  useEffect(() => {
    const el = localRef.current;
    if (!el) return;
    el.srcObject = room.local;
    if (room.local) void el.play().catch(() => {});
  }, [room.local]);
  useEffect(() => {
    const el = remoteRef.current;
    if (!el) return;
    el.srcObject = room.remote;
    if (!room.remote) { setNeedsTap(false); return; }
    void el.play().then(() => setNeedsTap(false)).catch(() => setNeedsTap(true));
  }, [room.remote]);
  useEffect(() => { const t = setInterval(() => setElapsed((e) => e + 1000), 1000); return () => clearInterval(t); }, []);

  useEffect(() => {
    if (isCandidate) return;
    let cancel = false;
    api.getNotes(id).then((n) => {
      if (cancel) return;
      saved.current = n.body;
      setNotes(n.body);
      setMarkers(n.markers ?? []);
    }).catch(() => { if (!cancel) saved.current = ""; });
    api.copilotQuota(id).then((q) => { if (!cancel) setQuota(q); }).catch(() => {});
    return () => { cancel = true; };
  }, [id, isCandidate]);

  useEffect(() => {
    if (isCandidate || saved.current === null || notes === saved.current) return;
    const handle = setTimeout(() => {
      setSaving(true);
      saved.current = notes;
      api.saveNotes(id, notes).then((n) => setMarkers(n.markers ?? [])).catch((e) => toast(errorMessage(e), "bad")).finally(() => setSaving(false));
    }, 1500);
    return () => clearTimeout(handle);
  }, [notes, id, isCandidate, toast]);

  useEffect(() => {
    if (isCandidate || !finished || rating !== "pending") return;
    const candidateId = interview.data?.candidate_id;
    if (!candidateId) return;
    let cancel = false;
    let attempts = 0;
    const poll = async () => {
      attempts += 1;
      try {
        const detail = await api.getCandidate(candidateId);
        if (cancel) return;
        if (detail.evaluation) {
          setRating("ready");
          return;
        }
      } catch { /* keep polling */ }
      if (cancel) return;
      if (attempts >= 24) {
        setRating("failed");
        return;
      }
      window.setTimeout(() => { void poll(); }, 2500);
    };
    void poll();
    return () => { cancel = true; };
  }, [finished, rating, isCandidate, interview.data?.candidate_id]);

  const ask = async (message: string) => {
    setAsking(true);
    try {
      const reply = await api.askCopilot(id, message);
      setQuestions(reply.questions);
      setQuota({ remaining: reply.remaining, limit: reply.limit });
    } catch (e) {
      toast(errorMessage(e), "bad");
      if (e instanceof ApiError && e.status === 429) {
        const next = await api.copilotQuota(id).catch(() => null);
        if (next) setQuota(next);
      }
    } finally { setAsking(false); }
  };
  const mark = async () => {
    try {
      const note = await api.saveNotes(id, notes, true);
      saved.current = note.body;
      setMarkers(note.markers ?? []);
    } catch (e) { toast(errorMessage(e), "bad"); }
  };
  const finish = async () => {
    setBusy(true);
    try {
      await api.endInterview(id);
      setFinished(true);
      setRating("pending");
    } catch (e) { toast(errorMessage(e), "bad"); }
    finally { setBusy(false); }
  };
  const download = async () => {
    try {
      const doc = await api.getTranscript(id);
      downloadText(`interview-${id}.json`, JSON.stringify(doc, null, 2));
    } catch (e) { toast(errorMessage(e), "bad"); }
  };
  const evaluate = async () => {
    setBusy(true);
    try {
      await api.generateAssessment(id);
      toast("Evaluation saved on the candidate profile.", "good");
      nav(`/candidates/${interview.data?.candidate_id ?? ""}`);
    } catch (e) { toast(errorMessage(e), "bad"); setBusy(false); }
  };
  const leave = () => { logout(); nav("/login"); };

  if (isCandidate && (room.ended || room.conn === "closed")) {
    return (
      <div className="auth-form" style={{ minHeight: "100%" }}><div className="consent">
        <h1>{room.ended ? "The interview has ended" : room.notice ? "You are not in the call" : "You left the interview"}</h1>
        <p className="muted">{room.notice ?? "Thank you. The employer will contact you about next steps. You can close this tab."}</p>
        <Button onClick={leave}>Close</Button>
      </div></div>
    );
  }

  if (!isCandidate && (room.ended || finished)) {
    const ratingCopy =
      rating === "pending" ? "Gemini is rating the sealed transcript. This usually takes a short while."
        : rating === "ready" ? "Rating is ready on the candidate profile."
          : rating === "failed" ? "Automatic rating did not finish. You can retry with Evaluate, or open the profile later."
            : "Download the transcript, then evaluate if needed.";
    return (
      <div className="auth-form" style={{ minHeight: "100%" }}><div className="consent">
        <h1>The interview has ended</h1>
        <p className="muted">{ratingCopy} Notes already saved stay on the candidate profile.</p>
        <div className="row">
          <Button onClick={download}>Download transcript</Button>
          {rating === "ready"
            ? <Link className="btn btn-primary" to={`/candidates/${interview.data?.candidate_id ?? ""}`}>View rating</Link>
            : <Button variant="primary" loading={busy || rating === "pending"} onClick={evaluate}>
              {rating === "pending" ? "Rating…" : "Evaluate"}
            </Button>}
          <Link className="btn" to={`/candidates/${interview.data?.candidate_id ?? ""}`}>Candidate profile</Link>
        </div>
      </div></div>
    );
  }

  const camOn = !room.cameraOff && !!room.local?.getVideoTracks().length;
  const rawCaption = room.caption ?? room.turns[room.turns.length - 1] ?? null;
  const liveCaption = rawCaption && rawCaption.language_mismatch && !rawCaption.edited
    ? { ...rawCaption, text: "not English, check the transcript" }
    : rawCaption;
  return (
    <div className={`room ${isCandidate ? "solo" : "staff"}`}>
      {!isCandidate && <CandidateBrief brief={brief.data ?? null} />}
      <div className="stage">
        <div className="stage-top">
          <div className="row">
            {!isCandidate && <Link to="/interviews" className="row"><ArrowLeft size={16} /> Interviews</Link>}
            <strong>{interview.data ? (isCandidate ? interview.data.job_title : `${brief.data?.name ?? interview.data.candidate_name} · ${interview.data.job_title}`) : "Interview"}</strong>
          </div>
          <div className="row">
            <Badge tone={room.conn === "open" ? "good" : room.conn === "closed" ? "bad" : "warn"}>{room.conn === "open" ? "Connected" : titleCase(room.conn)}</Badge>
            {!isCandidate && room.faceCheck && (
              <Badge tone={room.faceCheck.match ? "good" : "bad"}>
                {room.faceCheck.match ? "Face match" : room.faceCheck.reason === "no_face" ? "No face found" : "Face mismatch"}
              </Badge>
            )}
            <span className="muted" aria-label="Elapsed time">{formatClock(elapsed)}</span>
          </div>
        </div>
        <div className="stage-body">
          {room.conn === "reconnecting" && <div className="banner" role="alert">Connection lost. Reconnecting...</div>}
          {room.conn === "closed" && !room.ended && <div className="banner" role="alert">{room.notice ?? "The call disconnected. Reload the page to join again."}</div>}
          {room.mediaError && <div className="banner" role="alert">{room.mediaError}</div>}
          {room.speechNote && !isCandidate && <div className="banner" role="status">{room.speechNote}</div>}
          {room.sttNote && !isCandidate && room.sttState === "unavailable" && <div className="banner" role="status">{room.sttNote}</div>}
          {isCandidate && (
            <div className="banner" style={{ background: "#12303a", color: "#a5d8e6" }} role="status">
              This interview is being transcribed for the hiring team. Audio and video are not stored after the transcript is made.
            </div>
          )}
          <div className="video-main">
            <video ref={remoteRef} autoPlay playsInline aria-label="Other participant" />
            {!room.remote && <div className="video-wait"><Video size={30} aria-hidden /><strong>{room.peer ? "Connecting video..." : "Waiting for the other person to join"}</strong><span className="small">Keep this tab open. You will see and hear them here.</span></div>}
            {needsTap && room.remote && <button className="video-unlock" onClick={() => { void remoteRef.current?.play().then(() => setNeedsTap(false)); }}>Tap to hear the other person</button>}
            {!isCandidate && room.conn === "open" && (
              <>
                <div className="caption-status" aria-live="polite">
                  {room.sttMode === "browser"
                    ? "Browser speech (lower accuracy · Google)"
                    : room.sttState === "unavailable"
                      ? "Transcription unavailable"
                      : room.sttState === "transcribing" || room.sttPending > 0
                        ? `Transcribing…${room.sttPending > 0 ? ` ${room.sttPending} pending` : ""}`
                        : "Listening…"}
                </div>
                {liveCaption && (
                  <div className="live-caption" role="status" aria-live="polite" key={liveCaption.turn_id}>
                    <span className="live-caption-speaker">{titleCase(liveCaption.speaker)}</span>
                    <span className="live-caption-text">{liveCaption.text}</span>
                  </div>
                )}
              </>
            )}
            <div className="pip">
              <video ref={localRef} autoPlay muted playsInline aria-label="Your camera" />
              {!camOn && <div className="video-wait pip-cover"><VideoOff size={20} /></div>}
              <span className="pip-label">You</span>
            </div>
          </div>
        </div>
        <div className="controls" role="toolbar" aria-label="Call controls">
          <button className={`ctl ${room.muted ? "off" : ""}`} onClick={room.toggleMute} aria-pressed={room.muted} aria-label={room.muted ? "Unmute microphone" : "Mute microphone"}>{room.muted ? <MicOff size={20} /> : <Mic size={20} />}</button>
          <button className={`ctl ${room.cameraOff ? "off" : ""}`} onClick={room.toggleCamera} aria-pressed={room.cameraOff} aria-label={room.cameraOff ? "Turn camera on" : "Turn camera off"}>{room.cameraOff ? <VideoOff size={20} /> : <Video size={20} />}</button>
          {isCandidate
            ? <button className="ctl end" onClick={leave} aria-label="Leave interview"><PhoneOff size={20} /></button>
            : <button className="ctl end" disabled={busy} onClick={finish} aria-label="End interview for everyone"><PhoneOff size={20} /></button>}
        </div>
      </div>

      {!isCandidate && (
        <aside className="room-panel" aria-label="Interview tools">
          <Tabs
            tabs={[
              { id: "copilot", label: "Co-pilot" },
              { id: "notes", label: "Notes" },
              { id: "transcript", label: `Transcript${room.turns.length ? ` (${room.turns.length})` : ""}` },
            ]}
            value={tab}
            onChange={setTab}
          />
          <div className="panel-body">
            {tab === "copilot" && <CopilotPanel remaining={quota.remaining} limit={quota.limit} questions={questions} busy={asking} onAsk={ask} />}
            {tab === "notes" && <NotesPanel body={notes} markers={markers} saving={saving} onChange={setNotes} onMark={mark} />}
            {tab === "transcript" && (
              <TranscriptList
                turns={room.turns}
                onEdit={async (turnId, text) => {
                  const updated = await api.editTranscriptTurn(id, turnId, text);
                  room.replaceTurn(updated);
                }}
              />
            )}
          </div>
        </aside>
      )}
    </div>
  );
}
