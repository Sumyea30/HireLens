import { describe, expect, it } from "vitest";
import { clamp, formatClock, normalizeWeights, refLabel } from "./format";
import { clearEntries, getEntries, log, sanitize, setLevel, toLogText } from "./logger";
import {
  STT_FRAME_BYTES,
  STT_FRAME_SAMPLES,
  downmixToMono,
  encodePcm16Le,
  floatToInt16Sample,
} from "./pcmConvert";

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

describe("pcmConvert", () => {
  it("uses 250 ms frames of 8000 bytes at 16 kHz mono PCM16", () => {
    expect(STT_FRAME_SAMPLES).toBe(4000);
    expect(STT_FRAME_BYTES).toBe(8000);
  });

  it("clamps Float32 to Int16 little-endian samples", () => {
    expect(floatToInt16Sample(0)).toBe(0);
    expect(floatToInt16Sample(1)).toBe(0x7fff);
    expect(floatToInt16Sample(-1)).toBe(-0x8000);
    expect(floatToInt16Sample(2)).toBe(0x7fff);
    expect(floatToInt16Sample(-3)).toBe(-0x8000);
    const buf = encodePcm16Le(new Float32Array([0.5, -0.5]));
    expect(buf.byteLength).toBe(4);
    const view = new DataView(buf);
    expect(view.getInt16(0, true)).toBe(floatToInt16Sample(0.5));
    expect(view.getInt16(2, true)).toBe(floatToInt16Sample(-0.5));
  });

  it("downmixes stereo to mono", () => {
    const L = new Float32Array([1, 0]);
    const R = new Float32Array([-1, 0.5]);
    const m = downmixToMono([L, R]);
    expect([...m]).toEqual([0, 0.25]);
  });
});
