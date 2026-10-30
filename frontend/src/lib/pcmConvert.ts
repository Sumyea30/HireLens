/** PCM helpers for HireLens STT capture: Float32 → Int16 LE, mono, 16 kHz frames. */

export const STT_SAMPLE_RATE = 16000;
/** 250 ms of mono PCM16 @ 16 kHz. */
export const STT_FRAME_SAMPLES = 4000;
export const STT_FRAME_BYTES = STT_FRAME_SAMPLES * 2; // 8000

/** Clamp float sample to [-1, 1] and encode as little-endian Int16. */
export function floatToInt16Sample(s: number): number {
  const c = Math.max(-1, Math.min(1, s));
  return c < 0 ? (c * 0x8000) | 0 : (c * 0x7fff) | 0;
}

/** Downmix interleaved or planar channel buffers to mono Float32. */
export function downmixToMono(channels: Float32Array[]): Float32Array {
  if (!channels.length || !channels[0]?.length) return new Float32Array(0);
  const n = channels[0].length;
  if (channels.length === 1) return channels[0];
  const out = new Float32Array(n);
  const inv = 1 / channels.length;
  for (let i = 0; i < n; i += 1) {
    let sum = 0;
    for (let c = 0; c < channels.length; c += 1) sum += channels[c][i] ?? 0;
    out[i] = sum * inv;
  }
  return out;
}

/** Linear resample mono Float32 from `fromRate` to `toRate`. */
export function resampleLinear(input: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (!input.length || fromRate <= 0 || toRate <= 0) return new Float32Array(0);
  if (Math.abs(fromRate - toRate) < 0.5) return input;
  const ratio = fromRate / toRate;
  const outLen = Math.max(1, Math.floor(input.length / ratio));
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i += 1) {
    const src = i * ratio;
    const i0 = Math.floor(src);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const t = src - i0;
    out[i] = input[i0] * (1 - t) + input[i1] * t;
  }
  return out;
}

/** Encode mono Float32 as little-endian PCM16 ArrayBuffer (host endianness of Int16Array is LE on x86/ARM). */
export function encodePcm16Le(mono: Float32Array): ArrayBuffer {
  const pcm = new Int16Array(mono.length);
  for (let i = 0; i < mono.length; i += 1) pcm[i] = floatToInt16Sample(mono[i]);
  return pcm.buffer;
}
