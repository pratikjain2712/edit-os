import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, download } from "./api.js";

const LAYERS = {
  references: "References",
  structure: "Structure",
  language: "Language",
  house_style: "House style",
  tables_figures: "Tables & figures",
  numbers: "Numbers",
  query: "Queries",
};
const STEPS = ["reading manuscript", "style rules and front matter", "author queries", "copy-edit checkers", "rendering outputs"];
const DOWNLOADS = [["tracked", "Tracked .docx"], ["clean", "Clean .docx"], ["queries", "Query sheet"], ["qc_report", "QC report"]];

// Plain names for the rule that made a suggestion (the raw rule id stays in the tooltip).
const RULE_LABELS = {
  ref_lookup: "Reference checked in PubMed / CrossRef",
  ref_other: "Reference format (book, website, report)",
  ref_journal: "Journal abbreviation (NLM)",
  ref_initials: "Author initials", ref_et_al: "et al. in references", ref_pages: "Page range",
  ref_year: "Year and volume", ref_issue: "Issue number", ref_title_case: "Reference title case",
  citations: "Citation style", renumber: "Reference renumbering",
  fig_citations: "Figure / table citation", fig_captions: "Figure / table caption",
  heading_tags: "Heading tag", sections_headings: "Heading", sections_abstract: "Abstract",
  sections_keywords: "Keywords", sections_back_matter: "Back matter",
  statistics: "Statistics spacing", guide_replace: "Style guide fix",
  spelling: "Spelling (British / American)", langtool: "Grammar and typography", abbrev: "Abbreviation",
};
const FRONT = {
  running_head: "Running head", article_type: "Article type", title: "Title", authors: "Author names",
  affiliations: "Affiliations", affiliation: "Affiliations", correspondence: "Correspondence",
  dates: "History dates", cite: "How to cite", label: "Label",
};
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, " ");
function ruleLabel(reason) {
  const r = reason || "";
  let m = r.match(/^front_matter:\s*(\w+)/);
  if (m) return `Front matter · ${FRONT[m[1]] || cap(m[1])}`;
  m = r.match(/^claude\.(\w+)\s*(?:\((.*)\))?/);
  if (m) return m[2] ? `${cap(m[1])}: ${m[2]}` : cap(m[1]);
  const key = Object.keys(RULE_LABELS).find((k) => r === k || r.startsWith(k + "_") || r.startsWith(k + "."));
  if (key) return RULE_LABELS[key];
  if (r.startsWith("fmt_") || r.startsWith("formatting")) return `Formatting · ${cap(r.replace(/^fmt_|^formatting\.?/, ""))}`;
  if (r.startsWith("guide_")) return `Style guide · ${cap(r.slice(6))}`;
  return cap(r);
}
const FMT_NAMES = { italic: "italic", bold: "bold", superscript: "superscript", subscript: "subscript", small_caps: "small caps" };

const show = (s) => (s || "").replace(new RegExp("\\u2029", "g"), "¶ ").replace(/\t/g, "⇥");
const fmtLabel = (f) => Object.entries(f || {}).filter(([, v]) => v).map(([k]) => FMT_NAMES[k] || k).join(", ") ||
  Object.keys(f || {}).map((k) => `no ${FMT_NAMES[k] || k}`).join(", ");

function Progress({ job }) {
  const i = STEPS.indexOf(job.stage);
  return (
    <section className="card progress">
      <h2>{job.status === "queued" ? "Waiting in queue…" : "Editing…"}</h2>
      <ol className="steps">
        {STEPS.map((s, k) => (
          <li key={s} className={k < i ? "done" : k === i ? "now" : ""}>{s}</li>
        ))}
      </ol>
    </section>
  );
}

function Diff({ item, expanded }) {
  const before = item.context.before;
  const after = item.context.after;
  if (item.kind === "insert") {
    return (
      <div className="diff">
        <div className="muted small">
          {item.position === "start" ? "New line at the start of the article"
            : `New paragraph ${item.where} “${show(item.context.paragraph).slice(0, 60)}…”`}
        </div>
        <ins>{show(item.new)}</ins>
      </div>
    );
  }
  if (expanded) {
    const p = item.context.paragraph;
    return (
      <div className="diff">
        {show(p.slice(0, item.start))}
        {formatOnly(item) ? <span className="fmt">{show(item.old)}</span> : <>{item.old && <del>{show(item.old)}</del>}{item.new && <ins>{show(item.new)}</ins>}</>}
        {show(p.slice(item.end))}
      </div>
    );
  }
  return (
    <div className="diff">
      {before.length >= 60 ? "…" : ""}{show(before.slice(-60))}
      {formatOnly(item) ? <span className="fmt">{show(item.old)}</span> : <>{item.old && <del>{show(item.old)}</del>}{item.new && <ins>{show(item.new)}</ins>}</>}
      {show(after.slice(0, 60))}{after.length > 60 ? "…" : ""}
    </div>
  );
}

