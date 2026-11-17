import clsx from "clsx";
import { Link } from "react-router-dom";
import { FileText } from "lucide-react";
import { money, ms, useApi } from "../lib";
import { Outcome, StatusBadge } from "./ui";

function StageDetail({ s }: { s: any }) {
  const d = s.details || {};
  if (s.status === "PENDING" || s.status === "RUNNING" || !s.details) return null;
  const chip = (k: string, v: any, conf?: number) => (
    <span key={k} className={clsx("inline-flex items-baseline gap-1.5 rounded border px-2 py-0.5 text-xs", conf !== undefined && conf < 0.8 ? "border-warn/40 bg-warn-bg" : "border-line bg-slate-50")}>
      <span className="text-muted">{k}</span><span className="font-medium">{v ?? "unreadable"}</span>
      {conf !== undefined && <span className="num text-muted">{Math.round(conf * 100)}%</span>}
    </span>
  );
  if (s.key === "extract" && d.highlights) {
    const L: Record<string, string> = { vendor_name: "Vendor", invoice_number: "Invoice no.", invoice_date: "Date", po_reference: "PO", total_amount: "Total" };
    return <div className="flex flex-wrap gap-1.5 mt-2">{Object.entries(d.highlights).map(([k, v]: any) => chip(L[k], v.value, v.confidence))}</div>;
  }
  if (s.key === "po" && d.amount?.classification) {
    const a = d.amount;
    return <div className="flex flex-wrap gap-1.5 mt-2">{chip("PO", d.po_number)}{chip("PO total", money(a.po_total, a.currency))}{chip("Remaining", money(a.remaining_before, a.currency))}{chip("Invoice", money(a.invoice_total, a.currency))}{a.difference > 0 && chip("Over by", `${money(a.difference, a.currency)} (${a.difference_pct}%)`)}</div>;
  }
  if (s.key === "po" && d.candidates?.length) {
    return <div className="flex flex-wrap gap-1.5 mt-2">{d.candidates.map((c: any) => chip("Candidate", `${c.po_number} · ${Math.round(c.score * 100)}%`))}</div>;
  }
  if (s.key === "analyze") return <div className="flex flex-wrap gap-1.5 mt-2">{chip("Pages", d.page_count)}{chip("Text layer", d.needs_ocr ? "none" : `${d.text_chars_per_page?.[0]} chars`)}</div>;
  return null;
}

export default function LiveRun({ runId, compact }: { runId: string; compact?: boolean }) {
  const { data } = useApi<any>(`/api/runs/${runId}/live`, 450);
  if (!data) return <div className="text-muted p-6">Loading run…</div>;
  const { run, stages, decision } = data;
  const done = run.status === "COMPLETED" || run.status === "FAILED";
  const started = stages[0]?.started_at ? new Date(stages[0].started_at).getTime() : null;
  return (
    <div>
      {!compact && <div className="flex flex-wrap items-center justify-between gap-3 mb-5">
        <div className="min-w-0">
          <div className="font-mono text-[13px] text-muted">{run.id}</div>
          <div className="font-semibold truncate flex items-center gap-2"><FileText size={15} className="text-muted" />{run.filename}</div>
        </div>
        <StatusBadge s={done ? run.final_status ?? run.status : run.status} big />
      </div>}
      <ol className="relative">
        {stages.map((s: any, i: number) => {
          const t = s.completed_at && started ? new Date(s.completed_at).getTime() - started : null;
          return (
            <li key={s.key} className="relative pl-9 pb-4 last:pb-0">
              {i < stages.length - 1 && <span className={clsx("absolute left-[9px] top-6 bottom-0 w-px", s.status === "PENDING" ? "bg-line" : "bg-accent/35")} />}
              <span className={clsx("absolute left-0 top-0.5 grid place-items-center size-[19px] rounded-full bg-white", s.status === "RUNNING" && "running-dot")}><Outcome o={s.status} size={19} /></span>
              <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                <span className={clsx("font-medium", s.status === "PENDING" && "text-slate-400")}>{s.name}</span>
                {s.completed_at && <span className="num text-xs text-muted">{new Date(s.completed_at).toLocaleTimeString("en-IN", { hour12: false })}.{String(new Date(s.completed_at).getMilliseconds()).padStart(3, "0")} · took {ms(s.duration_ms)}{t !== null && !compact ? ` · +${(t / 1000).toFixed(1)} s` : ""}</span>}
                {s.status === "RUNNING" && <span className="text-xs text-accent">running…</span>}
              </div>
              {s.summary && <p className={clsx("text-[13px] mt-0.5", s.status === "FAILED" ? "text-bad" : "text-muted")}>{s.summary}</p>}
              {!compact && <StageDetail s={s} />}
            </li>
          );
        })}
      </ol>
      {run.status === "FAILED" && <div className="mt-5 rounded-md bg-bad-bg text-bad p-3 text-sm">Processing failed: {run.error}. Open the run to retry.</div>}
      {decision && done && !compact && (
        <div className={clsx("mt-6 rounded-lg border-2 p-4 stamp-in origin-left",
          { APPROVED: "border-ok/60 bg-ok-bg", NEEDS_REVIEW: "border-warn/50 bg-warn-bg", REJECTED: "border-bad/50 bg-bad-bg" }[decision.status as string])}>
          <div className={clsx("text-xl font-bold tracking-tight", { APPROVED: "text-ok", NEEDS_REVIEW: "text-warn", REJECTED: "text-bad" }[decision.status as string])}>
            {decision.status === "NEEDS_REVIEW" ? "Needs review" : decision.status === "APPROVED" ? "Approved" : "Rejected"}
          </div>
          <p className="mt-1 text-[13.5px]">{decision.headline}</p>
          <p className="mt-2 text-[13px] text-muted"><span className="font-medium text-ink">Next: </span>{decision.recommended_action}</p>
          <Link to={`/runs/${run.id}`} className="inline-block mt-3 text-sm font-medium text-ledger underline underline-offset-4">Open full decision and evidence</Link>
        </div>
      )}
    </div>
  );
}
