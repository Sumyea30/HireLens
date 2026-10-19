import { Navigate, Outlet, useLocation } from "react-router-dom";
import type { Role } from "../lib/types";
import { useAuth } from "./AuthContext";

/** Route guard (N9). Candidates only ever reach the interview room. */
export function RequireRole({ roles }: { roles: Role[] }) {
  const { role } = useAuth();
  const loc = useLocation();
  if (!role) return <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  if (!roles.includes(role)) return <Navigate to={role === "candidate" ? "/forbidden" : "/"} replace />;
  return <Outlet />;
}
