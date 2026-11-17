import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { ClipboardCheck, FileStack, Gauge, History, ScrollText, Settings as Cog, ShoppingCart, Upload, Users } from "lucide-react";
import clsx from "clsx";
import "./index.css";
import { api, loadPref, ReviewerCtx, REVIEWERS_FALLBACK, savePref, useApi } from "./lib";
import Dashboard from "./pages/Dashboard";
import Process from "./pages/Process";
import RunDetail from "./pages/RunDetail";
import { HistoryPage, ReviewQueue } from "./pages/Lists";
import { AuditLog, PurchaseOrders, Vendors } from "./pages/Master";
import Settings from "./pages/Settings";

const NAV = [
  { to: "/", label: "Overview", icon: Gauge, end: true },
  { to: "/process", label: "Process invoice", icon: Upload },
  { to: "/review", label: "Review queue", icon: ClipboardCheck, badge: true },
  { to: "/history", label: "Invoice history", icon: History },
  { to: "/purchase-orders", label: "Purchase orders", icon: ShoppingCart },
  { to: "/vendors", label: "Vendors", icon: Users },
  { to: "/audit", label: "Audit log", icon: ScrollText },
  { to: "/settings", label: "Rules & settings", icon: Cog },
];

function Shell() {
  const [reviewers, setReviewers] = useState(REVIEWERS_FALLBACK);
  const [reviewer, setR] = useState(loadPref("ap.reviewer", REVIEWERS_FALLBACK[0]));
  const { data: queue } = useApi<any[]>("/api/review-queue", 5000);
  useEffect(() => { api("/api/settings").then((s) => { setReviewers(s.reviewers); if (!s.reviewers.includes(reviewer)) setR(s.reviewers[0]); }).catch(() => {}); }, []);
  const setReviewer = (s: string) => { setR(s); savePref("ap.reviewer", s); };
  return (
    <ReviewerCtx.Provider value={{ reviewer, setReviewer, reviewers }}>
      <div className="flex min-h-full">
        <aside className="hidden md:flex w-60 shrink-0 flex-col bg-ledger text-white/85 sticky top-0 h-screen">
          <div className="px-5 pt-6 pb-7">
            <div className="flex items-center gap-2.5">
              <div className="grid place-items-center size-8 rounded-md bg-white/10"><FileStack size={17} /></div>
              <div>
                <div className="font-semibold text-white leading-tight">AP Intelligence</div>
                <div className="text-[11.5px] text-white/55">Invoice decision engine</div>
              </div>
            </div>
          </div>
          <nav className="flex-1 px-3 space-y-0.5" aria-label="Main">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => clsx("flex items-center gap-3 rounded-md px-3 py-2 text-[13.5px]", isActive ? "bg-white/12 text-white font-medium" : "hover:bg-white/6 hover:text-white")}>
                <n.icon size={16} strokeWidth={2} />
                <span className="flex-1">{n.label}</span>
                {n.badge && !!queue?.length && <span className="num text-[11px] rounded-full bg-amber-400/90 text-ledger px-1.5 font-semibold">{queue.length}</span>}
              </NavLink>
            ))}
          </nav>
          <div className="p-4 border-t border-white/10 text-[12px]">
            <label htmlFor="rv" className="block text-white/55 mb-1">Signed in as reviewer</label>
            <select id="rv" value={reviewer} onChange={(e) => setReviewer(e.target.value)} className="w-full rounded-md bg-white/10 text-white px-2 py-1.5 text-[12.5px]">
              {reviewers.map((r) => <option key={r} className="text-ink">{r}</option>)}
            </select>
          </div>
        </aside>
        <div className="flex-1 min-w-0">
          <div className="md:hidden flex gap-1 overflow-x-auto bg-ledger p-2">
            {NAV.map((n) => <NavLink key={n.to} to={n.to} end={n.end} className="text-white/85 text-xs whitespace-nowrap px-2 py-1 rounded">{n.label}</NavLink>)}
          </div>
          <main className="max-w-[1320px] mx-auto px-5 md:px-8 py-7">
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/process" element={<Process />} />
              <Route path="/runs/:id" element={<RunDetail />} />
              <Route path="/review" element={<ReviewQueue />} />
              <Route path="/history" element={<HistoryPage />} />
              <Route path="/purchase-orders" element={<PurchaseOrders />} />
              <Route path="/vendors" element={<Vendors />} />
              <Route path="/audit" element={<AuditLog />} />
              <Route path="/settings" element={<Settings />} />
            </Routes>
          </main>
        </div>
      </div>
    </ReviewerCtx.Provider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><BrowserRouter><Shell /></BrowserRouter></React.StrictMode>);
