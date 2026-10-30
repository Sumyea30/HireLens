/** Start sending local mic audio as 16 kHz PCM16 frames over the room socket. */

import { log } from "./logger";
import { STT_FRAME_BYTES } from "./pcmConvert";

export type PcmSender = { stop: () => void };

export async function startPcmCapture(
  media: MediaStream,
  send: (buf: ArrayBuffer) => void,
): Promise<PcmSender | null> {
  const track = media.getAudioTracks()[0];
  if (!track) return null;

  let ctx: AudioContext;
  try {
    ctx = new AudioContext({ sampleRate: 16000 });
  } catch {
    ctx = new AudioContext();
  }

  const unlock = () => { void ctx.resume(); };
  if (ctx.state === "suspended") {
    try { await ctx.resume(); } catch { /* gesture may still be required */ }
  }
  window.addEventListener("pointerdown", unlock);
  window.addEventListener("keydown", unlock);
  const keepAlive = window.setInterval(() => {
    if (ctx.state === "suspended") void ctx.resume();
  }, 2000);

  try {
    await ctx.audioWorklet.addModule(`${import.meta.env.BASE_URL}pcm-worklet.js?v=5`);
  } catch {
    window.clearInterval(keepAlive);
    window.removeEventListener("pointerdown", unlock);
    window.removeEventListener("keydown", unlock);
    await ctx.close();
    return null;
  }

  const source = ctx.createMediaStreamSource(new MediaStream([track]));
  const node = new AudioWorkletNode(ctx, "pcm-capture");
  let loggedFirst = false;
  let workletChannels = 0;

  node.port.onmessage = (ev) => {
    const data = ev.data;
    if (data instanceof ArrayBuffer) {
      if (!loggedFirst) {
        loggedFirst = true;
        // Once per session — browser console + client log (no audio samples).
        // eslint-disable-next-line no-console
        console.info("[hirelens pcm]", {
          audioContextSampleRate: ctx.sampleRate,
          workletInputChannels: workletChannels,
          firstFrameBytes: data.byteLength,
          expectedFrameBytes: STT_FRAME_BYTES,
        });
        log.info("pcm_first_frame", {
          ctx_rate: ctx.sampleRate,
          channels: workletChannels,
          bytes: data.byteLength,
        });
      }
      send(data);
      return;
    }
    if (data && typeof data === "object" && data.type === "meta") {
      workletChannels = Number(data.inputChannels) || 0;
      log.info("pcm_capture_ready", {
        sample_rate: data.sampleRate,
        ctx_rate: ctx.sampleRate,
        channels: workletChannels,
      });
    }
  };
  source.connect(node);
  const mute = ctx.createGain();
  mute.gain.value = 0;
  node.connect(mute);
  mute.connect(ctx.destination);

  log.info("pcm_capture_started", {
    track: track.label,
    settings: track.getSettings(),
    ctx_rate: ctx.sampleRate,
    ctx_state: ctx.state,
  });

  return {
    stop() {
      window.clearInterval(keepAlive);
      window.removeEventListener("pointerdown", unlock);
      window.removeEventListener("keydown", unlock);
      try { node.port.onmessage = null; node.disconnect(); source.disconnect(); } catch { /* already stopped */ }
      void ctx.close();
    },
  };
}
