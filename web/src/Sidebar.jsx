import React, { useState } from "react";
import { motion } from "framer-motion";
import { AlertTriangle, CheckCheck, CheckCircle2, FolderOpen, Layers, ListChecks, MessageSquareText, ShieldCheck, XCircle } from "lucide-react";
import FilesPanel from "./FilesPanel.jsx";
import { LAYERS, ruleLabel, show } from "./labels.js";

function Suggestions({ items, counts, layer, setLayer, onlyPending, setOnlyPending, nav, selId, onPick, bulk }) {
  const total = items.length;
  const pending = items.filter((i) => i.status === "suggested").length;
  const accepted = items.filter((i) => i.status === "accepted").length;
  const [limit, setLimit] = useState(80);
  return (
    <>
      <div className="summary">
        <div className="stat"><b>{pending}</b><span>pending</span></div>
        <div className="stat"><b style={{ color: "var(--ok)" }}>{accepted}</b><span>accepted</span></div>
        <div className="stat"><b>{total - pending - accepted}</b><span>rejected</span></div>
      </div>
      <div className="layer-list">
        <button className={`layer-row ${!layer ? "on" : ""}`} onClick={() => setLayer(null)}>
          <span className="name"><Layers size={14} /> All layers</span><span className="nums">{pending}/{total}</span>
        </button>
        {Object.entries(counts).map(([l, c]) => (
          <div key={l} className={`layer-row ${layer === l ? "on" : ""}`} role="button" tabIndex={0}
            onClick={() => setLayer(layer === l ? null : l)} onKeyDown={(e) => { if (e.key === "Enter") setLayer(l); }}>
            <span className="name"><span className={`dot l-${l}`} />{LAYERS[l] || l}</span>
            <span className="nums">
              {c.suggested}/{c.total}{" "}
              <button className="linkbtn" disabled={!c.suggested} onClick={(e) => { e.stopPropagation(); bulk(l); }} title={`Accept all pending ${LAYERS[l] || l}`}>
                <CheckCheck size={13} style={{ verticalAlign: "-2px" }} /> Accept all</button>
            </span>
            <span className="meter"><i className="a" style={{ width: `${(c.accepted / c.total) * 100}%` }} /><i className="r" style={{ width: `${(c.rejected / c.total) * 100}%` }} /></span>
          </div>
        ))}
      </div>
      <label className="toggle"><input type="checkbox" checked={onlyPending} onChange={(e) => setOnlyPending(e.target.checked)} /> Pending only</label>
      <div className="keys">
        <span><kbd>j</kbd><kbd>k</kbd> next / previous</span><span><kbd>a</kbd> accept</span><span><kbd>r</kbd> reject</span><span><kbd>u</kbd> undo</span><span><kbd>Esc</kbd> close</span>
      </div>
      <div className="mini-list">
        {nav.slice(0, limit).map((i) => (
          <button key={i.id} className={`mini ${i.id === selId ? "on" : ""}`} onClick={() => onPick(i.id)}>
            <span className={`dot l-${i.layer}`} />
            <span style={{ minWidth: 0 }}>
              <span className="what">
                {i.kind === "insert" ? <ins style={{ textDecoration: "none", color: "var(--ins-text)" }}>{show(i.new)}</ins>
                  : <>{i.old && <del style={{ color: "var(--del-text)" }}>{show(i.old)}</del>} {i.new !== i.old && <ins style={{ textDecoration: "none", color: "var(--ins-text)" }}>{show(i.new)}</ins>}</>}
              </span>
              <span className="why">{ruleLabel(i.reason)}{i.status !== "suggested" ? ` · ${i.status}` : ""}</span>
            </span>
          </button>
        ))}
        {nav.length > limit && <button className="btn sm" onClick={() => setLimit(limit + 120)}>Show more ({nav.length - limit})</button>}
        {nav.length === 0 && <p className="muted small">Nothing {onlyPending ? "pending " : ""}in this view. Nice work.</p>}
      </div>
    </>
  );
}

