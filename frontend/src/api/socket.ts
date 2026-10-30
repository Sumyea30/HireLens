import { log } from "../lib/logger";
import type { CoverageItem, Role, RoomIn, RoomOut } from "../lib/types";
import { USE_MOCK, WS_BASE } from "./client";
import { REQUIREMENTS, TRANSCRIPT } from "./mockData";

export type RoomConnState = "connecting" | "open" | "reconnecting" | "closed";
export interface RoomHandlers {
  onMessage(m: RoomIn): void;
  onState(s: RoomConnState, code?: number): void;
}
export interface RoomSocket {
  send(m: RoomOut): void;
  /** Binary audio chunk from this participant's own microphone (one channel per person). */
  sendAudio(buf: ArrayBuffer): void;
  close(): void;
}

const MAX_RECONNECTS = 6;
// Auth failed, not allowed (full, finished, or wrong person), or replaced by a newer tab.
const TERMINAL_CLOSE = new Set([4401, 4403, 4000]);

function openRealSocket(interviewId: string, token: string, h: RoomHandlers): RoomSocket {
  let ws: WebSocket | null = null;
  let closed = false;
  let attempts = 0;
  let retry: ReturnType<typeof setTimeout> | undefined;
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${WS_BASE}/interview/${encodeURIComponent(interviewId)}`;

  const connect = () => {
    if (closed) return;
    h.onState(attempts === 0 ? "connecting" : "reconnecting");
    const socket = new WebSocket(url);
    ws = socket;
    socket.binaryType = "arraybuffer";
    socket.onopen = () => {
      if (ws !== socket) return;
      attempts = 0;
      // The token travels in the first message, not in the URL, so it stays out of proxy logs.
      socket.send(JSON.stringify({ type: "auth", token } satisfies RoomOut));
      h.onState("open");
      log.info("room_socket_open", { interview_id: interviewId });
    };
    socket.onmessage = (ev) => {
      if (ws !== socket || typeof ev.data !== "string") return;
      try {
        const msg = JSON.parse(ev.data) as RoomIn;
        if (msg && typeof msg.type === "string") h.onMessage(msg);
      } catch {
        log.warn("room_bad_message");
      }
    };
    socket.onclose = (ev) => {
      if (ws !== socket) return;
      if (closed || TERMINAL_CLOSE.has(ev.code)) { h.onState("closed", ev.code); return; }
      attempts += 1;
      if (attempts > MAX_RECONNECTS) { log.error("room_socket_gave_up"); h.onState("closed", ev.code); return; }
      log.warn("room_socket_reconnecting", { attempt: attempts });
      h.onState("reconnecting");
      retry = setTimeout(connect, Math.min(1000 * 2 ** attempts, 10_000));
    };
    socket.onerror = () => { /* onclose follows and handles reconnects */ };
  };
  connect();

  return {
    send(m) { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(m)); },
    sendAudio(b) { if (ws?.readyState === WebSocket.OPEN) ws.send(b); },
    close() { closed = true; clearTimeout(retry); ws?.close(); },
  };
}

const COVERED_AFTER: string[][] = [[], ["R1", "R2"], ["R1", "R2"], ["R1", "R2", "R3"], ["R1", "R2", "R3"], ["R1", "R2", "R3", "R4"]];
const coverage = (i: number): CoverageItem[] =>
  REQUIREMENTS.map((r) => ({ requirement_id: r.id, text: r.text, state: COVERED_AFTER[i]?.includes(r.id) ? "covered" : "not_yet" }));

function openMockSocket(role: Role, h: RoomHandlers): RoomSocket {
  let closed = false;
  const timers: ReturnType<typeof setTimeout>[] = [];
  const at = (ms: number, fn: () => void) => { timers.push(setTimeout(() => { if (!closed) fn(); }, ms)); };
  h.onState("connecting");
  at(250, () => h.onState("open"));
  at(900, () => h.onMessage({ type: "peer", state: "joined" }));
  if (role === "interviewer") {
    h.onMessage({ type: "coverage", items: coverage(0) });
    TRANSCRIPT.forEach((turn, i) => at(3000 + i * 4500, () => {
      h.onMessage({ type: "transcript", turn });
      h.onMessage({ type: "coverage", items: coverage(i) });
    }));
  }
  return { send() {}, sendAudio() {}, close() { closed = true; timers.forEach(clearTimeout); } };
}

export function openRoomSocket(interviewId: string, token: string, role: Role, h: RoomHandlers): RoomSocket {
  return USE_MOCK ? openMockSocket(role, h) : openRealSocket(interviewId, token, h);
}
