import { USE_MOCK } from "./client";
import type { Api } from "./contract";
import { mockApi } from "./mock";
import { realApi } from "./real";

export const api: Api = USE_MOCK ? mockApi : realApi;
export { USE_MOCK, MAX_UPLOAD_MB } from "./client";
export { ApiError } from "./client";
export function errorMessage(e: unknown): string {
  if (e instanceof Error && "userMessage" in e) {
    const ref = (e as { reference?: string }).reference;
    return `${(e as { userMessage: string }).userMessage}${ref ? ` (reference ${ref})` : ""}`;
  }
  return "Something went wrong. Please try again.";
}
