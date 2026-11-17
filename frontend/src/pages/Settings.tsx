import { useEffect, useState } from "react";
import { Button, ErrorNote, PageHead, Panel } from "../components/ui";
import { api, useApi, useReviewer } from "../lib";

function NumField({ label, help, value, onChange, step = 0.5, suffix }: { label: string; help: string; value: number; onChange: (v: number) => void; step?: number; suffix?: string }) {
  return (
    <label className="block">
      <span className="text-[13px] font-medium">{label}</span>
      <div className="flex items-center gap-2 mt-1"><input type="number" step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} className="num w-32 h-9 rounded-md ring-1 ring-line px-2" />{suffix && <span className="text-muted text-[13px]">{suffix}</span>}</div>
      <span className="text-xs text-muted">{help}</span>
    </label>
  );
}

export default function Settings() {
  const { reviewer } = useReviewer();
  const { data, reload } = useApi<any>("/api/settings");
  const [rules, setRules] = useState<any>(null);
  const [msg, setMsg] = useState<string | null>(null); const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (data) setRules(structuredClone(data.rules)); }, [data]);
  if (!data || !rules) return <p className="text-muted">Loading…</p>;
  const set = (sec: string, k: string, v: any) => setRules({ ...rules, [sec]: { ...rules[sec], [k]: v } });
  const save = () => { setErr(null); setMsg(null); api("/api/settings", { method: "PUT", json: { rules, user: reviewer } })
    .then((r) => { setMsg(r.changes.length ? `Saved: ${r.changes.join("; ")}` : "No changes to save"); reload(); }).catch((e) => setErr(e.message)); };
  const reset = (history: boolean) => {
    if (!confirm(history ? "Clear all runs and reload the demo data, then process the 6 historical invoices?" : "Clear all runs and reload master data only?")) return;
    setBusy(true); api("/api/demo/reset", { method: "POST", json: { preload_history: history, user: reviewer } })
      .then((r) => setMsg(`Demo reset. ${r.history_runs.length} historical invoices processed.`)).catch((e) => setErr(e.message)).finally(() => setBusy(false));
  };
  const t = rules.tolerance;
  const codes = Object.entries(data.reason_codes) as [string, any][];
  return (
    <>
      <PageHead title="Rules & settings" sub="Business rules are data, not code. Changes apply to the next decision and are snapshotted into each decision record and the audit log."
        action={<Button onClick={save}>Save rules</Button>} />
      <div className="space-y-3 mb-5"><ErrorNote msg={err} />{msg && <div className="rounded-md bg-ok-bg text-ok px-3 py-2 text-[13px]">{msg}</div>}</div>
      <div className="grid gap-6 lg:grid-cols-2">
        <Panel title="Amount tolerance">
          <div className="grid gap-5 sm:grid-cols-2">
            <NumField label="Allowed excess over PO" help="Percent of PO value" value={t.amount_pct} onChange={(v) => set("tolerance", "amount_pct", v)} suffix="%" />
            <NumField label="Absolute cap on excess" help="Whichever of the two is lower applies" value={t.amount_abs} step={500} onChange={(v) => set("tolerance", "amount_abs", v)} suffix="₹ / $" />
            <NumField label="Hard reject above" help="Excess beyond this is rejected outright" value={t.hard_limit_pct} onChange={(v) => set("tolerance", "hard_limit_pct", v)} suffix="%" />
            <NumField label="Unit price tolerance" help="Per invoice line vs PO" value={t.unit_price_pct} onChange={(v) => set("tolerance", "unit_price_pct", v)} suffix="%" />
            <NumField label="Rounding tolerance" help="For arithmetic and tax checks" value={t.rounding_abs} onChange={(v) => set("tolerance", "rounding_abs", v)} />
            <NumField label="Maximum invoice age" help="Older invoices need review" value={t.max_invoice_age_days} step={30} onChange={(v) => set("tolerance", "max_invoice_age_days", v)} suffix="days" />
          </div>
        </Panel>
        <Panel title="Extraction and matching">
          <div className="grid gap-5 sm:grid-cols-2">
            <NumField label="Confidence threshold" help="Critical fields below this go to review" value={rules.extraction.confidence_threshold} step={0.05} onChange={(v) => set("extraction", "confidence_threshold", v)} />
            <NumField label="Vendor name similarity" help="Score that counts as a look-alike" value={rules.matching.vendor_name_similarity} step={1} onChange={(v) => set("matching", "vendor_name_similarity", v)} suffix="%" />
            <label className="block"><span className="text-[13px] font-medium">Extraction engine</span>
              <select value={rules.extraction_mode} onChange={(e) => setRules({ ...rules, extraction_mode: e.target.value })} className="block mt-1 h-9 rounded-md ring-1 ring-line px-2 bg-white">
                <option value="auto">Auto: Claude if configured, else local</option><option value="local">Local parser + OCR only</option><option value="llm">Claude (requires API key)</option>
              </select>
              <span className="text-xs text-muted">{data.llm_available ? "ANTHROPIC_API_KEY detected on the server." : "No ANTHROPIC_API_KEY on the server: the local parser + Tesseract OCR is used."}</span>
            </label>
            <NumField label="Pause between stages" help="Demo pacing so the live view is followable; excluded from timings" value={rules.demo.stage_pacing_ms} step={50} onChange={(v) => set("demo", "stage_pacing_ms", v)} suffix="ms" />
          </div>
        </Panel>
        <Panel title="Decision policy" className="lg:col-span-2" pad={false}>
          <p className="px-5 pt-4 text-[13px] text-muted">What each finding does to the decision. Reject wins over review; review wins over approval. Options are limited to what is safe for each finding.</p>
          <div className="grid md:grid-cols-2 gap-x-8 px-5 py-3">
            {codes.map(([code, m]) => (
              <div key={code} className="flex items-center justify-between gap-3 py-2 border-b border-line/70">
                <div className="min-w-0"><div className="text-[13px] font-medium">{m.label}</div><div className="text-[11px] text-slate-400 font-mono">{code}</div></div>
                <select value={rules.policies[code]} disabled={m.allowed.length < 2} onChange={(e) => set("policies", code, e.target.value)} className="h-8 rounded ring-1 ring-line px-2 bg-white text-[13px] disabled:opacity-60" aria-label={`Effect of ${m.label}`}>
                  {m.allowed.map((a: string) => <option key={a} value={a}>{{ REJECT: "Reject", REVIEW: "Send to review", NONE: "Record only" }[a]}</option>)}
                </select>
              </div>
            ))}
          </div>
        </Panel>
        <Panel title="Demo environment" className="lg:col-span-2">
          <p className="text-[13px] text-muted mb-4">Reset clears all runs, reviews and audit events and reloads 10 vendors and 17 purchase orders. Loading history pushes 6 older invoices through the real pipeline so the dashboard has data. Re-running a sample after its first run is correctly treated as a duplicate.</p>
          <div className="flex flex-wrap gap-3">
            <Button disabled={busy} onClick={() => reset(true)}>{busy ? "Resetting…" : "Reset and load demo history"}</Button>
            <Button variant="outline" disabled={busy} onClick={() => reset(false)}>Reset to empty</Button>
          </div>
        </Panel>
      </div>
    </>
  );
}
