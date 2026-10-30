// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CandidateBrief } from "./CandidateBrief";
import { CopilotPanel } from "./CopilotPanel";
import { NotesPanel } from "./NotesPanel";
import type { InterviewBrief } from "../lib/types";

afterEach(cleanup);

const brief: InterviewBrief = {
  name: "Ada", role: "Backend Developer", aggregate_score: 76,
  dimension_scores: [{ dimension: "technical_skills", score: 80, weight: 0.5, justification: "Match", evidence_refs: [] }],
  matches: [
    { requirement_id: "R1", requirement_text: "Python", status: "met", justification: "Shown", evidence: [] },
    { requirement_id: "R2", requirement_text: "Docker", status: "unmet", justification: "Missing", evidence: [] },
  ],
  gaps: [{ requirement_id: "R2", requirement_text: "Docker", status: "unmet", justification: "Missing", evidence: [] }],
};

describe("interview room panels", () => {
  it("shows the candidate brief with gaps", () => {
    render(<CandidateBrief brief={brief} />);
    expect(screen.getByText("Ada")).toBeTruthy();
    expect(screen.getByText("Backend Developer")).toBeTruthy();
    expect(screen.getByText("76")).toBeTruthy();
    expect(screen.getAllByText("Docker").length).toBeGreaterThan(0);
    expect(screen.getByText("Gaps to probe")).toBeTruthy();
  });

  it("shows how many co-pilot questions are left and disables input at zero", async () => {
    const user = userEvent.setup();
    const onAsk = vi.fn();
    const { rerender } = render(<CopilotPanel remaining={3} limit={15} questions={[]} busy={false} onAsk={onAsk} />);
    expect(screen.getByText("3 of 15 questions left")).toBeTruthy();
    await user.type(screen.getByLabelText("Ask the co-pilot"), "What next?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(onAsk).toHaveBeenCalledWith("What next?");
    rerender(<CopilotPanel remaining={0} limit={15} questions={[]} busy={false} onAsk={onAsk} />);
    expect((screen.getByLabelText("Ask the co-pilot") as HTMLTextAreaElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Ask" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps notes within the field and can mark a moment", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onMark = vi.fn();
    render(<NotesPanel body="Ask about testing" markers={[{ offset_ms: 65000, at: "t1" }]} saving={false} onChange={onChange} onMark={onMark} />);
    expect(screen.getByLabelText("Private notes")).toHaveProperty("maxLength", 5000);
    await user.type(screen.getByLabelText("Private notes"), "!");
    expect(onChange).toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Mark this moment" }));
    expect(onMark).toHaveBeenCalled();
    expect(screen.getByText(/Marked at/)).toBeTruthy();
  });
});
