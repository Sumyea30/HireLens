import { useNavigate } from "react-router-dom";
import { USE_MOCK } from "../api";
import { useAuth } from "../auth/AuthContext";
import { Button, Card, PageHeader } from "../components/ui";
import { useTheme, type ThemePref } from "../hooks/useTheme";

export default function Settings() {
  const { session, logout } = useAuth();
  const { pref, setPref } = useTheme();
  const nav = useNavigate();
  return (
    <>
      <PageHeader title="Settings" subtitle="Your account, appearance and privacy information." />
      <div className="grid-2">
        <Card title="Account">
          <div className="stack">
            <div className="field-state"><span>Name</span><strong>{session?.user.name}</strong></div>
            <div className="field-state"><span>Role</span><strong>{session?.user.role}</strong></div>
            <div className="field-state"><span>Data source</span><strong>{USE_MOCK ? "Demo (synthetic)" : "Live backend"}</strong></div>
            <div><Button onClick={() => { logout(); nav("/login"); }}>Sign out</Button></div>
          </div>
        </Card>
        <Card title="Appearance">
          <div className="field"><label htmlFor="theme">Theme</label>
            <select id="theme" value={pref} onChange={(e) => setPref(e.target.value as ThemePref)}>
              <option value="system">Match my device</option><option value="light">Light</option><option value="dark">Dark</option>
            </select>
          </div>
        </Card>
      </div>
      <Card title="Privacy and data handling">
        <ul className="list">
          <li>Only the transcript of an interview is kept. Audio and video are not stored.</li>
          <li>Age, gender, ethnicity, nationality, religion and marital status are removed before scoring and never used.</li>
          <li>Every score and finding links to the CV text or transcript segment behind it. People make the decisions.</li>
          <li>An administrator can delete a candidate and all related data.</li>
          <li>Face and voice recognition are not used.</li>
          <li>Logs contain event names and IDs only. They never contain names, contact details, CV text or transcripts.</li>
          <li>External services that receive data (language model, embeddings, speech-to-text, posting channel, mail): see the project README for exactly what each receives.</li>
        </ul>
      </Card>
    </>
  );
}
