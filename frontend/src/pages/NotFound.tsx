import { Link } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { EmptyState } from "../components/ui";

export function NotFound() {
  return <div className="auth-form" style={{ minHeight: "100%" }}><EmptyState title="Page not found" text="The page you are looking for does not exist." action={<Link className="btn" to="/">Go home</Link>} /></div>;
}

export function Forbidden() {
  const { logout } = useAuth();
  return (
    <div className="auth-form" style={{ minHeight: "100%" }}>
      <EmptyState title="You do not have access to this page" text="Candidates can only join their own interview. Use the link you received by email."
        action={<button className="btn" onClick={logout}>Sign out</button>} />
    </div>
  );
}
