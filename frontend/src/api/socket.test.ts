// @vitest-environment node
import { describe, expect, it, vi } from "vitest";

vi.stubEnv("VITE_USE_MOCK", "true");

describe("mock room socket transcript isolation", () => {
  it("sends fake transcript turns only to interviewer role", async () => {
    const { openRoomSocket } = await import("./socket");
    const interviewer: unknown[] = [];
    const candidate: unknown[] = [];
    const staff = openRoomSocket("int-1", "t", "interviewer", {
      onMessage: (m) => interviewer.push(m),
      onState: () => {},
    });
    const person = openRoomSocket("int-1", "t", "candidate", {
      onMessage: (m) => candidate.push(m),
      onState: () => {},
    });
    await new Promise((r) => setTimeout(r, 3500));
    staff.close();
    person.close();
    expect(interviewer.some((m) => (m as { type: string }).type === "transcript")).toBe(true);
    expect(candidate.some((m) => (m as { type: string }).type === "transcript")).toBe(false);
  });
});
