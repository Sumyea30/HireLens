import { Eye, EyeOff, UserPlus } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, Navigate } from "react-router-dom";
import { ApiError, errorMessage } from "../api";
import { useAuth } from "../auth/AuthContext";
import { Button } from "../components/ui";
import { AuthFrame } from "./Login";

const EMAIL = /^[a-z0-9](?:[a-z0-9._%+-]{0,62}[a-z0-9])?@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z]{2,}$/i;

export default function Signup() {
  const { signup, role } = useAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (role) return <Navigate to={role === "candidate" ? "/forbidden" : "/"} replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!name.trim() || email.includes("..") || !EMAIL.test(email.trim())) {
      setError("Enter your name and a valid email address.");
      return;
    }
    if (password.length < 8) {
      setError("Use at least 8 characters for the password.");
      return;
    }
    setBusy(true);
    try {
      await signup(name, email, password);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) setError("An account with this email already exists.");
      else setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame>
      <form onSubmit={submit} noValidate>
        <div><h1>Create an account</h1><p className="muted">This email is your recruiter sign-in.</p></div>
        {error && <div className="error-box" role="alert">{error}</div>}
        <div className="field">
          <label htmlFor="name">Name</label>
          <input id="name" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        </div>
        <div className="field">
          <label htmlFor="signup-email">Email</label>
          <input id="signup-email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="signup-password">Password</label>
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <input id="signup-password" type={show ? "text" : "password"} autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            <button type="button" className="icon-btn" aria-label={show ? "Hide password" : "Show password"} onClick={() => setShow((s) => !s)}>
              {show ? <EyeOff size={18} /> : <Eye size={18} />}
            </button>
          </div>
        </div>
        <Button variant="primary" type="submit" loading={busy}><UserPlus size={16} /> Create account</Button>
        <p className="muted small">Already have an account? <Link to="/login">Sign in</Link></p>
      </form>
    </AuthFrame>
  );
}
