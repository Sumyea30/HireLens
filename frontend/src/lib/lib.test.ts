import { describe, expect, it } from "vitest";
import { clamp, formatClock, normalizeWeights, refLabel } from "./format";
import { clearEntries, getEntries, log, sanitize, setLevel, toLogText } from "./logger";

describe("logger", () => {
  it("drops keys that could hold personal data or secrets", () => {
    const ctx = sanitize({ candidate_id: "c1", email: "a@b.c", token: "x", cv_text: "y", count: 3, nested: { a: 1 } });
    expect(ctx).toEqual({ candidate_id: "c1", count: 3 });
  });
  it("keeps entries and respects the level", () => {
    clearEntries();
    setLevel("info");
    log.debug("hidden");
    log.info("page_view", { path: "/jobs", name: "secret" });
    expect(getEntries().map((e) => e.event)).toEqual(["page_view"]);
    expect(toLogText(getEntries())).toContain('page_view {"path":"/jobs"}');
  });
});

describe("format", () => {
  it("formats clocks, labels and weights", () => {
    expect(formatClock(65000)).toBe("01:05");
    expect(refLabel("tr:s1:t2")).toBe("Transcript t2");
    expect(refLabel("req:R2")).toBe("Requirement R2");
    expect(clamp(120, 0, 100)).toBe(100);
    expect(normalizeWeights({ a: 2, b: 2 })).toEqual({ a: 0.5, b: 0.5 });
  });
});