const formatOnly = (c) => c.format && c.old === c.new;

function ChangeCard({ item, selected, onSelect, onDecide, onEdit }) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.new);
  const ref = useRef(null);
  useEffect(() => {
    if (selected && ref.current) ref.current.scrollIntoView({ block: "nearest" });
  }, [selected]);
  return (
    <article ref={ref} className={`change ${item.status} ${selected ? "selected" : ""}`} onClick={onSelect}>
      <div className="meta">
        <span className={`layer l-${item.layer}`}>{LAYERS[item.layer] || item.layer}</span>
        <span className="reason" title={`${item.reason} · ${item.para_id}`}>{ruleLabel(item.reason)}</span>
        {item.format && <span className="tag">{fmtLabel(item.format)}</span>}
        {item.source === "ai" && <span className="tag">AI suggestion</span>}
        {item.edited && <span className="tag">edited</span>}
        <span className="spacer" />
        <span className={`pill ${item.status}`}>{item.status === "suggested" ? "pending" : item.status}</span>
      </div>
      <Diff item={item} expanded={expanded} />
      {editing && (
        <form className="edit" onClick={(e) => e.stopPropagation()}
          onSubmit={(e) => { e.preventDefault(); onEdit(draft); setEditing(false); }}>
          <label className="small muted">{item.old ? <>Replace “{show(item.old)}” with</> : "Insert"}</label>
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={Math.min(6, 1 + Math.ceil(draft.length / 90))} autoFocus />
          <div className="actions">
            <button className="btn ok small" type="submit">Save and accept</button>
            <button className="btn small" type="button" onClick={() => { setDraft(item.new); setEditing(false); }}>Cancel</button>
          </div>
        </form>
      )}
      <div className="actions">
        <button className="btn ok small" onClick={(e) => { e.stopPropagation(); onDecide("accepted"); }} title="Accept (a)">Accept</button>
        <button className="btn no small" onClick={(e) => { e.stopPropagation(); onDecide("rejected"); }} title="Reject (r)">Reject</button>
        {item.status !== "suggested" && (
          <button className="btn small" onClick={(e) => { e.stopPropagation(); onDecide("suggested"); }} title="Undo (u)">Undo</button>
        )}
        {!formatOnly(item) && !editing && (
          <button className="btn small" onClick={(e) => { e.stopPropagation(); setDraft(item.new); setEditing(true); }} title="Change the wording">Edit</button>
        )}
        {item.kind !== "insert" && (
          <button className="link small" onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}>
            {expanded ? "less context" : "whole paragraph"}
          </button>
        )}
      </div>
    </article>
  );
}

function Queries({ items, onDecide }) {
  if (!items.length) return <p className="muted">No author queries.</p>;
  return (
    <div className="queries">
      {items.map((q) => (
        <article key={q.key} className={`query ${q.status}`}>
          <div className="meta">
            <b>{q.id || "—"}</b>
            <span className="reason" title={`${q.check} · ${q.para_id}`}>{ruleLabel(q.check.split(".")[0])}</span>
            <span className="spacer" />
            <span className={`pill ${q.status}`}>{q.status === "rejected" ? "dropped" : q.status === "accepted" ? "kept" : "pending"}</span>
          </div>
          <p className="qtext">{q.text}</p>
          <p className="muted small">{q.anchor ? <>on “<mark>{show(q.anchor).slice(0, 160)}</mark>”</> : <>paragraph: “{show(q.paragraph).slice(0, 160)}”</>}</p>
          <div className="actions">
            <button className="btn ok small" onClick={() => onDecide(q.key, "accepted")}>Keep</button>
            <button className="btn no small" onClick={() => onDecide(q.key, "rejected")}>Drop</button>
          </div>
        </article>
      ))}
    </div>
  );
}

