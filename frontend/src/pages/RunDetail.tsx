import clsx from "clsx";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Download, ExternalLink, Pencil, RefreshCw, RotateCcw } from "lucide-react";
import LiveRun from "../components/LiveRun";
import { Button, Confidence, ErrorNote, KV, Outcome, Panel, StatusBadge, Td, Th } from "../components/ui";
import { api, money, ms, useReviewer, useApi, when } from "../lib";

const TABS = ["Summary", "Extracted data", "PO match", "All checks", "Document", "Audit trail"] as const;
const STAGE_LABEL: Record<string, string> = { extraction: "Extraction", vendor: "Vendor", po: "Purchase order", financial: "Financial", duplicate: "Duplicates" };

export default function RunDetail() {
  const { id } = useParams();
  const { data, error, setData, reload } = useApi<any>(`/api/runs/${id}`);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Summary");
  if (error) return <ErrorNote msg={error} />;
  if (!data) return <p className="text-muted">Loading…</p>;
  const { run, decision } = data;
  if (run.status !== "COMPLETED" && run.status !== "FAILED") {
    return <Panel title="Processing in progress"><LiveRun runId={run.id} /><Button className="mt-4" variant="outline" onClick={reload}>Refresh</Button></Panel>;
  }
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-4 mb-5">
        <div className="min-w-0">
          <Link to="/history" className="text-[13px] text-muted hover:text-ink">Invoice history</Link>
          <h1 className="text-[24px] font-semibold tracking-tight mt-1 flex flex-wrap items-center gap-3">
            {run.vendor_name ?? "Unidentified vendor"}<span className="text-muted font-normal">{run.invoice_number ?? "no invoice number"}</span>
          </h1>
          <div className="text-[13px] text-muted mt-1 flex flex-wrap gap-x-4">
            <span className="font-mono">{run.id}</span><span>{run.filename}</span><span>received {when(run.created_at, true)}</span>
            <span>{run.extraction_method === "ocr" ? "OCR (scanned)" : run.extraction_method}</span><span>{ms(run.processing_ms)} compute</span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <a href={`/api/runs/${run.id}/document`} target="_blank" rel="noreferrer"><Button variant="outline"><ExternalLink size={14} />Original PDF</Button></a>
          {run.status === "COMPLETED" && <a href={`/api/runs/${run.id}/report.pdf`}><Button><Download size={14} />Processing report</Button></a>}
          <a href={`/api/runs/${run.id}/report.json`}><Button variant="ghost">JSON</Button></a>
        </div>
      </div>

      {run.status === "FAILED" && (
        <div className="rounded-lg bg-bad-bg text-bad p-4 mb-5 flex items-center justify-between gap-4">
          <span>Processing failed: {run.error}</span>
          <Button variant="danger" onClick={() => api(`/api/runs/${run.id}/retry`, { method: "POST" }).then(reload)}><RotateCcw size={14} />Retry</Button>
        </div>
      )}

      {decision && <DecisionBanner d={decision} run={run} original={data.original_run} />}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px] mt-6">
        <div className="min-w-0">
          <div role="tablist" className="flex gap-1 border-b border-line mb-5 overflow-x-auto">
            {TABS.map((t) => (
              <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
                className={clsx("px-3.5 py-2.5 text-[13.5px] whitespace-nowrap border-b-2 -mb-px", tab === t ? "border-ledger font-medium" : "border-transparent text-muted hover:text-ink")}>{t}</button>
            ))}
          </div>
          {tab === "Summary" && <Summary data={data} />}
          {tab === "Extracted data" && <Fields data={data} onChange={setData} />}
          {tab === "PO match" && <POTab data={data} onChange={setData} />}
          {tab === "All checks" && <Checks checks={data.checks} />}
          {tab === "Document" && <DocTab run={run} />}
          {tab === "Audit trail" && <Audit events={data.audit} />}
        </div>
        <div className="space-y-6">
          {data.actions.can_review && <ReviewPanel data={data} onChange={setData} />}
          <Panel title="Processing stages"><LiveRun runId={run.id} compact /></Panel>
          {data.decisions.length > 1 && (
            <Panel title="Decision history">
              <ol className="space-y-3 text-[13px]">
                {data.decisions.map((d: any) => (
                  <li key={d.version} className="flex gap-3"><span className="num text-muted w-6">v{d.version}</span>
                    <div><StatusBadge s={d.status} /> <span className="text-muted">{d.trigger} by {d.created_by}, {when(d.created_at, true)}</span></div></li>
                ))}
              </ol>
            </Panel>
          )}
        </div>
      </div>
    </>
  );
}

