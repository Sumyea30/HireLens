import { useCallback, useEffect, useRef, useState } from "react";
import { USE_MOCK } from "../api/client";
import { openRoomSocket, type RoomConnState, type RoomSocket } from "../api/socket";
import { log } from "../lib/logger";
import type { CoverageItem, Role, RoomIn, SignalKind, TranscriptTurn } from "../lib/types";

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
  const stream = useRef<MediaStream | null>(null);
  const sock = useRef<RoomSocket | null>(null);
  const pc = useRef<RTCPeerConnection | null>(null);
  const speech = useRef<{ stop: () => void } | null>(null);
  const mutedRef = useRef(false);
  const offered = useRef(false);
  const pendingIce = useRef<RTCIceCandidateInit[]>([]);
  const epoch = useRef(0);
  const restarts = useRef(0);
  const chain = useRef(Promise.resolve());

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
      if (m.type === "transcript") {
        setTurns((list) => (list.some((t) => t.turn_id === m.turn.turn_id) ? list : [...list, m.turn]));
        return;
      }
      if (m.type === "coverage") { setCoverage(m.items); return; }
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
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
          video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" },
        });
      } catch {
        try {
          media = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
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

      const SpeechApi = (window as unknown as { SpeechRecognition?: new () => BrowserSpeech; webkitSpeechRecognition?: new () => BrowserSpeech }).SpeechRecognition
        || (window as unknown as { webkitSpeechRecognition?: new () => BrowserSpeech }).webkitSpeechRecognition;
      if (!SpeechApi) {
        setSpeechNote("This browser cannot transcribe speech. The call still works. Chrome can transcribe each person's microphone.");
      } else if (!USE_MOCK) {
        const heard = new SpeechApi();
        heard.continuous = true;
        heard.interimResults = false;
        heard.onresult = (ev) => {
          for (let i = ev.resultIndex; i < ev.results.length; i += 1) {
            const said = ev.results[i];
            if (!said.isFinal || mutedRef.current) continue;
            const text = said[0].transcript.trim();
            if (text) sock.current?.send({ type: "speech", text });
          }
        };
        heard.onerror = (ev) => {
          if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
            setSpeechNote("Speech recognition is blocked in this browser. The call still works.");
          }
        };
        heard.onend = () => { if (!cancelled) { try { heard.start(); } catch { /* already running */ } } };
        try { heard.start(); speech.current = heard; }
        catch { setSpeechNote("Speech recognition did not start. The call still works."); }
      }
      log.info("room_started", { role });
    })();

    return () => {
      cancelled = true;
      epoch.current += 1;
      offered.current = false;
      pendingIce.current = [];
      try { speech.current?.stop(); } catch { /* recognition already stopped */ }
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
  }, [interviewId, token, role]);

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

  return { local, remote, conn, peer, turns, coverage, muted, cameraOff, ended, mediaError, notice, speechNote, toggleMute, toggleCamera };
}

type BrowserSpeech = {
  continuous: boolean;
  interimResults: boolean;
  onresult: ((ev: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null;
  onerror: ((ev: { error: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};