function QC({ report }) {
  if (!report) return <p className="muted">Loading…</p>;
  const qc = report.qc;
  return (
    <div className="qc">
      <p>
        <span className={`badge ${qc.passed ? "pass" : "fail"}`}>QC {qc.passed ? "PASSED" : "BLOCKED"}</span>{" "}
        {qc.errors} errors, {qc.warnings} warnings · checked on {qc.basis} · delivery ready: {report.delivery_ready ? "yes" : "no"}
      </p>
      <table>
        <thead><tr><th>Result</th><th>Check</th><th>What</th><th>Issues</th></tr></thead>
        <tbody>
          {qc.checks.map((c) => (
            <tr key={c.check} className={`sev-${c.passed ? "ok" : c.severity || "error"}`}>
              <td>{c.passed ? "pass" : c.severity}</td><td>{c.check}</td><td>{c.description}</td><td>{c.issues || ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {qc.issues.length > 0 && (
        <>
          <h3>Issues</h3>
          <ul className="issues">{qc.issues.map((i, k) => <li key={k} className={`sev-${i.severity}`}><b>{i.check}</b> {i.para_id && <span className="muted">{i.para_id}</span>} {i.message}</li>)}</ul>
        </>
      )}
      {report.render_conflicts.length > 0 && (
        <>
          <h3>Could not render</h3>
          <ul className="issues">{report.render_conflicts.map((c, k) => <li key={k}>{c.para_id}: {c.error}</li>)}</ul>
        </>
      )}
    </div>
  );
}

export default function Review({ id }) {
  const [job, setJob] = useState(null);
  const [data, setData] = useState(null);
  const [queries, setQueries] = useState([]);
  const [report, setReport] = useState(null);
  const [tab, setTab] = useState("changes");
  const [layer, setLayer] = useState(null);
  const [onlyPending, setOnlyPending] = useState(true);
  const [sel, setSel] = useState(0);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [final, setFinal] = useState(false);

  const loadAll = useCallback(async () => {
    const [c, q, r] = await Promise.all([api.changes(id), api.queries(id), api.qc(id)]);
    setData(c);
    setQueries(q.items);
    setReport(r);
  }, [id]);

  useEffect(() => {
    let t;
    const poll = async () => {
      try {
        const j = await api.job(id);
        setJob(j);
        if (j.status === "done") await loadAll();
        else if (j.status !== "failed") t = setTimeout(poll, 1200);
      } catch (e) {
        setError(e.message);
      }
    };
    poll();
    return () => clearTimeout(t);
  }, [id, loadAll]);

  const visible = useMemo(() => {
    if (!data) return [];
    return data.items.filter((i) => (!layer || i.layer === layer) && (!onlyPending || i.status === "suggested"));
  }, [data, layer, onlyPending]);

  const counts = useMemo(() => {
    const out = {};
    for (const i of data ? data.items : []) {
      const row = (out[i.layer] = out[i.layer] || { total: 0, suggested: 0, accepted: 0, rejected: 0 });
      row.total += 1;
      row[i.status] += 1;
    }
    return out;
  }, [data]);
  const pending = data ? data.items.filter((i) => i.status === "suggested").length : 0;

  const decide = useCallback(async (ids, status) => {
    setData((d) => ({ ...d, items: d.items.map((i) => (ids.includes(i.id) ? { ...i, status } : i)) }));
    try {
      await api.decide(id, { ids, status });
    } catch (e) {
      setError(e.message);
      loadAll();
    }
  }, [id, loadAll]);

  const editItem = useCallback(async (itemId, text) => {
    setData((d) => ({ ...d, items: d.items.map((i) => (i.id === itemId ? { ...i, new: text, status: "accepted", edited: true } : i)) }));
    try {
      await api.edit(id, itemId, text);
    } catch (e) {
      setError(e.message);
      loadAll();
    }
  }, [id, loadAll]);

  const decideSelected = useCallback((status) => {
    const item = visible[sel];
    if (!item) return;
    decide([item.id], status);
    if (!onlyPending || status === "suggested") setSel((s) => Math.min(s + 1, visible.length - 1));
  }, [visible, sel, decide, onlyPending]);

  useEffect(() => {
    if (sel >= visible.length && visible.length) setSel(visible.length - 1);
  }, [visible.length, sel]);

  useEffect(() => {
    const onKey = (e) => {
      if (tab !== "changes" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;
      if (e.key === "j") setSel((s) => Math.min(s + 1, visible.length - 1));
      else if (e.key === "k") setSel((s) => Math.max(s - 1, 0));
      else if (e.key === "a") decideSelected("accepted");
      else if (e.key === "r") decideSelected("rejected");
      else if (e.key === "u") decideSelected("suggested");
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [tab, visible.length, decideSelected]);

  async function bulk(l) {
    const n = counts[l] ? counts[l].suggested : 0;
    if (!n || !window.confirm(`Accept all ${n} pending ${LAYERS[l] || l} suggestions?`)) return;
    try {
      await api.decide(id, { layer: l, status: "accepted" });
      await loadAll();
    } catch (e) {
      setError(e.message);
    }
  }

  async function rerender() {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.render(id, final);
      setReport(r);
      setMsg(`Outputs rendered · QC ${r.qc.passed ? "passed" : "blocked"} · ${r.changes.pending} pending`);
      const q = await api.queries(id);
      setQueries(q.items);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function decideQuery(key, status) {
    setQueries((qs) => qs.map((q) => (q.key === key ? { ...q, status } : q)));
    try { await api.decide(id, { ids: [key], status }); } catch (e) { setError(e.message); }
  }

  if (error && !job) return <main className="review"><p className="error">{error}</p></main>;
  if (!job) return <main className="review"><p className="muted">Loading…</p></main>;

  return (
    <main className="review">
      <section className="jobbar card">
        <div>
          <h2>{job.input_name}</h2>
          <div className="muted small">{job.journal.toUpperCase()} · {job.stages.join(" + ")} · job {job.id}</div>
        </div>
        <span className="spacer" />
        {job.status === "done" && (
          <div className="tools">
            <span className="muted small">{pending} pending</span>
            <label className="small" title="Leave undecided suggestions out of the tracked copy">
              <input type="checkbox" checked={final} onChange={(e) => setFinal(e.target.checked)} /> final
            </label>
            <button className="btn primary" onClick={rerender} disabled={busy}>{busy ? "Rendering…" : "Apply decisions"}</button>
            {DOWNLOADS.map(([k, label]) => (
              <button key={k} className="btn" onClick={() => download(id, k).catch((e) => setError(e.message))}>{label}</button>
            ))}
          </div>
        )}
      </section>
      {msg && <p className="note">{msg}</p>}
      {error && <p className="error" onClick={() => setError("")}>{error}</p>}
      {(job.status === "queued" || job.status === "running") && <Progress job={job} />}
      {job.status === "failed" && <section className="card"><h2>Failed</h2><pre className="error">{job.error}</pre></section>}
      {job.status === "done" && data && (
        <>
          <nav className="tabs">
            <button className={tab === "changes" ? "on" : ""} onClick={() => setTab("changes")}>Suggestions <b>{data.items.length}</b></button>
            <button className={tab === "queries" ? "on" : ""} onClick={() => setTab("queries")}>Author queries <b>{queries.filter((q) => q.status !== "rejected").length}</b></button>
            <button className={tab === "qc" ? "on" : ""} onClick={() => setTab("qc")}>QC {report && <b className={report.qc.passed ? "okc" : "noc"}>{report.qc.passed ? "passed" : "blocked"}</b>}</button>
          </nav>
          {tab === "changes" && (
            <div className="split">
              <aside className="layers card">
                <button className={`layerbtn ${!layer ? "on" : ""}`} onClick={() => { setLayer(null); setSel(0); }}>
                  <span>All layers</span><span className="count">{pending}/{data.items.length}</span>
                </button>
                {Object.entries(counts).map(([l, c]) => (
                  <div key={l} className="layerrow">
                    <button className={`layerbtn ${layer === l ? "on" : ""}`} onClick={() => { setLayer(l); setSel(0); }}>
                      <span><i className={`dot l-${l}`} />{LAYERS[l] || l}</span><span className="count">{c.suggested}/{c.total}</span>
                    </button>
                    <button className="link small" disabled={!c.suggested} onClick={() => bulk(l)}>accept all</button>
                  </div>
                ))}
                <label className="small filter"><input type="checkbox" checked={onlyPending} onChange={(e) => { setOnlyPending(e.target.checked); setSel(0); }} /> pending only</label>
                <div className="keys small muted"><kbd>j</kbd>/<kbd>k</kbd> move · <kbd>a</kbd> accept · <kbd>r</kbd> reject · <kbd>u</kbd> undo</div>
              </aside>
              <section className="list">
                {visible.length === 0 && <p className="muted">Nothing {onlyPending ? "pending" : ""} here.</p>}
                {visible.map((item, k) => (
                  <ChangeCard key={item.id} item={item} selected={k === sel} onSelect={() => setSel(k)}
                    onDecide={(s) => { setSel(k); decide([item.id], s); }}
                    onEdit={(t) => { setSel(k); editItem(item.id, t); }} />
                ))}
              </section>
            </div>
          )}
          {tab === "queries" && <Queries items={queries} onDecide={decideQuery} />}
          {tab === "qc" && <QC report={report} />}
        </>
      )}
    </main>
  );
}
