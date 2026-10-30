import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { USE_MOCK } from "../api/client";
import { openRoomSocket, type RoomConnState, type RoomSocket } from "../api/socket";
import { log } from "../lib/logger";
import { startBrowserSpeech, type BrowserSpeechSender } from "../lib/browserSpeech";
import { startPcmCapture, type PcmSender } from "../lib/pcmCapture";
import type { CoverageItem, FaceCheckResult, Role, RoomIn, SignalKind, TranscriptTurn } from "../lib/types";

/** Mirror backend STT_PROVIDER=browser. Audio goes to the browser/Google speech service. */
const USE_BROWSER_STT = (import.meta.env.VITE_STT_PROVIDER as string | undefined)?.trim().toLowerCase() === "browser";
const BROWSER_STT_LANG = (import.meta.env.VITE_BROWSER_STT_LANG as string | undefined)?.trim() || "en-IN";

const DEFAULT_ICE: RTCIceServer[] = [
  { urls: "stun:stun.l.google.com:19302" },
  { urls: "stun:stun1.l.google.com:19302" },
];

function iceServers(): RTCIceServer[] {
  try {
    const raw = import.meta.env.VITE_ICE_SERVERS as string | undefined;
    if (!raw) return DEFAULT_ICE;
    const parsed = JSON.parse(raw) as RTCIceServer[];
    return Array.isArray(parsed) && parsed.length ? parsed : DEFAULT_ICE;
  } catch {
    return DEFAULT_ICE;
  }
}

/**
 * Camera, microphone and a one-to-one WebRTC call.
 * The interviewer creates the offer when the other person is in the room.
 * The candidate answers. ICE candidates are applied only after the remote
 * description is set, and in the order they arrived.
 */
