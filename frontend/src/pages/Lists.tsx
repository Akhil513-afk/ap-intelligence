import clsx from "clsx";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Empty, PageHead, Panel, StatusBadge } from "../components/ui";
import { money, useApi } from "../lib";
import { RunTable } from "./Dashboard";

export function ReviewQueue() {
  const { data } = useApi<any[]>("/api/review-queue", 5000);
  return (
    <>
      <PageHead title="Review queue" sub="Invoices the engine could not decide on its own. Oldest first. Each card says what needs a human and why." />
      {!data ? <p className="text-muted">Loading…</p> : data.length === 0 ? <Panel><Empty title="Nothing waiting for review">New exceptions appear here as soon as they are decided.</Empty></Panel> : (
        <div className="space-y-3">
          {data.map((r) => (
            <Link key={r.id} to={`/runs/${r.id}`} className="block bg-white border border-line rounded-lg p-4 hover:border-ledger/40">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-semibold">{r.vendor_name ?? "Unidentified vendor"} <span className="font-normal text-muted">{r.invoice_number ?? "invoice no. unreadable"}</span></div>
                  <div className="text-xs text-muted mt-0.5"><span className="font-mono">{r.id}</span> · {r.filename} · waiting {r.age_hours < 1 ? `${Math.round(r.age_hours * 60)} min` : `${r.age_hours} h`}</div>
                </div>
                <div className="flex items-center gap-3"><span className="num font-semibold">{money(r.total_amount, r.currency ?? "INR")}</span><StatusBadge s={r.final_status} /></div>
              </div>
              <ul className="mt-3 flex flex-wrap gap-2">
                {r.reason_codes.filter((c: any) => c.effect !== "NONE").map((c: any) => (
                  <li key={c.code} className={clsx("text-xs rounded px-2 py-0.5 ring-1", r.blocking.includes(c.code) ? "bg-bad-bg text-bad ring-bad/20" : "bg-warn-bg text-warn ring-warn/25")}>{c.label}</li>
                ))}
              </ul>
              <p className="text-[13px] mt-2"><span className="text-muted">Next: </span>{r.recommended_action}</p>
            </Link>
          ))}
        </div>
      )}
    </>
  );
}

const FILTERS = [["", "All"], ["APPROVED", "Approved"], ["NEEDS_REVIEW,INFO_REQUESTED", "Needs review"], ["REJECTED", "Rejected"], ["FAILED", "Failed"]];
export function HistoryPage() {
  const [status, setStatus] = useState(""); const [q, setQ] = useState("");
  const { data } = useApi<any[]>(`/api/runs?status=${status}&q=${encodeURIComponent(q)}`, 5000);
  return (
    <>
      <PageHead title="Invoice history" sub="Every processing run is kept, including rejected and failed ones, so nothing disappears from the record." />
      <div className="flex flex-wrap gap-3 mb-4">
        <div className="flex rounded-md ring-1 ring-line bg-white overflow-hidden" role="group" aria-label="Filter by status">
          {FILTERS.map(([v, l]) => <button key={l} onClick={() => setStatus(v)} className={clsx("px-3 h-9 text-[13px]", status === v ? "bg-ledger text-white" : "hover:bg-slate-50")}>{l}</button>)}
        </div>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search vendor, invoice, PO or run ID" aria-label="Search" className="h-9 rounded-md ring-1 ring-line px-3 w-72 bg-white" />
      </div>
      <Panel pad={false}>{data ? <RunTable runs={data} /> : <p className="p-5 text-muted">Loading…</p>}</Panel>
    </>
  );
}
