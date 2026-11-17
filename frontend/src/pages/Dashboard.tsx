import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Button, Empty, PageHead, Panel, StatusBadge, Td, Th } from "../components/ui";
import { money, ms, useApi, when } from "../lib";

const DEC_COLORS: Record<string, string> = { APPROVED: "#0f7a4f", NEEDS_REVIEW: "#c27a12", INFO_REQUESTED: "#1d5fbf", REJECTED: "#b42318" };
const LABEL: Record<string, string> = { APPROVED: "Approved", NEEDS_REVIEW: "Needs review", INFO_REQUESTED: "Info requested", REJECTED: "Rejected" };

function Kpi({ label, value, note, tone }: { label: string; value: React.ReactNode; note?: React.ReactNode; tone?: string }) {
  return (
    <div className="bg-white border border-line rounded-lg px-4 py-3.5">
      <div className="text-[12.5px] text-muted">{label}</div>
      <div className={`num text-[24px] font-semibold tracking-tight mt-0.5 ${tone ?? ""}`}>{value}</div>
      {note && <div className="text-xs text-muted mt-0.5">{note}</div>}
    </div>
  );
}

export default function Dashboard() {
  const { data: m } = useApi<any>("/api/metrics", 4000);
  if (!m) return <p className="text-muted">Loading…</p>;
  const inr = m.value_by_currency.INR ?? 0;
  const other = Object.entries(m.value_by_currency).filter(([c]) => c !== "INR");
  return (
    <>
      <PageHead title="Accounts payable overview" sub="Live figures from every invoice processed by the engine. Nothing on this page is static."
        action={<Link to="/process"><Button>Process an invoice</Button></Link>} />
      {m.total_processed === 0 ? (
        <Panel><Empty title="No invoices processed yet">Run a sample from <Link className="underline" to="/process">Process invoice</Link>, or load demo history in <Link className="underline" to="/settings">Rules & settings</Link>.</Empty></Panel>
      ) : (<>
        <div className="grid gap-3 grid-cols-2 lg:grid-cols-4">
          <Kpi label="Invoices processed" value={m.total_processed} note={`${m.scanned_count} scanned · ${m.in_progress} in progress${m.failed ? ` · ${m.failed} failed` : ""}`} />
          <Kpi label="Approved" value={m.approved} tone="text-ok" note={`${money(m.approved_value_by_currency.INR ?? 0, "INR", true)} approved for payment`} />
          <Kpi label="Waiting for a reviewer" value={m.needs_review} tone="text-warn" note={<Link to="/review" className="underline underline-offset-2">Open review queue</Link>} />
          <Kpi label="Rejected" value={m.rejected} tone="text-bad" note={`${m.duplicates} confirmed duplicate${m.duplicates === 1 ? "" : "s"} blocked`} />
          <Kpi label="Invoice value received" value={money(inr, "INR", true)} note={other.length ? `plus ${other.map(([c, v]) => money(v as number, c)).join(", ")} · duplicates excluded` : "duplicates excluded"} />
          <Kpi label="Decided without a human" value={m.automation_rate != null ? `${m.automation_rate}%` : "—"} note={`${m.straight_through} auto-approved or auto-rejected`} />
          <Kpi label="Avg. processing time" value={ms(m.avg_processing_ms)} note={`digital ${ms(m.avg_processing_ms_digital)} · scanned ${ms(m.avg_processing_ms_scanned)}`} />
          <Kpi label="Decided by reviewers" value={m.human_decided} note="approve / reject after review" />
        </div>

        <div className="grid gap-6 lg:grid-cols-3 mt-6">
          <Panel title="Decisions">
            <ResponsiveContainer width="100%" height={190}>
              <BarChart data={m.decision_breakdown.map((d: any) => ({ ...d, label: LABEL[d.status] }))} layout="vertical" margin={{ left: 10, right: 16 }}>
                <XAxis type="number" allowDecimals={false} hide />
                <YAxis type="category" dataKey="label" width={96} tickLine={false} axisLine={false} tick={{ fontSize: 12, fill: "#5b6b73" }} />
                <Tooltip cursor={{ fill: "#f3f5f7" }} />
                <Bar dataKey="count" radius={[0, 3, 3, 0]} barSize={18} label={{ position: "right", fontSize: 12, fill: "#13232a" }}>
                  {m.decision_breakdown.map((d: any) => <Cell key={d.status} fill={DEC_COLORS[d.status]} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </Panel>
          <Panel title="Main reason each invoice was stopped" className="lg:col-span-2">
            {m.top_reasons.length === 0 ? <Empty title="No exceptions yet" /> : (
              <ResponsiveContainer width="100%" height={190}>
                <BarChart data={m.top_reasons} layout="vertical" margin={{ left: 10, right: 24 }}>
                  <XAxis type="number" allowDecimals={false} hide />
                  <YAxis type="category" dataKey="label" width={250} tickLine={false} axisLine={false} tick={{ fontSize: 12, fill: "#5b6b73" }} />
                  <Tooltip cursor={{ fill: "#f3f5f7" }} />
                  <Bar dataKey="count" fill="#15505a" radius={[0, 3, 3, 0]} barSize={14} label={{ position: "right", fontSize: 12, fill: "#13232a" }} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </Panel>
          <Panel title="Invoice value by vendor (INR)" className="lg:col-span-2">
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={m.value_by_vendor} margin={{ left: 10, right: 10 }}>
                <CartesianGrid vertical={false} stroke="#eef1f3" />
                <XAxis dataKey="vendor" tick={{ fontSize: 11, fill: "#5b6b73" }} tickFormatter={(v: string) => v.split(" ").slice(0, 2).join(" ")} interval={0} />
                <YAxis tick={{ fontSize: 11, fill: "#5b6b73" }} tickFormatter={(v: number) => `₹${(v / 100000).toFixed(1)}L`} width={56} />
                <Tooltip formatter={(v: any) => money(v, "INR")} cursor={{ fill: "#f3f5f7" }} />
                <Bar dataKey="value" fill="#0f766e" radius={[3, 3, 0, 0]} barSize={30} />
              </BarChart>
            </ResponsiveContainer>
          </Panel>
          <Panel title="Average time per stage">
            <ul className="space-y-2 text-[13px]">
              {m.stage_avg_ms.map((s: any) => {
                const max = Math.max(...m.stage_avg_ms.map((x: any) => x.ms), 1);
                return <li key={s.stage}><div className="flex justify-between"><span className="text-muted">{s.stage}</span><span className="num">{ms(s.ms)}</span></div>
                  <div className="h-1 bg-slate-100 rounded mt-1"><div className="h-1 bg-ledger-2 rounded" style={{ width: `${(s.ms / max) * 100}%` }} /></div></li>;
              })}
            </ul>
          </Panel>
        </div>

        <Panel title="Latest runs" className="mt-6" pad={false} action={<Link to="/history" className="text-[13px] underline underline-offset-2">All invoices</Link>}>
          <RunTable runs={m.recent} />
        </Panel>
      </>)}
    </>
  );
}

export function RunTable({ runs }: { runs: any[] }) {
  if (!runs.length) return <Empty title="No invoices match" />;
  return (
    <div className="overflow-x-auto"><table className="w-full text-[13px]">
      <thead><tr><Th>Run</Th><Th>Received</Th><Th>Vendor</Th><Th>Invoice no.</Th><Th>PO</Th><Th right>Total</Th><Th>Status</Th></tr></thead>
      <tbody>{runs.map((r) => (
        <tr key={r.id} className="hover:bg-slate-50">
          <Td><Link to={`/runs/${r.id}`} className="font-mono text-[12.5px] underline-offset-2 hover:underline">{r.id}</Link><div className="text-xs text-muted truncate max-w-[220px]">{r.filename}</div></Td>
          <Td className="text-muted whitespace-nowrap">{when(r.created_at)}</Td>
          <Td>{r.vendor_name ?? <span className="text-muted">—</span>}{r.vendor_name && !r.vendor_id && <div className="text-xs text-bad">not verified</div>}</Td>
          <Td className="font-mono text-[12.5px] whitespace-nowrap">{r.invoice_number ?? <span className="text-bad font-sans">unreadable</span>}</Td>
          <Td className="font-mono text-[12.5px] whitespace-nowrap">{r.po_number ?? "—"}</Td>
          <Td right>{money(r.total_amount, r.currency ?? "INR")}</Td>
          <Td><StatusBadge s={r.status === "COMPLETED" ? r.final_status : r.status} />{r.is_duplicate && <div className="text-xs text-bad mt-0.5">duplicate of {r.duplicate_of}</div>}</Td>
        </tr>))}</tbody>
    </table></div>
  );
}
