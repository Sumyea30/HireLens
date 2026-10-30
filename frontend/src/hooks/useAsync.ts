import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../api";

interface Options<T> {
  /** Poll again after this many ms while pollWhile(data) is true (progress feedback for long jobs). */
  pollMs?: number;
  pollWhile?: (data: T) => boolean;
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = [], opts: Options<T> = {}) {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: true });
  const [tick, setTick] = useState(0);
  const fnRef = useRef(fn);
  const optsRef = useRef(opts);
  fnRef.current = fn;
  optsRef.current = opts;

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const run = async (first: boolean) => {
      if (first) setState((s) => ({ ...s, loading: true, error: null }));
      try {
        const data = await fnRef.current();
        if (!live) return;
        setState({ data, error: null, loading: false });
        const o = optsRef.current;
        if (o.pollMs && o.pollWhile?.(data)) timer = setTimeout(() => void run(false), o.pollMs);
      } catch (e) {
        if (!live) return;
        setState((s) => ({ ...s, loading: false, error: errorMessage(e) }));
      }
    };
    void run(true);
    return () => { live = false; if (timer) clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}
