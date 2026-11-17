import { createContext, useContext, useEffect, useRef, useState } from "react";

export async function api<T = any>(path: string, opts: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, ...rest } = opts;
  const res = await fetch(path, {
    ...rest,
    headers: json !== undefined ? { "content-type": "application/json", ...(rest.headers || {}) } : rest.headers,
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const b = await res.json(); msg = typeof b.detail === "string" ? b.detail : JSON.stringify(b.detail ?? b); } catch {}
    throw new Error(msg);
  }
  return res.json();
}

export function useApi<T = any>(path: string | null, pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!path) return;
    let t: number | undefined;
    const load = () => api<T>(path).then((d) => { if (alive.current) { setData(d); setError(null); } })
      .catch((e) => alive.current && setError(e.message));
    load();
    if (pollMs) t = window.setInterval(load, pollMs);
    return () => { if (t) clearInterval(t); };
  }, [path, pollMs, tick]);
  return { data, error, reload: () => setTick((x) => x + 1), setData };
}

const SYM: Record<string, string> = { INR: "₹", USD: "$", EUR: "€", GBP: "£" };
export function money(v: number | null | undefined, cur = "INR", compact = false) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const loc = cur === "INR" ? "en-IN" : "en-US";
  if (compact && Math.abs(v) >= 100000 && cur === "INR") return `${SYM[cur]}${(v / 100000).toLocaleString(loc, { maximumFractionDigits: 2 })} L`;
  return `${SYM[cur] ?? cur + " "}${v.toLocaleString(loc, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
export function when(s?: string | null, withSec = false) {
  if (!s) return "—";
  const d = new Date(s);
  return d.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", ...(withSec ? { second: "2-digit" } : {}), hour12: false });
}
export function ms(v?: number | null) { if (v == null) return "—"; return v < 1000 ? `${v} ms` : `${(v / 1000).toFixed(1)} s`; }
export const pct = (v: number) => `${Math.round(v * 100)}%`;

export const REVIEWERS_FALLBACK = ["Priya Nair (AP Lead)"];
type Ctx = { reviewer: string; setReviewer: (s: string) => void; reviewers: string[] };
export const ReviewerCtx = createContext<Ctx>({ reviewer: REVIEWERS_FALLBACK[0], setReviewer: () => {}, reviewers: REVIEWERS_FALLBACK });
export const useReviewer = () => useContext(ReviewerCtx);
export function loadPref(k: string, d: string) { try { return localStorage.getItem(k) ?? d; } catch { return d; } }
export function savePref(k: string, v: string) { try { localStorage.setItem(k, v); } catch {} }
