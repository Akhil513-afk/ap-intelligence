import clsx from "clsx";
import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, ChevronRight } from "lucide-react";
import { PageHead, Panel, StatusBadge, Td, Th } from "../components/ui";
import { money, useApi, when } from "../lib";

export function PurchaseOrders() {
  const { data } = useApi<any[]>("/api/purchase-orders", 6000);
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <PageHead title="Purchase orders" sub="The PO register invoices are matched against. Remaining balance drops only when an invoice is approved." />
      <Panel pad={false}>
        <div className="overflow-x-auto"><table className="w-full text-[13px]">
          <thead><tr><Th /><Th>PO</Th><Th>Vendor</Th><Th>Description</Th><Th>Status</Th><Th right>PO value</Th><Th right>Invoiced</Th><Th right>Remaining</Th><Th>Used</Th></tr></thead>
          <tbody>{data?.map((p) => (
            <Fragment key={p.po_number}>
              <tr className="hover:bg-slate-50 cursor-pointer" onClick={() => setOpen(open === p.po_number ? null : p.po_number)}>
                <Td>{open === p.po_number ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</Td>
                <Td className="font-mono">{p.po_number}</Td><Td>{p.vendor_name}<div className="text-xs text-muted">{p.vendor_id}</div></Td>
                <Td className="max-w-[260px]">{p.description}<div className="text-xs text-muted">{p.po_date} · {p.requester}</div></Td>
                <Td><StatusBadge s={p.status} /></Td>
                <Td right>{money(p.total_amount, p.currency)}</Td><Td right>{money(p.consumed, p.currency)}</Td><Td right className="font-medium">{money(p.remaining, p.currency)}</Td>
                <Td><div className="w-20 h-1.5 bg-slate-100 rounded mt-1.5"><div className="h-full bg-accent rounded" style={{ width: `${Math.min(100, p.utilisation_pct)}%` }} /></div><div className="text-xs text-muted num">{p.utilisation_pct}%</div></Td>
              </tr>
              {open === p.po_number && (
                <tr><td colSpan={9} className="bg-slate-50/60 px-10 py-4 border-b border-line">
                  <table className="w-full text-[12.5px] mb-3"><thead><tr className="text-muted text-left"><th className="py-1">#</th><th>Line</th><th className="text-right">Qty</th><th className="text-right">Billed qty</th><th className="text-right">Unit price</th><th className="text-right">GST</th><th className="text-right">Amount</th></tr></thead>
                    <tbody>{p.lines.map((l: any) => <tr key={l.id}><td className="py-1">{l.line_no}</td><td>{l.description}</td><td className="text-right num">{l.quantity} {l.unit}</td><td className="text-right num">{l.consumed_qty}</td><td className="text-right num">{money(l.unit_price, p.currency)}</td><td className="text-right num">{l.tax_rate}%</td><td className="text-right num">{money(l.amount, p.currency)}</td></tr>)}</tbody></table>
                  <div className="text-[12.5px]"><span className="text-muted">Invoices referencing this PO: </span>{p.invoices.length ? p.invoices.map((i: any) => <Link key={i.id} to={`/runs/${i.id}`} className="mr-3 underline underline-offset-2">{i.invoice_number ?? i.id} ({i.status.toLowerCase().replace("_", " ")})</Link>) : "none yet"}</div>
                </td></tr>
              )}
            </Fragment>
          ))}</tbody>
        </table></div>
      </Panel>
    </>
  );
}

export function Vendors() {
  const { data } = useApi<any[]>("/api/vendors");
  return (
    <>
      <PageHead title="Vendor directory" sub="The vendor master. Identity is confirmed by tax ID; bank details here are the only ones payments may use." />
      <div className="grid gap-4 md:grid-cols-2">
        {data?.map((v) => (
          <Panel key={v.id}>
            <div className="flex items-start justify-between gap-3">
              <div><div className="font-semibold">{v.name}</div><div className="text-xs text-muted">{v.id} · {v.category}</div></div>
              <div className="flex gap-1.5"><StatusBadge s={v.status} />{v.approval_status !== "APPROVED" && <StatusBadge s={v.approval_status} />}</div>
            </div>
            <dl className="grid grid-cols-[110px_1fr] gap-y-1 text-[13px] mt-3">
              <dt className="text-muted">{v.tax_id_type}</dt><dd className="font-mono text-[12.5px]">{v.tax_id}</dd>
              <dt className="text-muted">Bank</dt><dd>{v.bank_name} {v.bank_account} {v.ifsc && <span className="text-muted">({v.ifsc})</span>}</dd>
              <dt className="text-muted">Terms</dt><dd>{v.payment_terms} · {v.currency}</dd>
              <dt className="text-muted">Activity</dt><dd>{v.invoices} invoice{v.invoices === 1 ? "" : "s"} · {money(v.approved_value, v.currency)} approved · {v.open_pos} open PO{v.open_pos === 1 ? "" : "s"}</dd>
            </dl>
            {v.notes && <p className={clsx("text-[12.5px] mt-3 rounded px-2.5 py-1.5", v.status !== "ACTIVE" ? "bg-bad-bg text-bad" : "bg-warn-bg text-warn")}>{v.notes}</p>}
          </Panel>
        ))}
      </div>
    </>
  );
}

export function AuditLog() {
  const [type, setType] = useState("");
  const { data } = useApi<any[]>(`/api/audit?limit=400${type ? `&event_type=${type}` : ""}`, 5000);
  const types = ["", "RUN_CREATED", "DECISION", "FIELD_CORRECTED", "REEVALUATED", "APPROVED_BY_REVIEWER", "REJECTED_BY_REVIEWER", "INFO_REQUESTED", "PO_BALANCE_UPDATED", "RULES_UPDATED", "RUN_FAILED", "DEMO_RESET"];
  return (
    <>
      <PageHead title="Audit log" sub="Append-only record of every system step and human action, newest first."
        action={<select value={type} onChange={(e) => setType(e.target.value)} aria-label="Event type" className="h-9 rounded-md ring-1 ring-line bg-white px-2 text-[13px]">{types.map((t) => <option key={t} value={t}>{t ? t.toLowerCase().replace(/_/g, " ") : "All events"}</option>)}</select>} />
      <Panel pad={false}>
        <div className="overflow-x-auto"><table className="w-full text-[13px]">
          <thead><tr><Th>Time</Th><Th>Actor</Th><Th>Event</Th><Th>Run</Th><Th>Detail</Th></tr></thead>
          <tbody>{data?.map((e) => (
            <tr key={e.id}><Td className="num text-muted whitespace-nowrap">{when(e.ts, true)}</Td><Td className="whitespace-nowrap">{e.actor}</Td>
              <Td className="text-xs text-muted whitespace-nowrap">{e.event_type.toLowerCase().replace(/_/g, " ")}</Td>
              <Td>{e.run_id ? <Link to={`/runs/${e.run_id}`} className="font-mono text-[12px] underline-offset-2 hover:underline">{e.run_id}</Link> : "—"}</Td><Td>{e.message}</Td></tr>
          ))}</tbody>
        </table></div>
      </Panel>
    </>
  );
}
