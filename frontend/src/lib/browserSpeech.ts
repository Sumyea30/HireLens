/** Browser SpeechRecognition fallback (STT_PROVIDER=browser / VITE_STT_PROVIDER=browser). */

export type BrowserSpeechSender = { stop: () => void };

type SpeechRec = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((ev: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null;
  onerror: ((ev: { error?: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

function recognitionCtor(): (new () => SpeechRec) | null {
  const w = window as unknown as {
    SpeechRecognition?: new () => SpeechRec;
    webkitSpeechRecognition?: new () => SpeechRec;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

/**
 * Final phrases only → `onFinal(text)`. Audio is processed by the browser / Google
 * speech service; HireLens never receives PCM in this mode.
 */
export function startBrowserSpeech(
  onFinal: (text: string) => void,
  lang = "en-IN",
): BrowserSpeechSender | null {
  const Ctor = recognitionCtor();
  if (!Ctor) return null;
  let stopped = false;
  const rec = new Ctor();
  rec.continuous = true;
  rec.interimResults = false;
  rec.lang = lang || "en-IN";
  rec.onresult = (ev) => {
    for (let i = ev.resultIndex; i < ev.results.length; i += 1) {
      const row = ev.results[i];
      if (!row?.isFinal) continue;
      const text = String(row[0]?.transcript || "").trim();
      if (text) onFinal(text);
    }
  };
  rec.onerror = () => { /* onend may restart */ };
  rec.onend = () => {
    if (!stopped) {
      try { rec.start(); } catch { /* already started or permission revoked */ }
    }
  };
  try {
    rec.start();
  } catch {
    return null;
  }
  return {
    stop() {
      stopped = true;
      rec.onend = null;
      try { rec.stop(); } catch { /* already stopped */ }
    },
  };
}
