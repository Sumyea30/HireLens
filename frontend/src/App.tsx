import { Navigate, Route, Routes } from "react-router-dom";
import { RequireRole } from "./auth/RequireRole";
import { Layout } from "./components/Layout";
import Assessment from "./pages/Assessment";
import BatchReview from "./pages/BatchReview";
import Assessments from "./pages/Assessments";
import CandidateDetail from "./pages/CandidateDetail";
import Candidates from "./pages/Candidates";
import Compare from "./pages/Compare";
import Dashboard from "./pages/Dashboard";
import InterviewRoom from "./pages/InterviewRoom";
import Interviews from "./pages/Interviews";
import JobAssistant from "./pages/JobAssistant";
import JobDetail from "./pages/JobDetail";
import Jobs from "./pages/Jobs";
import Join from "./pages/Join";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import Logs from "./pages/Logs";
import { Forbidden, NotFound } from "./pages/NotFound";
import Policies from "./pages/Policies";
import Settings from "./pages/Settings";

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/signup" element={<Signup />} />
      <Route path="/join/:token" element={<Join />} />
      <Route path="/forbidden" element={<Forbidden />} />

      {/* The room is the only protected page a candidate can reach (N9). */}
      <Route element={<RequireRole roles={["interviewer", "admin", "candidate"]} />}>
        <Route path="/interviews/:id/room" element={<InterviewRoom />} />
      </Route>

      <Route element={<RequireRole roles={["interviewer", "admin"]} />}>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="jobs" element={<Jobs />} />
          <Route path="jobs/new/:id?" element={<JobAssistant />} />
          <Route path="jobs/:id" element={<JobDetail />} />
          <Route path="candidates" element={<Candidates />} />
          <Route path="batch" element={<BatchReview />} />
          <Route path="candidates/:id" element={<CandidateDetail />} />
          <Route path="interviews" element={<Interviews />} />
          <Route path="assessments" element={<Assessments />} />
          <Route path="assessments/:id" element={<Assessment />} />
          <Route path="compare" element={<Compare />} />
          <Route path="policies" element={<Policies />} />
          <Route element={<RequireRole roles={["admin"]} />}>
            <Route path="logs" element={<Logs />} />
          </Route>
          <Route path="settings" element={<Settings />} />
        </Route>
      </Route>
      <Route path="/home" element={<Navigate to="/" replace />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
