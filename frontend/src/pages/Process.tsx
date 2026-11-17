import clsx from "clsx";
import { useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { FileUp, Play, ScanLine } from "lucide-react";
import LiveRun from "../components/LiveRun";
import { Button, ErrorNote, PageHead, Panel, StatusBadge } from "../components/ui";
import { api, useApi } from "../lib";

const MAX_MB = 10;

export default function Process() {
  const [params, setParams] = useSearchParams();
  const runId = params.get("run");
  const { data: samples, reload } = useApi<any[]>("/api/samples");
  const [drag, setDrag] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const start = (id: string) => { setParams({ run: id }); setTimeout(reload, 1500); };

  function upload(f: File) {
    setErr(null);
    if (!f.name.toLowerCase().endsWith(".pdf")) return setErr(`${f.name} is not a PDF. Upload the invoice as a .pdf file.`);
    if (f.size > MAX_MB * 1024 * 1024) return setErr(`${f.name} is ${(f.size / 1048576).toFixed(1)} MB. The limit is ${MAX_MB} MB.`);
    if (f.size === 0) return setErr("That file is empty.");
    const fd = new FormData(); fd.append("file", f);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/runs/upload");
    xhr.upload.onprogress = (e) => e.lengthComputable && setProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => {
      setProgress(null);
      try { const b = JSON.parse(xhr.responseText); if (xhr.status >= 300) setErr(b.detail || "Upload failed"); else start(b.run_id); }
      catch { setErr("Upload failed"); }
    };
    xhr.onerror = () => { setProgress(null); setErr("Upload failed: the server could not be reached."); };
    setProgress(0); xhr.send(fd);
  }

  const runSample = (id: string) => api(`/api/samples/${id}/run`, { method: "POST" }).then((r) => start(r.run_id)).catch((e) => setErr(e.message));
  const groups = ["Happy path", "Edge case", "Additional"];

  return (
    <>
      <PageHead title="Process an invoice" sub="Upload a vendor invoice PDF or run one of the prepared samples. Every stage runs for real and is written to the audit trail as it happens." />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_480px]">
        <div className="space-y-6 min-w-0">
          <div
            onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
            onDrop={(e) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files[0]; if (f) upload(f); }}
            className={clsx("rounded-lg border-2 border-dashed bg-white px-6 py-9 text-center transition-colors", drag ? "border-accent bg-teal-50/40" : "border-line")}>
            <FileUp className="mx-auto text-ledger" size={28} />
            <p className="mt-3 font-medium">Drop an invoice PDF here</p>
            <p className="text-muted text-[13px] mt-1">Digital or scanned, up to {MAX_MB} MB. Scanned pages are read with OCR.</p>
            <Button className="mt-4" onClick={() => input.current?.click()} disabled={progress !== null}>Choose file</Button>
            <input ref={input} type="file" accept="application/pdf,.pdf" className="sr-only" onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = ""; }} />
            {progress !== null && (
              <div className="mt-4 max-w-xs mx-auto" aria-live="polite">
                <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden"><div className="h-full bg-accent transition-all" style={{ width: `${progress}%` }} /></div>
                <p className="text-xs text-muted mt-1">Uploading {progress}%</p>
              </div>
            )}
            <div className="mt-4 max-w-md mx-auto"><ErrorNote msg={err} /></div>
          </div>

          <Panel title="Sample invoices" pad={false} action={<span className="text-xs text-muted">Generated from the vendor side; the engine has no answer key</span>}>
            {groups.map((g) => (
              <div key={g}>
                <div className="px-5 pt-4 pb-1 text-[13px] font-semibold text-ledger">{g === "Edge case" ? "Edge cases" : g === "Happy path" ? "Happy path" : "More scenarios"}</div>
                <ul>
                  {samples?.filter((s) => s.category === g).map((s) => (
                    <li key={s.id} className="flex items-start gap-4 px-5 py-3 border-b border-line/70 last:border-0">
                      <span className="num text-muted text-xs w-5 pt-0.5">{s.id}</span>
                      <div className="flex-1 min-w-0">
                        <div className="font-medium flex items-center gap-2 flex-wrap">{s.title}{s.scanned && <span className="inline-flex items-center gap-1 text-[11px] text-info"><ScanLine size={12} />scanned</span>}</div>
                        <p className="text-[13px] text-muted mt-0.5">{s.description}</p>
                        <div className="text-xs mt-1 flex flex-wrap gap-x-4 gap-y-1">
                          <a href={`/api/samples/${s.id}/file`} target="_blank" rel="noreferrer" className="underline underline-offset-2 text-muted hover:text-ink">{s.file}</a>
                          <span className="text-muted">Expected: <StatusBadge s={s.expected} /></span>
                          {s.last_run_id && <Link to={`/runs/${s.last_run_id}`} className="text-muted">Last run <StatusBadge s={s.last_status} /></Link>}
                        </div>
                      </div>
                      <Button variant="outline" onClick={() => runSample(s.id)} aria-label={`Run sample ${s.id}`}><Play size={14} />Run</Button>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </Panel>
        </div>
        <div className="xl:sticky xl:top-6 self-start">
          <Panel title="Live run">
            {runId ? <LiveRun runId={runId} /> : <p className="text-muted text-[13.5px] py-10 text-center">Upload a file or run a sample to watch each stage execute.</p>}
          </Panel>
        </div>
      </div>
    </>
  );
}
