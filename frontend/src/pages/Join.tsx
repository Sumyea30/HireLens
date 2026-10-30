import { Video } from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { USE_MOCK, api, errorMessage } from "../api";
import { useAuth } from "../auth/AuthContext";
import { Button } from "../components/ui";
import { log } from "../lib/logger";

/** Candidate entry. The link is the credential, and consent is explicit (N17). */
export default function Join() {
  const { token = "" } = useParams();
  const { joinWith } = useAuth();
  const nav = useNavigate();
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const join = async () => {
    setBusy(true);
    setError(null);
    try {
      const s = await api.joinInterview(token, consent);
      joinWith(s);
      nav(`/interviews/${s.interview_id}/room`, { replace: true });
    } catch (e) {
      log.warn("join_failed");
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-form" style={{ minHeight: "100%" }}>
      <div className="consent">
        <div className="brand"><span className="logo" aria-hidden /> <strong>HireLens</strong></div>
        <h1>Join your interview</h1>
        <p className="muted">The room opens 5 minutes before the scheduled time. You can still join up to 10 minutes after it starts. Before you join, please read how your interview is handled.</p>
        <div className="card stack">
          <ul className="list">
            <li>Your camera and microphone are used for the live video call.</li>
            <li>Your speech is transcribed on our servers for the hiring team only. You will not see the transcript. Audio and video are not kept after the text is made.</li>
            <li>The transcript and an AI-assisted assessment are reviewed by people. AI does not make the hiring decision.</li>
            <li>You can ask the employer to delete your data.</li>
          </ul>
          <label className="check">
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
            <span>I understand and agree to the transcription of this interview.</span>
          </label>
        </div>
        {USE_MOCK && <div className="hint">Demo mode. A valid demo link is <code>/join/join-int-3-demo</code>.</div>}
        {error && <div className="error-box" role="alert">{error}</div>}
        <Button variant="primary" disabled={!consent} loading={busy} onClick={join}><Video size={16} /> Join interview</Button>
      </div>
    </div>
  );
}