export function useInterviewRoom(interviewId: string, token: string, role: Role) {
  const [local, setLocal] = useState<MediaStream | null>(null);
  const [remote, setRemote] = useState<MediaStream | null>(null);
  const [conn, setConn] = useState<RoomConnState>("connecting");
  const [peer, setPeer] = useState(false);
  const [turns, setTurns] = useState<TranscriptTurn[]>([]);
  const [coverage, setCoverage] = useState<CoverageItem[]>([]);
  const [muted, setMuted] = useState(false);
  const [cameraOff, setCameraOff] = useState(false);
  const [ended, setEnded] = useState(false);
  const [mediaError, setMediaError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [speechNote, setSpeechNote] = useState<string | null>(null);
  const [sttNote, setSttNote] = useState<string | null>(null);
  const [sttState, setSttState] = useState<"listening" | "transcribing" | "unavailable" | null>(null);
  const [sttPending, setSttPending] = useState(0);
  const [speechActive, setSpeechActive] = useState(false);
  const [caption, setCaption] = useState<TranscriptTurn | null>(null);
  const [faceCheck, setFaceCheck] = useState<FaceCheckResult | null>(null);
  const faceTried = useRef(false);
  const captionTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const stream = useRef<MediaStream | null>(null);
  const sock = useRef<RoomSocket | null>(null);
  const pc = useRef<RTCPeerConnection | null>(null);
  const pcm = useRef<PcmSender | null>(null);
  const browserSpeech = useRef<BrowserSpeechSender | null>(null);
  const mutedRef = useRef(false);
  const isStaff = role === "interviewer" || role === "admin";
  const [sttMode] = useState<"pcm" | "browser">(USE_BROWSER_STT ? "browser" : "pcm");
  const offered = useRef(false);
  const pendingIce = useRef<RTCIceCandidateInit[]>([]);
  const epoch = useRef(0);
  const restarts = useRef(0);
  const chain = useRef(Promise.resolve());

  useEffect(() => {
    if (!isStaff || USE_MOCK || !interviewId) return;
    let cancel = false;
    const mergeTurns = (incoming: TranscriptTurn[]) => {
      if (cancel || !incoming.length) return;
      setTurns((list) => {
        const byId = new Map(list.map((t) => [t.turn_id, t]));
        for (const t of incoming) byId.set(t.turn_id, t);
        return [...byId.values()].sort((a, b) => (a.start_ms ?? 0) - (b.start_ms ?? 0));
      });
      const latest = [...incoming].sort((a, b) => (a.start_ms ?? 0) - (b.start_ms ?? 0)).at(-1);
      if (latest) setCaption((cur) => cur ?? latest);
    };
    const pull = () => {
      api.getTranscript(interviewId).then((doc) => mergeTurns(doc?.turns ?? [])).catch(() => {});
    };
    pull();
    // WS can miss a broadcast after refresh; poll as a safety net.
    const timer = window.setInterval(pull, 4000);
    return () => { cancel = true; window.clearInterval(timer); };
  }, [interviewId, isStaff]);

  useEffect(() => {
    let cancelled = false;
    const offerer = role !== "candidate";

    const enqueue = (task: () => Promise<void>) => {
      chain.current = chain.current.then(task, task);
    };

    const teardown = () => {
      epoch.current += 1;
      offered.current = false;
      pendingIce.current = [];
      const current = pc.current;
      pc.current = null;
      if (current) {
        current.onconnectionstatechange = null;
        current.onicecandidate = null;
        current.ontrack = null;
        current.close();
      }
      setRemote(null);
    };

    const makePc = (): RTCPeerConnection => {
      const previous = pc.current;
      if (previous) {
        previous.onconnectionstatechange = null;
        previous.onicecandidate = null;
        previous.ontrack = null;
        previous.close();
      }
      const p = new RTCPeerConnection({ iceServers: iceServers() });
      pc.current = p;
      const localStream = stream.current;
      const audio = localStream?.getAudioTracks() ?? [];
      const video = localStream?.getVideoTracks() ?? [];
      if (localStream && audio.length) audio.forEach((t) => p.addTrack(t, localStream));
      else p.addTransceiver("audio", { direction: "recvonly" });
      if (localStream && video.length) video.forEach((t) => p.addTrack(t, localStream));
      else p.addTransceiver("video", { direction: "recvonly" });

      const remoteMedia = new MediaStream();
      p.ontrack = (ev) => {
        if (pc.current !== p) return;
        if (!remoteMedia.getTracks().some((t) => t.id === ev.track.id)) remoteMedia.addTrack(ev.track);
        setRemote(remoteMedia);
      };
      p.onicecandidate = (ev) => {
        if (ev.candidate && pc.current === p) {
          sock.current?.send({ type: "signal", kind: "ice", data: ev.candidate.toJSON() });
        }
      };
      p.onconnectionstatechange = () => {
        if (pc.current !== p) return;
        log.info("rtc_state", { state: p.connectionState });
        if (p.connectionState === "connected") restarts.current = 0;
        if (p.connectionState === "failed" && offerer && restarts.current < 2 && p.signalingState === "stable") {
          restarts.current += 1;
          enqueue(async () => {
            if (cancelled || pc.current !== p) return;
            try {
              const offer = await p.createOffer({ iceRestart: true });
              if (cancelled || pc.current !== p) return;
              await p.setLocalDescription(offer);
              sock.current?.send({ type: "signal", kind: "offer", data: p.localDescription });
            } catch {
              log.warn("rtc_ice_restart_failed");
            }
          });
        }
      };
      return p;
    };

    const flushIce = async (p: RTCPeerConnection) => {
      const queued = pendingIce.current.splice(0);
      for (const candidate of queued) {
        try { await p.addIceCandidate(candidate); } catch { log.warn("rtc_ice_failed"); }
      }
    };

    const startOffer = async () => {
      if (!offerer || offered.current || cancelled) return;
      offered.current = true;
      pendingIce.current = [];
      const gen = epoch.current;
      const p = makePc();
      try {
        const offer = await p.createOffer();
        if (cancelled || gen !== epoch.current || pc.current !== p) return;
        await p.setLocalDescription(offer);
        if (cancelled || gen !== epoch.current || pc.current !== p) return;
        sock.current?.send({ type: "signal", kind: "offer", data: p.localDescription });
      } catch {
        if (pc.current === p) offered.current = false;
        log.warn("rtc_signal_failed", { kind: "offer" });
      }
    };

    const onSignal = async (kind: SignalKind, data: unknown) => {
      try {
        if (kind === "offer") {
          const incoming = data as RTCSessionDescriptionInit;
          let p = pc.current;
          const glare = !!p && p.signalingState === "have-local-offer";
          if (glare && p) {
            const mine = p.localDescription?.sdp ?? "";
            const theirs = incoming.sdp ?? "";
            // Both sides compare the same two descriptions, so exactly one of them continues.
            if (mine && theirs && mine < theirs) {
              pendingIce.current = [];
              return;
            }
            const rollbackGen = epoch.current;
            await p.setLocalDescription({ type: "rollback" });
            if (cancelled || rollbackGen !== epoch.current || pc.current !== p) return;
            offered.current = false;
          } else {
            p = makePc();
          }
          if (!p) return;
          const early = pendingIce.current.splice(0);
          const gen = epoch.current;
          await p.setRemoteDescription(incoming);
          if (cancelled || gen !== epoch.current || pc.current !== p) return;
          for (const candidate of early) {
            try { await p.addIceCandidate(candidate); } catch { /* belongs to a replaced description */ }
          }
          await flushIce(p);
          const answer = await p.createAnswer();
          if (cancelled || gen !== epoch.current || pc.current !== p) return;
          await p.setLocalDescription(answer);
          if (cancelled || gen !== epoch.current || pc.current !== p) return;
          sock.current?.send({ type: "signal", kind: "answer", data: p.localDescription });
        } else if (kind === "answer") {
          const p = pc.current;
          if (!p || p.signalingState !== "have-local-offer") return;
          await p.setRemoteDescription(data as RTCSessionDescriptionInit);
          await flushIce(p);
        } else {
          const p = pc.current;
          const candidate = data as RTCIceCandidateInit;
          if (!candidate) return;
          if (!p || !p.remoteDescription) {
            pendingIce.current.push(candidate);
            return;
          }
          try { await p.addIceCandidate(candidate); } catch { log.warn("rtc_ice_failed"); }
        }
      } catch {
        log.warn("rtc_signal_failed", { kind });
      }
    };

    const onMessage = (m: RoomIn) => {
      if (cancelled) return;
      if (m.type === "stt_status") {
        // Server only sends these to staff; ignore if a candidate ever receives one.
        if (!isStaff) return;
        if (m.state) setSttState(m.state);
        if (typeof m.pending === "number") setSttPending(m.pending);
        if (m.message) setSttNote(m.message);
        else if (m.state === "listening") setSttNote(null);
        return;
      }
      if (m.type === "speech_activity") {
        if (!isStaff) return;
        setSpeechActive(!!m.active);
        return;
      }
      if (m.type === "transcript") {
        // Server only sends these to staff; ignore if a candidate ever receives one.
        if (!isStaff) return;
        if (!m.turn?.turn_id || !m.turn?.text) return;
        setTurns((list) => {
          const i = list.findIndex((t) => t.turn_id === m.turn.turn_id);
          if (i < 0) return [...list, m.turn];
          const next = list.slice();
          next[i] = m.turn;
          return next;
        });
        // Keep the latest caption on-screen until a newer turn replaces it.
        setCaption(m.turn);
        if (captionTimer.current) clearTimeout(captionTimer.current);
        return;
      }
      if (m.type === "coverage") { if (isStaff) setCoverage(m.items); return; }
      if (m.type === "ended") {
        setEnded(true);
        teardown();
        stream.current?.getTracks().forEach((t) => t.stop());
        return;
      }
      if (USE_MOCK) {
        if (m.type === "peer" && m.state === "joined") setPeer(true);
        if (m.type === "peer" && m.state === "left") { setPeer(false); setRemote(null); }
        return;
      }
      enqueue(async () => {
        if (cancelled) return;
        if (m.type === "signal") {
          await onSignal(m.kind, m.data);
          return;
        }
        if (m.type !== "peer") return;
        if (m.state === "left") {
          teardown();
          setPeer(false);
          return;
        }
        if (m.state === "joined") {
          teardown();
          setPeer(true);
          if (offerer) await startOffer();
          else sock.current?.send({ type: "ready" });
          return;
        }
        if (m.state === "ready") await startOffer();
      });
    };

    (async () => {
      let media: MediaStream | null = null;
      try {
        media = await navigator.mediaDevices.getUserMedia({
          // Prefer 16 kHz mono for STT. Keep AGC; avoid aggressive NS that
          // can strip speech energy before it reaches AssemblyAI.
          audio: {
            channelCount: 1,
            sampleRate: { ideal: 16000 },
            echoCancellation: true,
            noiseSuppression: false,
            autoGainControl: true,
          },
          video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" },
        });
      } catch {
        try {
          media = await navigator.mediaDevices.getUserMedia({
            audio: { channelCount: 1, echoCancellation: true, noiseSuppression: false, autoGainControl: true },
          });
          if (!cancelled) setMediaError("Your camera is not available, so you are joining with audio only.");
        } catch {
          if (!cancelled) setMediaError("We could not access your microphone or camera. Allow access in your browser and reload the page.");
        }
      }
      if (cancelled) { media?.getTracks().forEach((t) => t.stop()); return; }
      stream.current = media;
      setLocal(media);

      const socket = openRoomSocket(interviewId, token, role, {
        onMessage,
        onState: (st, code) => {
          if (cancelled) return;
          setConn(st);
          if (st === "reconnecting") teardown();
          if (st === "closed" && code === 4403) setNotice("This interview is full, finished, or you do not have access.");
          if (st === "closed" && code === 4000) setNotice("This interview was opened in another tab. That tab is the one in the call.");
          if (st === "closed" && code === 4401) setNotice("Your session has expired. Open the interview link again.");
          if (st === "open" && role === "candidate") sock.current?.send({ type: "ready" });
        },
      });
      if (cancelled) { socket.close(); media?.getTracks().forEach((t) => t.stop()); return; }
      sock.current = socket;

      if (!USE_MOCK && media) {
        if (USE_BROWSER_STT) {
          if (isStaff) {
            setSpeechNote("Browser speech (lower accuracy). Audio is processed by Google’s speech service in this browser.");
          }
          const speech = startBrowserSpeech((text) => {
            if (mutedRef.current || cancelled) return;
            sock.current?.send({ type: "speech", text });
          }, BROWSER_STT_LANG);
          if (cancelled) { speech?.stop(); return; }
          if (speech) browserSpeech.current = speech;
          else if (isStaff) setSpeechNote("This browser does not support SpeechRecognition. The call still works.");
        } else {
          const capture = await startPcmCapture(media, (buf) => {
            if (mutedRef.current || cancelled) return;
            sock.current?.sendAudio(buf);
          });
          if (cancelled) { capture?.stop(); return; }
          if (capture) pcm.current = capture;
          else if (isStaff) setSpeechNote("This browser cannot send audio for transcription. The call still works.");
        }
      }
      log.info("room_started", { role, stt: USE_BROWSER_STT ? "browser" : "pcm" });
    })();

    return () => {
      cancelled = true;
      epoch.current += 1;
      offered.current = false;
      pendingIce.current = [];
      try { pcm.current?.stop(); } catch { /* capture already stopped */ }
      pcm.current = null;
      try { browserSpeech.current?.stop(); } catch { /* already stopped */ }
      browserSpeech.current = null;
      if (captionTimer.current) clearTimeout(captionTimer.current);
      sock.current?.close();
      sock.current = null;
      if (pc.current) {
        pc.current.onconnectionstatechange = null;
        pc.current.onicecandidate = null;
        pc.current.ontrack = null;
        pc.current.close();
        pc.current = null;
      }
      stream.current?.getTracks().forEach((t) => t.stop());
      log.info("room_left", { role });
    };
  }, [interviewId, token, role, isStaff]);

  useEffect(() => {
    if (!isStaff || USE_MOCK || !interviewId || !remote || faceTried.current) return;
    const track = remote.getVideoTracks()[0];
    if (!track || track.readyState !== "live") return;
    faceTried.current = true;
    let cancelled = false;
    const run = async (attempt: number) => {
      try {
        const stream = new MediaStream([track]);
        const video = document.createElement("video");
        video.srcObject = stream;
        video.muted = true;
        await video.play().catch(() => undefined);
        await new Promise((r) => setTimeout(r, 400));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(160, video.videoWidth || 320);
        canvas.height = Math.max(120, video.videoHeight || 240);
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
        video.srcObject = null;
        if (!blob || cancelled) return;
        const result = await api.faceCheck(interviewId, blob);
        if (!cancelled) setFaceCheck(result);
        if (result.reason === "no_face" && attempt < 1) {
          faceTried.current = false;
          setTimeout(() => { if (!cancelled) { faceTried.current = true; void run(attempt + 1); } }, 1500);
        }
      } catch (err) {
        log.warn("face_check_failed", { err: String(err) });
        if (!cancelled) setFaceCheck({ match: false, score: 0, reason: "error" });
      }
    };
    void run(0);
    return () => { cancelled = true; };
  }, [isStaff, interviewId, remote]);

  const toggleMute = useCallback(() => {
    const next = !mutedRef.current;
    mutedRef.current = next;
    stream.current?.getAudioTracks().forEach((t) => { t.enabled = !next; });
    setMuted(next);
  }, []);
  const toggleCamera = useCallback(() => {
    const tracks = stream.current?.getVideoTracks() ?? [];
    const turningOff = tracks.some((t) => t.enabled);
    tracks.forEach((t) => { t.enabled = !turningOff; });
    setCameraOff(turningOff);
  }, []);

  const replaceTurn = useCallback((turn: TranscriptTurn) => {
    setTurns((list) => list.map((t) => (t.turn_id === turn.turn_id ? turn : t)));
  }, []);

  return {
    local, remote, conn, peer, turns, coverage, muted, cameraOff, ended, mediaError, notice,
    speechNote, sttNote, sttState, sttPending, speechActive, caption, sttMode, faceCheck,
    toggleMute, toggleCamera, replaceTurn,
  };
}
