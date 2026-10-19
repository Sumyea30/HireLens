import { afterEach, describe, expect, it, vi } from "vitest";
import { realApi } from "./real";

const file = (name: string) => new File(["%PDF-1.4"], name, { type: "application/pdf" });

afterEach(() => vi.unstubAllGlobals());

describe("uploadCvs", () => {
  it("sends one request per file and keeps going when one is refused", async () => {
    const calls: number[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      calls.push((init.body as FormData).getAll("files").length);
      const name = ((init.body as FormData).get("files") as File).name;
      if (name === "big.pdf") return new Response("", { status: 413 });
      return new Response(JSON.stringify([{ filename: name, status: "queued", reason: null }]), { status: 200 });
    }));
    const out = await realApi.uploadCvs("job-1", [file("a.pdf"), file("big.pdf"), file("c.pdf")]);
    expect(calls).toEqual([1, 1, 1]);
    expect(out.map((r) => r.status)).toEqual(["queued", "rejected", "queued"]);
    expect(out[1].reason).toBe("That file is too large.");
  });

  it("turns a network failure into a rejected entry instead of throwing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline"); }));
    const out = await realApi.uploadCvs("job-1", [file("a.pdf")]);
    expect(out).toEqual([{ filename: "a.pdf", status: "rejected", reason: "Cannot reach the server. Check your connection and try again." }]);
  });
});