function Queries({ items, onDecide, onPick }) {
  if (!items.length) return <p className="muted">No author queries.</p>;
  return items.map((q) => (
    <div key={q.key} className={`qcard ${q.status}`}>
      <div className="note-head"><MessageSquareText size={13} color="var(--warn)" /><b>{q.id || "—"}</b>
        <span>{ruleLabel((q.check || "").split(".")[0])}</span><span className="spacer" />
        <span className={`pill ${q.status}`}>{q.status === "rejected" ? "dropped" : q.status === "accepted" ? "kept" : "pending"}</span></div>
      <div>{q.text}</div>
      <button className="anchor linkbtn" style={{ textAlign: "left", color: "var(--text-2)", fontWeight: 400 }} onClick={() => onPick(q)}>
        {q.anchor ? <>on “<mark>{show(q.anchor).slice(0, 140)}</mark>”</> : <>“{show(q.paragraph).slice(0, 140)}…”</>}
      </button>
      <div className="pop-actions">
        <button className={`btn sm ok ${q.status === "accepted" ? "solid" : ""}`} onClick={() => onDecide(q.key, "accepted")}>Keep</button>
        <button className={`btn sm no ${q.status === "rejected" ? "solid" : ""}`} onClick={() => onDecide(q.key, "rejected")}>Drop</button>
      </div>
    </div>
  ));
}

function QC({ report }) {
  if (!report) return <p className="muted">Loading…</p>;
  const qc = report.qc;
  const failing = qc.checks.filter((c) => !c.passed);
  return (
    <>
      <div className="qc-head">
        <span className={`pill ${qc.passed ? "pass" : "fail"}`} style={{ height: 28, fontSize: 13 }}>
          {qc.passed ? <ShieldCheck size={15} /> : <AlertTriangle size={15} />} QC {qc.passed ? "passed" : "blocked"}</span>
        <span className="muted small">{qc.errors} errors · {qc.warnings} warnings</span>
      </div>
      <p className="muted small" style={{ margin: 0 }}>Checked on {qc.basis}. Delivery ready: {report.delivery_ready ? "yes" : "no"}.</p>
      <div>
        {[...failing, ...qc.checks.filter((c) => c.passed)].map((c) => (
          <div key={c.check} className="qc-check">
            <span className={`ico ${c.passed ? "ok" : c.severity || "error"}`}>{c.passed ? <CheckCircle2 size={16} /> : c.severity === "warning" ? <AlertTriangle size={16} /> : <XCircle size={16} />}</span>
            <span>{c.description}<small>{c.check}</small></span>
            <span className="muted small">{c.issues || ""}</span>
          </div>
        ))}
      </div>
      {qc.issues.length > 0 && (
        <div>
          <h4 style={{ margin: "6px 0" }}>Issues</h4>
          {qc.issues.slice(0, 200).map((i, k) => (
            <div key={k} className="qc-check"><span className={`ico ${i.severity}`}>{i.severity === "warning" ? <AlertTriangle size={14} /> : <XCircle size={14} />}</span>
              <span>{i.message}<small>{i.check} {i.para_id || ""}</small></span><span /></div>
          ))}
        </div>
      )}
      {report.render_conflicts && report.render_conflicts.length > 0 && (
        <div className="error-box small">Could not render: {report.render_conflicts.map((c) => `${c.para_id}: ${c.error}`).join("; ")}</div>
      )}
    </>
  );
}

export default function Sidebar(props) {
  const { tab, setTab, items, queries, report, open, setOpen } = props;
  const liveQ = queries.filter((q) => q.status !== "rejected").length;
  const pending = items.filter((i) => i.status === "suggested").length;
  const tabs = [
    ["changes", <ListChecks size={15} key="i" />, "Suggestions", pending],
    ["queries", <MessageSquareText size={15} key="i" />, "Queries", liveQ],
    ["qc", <ShieldCheck size={15} key="i" />, "QC", report ? (report.qc.passed ? "✓" : "!") : ""],
    ["files", <FolderOpen size={15} key="i" />, "Files", props.files ? props.files.length : ""],
  ];
  return (
    <aside className={`sidebar ${open ? "open" : ""}`} aria-label="Review panel">
      <button className="sheet-handle" onClick={() => setOpen(!open)} aria-label={open ? "Collapse panel" : "Expand panel"}><i /></button>
      <div className="side-tabs" role="tablist">
        {tabs.map(([k, icon, label, n]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => { setTab(k); setOpen(true); }}>
            {icon}{label}{n !== "" && <span className="count">{n}</span>}
            {tab === k && <motion.span layoutId="tabline" className="tab-line" style={{ left: 6, right: 6 }} transition={{ type: "spring", stiffness: 500, damping: 38 }} />}
          </button>
        ))}
      </div>
      <div className="side-body">
        {tab === "changes" && <Suggestions {...props} />}
        {tab === "queries" && <Queries items={queries} onDecide={props.decideQuery} onPick={props.onPickQuery} />}
        {tab === "qc" && <QC report={report} />}
        {tab === "files" && <FilesPanel files={props.files} error={props.filesError} active={props.activeFile} canSplit={props.canSplit} onOpen={props.onOpenFile} />}
      </div>
    </aside>
  );
}
