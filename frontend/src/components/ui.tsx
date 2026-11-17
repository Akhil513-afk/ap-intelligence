import clsx from "clsx";
import { AlertTriangle, CheckCircle2, CircleDashed, CircleSlash, Info, Loader2, XCircle } from "lucide-react";
import type { ReactNode } from "react";

export function Panel({ title, action, children, className, pad = true }: { title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string; pad?: boolean }) {
  return (
    <section className={clsx("bg-white border border-line rounded-lg", className)}>
      {title && (
        <header className="flex items-center justify-between gap-3 px-5 py-3.5 border-b border-line">
          <h2 className="text-[15px] font-semibold">{title}</h2>
          {action}
        </header>
      )}
      <div className={pad ? "p-5" : ""}>{children}</div>
    </section>
  );
}

export function PageHead({ title, sub, action }: { title: string; sub?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 mb-6">
      <div>
        <h1 className="text-[26px] leading-tight font-semibold tracking-tight">{title}</h1>
        {sub && <p className="text-muted mt-1 max-w-3xl">{sub}</p>}
      </div>
      {action}
    </div>
  );
}

const STATUS: Record<string, { label: string; cls: string }> = {
  APPROVED: { label: "Approved", cls: "bg-ok-bg text-ok ring-ok/20" },
  NEEDS_REVIEW: { label: "Needs review", cls: "bg-warn-bg text-warn ring-warn/25" },
  REJECTED: { label: "Rejected", cls: "bg-bad-bg text-bad ring-bad/20" },
  INFO_REQUESTED: { label: "Info requested", cls: "bg-info-bg text-info ring-info/20" },
  PROCESSING: { label: "Processing", cls: "bg-white text-accent ring-accent/30" },
  QUEUED: { label: "Queued", cls: "bg-white text-muted ring-line" },
  FAILED: { label: "Failed", cls: "bg-bad-bg text-bad ring-bad/20" },
  OPEN: { label: "Open", cls: "bg-ok-bg text-ok ring-ok/20" },
  CLOSED: { label: "Closed", cls: "bg-slate-100 text-slate-600 ring-slate-300" },
  ACTIVE: { label: "Active", cls: "bg-ok-bg text-ok ring-ok/20" },
  BLOCKED: { label: "Blocked", cls: "bg-bad-bg text-bad ring-bad/20" },
  PENDING: { label: "Pending", cls: "bg-warn-bg text-warn ring-warn/25" },
};
export function StatusBadge({ s, big }: { s?: string | null; big?: boolean }) {
  if (!s) return <span className="text-muted">—</span>;
  const m = STATUS[s] ?? { label: s.replace(/_/g, " ").toLowerCase(), cls: "bg-slate-100 text-slate-700 ring-slate-200" };
  return <span className={clsx("inline-flex items-center rounded-full ring-1 font-medium whitespace-nowrap", big ? "px-3 py-1 text-sm" : "px-2 py-0.5 text-xs", m.cls)}>{m.label}</span>;
}

export function Outcome({ o, size = 16 }: { o: string; size?: number }) {
  const p = { size, strokeWidth: 2.2 };
  switch (o) {
    case "PASS": case "SUCCESS": return <CheckCircle2 {...p} className="text-ok shrink-0" aria-label="passed" />;
    case "WARN": case "WARNING": return <AlertTriangle {...p} className="text-warn shrink-0" aria-label="warning" />;
    case "FAIL": case "FAILED": return <XCircle {...p} className="text-bad shrink-0" aria-label="failed" />;
    case "INFO": return <Info {...p} className="text-info shrink-0" aria-label="info" />;
    case "RUNNING": return <Loader2 {...p} className="text-accent animate-spin shrink-0" aria-label="running" />;
    case "SKIPPED": case "UNABLE": return <CircleSlash {...p} className="text-slate-400 shrink-0" aria-label="not evaluated" />;
    default: return <CircleDashed {...p} className="text-slate-300 shrink-0" aria-label="pending" />;
  }
}

export function Confidence({ v, threshold = 0.8 }: { v: number; threshold?: number }) {
  const c = v >= threshold ? "bg-ok" : v >= 0.5 ? "bg-warn" : "bg-bad";
  return (
    <div className="flex items-center gap-2 min-w-[92px]" title={`Confidence ${Math.round(v * 100)}% (threshold ${Math.round(threshold * 100)}%)`}>
      <div className="relative h-1.5 w-14 rounded-full bg-slate-100 overflow-hidden">
        <div className={clsx("h-full rounded-full", c)} style={{ width: `${Math.max(3, v * 100)}%` }} />
        <div className="absolute top-0 h-full w-px bg-ink/40" style={{ left: `${threshold * 100}%` }} />
      </div>
      <span className="num text-xs text-muted w-8 text-right">{Math.round(v * 100)}%</span>
    </div>
  );
}

export function Button({ children, variant = "primary", className, ...p }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" | "danger" | "ok" | "outline" }) {
  return (
    <button {...p} className={clsx("inline-flex items-center justify-center gap-2 rounded-md px-3.5 h-9 text-sm font-medium transition-colors disabled:opacity-45 disabled:cursor-not-allowed",
      { primary: "bg-ledger text-white hover:bg-ledger-2", ok: "bg-ok text-white hover:brightness-110", danger: "bg-white text-bad ring-1 ring-bad/40 hover:bg-bad-bg",
        ghost: "text-ink hover:bg-slate-100", outline: "bg-white ring-1 ring-line hover:bg-slate-50" }[variant], className)}>
      {children}
    </button>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="text-center py-12 px-6"><p className="font-medium">{title}</p>{children && <div className="text-muted mt-1">{children}</div>}</div>;
}

export function ErrorNote({ msg }: { msg: string | null }) {
  if (!msg) return null;
  return <div role="alert" className="rounded-md bg-bad-bg text-bad px-3 py-2 text-sm">{msg}</div>;
}

export function KV({ k, children, mono }: { k: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="grid grid-cols-[150px_1fr] gap-3 py-1.5 text-sm">
      <dt className="text-muted">{k}</dt>
      <dd className={clsx("min-w-0 break-words", mono && "font-mono text-[13px]")}>{children ?? "—"}</dd>
    </div>
  );
}

export const Th = ({ children, right }: { children?: ReactNode; right?: boolean }) =>
  <th className={clsx("px-4 py-2.5 font-medium text-muted text-xs border-b border-line bg-slate-50/70", right ? "text-right" : "text-left")}>{children}</th>;
export const Td = ({ children, right, className }: { children?: ReactNode; right?: boolean; className?: string }) =>
  <td className={clsx("px-4 py-2.5 border-b border-line/70 align-top", right && "text-right num", className)}>{children}</td>;