function DecisionBanner({ d, run, original }: { d: any; run: any; original: any }) {
  const final = run.final_status;
  const tone = { APPROVED: "border-ok bg-ok-bg", NEEDS_REVIEW: "border-warn bg-warn-bg", REJECTED: "border-bad bg-bad-bg", INFO_REQUESTED: "border-info bg-info-bg" }[final as string] ?? "border-line bg-white";
  const s = d.checks_summary;
  return (
    <section className={clsx("rounded-lg border-l-4 p-5 bg-white ring-1 ring-line", tone)} aria-label="Decision">
      <div className="flex flex-wrap items-center gap-3">
        <StatusBadge s={final} big />
        {final !== d.status && <span className="text-[13px] text-muted">Engine decision v{d.version}: <b>{d.status.replace("_", " ").toLowerCase()}</b></span>}
        <span className="text-[13px] text-muted">Final status set by {run.final_status_by}</span>
      </div>
      <p className="mt-3 text-[16px] font-medium leading-snug max-w-4xl">{d.headline}</p>
      {d.reason_codes.length > 0 && (
        <ul className="mt-3 space-y-1.5 max-w-4xl">
          {d.reason_codes.map((r: any) => (
            <li key={r.code} className="flex gap-2.5 text-[13.5px]">
              <span className={clsx("shrink-0 mt-0.5 rounded px-1.5 text-[11px] font-semibold h-fit", r.effect === "REJECT" ? "bg-bad text-white" : r.effect === "REVIEW" ? "bg-warn text-white" : "bg-slate-200 text-slate-700")}>{r.effect === "NONE" ? "NOTE" : r.effect}</span>
              <span><span className="font-medium">{r.label}.</span> <span className="text-muted">{r.message}</span> <code className="text-[11px] text-slate-400">{r.code}</code></span>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-4 grid gap-4 md:grid-cols-[1fr_auto] items-end">
        <p className="text-[13.5px]"><span className="font-semibold">Recommended next action: </span>{d.recommended_action}</p>
        <div className="flex gap-4 text-[13px] num">
          <span className="flex items-center gap-1"><Outcome o="PASS" size={14} />{s.passed} passed</span>
          <span className="flex items-center gap-1"><Outcome o="WARN" size={14} />{s.warnings}</span>
          <span className="flex items-center gap-1"><Outcome o="FAIL" size={14} />{s.failed}</span>
          <span className="flex items-center gap-1"><Outcome o="UNABLE" size={14} />{s.unable}</span>
        </div>
      </div>
      {original && (
        <p className="mt-3 text-[13px]">Original submission: <Link className="underline underline-offset-2 font-medium" to={`/runs/${original.id}`}>{original.id}</Link> · {original.filename} · processed {when(original.created_at)} · <StatusBadge s={original.final_status} /></p>
      )}
    </section>
  );
}

function Summary({ data }: { data: any }) {
  const f = Object.fromEntries(data.fields.map((x: any) => [x.key, x]));
  const ctx = data.decision?.context ?? {};
  const v = ctx.vendor?.vendor;
  const cur = data.run.currency ?? "INR";
  const val = (k: string) => f[k]?.value ?? <span className="text-bad">not readable</span>;
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Panel title="Invoice">
        <dl>
          <KV k="Invoice number" mono>{val("invoice_number")}</KV>
          <KV k="Invoice date">{val("invoice_date")}</KV>
          <KV k="Due date">{val("due_date")}</KV>
          <KV k="PO reference" mono>{val("po_reference")}</KV>
          <KV k="Payment terms">{f.payment_terms?.value}</KV>
          <KV k="Taxable value"><span className="num">{money(Number(f.subtotal?.value ?? NaN), cur)}</span></KV>
          <KV k="Tax">{f.tax_type?.value ?? "—"} <span className="num">{money(Number(f.tax_amount?.value ?? NaN), cur)}</span>{ctx.tax_mode === "inclusive" && <span className="text-muted"> (prices inclusive of GST)</span>}</KV>
          <KV k="Total"><span className="num font-semibold">{money(Number(f.total_amount?.value ?? NaN), cur)}</span></KV>
        </dl>
      </Panel>
      <Panel title="Vendor">
        <dl>
          <KV k="Name on invoice">{f.vendor_name?.value}</KV>
          <KV k="Tax ID on invoice" mono>{f.vendor_tax_id?.value}</KV>
          <KV k="Vendor master">{v ? <>{v.id} · {v.name}</> : <span className="text-bad font-medium">Not verified ({ctx.vendor?.status?.toLowerCase().replace("_", " ")})</span>}</KV>
          {v && <KV k="Status"><StatusBadge s={v.status} /> <StatusBadge s={v.approval_status} /></KV>}
          {v && <KV k="Registered bank">{v.bank_name} {v.bank_account}</KV>}
          {!v && ctx.vendor?.candidates?.length > 0 && <KV k="Similar vendors">{ctx.vendor.candidates.map((c: any) => <div key={c.vendor_id}>{c.vendor_id} · {c.name} <span className="text-muted">({c.score}% name match, GSTIN {c.tax_id})</span></div>)}</KV>}
          <KV k="Address">{f.vendor_address?.value}</KV>
        </dl>
      </Panel>
      <Panel title="Line items" className="lg:col-span-2" pad={false}><LineTable data={data} /></Panel>
    </div>
  );
}

function LineTable({ data }: { data: any }) {
  const cur = data.run.currency ?? "INR";
  if (!data.line_items.length) return <p className="p-5 text-muted">No line items could be extracted.</p>;
  return (
    <div className="overflow-x-auto"><table className="w-full text-[13px]">
      <thead><tr><Th>#</Th><Th>Description</Th><Th right>Qty</Th><Th right>Unit price</Th><Th right>Amount</Th><Th>PO line</Th></tr></thead>
      <tbody>{data.line_items.map((l: any) => {
        const m = l.match;
        return (
          <tr key={l.line_no}>
            <Td className="text-muted">{l.line_no}</Td><Td>{l.description}</Td><Td right>{l.quantity}</Td>
            <Td right>{money(l.unit_price, cur)}</Td><Td right>{money(l.amount, cur)}</Td>
            <Td>{!m ? <span className="text-muted">not compared</span> : m.status === "UNMATCHED" ? <span className="text-bad font-medium">Not on PO</span> :
              <span className={clsx(m.status === "BUNDLED" ? "text-info" : m.issues?.some((i: string) => i !== "price-within-tolerance") ? "text-bad" : "text-ok")}>
                {m.status === "BUNDLED" ? `Bundles PO lines ${m.bundled_po_lines.join(", ")}` : `Line ${m.po_line_no}`}{m.partial ? ` · ${m.partial}` : ""}{m.price_diff_pct ? ` · price ${m.price_diff_pct > 0 ? "+" : ""}${m.price_diff_pct}%` : ""}</span>}</Td>
          </tr>);
      })}</tbody>
    </table></div>
  );
}

function Fields({ data, onChange }: { data: any; onChange: (d: any) => void }) {
  const { reviewer } = useReviewer();
  const [edit, setEdit] = useState<string | null>(null);
  const [val, setVal] = useState(""); const [reason, setReason] = useState(""); const [err, setErr] = useState<string | null>(null);
  const can = data.actions.can_review;
  const save = (key: string) => api(`/api/runs/${data.run.id}/fields/${key}/correct`, { method: "POST", json: { value: val, reason, reviewer } })
    .then((d) => { onChange(d); setEdit(null); setErr(null); }).catch((e) => setErr(e.message));
  const groups: [string, string][] = [["invoice", "Invoice"], ["amounts", "Amounts and tax"], ["vendor", "Vendor"], ["payment", "Payment details"]];
  return (
    <div className="space-y-6">
      <p className="text-[13px] text-muted max-w-3xl">Every value shows where it came from and how sure the extractor is. The thin mark on each bar is the {Math.round(data.confidence_threshold * 100)}% threshold below which a critical field goes to a human. Corrections never overwrite the original extraction.</p>
      {groups.map(([g, title]) => (
        <Panel key={g} title={title} pad={false}>
          <div className="overflow-x-auto"><table className="w-full text-[13px]">
            <thead><tr><Th>Field</Th><Th>Value</Th><Th>Confidence</Th><Th>Status</Th><Th>Evidence</Th>{can && <Th />}</tr></thead>
            <tbody>{data.fields.filter((f: any) => f.group === g).map((f: any) => (
              <tr key={f.key} className={clsx(f.needs_review && "bg-warn-bg/50")}>
                <Td><span className="font-medium">{f.label}</span>{f.critical && <span className="ml-1 text-bad" title="Critical field">*</span>}</Td>
                <Td className="max-w-[260px]">
                  {edit === f.key ? (
                    <div className="space-y-2 min-w-[220px]">
                      <input autoFocus value={val} onChange={(e) => setVal(e.target.value)} className="w-full rounded border border-line px-2 py-1" aria-label={`Corrected ${f.label}`} />
                      <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason for correction" className="w-full rounded border border-line px-2 py-1" />
                      <ErrorNote msg={err} />
                      <div className="flex gap-2"><Button onClick={() => save(f.key)} disabled={!val || reason.length < 3}>Save correction</Button><Button variant="ghost" onClick={() => setEdit(null)}>Cancel</Button></div>
                    </div>
                  ) : f.corrected ? (
                    <div><span className="font-medium">{f.value}</span><div className="text-xs text-muted mt-0.5">Extracted: <s>{f.original ?? "unreadable"}</s> · corrected by {f.corrected_by}: {f.correction_reason}</div></div>
                  ) : f.value ?? <span className="text-bad">{f.raw_text ? `Unreadable (OCR read "${f.raw_text}")` : "Not found"}</span>}
                </Td>
                <Td>{f.value || f.original ? <Confidence v={f.effective_confidence} threshold={data.confidence_threshold} /> : <span className="text-muted">—</span>}</Td>
                <Td><span className={clsx("text-xs font-medium", { VALID: "text-ok", UNCERTAIN: "text-warn" }[f.status as string] ?? "text-bad")}>{f.status.toLowerCase()}</span></Td>
                <Td className="text-xs text-muted max-w-[300px]">{f.method}{f.source?.page ? `, page ${f.source.page}` : ""}{f.notes?.length > 0 && <div className="mt-0.5">{f.notes.join("; ")}</div>}</Td>
                {can && <Td>{f.key !== "bank_account" && edit !== f.key && <button onClick={() => { setEdit(f.key); setVal(f.value ?? ""); setReason(""); setErr(null); }} className="text-muted hover:text-ink" aria-label={`Correct ${f.label}`}><Pencil size={14} /></button>}</Td>}
              </tr>
            ))}</tbody>
          </table></div>
        </Panel>
      ))}
    </div>
  );
}

function POTab({ data, onChange }: { data: any; onChange: (d: any) => void }) {
  const { reviewer } = useReviewer();
  const [err, setErr] = useState<string | null>(null);
  const ctx = data.decision?.context?.po ?? {};
  const po = ctx.po; const a = ctx.amount ?? {};
  const choose = (pn: string) => api(`/api/runs/${data.run.id}/fields/po_reference/correct`, { method: "POST", json: { value: pn, reason: `Reviewer selected candidate ${pn}`, reviewer } })
    .then(() => api(`/api/runs/${data.run.id}/reevaluate`, { method: "POST", json: { reviewer } })).then(onChange).catch((e) => setErr(e.message));
  const cls: Record<string, string> = { EXACT: "Exact match", PARTIAL: "Partial billing within PO", WITHIN_TOLERANCE: "Within permitted tolerance", OUTSIDE_TOLERANCE: "Outside tolerance", UNABLE: "Unable to validate" };
  return (
    <div className="space-y-6">
      <ErrorNote msg={err} />
      {po ? (
        <Panel title={<span className="flex items-center gap-3">{po.po_number} <StatusBadge s={po.status} />{!ctx.accepted && <span className="text-bad text-[13px] font-medium">Match not accepted</span>}</span>}>
          <div className="grid gap-6 md:grid-cols-2">
            <dl>
              <KV k="Issued to">{po.vendor_id} · {po.vendor_name}</KV>
              <KV k="Vendor tax ID" mono>{po.vendor_tax_id}</KV>
              <KV k="Description">{po.description}</KV>
              <KV k="PO date">{po.po_date} · {po.requester}</KV>
            </dl>
            {ctx.accepted && a.classification ? (
              <div>
                <div className="space-y-1.5 text-[13.5px] num">
                  <Row k="PO value" v={money(po.total_amount, po.currency)} />
                  <Row k="Already invoiced" v={money(a.already_invoiced, po.currency)} />
                  <Row k="Remaining before this invoice" v={money(a.remaining_before, po.currency)} />
                  <Row k="This invoice" v={money(a.invoice_total, po.currency)} strong />
                  {a.classification === "PARTIAL" ? <Row k="Balance left after this invoice" v={money(a.remaining_before - a.invoice_total, po.currency)} /> : <Row k="Difference" v={`${a.difference > 0 ? "+" : ""}${money(a.difference, po.currency)} (${a.difference_pct > 0 ? "+" : ""}${a.difference_pct}% of PO)`} tone={a.classification === "OUTSIDE_TOLERANCE" ? "text-bad" : a.classification === "WITHIN_TOLERANCE" ? "text-warn" : undefined} />}
                  <Row k="Permitted excess" v={money(a.allowed_excess, po.currency)} />
                </div>
                <p className={clsx("mt-3 font-semibold", { EXACT: "text-ok", PARTIAL: "text-ok", WITHIN_TOLERANCE: "text-warn", OUTSIDE_TOLERANCE: "text-bad" }[a.classification as string])}>{cls[a.classification]}</p>
                <p className="text-xs text-muted mt-1">{a.rule}</p>
              </div>
            ) : !ctx.accepted ? (
              <div className="rounded-md bg-bad-bg p-4 text-[13.5px]">
                <p className="font-semibold text-bad">Vendor on PO and invoice differ</p>
                <p className="mt-2"><span className="text-muted">PO vendor:</span> {po.vendor_name} ({po.vendor_tax_id})</p>
                <p><span className="text-muted">Invoice vendor:</span> {data.fields.find((f: any) => f.key === "vendor_name")?.value} ({data.fields.find((f: any) => f.key === "vendor_tax_id")?.value})</p>
                <p className="mt-2 text-muted">Amounts and lines are not compared against another vendor's PO.</p>
              </div>
            ) : null}
          </div>
        </Panel>
      ) : <Panel><p className="text-muted">No PO was matched{data.fields.find((f: any) => f.key === "po_reference")?.value ? "" : ": the invoice has no readable PO reference"}.</p></Panel>}

      {ctx.candidates?.length > 0 && (
        <Panel title="Candidate purchase orders" action={<span className="text-xs text-muted">Suggested, never selected automatically</span>} pad={false}>
          <table className="w-full text-[13px]"><thead><tr><Th>PO</Th><Th>Vendor</Th><Th right>Remaining</Th><Th>Match evidence</Th><Th right>Score</Th>{data.actions.can_review && <Th />}</tr></thead>
            <tbody>{ctx.candidates.map((c: any) => (
              <tr key={c.po_number}><Td className="font-mono">{c.po_number}</Td><Td>{c.vendor_name}<div className="text-xs text-muted">{c.description}</div></Td>
                <Td right>{money(c.remaining, c.currency)}</Td><Td className="text-xs text-muted">{c.reasons.join("; ")}</Td><Td right>{Math.round(c.score * 100)}%</Td>
                {data.actions.can_review && <Td><Button variant="outline" onClick={() => choose(c.po_number)}>Use this PO</Button></Td>}</tr>
            ))}</tbody></table>
        </Panel>
      )}
      {po && ctx.accepted && (
        <Panel title="Line-by-line comparison" pad={false}>
          <div className="overflow-x-auto"><table className="w-full text-[13px]">
            <thead><tr><Th>Invoice line</Th><Th>PO line</Th><Th right>Qty (inv / PO left)</Th><Th right>Unit price (inv ex-tax / PO)</Th><Th>Result</Th></tr></thead>
            <tbody>{ctx.line_matches.map((m: any) => (
              <tr key={m.line_no}>
                <Td>{m.description}</Td><Td className="text-muted">{m.po_description ?? "—"}</Td>
                <Td right>{m.quantity ?? "—"} / {m.po_remaining_qty ?? "—"}</Td>
                <Td right>{m.unit_price_ex_tax != null ? `${money(m.unit_price_ex_tax, po.currency)} / ${money(m.po_unit_price, po.currency)}` : "—"}</Td>
                <Td><Outcome o={m.status === "UNMATCHED" || m.issues?.some((i: string) => i === "price" || i === "quantity") ? "FAIL" : m.issues?.length ? "WARN" : "PASS"} /></Td>
              </tr>
            ))}</tbody>
          </table></div>
        </Panel>
      )}
    </div>
  );
}
const Row = ({ k, v, strong, tone }: { k: string; v: string; strong?: boolean; tone?: string }) =>
  <div className="flex justify-between gap-4"><span className="text-muted">{k}</span><span className={clsx(strong && "font-semibold", tone)}>{v}</span></div>;

function Checks({ checks }: { checks: any[] }) {
  const stages = ["extraction", "vendor", "po", "financial", "duplicate"];
  return (
    <div className="space-y-6">{stages.map((s) => {
      const cs = checks.filter((c) => c.stage === s);
      if (!cs.length) return null;
      return (
        <Panel key={s} title={STAGE_LABEL[s]} pad={false}>
          <ul>{cs.map((c) => (
            <li key={c.id} className="flex gap-3 px-5 py-3 border-b border-line/70 last:border-0">
              <span className="pt-0.5"><Outcome o={c.outcome} /></span>
              <div className="min-w-0 flex-1">
                <div className="font-medium text-[13.5px]">{c.name}</div>
                <p className="text-[13px] text-muted">{c.message}</p>
                {(c.expected || c.actual || c.rule) && (
                  <div className="text-xs mt-1 flex flex-wrap gap-x-4 gap-y-0.5">
                    {c.expected && <span><span className="text-muted">Expected</span> {c.expected}</span>}
                    {c.actual && <span><span className="text-muted">Actual</span> {c.actual}</span>}
                    {c.difference && <span><span className="text-muted">Difference</span> {c.difference}</span>}
                    {c.rule && <span className="text-muted">Rule: {c.rule}</span>}
                  </div>)}
              </div>
              {c.reason_code && <code className="text-[11px] text-slate-400 shrink-0">{c.reason_code}</code>}
            </li>))}</ul>
        </Panel>);
    })}</div>
  );
}

function DocTab({ run }: { run: any }) {
  const pages = Array.from({ length: run.page_count || 1 }, (_, i) => i + 1);
  return (
    <div className="space-y-4">
      {pages.map((p) => <img key={p} src={`/api/runs/${run.id}/page/${p}.png`} alt={`Page ${p} of ${run.filename}`} className="w-full max-w-[820px] border border-line rounded-md bg-white shadow-sm" />)}
    </div>
  );
}

function Audit({ events }: { events: any[] }) {
  return (
    <Panel pad={false}>
      <ol>{events.map((e) => (
        <li key={e.id} className="grid grid-cols-[110px_170px_1fr] gap-4 px-5 py-2.5 border-b border-line/70 last:border-0 text-[13px]">
          <span className="num text-muted">{new Date(e.ts).toLocaleTimeString("en-IN", { hour12: false })}</span>
          <span className={clsx("truncate", e.actor.includes("(") ? "font-medium text-ledger" : "text-muted")}>{e.actor}</span>
          <span><span className="text-xs text-slate-400 mr-2">{e.event_type}</span>{e.message}</span>
        </li>))}</ol>
    </Panel>
  );
}

function ReviewPanel({ data, onChange }: { data: any; onChange: (d: any) => void }) {
  const { reviewer } = useReviewer();
  const [comment, setComment] = useState(""); const [err, setErr] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const act = data.actions; const latest = data.decision;
  const hasCorrections = data.fields.some((f: any) => f.corrected) && !data.decisions.some((d: any) => d.trigger === "re-evaluation" && d.created_at >= (data.fields.filter((f: any) => f.corrected).map((f: any) => f.corrected_at).sort().pop() ?? ""));
  const go = (p: Promise<any>) => { setBusy(true); setErr(null); p.then((d) => { onChange(d); setComment(""); }).catch((e) => setErr(e.message)).finally(() => setBusy(false)); };
  const review = (action: string) => go(api(`/api/runs/${data.run.id}/review`, { method: "POST", json: { action, reviewer, comment } }));
  return (
    <Panel title="Review" className="ring-2 ring-warn/30">
      <p className="text-[13px] text-muted">Acting as <b className="text-ink">{reviewer}</b>. Every action is recorded in the audit trail.</p>
      {latest.status === "APPROVED" && <p className="mt-3 text-[13px] rounded bg-ok-bg text-ok p-2.5">After corrections the engine finds no open issues. Approve to confirm.</p>}
      {hasCorrections && (
        <div className="mt-3 rounded bg-info-bg p-3 text-[13px]">
          <p>You corrected data since the last decision. Re-run validation to see the effect.</p>
          <Button className="mt-2" disabled={busy} onClick={() => go(api(`/api/runs/${data.run.id}/reevaluate`, { method: "POST", json: { reviewer } }))}><RefreshCw size={14} />Re-run validation</Button>
        </div>
      )}
      {act.approve_blocked_by.length > 0 && (
        <div className="mt-3 text-[13px] rounded bg-slate-50 ring-1 ring-line p-3">
          <p className="font-medium">Approval is locked until fixed</p>
          <p className="text-muted mt-1">{act.approve_blocked_by.join(", ")}. Correct the data (Extracted data or PO match tab) and re-run validation, or reject.</p>
        </div>
      )}
      <label htmlFor="cm" className="block text-[13px] font-medium mt-4 mb-1">Comment, reason or justification</label>
      <textarea id="cm" value={comment} onChange={(e) => setComment(e.target.value)} rows={3} className="w-full rounded-md border border-line p-2 text-[13px]"
        placeholder={act.approve_needs_justification ? "Required to approve over open review items" : "Optional for approval, required for rejection"} />
      <ErrorNote msg={err} />
      <div className="grid grid-cols-2 gap-2 mt-3">
        <Button variant="ok" disabled={busy || !act.can_approve} onClick={() => review("APPROVE")}>Approve</Button>
        <Button variant="danger" disabled={busy} onClick={() => review("REJECT")}>Reject</Button>
        <Button variant="outline" disabled={busy} onClick={() => review("REQUEST_INFO")}>Request info</Button>
        <Button variant="ghost" disabled={busy || !comment} onClick={() => review("NOTE")}>Add note</Button>
      </div>
      <p className="text-xs text-muted mt-3">Approving schedules the invoice for payment. It does not release funds or change vendor bank details.</p>
    </Panel>
  );
}
