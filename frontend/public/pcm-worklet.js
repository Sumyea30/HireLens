/* Capture mono PCM16 at 16 kHz in 250 ms frames (8000 bytes) for HireLens STT. */
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = new Float32Array(0);
    this._target = 4000; // 250 ms @ 16 kHz
    this._logged = false;
  }

  process(inputs) {
    const chans = inputs[0];
    if (!chans || !chans.length || !chans[0] || !chans[0].length) return true;

    if (!this._logged) {
      this._logged = true;
      this.port.postMessage({
        type: "meta",
        sampleRate,
        inputChannels: chans.length,
        blockSize: chans[0].length,
      });
    }

    // Downmix all input channels to mono.
    const n = chans[0].length;
    const mono = new Float32Array(n);
    const inv = 1 / chans.length;
    for (let i = 0; i < n; i += 1) {
      let sum = 0;
      for (let c = 0; c < chans.length; c += 1) sum += chans[c][i];
      mono[i] = sum * inv;
    }

    // Linear resample context rate → 16 kHz.
    const ratio = sampleRate / 16000;
    const outLen = Math.max(1, Math.floor(mono.length / ratio));
    const down = new Float32Array(outLen);
    for (let i = 0; i < outLen; i += 1) {
      const src = i * ratio;
      const i0 = Math.floor(src);
      const i1 = Math.min(i0 + 1, mono.length - 1);
      const t = src - i0;
      down[i] = mono[i0] * (1 - t) + mono[i1] * t;
    }

    const merged = new Float32Array(this._buf.length + down.length);
    merged.set(this._buf);
    merged.set(down, this._buf.length);
    this._buf = merged;

    while (this._buf.length >= this._target) {
      const frame = this._buf.subarray(0, this._target);
      this._buf = this._buf.slice(this._target);
      const pcm = new Int16Array(frame.length);
      for (let i = 0; i < frame.length; i += 1) {
        const s = Math.max(-1, Math.min(1, frame[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
