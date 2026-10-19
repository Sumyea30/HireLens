import { Eye, EyeOff, LogIn } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { Link, Navigate, useLocation } from "react-router-dom";
import { USE_MOCK, errorMessage } from "../api";
import { useAuth } from "../auth/AuthContext";
import { Button } from "../components/ui";

export function AuthFrame({ children }: { children: ReactNode }) {
  return (
    <div className="auth">
      <div className="auth-hero">
        <div className="brand"><span className="logo" aria-hidden /> <strong>HireLens</strong></div>
        <h1>See the evidence behind every hiring decision.</h1>
        <ul>
          <li>Describe a role in plain words and review a policy-checked job post.</li>
          <li>Screen CVs against requirements, with a score you can trace.</li>
          <li>Interview with a live, speaker-labelled transcript.</li>
          <li>Decide with an assessment that cites its sources.</li>
        </ul>
      </div>
      <div className="auth-form">{children}</div>
    </div>
  );
}

export default function Login() {
  const { login, role } = useAuth();
  const loc = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const from = (loc.state as { from?: string } | null)?.from ?? "/";
  if (role) return <Navigate to={role === "candidate" ? "/forbidden" : from} replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!email.trim() || !password) { setError("Enter your email and password."); return; }
    setBusy(true);
    try {
      await login(email, password);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame>
      <form onSubmit={submit} noValidate>
        <div><h1>Sign in</h1><p className="muted">For interviewers and administrators.</p></div>
        {USE_MOCK && (
          <div className="hint">Demo mode with synthetic data. Use <strong>interviewer</strong> or <strong>admin</strong> with password <strong>demo-pass</strong>.</div>
        )}
        {error && <div className="error-box" role="alert">{error}</div>}
        <div className="field">
          <label htmlFor="email">Email</label>
          <input id="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
        </div>
        <div className="field">
          <label htmlFor="p">Password</label>
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <input id="p" type={show ? "text" : "password"} autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            <button type="button" className="icon-btn" aria-label={show ? "Hide password" : "Show password"} onClick={() => setShow((s) => !s)}>
              {show ? <EyeOff size={18} /> : <Eye size={18} />}
            </button>
          </div>
        </div>
        <Button variant="primary" type="submit" loading={busy}><LogIn size={16} /> Sign in</Button>
        <p className="muted small">New recruiter? <Link to="/signup">Create an account</Link></p>
        <p className="muted small">Candidates do not sign in. Use the interview link you received by email.</p>
      </form>
    </AuthFrame>
  );
}
