import React, { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { Check, CircleAlert, Download, Plane, RotateCcw, ShieldCheck, X } from "lucide-react";
import { Sparkle, Ticker } from "./ui.jsx";

// Safe autopilot: a confirm sheet, the running state and the report (POST /api/jobs/{id}/autopilot).

const STEPS = [
  "Accepting journal-style edits",
  "Accepting PubMed / CrossRef-verified references",
  "Second AI check of the copy edits",
  "Rendering the tracked file and running QC",
];

function Sheet({ children, onClose, busy }) {
  useEffect(() => {
    const h = (e) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose, busy]);
  return (
    <motion.div className="ap-scrim" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
      onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <motion.div className="ap-sheet card" role="dialog" aria-modal="true" aria-label="Autopilot"
        initial={{ opacity: 0, y: 18, scale: 0.97 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 10, scale: 0.98 }}
        transition={{ type: "spring", stiffness: 380, damping: 32 }}>
        {children}
      </motion.div>
    </motion.div>
  );
}

function Running() {
  const [step, setStep] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setStep((s) => Math.min(s + 1, STEPS.length - 1)), 1400);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="ap-run">
      <div className="ap-orb"><Plane size={22} />{[0, 1, 2, 3].map((i) => <span key={i} className="spark" style={{ "--d": `${i * 0.5}s`, left: `${[8, 70, 82, 18][i]}%`, top: `${[14, 6, 74, 80][i]}%` }}><Sparkle /></span>)}</div>
      <h2>AI is finishing the edit…</h2>
      <ul className="ap-steps">
        {STEPS.map((s, i) => (
          <li key={s} className={i < step ? "done" : i === step ? "now" : ""}>
            <span className="dot">{i < step ? <Check size={12} /> : null}</span>{s}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function AutopilotSheet({ phase, report, error, onRun, onClose, onUndo, onReview, onDownload }) {
  const busy = phase === "running" || phase === "undoing";
  return (
    <Sheet onClose={onClose} busy={busy}>
      {!busy && <button className="btn ghost icon ap-x" onClick={onClose} aria-label="Close"><X size={16} /></button>}
      {phase === "confirm" && (
        <div className="ap-body">
          <div className="ap-head"><span className="ap-badge"><Plane size={16} /></span><h2>Autopilot</h2></div>
          <p className="muted">AI finishes the edit and leaves you only what needs a person.</p>
          <ul className="ap-list">
            <li><Check size={14} /> Accepts journal-style edits, verified references, spelling and grammar-checker fixes</li>
            <li><Check size={14} /> Accepts an AI copy edit only when a second AI check agrees</li>
            <li><ShieldCheck size={14} /> Keeps every author query and every decision you already made</li>
            <li><ShieldCheck size={14} /> Every change stays tracked in Word, and you can undo it in one click</li>
          </ul>
          {error && <div className="error-box">{error}</div>}
          <div className="ap-actions">
            <button className="btn" onClick={onClose}>Cancel</button>
            <button className="btn magic" onClick={onRun}><Plane size={15} /> Run autopilot</button>
          </div>
        </div>
      )}
      {busy && (phase === "running" ? <Running /> : <div className="ap-run"><h2>Undoing…</h2></div>)}
      {phase === "done" && report && (
        <div className="ap-body">
          <div className="ap-head"><span className="ap-badge"><Plane size={16} /></span><h2>Autopilot finished</h2></div>
          <div className="counters ap-counters">
            <div className="counter"><b><Ticker value={report.accepted} /></b><span>accepted</span></div>
            <div className="counter"><b><Ticker value={report.left} /></b><span>left for you</span></div>
            <div className="counter"><b><Ticker value={report.queries} /></b><span>author queries</span></div>
          </div>
          <table className="ap-table">
            <thead><tr><th>Suggestions</th><th>Accepted</th><th>Left</th></tr></thead>
            <tbody>
              {report.by_category.map((c) => <tr key={c.category}><td>{c.label}</td><td>{c.accepted}</td><td>{c.left || ""}</td></tr>)}
            </tbody>
          </table>
          {report.judge.used && <p className="muted small">Second AI check agreed with {report.judge.agreed} of {report.judge.asked} AI copy edits.</p>}
          <div className={`ap-qc ${report.qc.passed ? "pass" : "fail"}`}>
            {report.qc.passed ? <ShieldCheck size={16} /> : <CircleAlert size={16} />}
            <div>
              <b>{report.qc.passed ? `QC passed${report.qc.warnings ? ` · ${report.qc.warnings} warning${report.qc.warnings === 1 ? "" : "s"}` : ""}` : `QC blocked: ${report.qc.errors} error${report.qc.errors === 1 ? "" : "s"}`}</b>
              {report.qc.failed.length > 0 && <small>{report.qc.failed.map((c) => `${c.description} (${c.issues})`).join(" · ")}</small>}
            </div>
          </div>
          {report.left_items.length > 0 && (
            <details className="ap-left">
              <summary>Why {report.left} {report.left === 1 ? "was" : "were"} left for you</summary>
              <ul>{report.left_items.slice(0, 50).map((d) => (
                <li key={d.id}><span className="pill">{d.label}</span> <del>{d.old}</del> → <ins>{d.new}</ins> <small>{d.why}</small></li>
              ))}</ul>
            </details>
          )}
          {error && <div className="error-box">{error}</div>}
          <div className="ap-actions">
            <button className="btn ghost" onClick={onUndo}><RotateCcw size={15} /> Undo autopilot</button>
            <span className="spacer" />
            <button className="btn" onClick={onDownload}><Download size={15} /> Tracked .docx</button>
            <button className="btn primary" onClick={onReview}>{report.left ? `Review ${report.left} left` : "Done"}</button>
          </div>
        </div>
      )}
    </Sheet>
  );
}
