// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { setSession } from "./auth/session";
import { ToastProvider } from "./components/Toast";

const mount = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider><ToastProvider><App /></ToastProvider></AuthProvider>
    </MemoryRouter>,
  );

const asInterviewer = () => setSession({ token: "t", user: { id: "u", name: "Demo Interviewer", role: "interviewer" } });

beforeEach(() => sessionStorage.clear());
afterEach(cleanup);

describe("app in demo mode", () => {
  it("redirects to login, rejects bad credentials, then signs in", async () => {
    const user = userEvent.setup();
    mount("/candidates");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
    await user.type(screen.getByLabelText("Email"), "interviewer");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByText(/not correct/i)).toBeTruthy();
    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), "demo-pass");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("heading", { name: "Candidates" })).toBeTruthy();
  });

  it("creates an email account and signs that recruiter in", async () => {
    const user = userEvent.setup();
    mount("/signup");
    expect(await screen.findByRole("heading", { name: "Create an account" })).toBeTruthy();
    await user.type(screen.getByLabelText("Name"), "Samira");
    await user.type(screen.getByLabelText("Email"), "samira@example.com");
    await user.type(screen.getByLabelText("Password"), "short");
    await user.click(screen.getByRole("button", { name: /create account/i }));
    expect(await screen.findByText(/at least 8 characters/i)).toBeTruthy();
    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), "long-enough");
    await user.click(screen.getByRole("button", { name: /create account/i }));
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeTruthy();
  });

  it("shows ranked candidates with the failed CV last and unscored", async () => {
    asInterviewer();
    mount("/candidates");
    const first = await screen.findByText("Nadia Karim");
    expect(first).toBeTruthy();
    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("Nadia Karim")).toBeTruthy();
    expect(within(rows[rows.length - 1]).getByText("Failed")).toBeTruthy();
  });

  it("keeps candidates out of every page except the room", async () => {
    setSession({ token: "t", user: { id: "g", name: "Guest", role: "candidate" } });
    mount("/candidates");
    expect(await screen.findByText(/do not have access/i)).toBeTruthy();
  });

  it("runs the job assistant: collect, draft, approve, publish", async () => {
    const user = userEvent.setup();
    asInterviewer();
    mount("/jobs/new");
    await user.type(await screen.findByLabelText("Job title"), "Backend Developer");
    await user.type(screen.getByLabelText("Company"), "HireLens");
    await user.type(screen.getByLabelText("Location"), "Dhaka, Bangladesh");
    await user.selectOptions(screen.getByLabelText("Employment type"), "Full-time");
    await user.selectOptions(screen.getByLabelText("Experience level"), "Mid-level");
    await user.type(screen.getByLabelText("Salary range"), "80000 BDT per month");
    await user.type(screen.getByLabelText("Application deadline"), "1 November 2026");
    await user.type(screen.getByLabelText("Years of experience"), "3");
    await user.type(screen.getByLabelText("About the role"), "Build hiring tools with the team.");
    await user.type(screen.getByLabelText("Key responsibilities"), "Build APIs");
    await user.type(screen.getByLabelText("Education"), "BSc in Computer Science");
    await user.type(screen.getByLabelText("Technical skills"), "Python, Docker");
    expect(screen.getByLabelText("Contact")).toHaveProperty("value", "sumyeac@gmail.com");
    expect(screen.queryByLabelText("Documents to include")).toBeNull();
    await user.click(screen.getByRole("button", { name: /create draft/i }));
    const approve = await screen.findByRole("button", { name: /approve and publish/i }, { timeout: 4000 });
    await user.click(approve);
    await waitFor(() => expect(screen.getByText(/Candidates apply by email/i)).toBeTruthy(), { timeout: 4000 });
  });

  it("blocks publishing a post without the equal opportunity statement", async () => {
    const user = userEvent.setup();
    asInterviewer();
    mount("/jobs/new/job-2");
    const box = await screen.findByLabelText(/Draft job post/i);
    await user.clear(box);
    await user.type(box, "Data Analyst needed.");
    await user.click(screen.getByRole("button", { name: /approve and publish/i }));
    expect(await screen.findByText(/equal opportunity statement/i)).toBeTruthy();
  });

  it("opens an assessment and shows evidence behind a finding", async () => {
    const user = userEvent.setup();
    asInterviewer();
    mount("/assessments/asm-1");
    expect(await screen.findByText(/Assessment: Nadia Karim/)).toBeTruthy();
    await user.click((await screen.findAllByRole("button", { name: "Transcript t2" }))[0]);
    expect(await screen.findByText(/versioned through the URL path/i)).toBeTruthy();
  });
});
