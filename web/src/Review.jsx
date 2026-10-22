import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Download, FileText, LayoutList, PanelRightClose, PanelRightOpen, Plane, RefreshCw, ScrollText, Wand2 } from "lucide-react";
import { api, download, submission } from "./api.js";
import { AutopilotSheet } from "./Autopilot.jsx";
import { Brand } from "./App.jsx";
import DocView from "./DocView.jsx";
import FileViewer from "./FileViewer.jsx";
import ListView from "./ListView.jsx";
import Popover from "./Popover.jsx";
import Processing from "./Processing.jsx";
import Sidebar from "./Sidebar.jsx";
import { LAYERS } from "./labels.js";
import { ProgressRing, ThemeButton, useMedia, useOutside, useToast } from "./ui.jsx";

const DOWNLOADS = [["tracked", "Tracked .docx"], ["clean", "Clean .docx"], ["queries", "Query sheet (.xlsx)"], ["qc_report", "QC report"]];
const VIEW_KEY = "editos.view";
const PANE_KEY = "editos.filePaneW";

/** Drag handle between the manuscript and a file opened beside it (arrow keys resize too). */
function SplitHandle({ width, setWidth, max }) {
  const drag = useRef(null);
  const clamp = (w) => Math.round(Math.min(Math.max(w, 320), Math.max(320, max)));
  return (
    <div className="split-handle" role="separator" aria-orientation="vertical" aria-label="Resize file pane" tabIndex={0}
      aria-valuenow={width} aria-valuemin={320} aria-valuemax={max}
      onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); drag.current = { x: e.clientX, w: width }; e.currentTarget.classList.add("drag"); }}
      onPointerMove={(e) => { if (drag.current) setWidth(clamp(drag.current.w + drag.current.x - e.clientX)); }}
      onPointerUp={(e) => { drag.current = null; e.currentTarget.classList.remove("drag"); }}
      onPointerCancel={(e) => { drag.current = null; e.currentTarget.classList.remove("drag"); }}
      onDoubleClick={() => setWidth(clamp(560))}
      onKeyDown={(e) => {
        if (e.key === "ArrowLeft") setWidth(clamp(width + 32));
        else if (e.key === "ArrowRight") setWidth(clamp(width - 32));
        else return;
        e.preventDefault();
      }}>
      <i />
    </div>
  );
}

function DownloadMenu({ id, final, setFinal, onError }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  return (
    <div className="menu" ref={ref}>
      <button className="btn icon" onClick={() => setOpen(!open)} title="Downloads" aria-label="Downloads"><Download size={16} /></button>
      <AnimatePresence>
        {open && (
          <motion.div className="menu-panel" initial={{ opacity: 0, y: -6, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.16 }}>
            <div className="hint">Downloads reflect the last “Apply decisions”.</div>
            {DOWNLOADS.map(([k, label]) => (
              <button key={k} onClick={() => { setOpen(false); download(id, k).catch((e) => onError(e.message)); }}><FileText size={15} /> {label}</button>
            ))}
            <hr />
            <label className="toggle" style={{ padding: "8px 10px" }}><input type="checkbox" checked={final} onChange={(e) => setFinal(e.target.checked)} /> Final: leave undecided suggestions out of the tracked copy</label>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export default function Review({ id, theme, nextTheme }) {
  const toast = useToast();
  const wideScreen = useMedia("(min-width: 761px)");
  const [job, setJob] = useState(null);
  const [data, setData] = useState(null);
  const [doc, setDoc] = useState(null);
  const [queries, setQueries] = useState([]);
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  const [view, setView] = useState(() => { try { return localStorage.getItem(VIEW_KEY) || "doc"; } catch { return "doc"; } });
  const [tab, setTab] = useState("changes");
  const [sideOpen, setSideOpen] = useState(true);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [layer, setLayer] = useState(null);
  const [onlyPending, setOnlyPending] = useState(true);
  const [selId, setSelId] = useState(null);
  const [popOpen, setPopOpen] = useState(false);
  const [scrollKey, setScrollKey] = useState(0);
  const [anim, setAnim] = useState({});
  const [reveal, setReveal] = useState(false);
  const [sawRun, setSawRun] = useState(false);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [final, setFinal] = useState(false);
  const [ap, setAp] = useState(null); // autopilot: {phase: confirm | running | done | undoing, report, error}
  const [stageW, setStageW] = useState(1200);
  const stageRef = useRef(null);
  const ranRef = useRef(false);
  const reviewRef = useRef(null);
  const [files, setFiles] = useState(null); // the submission's files (Files tab)
  const [filesError, setFilesError] = useState("");
  const [fileOpen, setFileOpen] = useState(null); // {path, mode: overlay | beside}
  const [paneW, setPaneW] = useState(() => { try { return Number(localStorage.getItem(PANE_KEY)) || 520; } catch { return 520; } });
  const [reviewW, setReviewW] = useState(1440);
  useEffect(() => { try { localStorage.setItem(PANE_KEY, String(paneW)); } catch { /* private mode */ } }, [paneW]);

  useEffect(() => { try { localStorage.setItem(VIEW_KEY, view); } catch { /* private mode */ } }, [view]);

  const loadAll = useCallback(async () => {
    const [c, q, r, d] = await Promise.all([api.changes(id), api.queries(id), api.qc(id), api.document(id)]);
    setData(c);
    setQueries(q.items);
    setReport(r);
    setDoc(d);
  }, [id]);
  const reloadChanges = useCallback(async () => { setData(await api.changes(id)); }, [id]);

  // Poll the job while it runs; then load everything and reveal the suggestions.
  useEffect(() => {
    let t;
    let alive = true;
    const poll = async () => {
      try {
        const j = await api.job(id);
        if (!alive) return;
        setJob(j);
        if (j.status === "done") {
          await loadAll();
          if (!alive) return;
          // Let the processing view finish its last stage before the page slides in.
          setTimeout(() => {
            if (!alive) return;
            setReady(true);
            setReveal(true);
            setTimeout(() => setReveal(false), 2800);
          }, ranRef.current ? 900 : 0);
        } else if (j.status !== "failed") {
          setSawRun(true);
          ranRef.current = true;
          t = setTimeout(poll, 900);
        }
      } catch (e) {
        if (alive) setError(e.message);
      }
    };
    poll();
    return () => { alive = false; clearTimeout(t); };
  }, [id, loadAll]);

  useEffect(() => {
    const el = stageRef.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(() => setStageW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, [job && job.status, doc]);

  const jobDone = job && job.status === "done";
  useEffect(() => {
    if (!jobDone) return;
    submission.list(id).then((r) => setFiles(r.files)).catch((e) => setFilesError(e.message));
  }, [id, jobDone]);
  useEffect(() => {
    const el = reviewRef.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(() => setReviewW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const openFile = useCallback((f, mode = "overlay") => setFileOpen({ path: f.path, mode }), []);
  const closeFile = useCallback(() => setFileOpen(null), []);

  const items = data ? data.items : [];
  const counts = useMemo(() => {
    const out = {};
    for (const i of items) {
      const row = (out[i.layer] = out[i.layer] || { total: 0, suggested: 0, accepted: 0, rejected: 0 });
      row.total += 1;
      row[i.status] += 1;
    }
    return out;
  }, [items]);
  const nav = useMemo(() => items.filter((i) => (!layer || i.layer === layer) && (!onlyPending || i.status === "suggested" || i.id === selId)),
    [items, layer, onlyPending, selId]);
  const decided = items.filter((i) => i.status !== "suggested").length;
  const sel = items.find((i) => i.id === selId) || null;

  const select = useCallback((itemId, scroll = false) => {
    setSelId(itemId);
    setPopOpen(!!itemId);
    if (scroll) setScrollKey((k) => k + 1);
  }, []);

  const flash = useCallback((ids, kind) => {
    setAnim((a) => { const n = { ...a }; for (const i of ids) n[i] = kind; return n; });
    setTimeout(() => {
      setAnim((a) => { const n = { ...a }; for (const i of ids) n[i] = "settle"; return n; });
      setTimeout(() => setAnim((a) => { const n = { ...a }; for (const i of ids) if (n[i] === "settle") delete n[i]; return n; }), 650);
    }, 330);
  }, []);

  const decide = useCallback(async (ids, status) => {
    const before = Object.fromEntries(items.filter((i) => ids.includes(i.id)).map((i) => [i.id, i.status]));
    const animated = ids.filter((i) => before[i] === "suggested" && status !== "suggested");
    if (animated.length && ids.length < 30) flash(animated, status === "accepted" ? "accepting" : "rejecting");
    setData((d) => ({ ...d, items: d.items.map((i) => (ids.includes(i.id) ? { ...i, status } : i)) }));
    try {
      await api.decide(id, { ids, status });
    } catch (e) {
      toast(e.message, "error");
      reloadChanges();
    }
  }, [id, items, flash, toast, reloadChanges]);

  const editWording = useCallback(async (itemId, text) => {
    setData((d) => ({ ...d, items: d.items.map((i) => (i.id === itemId ? { ...i, new: text, status: "accepted", edited: true } : i)) }));
    try {
      await api.edit(id, itemId, text);
      toast("Reworded and accepted");
    } catch (e) {
      toast(e.message, "error");
      reloadChanges();
    }
  }, [id, toast, reloadChanges]);

  const directEdit = useCallback(async (paraId, base, text) => {
    try {
      const r = await api.directEdit(id, { para_id: paraId, base, text });
      await reloadChanges();
      const n = r.created.length;
      const s = r.superseded.length;
      toast(n ? `Saved your edit${n > 1 ? ` (${n} changes)` : ""}${s ? ` · ${s} overlapping suggestion${s > 1 ? "s" : ""} set aside` : ""}` : "Edit removed — paragraph back to the suggestions");
      return true;
    } catch (e) {
      toast(e.message, "error");
      return false;
    }
  }, [id, toast, reloadChanges]);

  const decideQuery = useCallback(async (key, status) => {
    setQueries((qs) => qs.map((q) => (q.key === key ? { ...q, status } : q)));
    try {
      await api.decide(id, { ids: [key], status });
      toast(status === "rejected" ? "Query dropped" : status === "accepted" ? "Query kept" : "Query restored");
    } catch (e) { toast(e.message, "error"); }
  }, [id, toast]);

  const move = useCallback((dir) => {
    if (!nav.length) return;
    const k = nav.findIndex((i) => i.id === selId);
    const next = k < 0 ? (dir > 0 ? 0 : nav.length - 1) : Math.min(Math.max(k + dir, 0), nav.length - 1);
    select(nav[next].id, true);
  }, [nav, selId, select]);

  const decideSelected = useCallback((status) => {
    if (!sel) return;
    const k = nav.findIndex((i) => i.id === sel.id);
    const rest = onlyPending && status !== "suggested" ? nav.filter((i) => i.id !== sel.id) : nav;
    decide([sel.id], status);
    if (status === "suggested") return;
    const nextItem = onlyPending ? rest[Math.min(k, rest.length - 1)] : nav[Math.min(k + 1, nav.length - 1)];
    if (nextItem && nextItem.id !== sel.id) setTimeout(() => select(nextItem.id, true), 260);
    else setPopOpen(false);
  }, [sel, nav, onlyPending, decide, select]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName) || e.target.isContentEditable) return;
      if (e.key === "j") move(1);
      else if (e.key === "k") move(-1);
      else if (e.key === "a") decideSelected("accepted");
      else if (e.key === "r") decideSelected("rejected");
      else if (e.key === "u") decideSelected("suggested");
      else if (e.key === "Escape") { if (popOpen) setPopOpen(false); else if (fileOpen) setFileOpen(null); else setSelId(null); }
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [move, decideSelected, popOpen, fileOpen]);

  // Close the popover on a click outside it (and outside a suggestion).
  useEffect(() => {
    if (!popOpen) return undefined;
    const h = (e) => { if (!e.target.closest(".pop, [data-cid], .mini, .ccard, .sidebar")) setPopOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [popOpen]);

  async function bulk(l) {
    const n = counts[l] ? counts[l].suggested : 0;
    if (!n || !window.confirm(`Accept all ${n} pending ${LAYERS[l] || l} suggestions?`)) return;
    const ids = items.filter((i) => i.layer === l && i.status === "suggested").map((i) => i.id);
    setData((d) => ({ ...d, items: d.items.map((i) => (ids.includes(i.id) ? { ...i, status: "accepted" } : i)) }));
    try {
      await api.decide(id, { layer: l, status: "accepted" });
      toast(`Accepted ${n} ${LAYERS[l] || l} suggestion${n > 1 ? "s" : ""}`);
      reloadChanges();
    } catch (e) { toast(e.message, "error"); reloadChanges(); }
  }

  async function rerender() {
    setBusy(true);
    try {
      const r = await api.render(id, final);
      setReport(r);
      setQueries((await api.queries(id)).items);
      toast(`Outputs rendered · QC ${r.qc.passed ? "passed" : "blocked"} · ${r.changes.pending} pending`);
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function runAutopilot() {
    setAp({ phase: "running" });
    const t0 = Date.now();
    try {
      const rep = await api.autopilot(id);
      const before = new Set(items.filter((i) => i.status === "suggested").map((i) => i.id));
      const c = await api.changes(id);
      // Let the steps play out before the report replaces them.
      await new Promise((r) => setTimeout(r, Math.max(0, 4200 - (Date.now() - t0))));
      setData(c);
      setReport(await api.qc(id));
      setQueries((await api.queries(id)).items);
      flash(c.items.filter((i) => before.has(i.id) && i.status === "accepted").map((i) => i.id), "accepting");
      setAp({ phase: "done", report: rep });
    } catch (e) {
      setAp({ phase: "confirm", error: e.message });
    }
  }

  async function undoAutopilot() {
    const rep = ap && ap.report;
    setAp({ phase: "undoing", report: rep });
    try {
      const r = await api.undoAutopilot(id);
      await loadAll();
      setAp(null);
      toast(`Autopilot undone · ${r.undone} suggestion${r.undone === 1 ? "" : "s"} back to pending`);
    } catch (e) {
      setAp({ phase: "done", report: rep, error: e.message });
    }
  }

  function reviewLeft() {
    setAp(null);
    setOnlyPending(true);
    setLayer(null);
    const first = items.find((i) => i.status === "suggested");
    if (first) select(first.id, true);
  }

  const pickQuery = (q) => {
    setView("doc");
    const el = document.querySelector(`[data-q~="${q.key}"]`) || document.querySelector(`[data-pid="${q.para_id}"]`);
    if (el) el.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    if (!wideScreen) setSheetOpen(false);
  };

  const done = job && job.status === "done" && data && doc && ready;
  const showSide = done && (wideScreen ? sideOpen : true);
  // Comment margin only when the side panel is closed: with the panel open the page stays centred
  // and author queries show under their paragraph (they are also in the panel's Queries tab).
  const wide = wideScreen && stageW >= 1020 && !showSide;
  const openedFile = fileOpen && files ? files.find((f) => f.path === fileOpen.path) : null;
  // Beside the manuscript only where there is room; phones get the file full screen with a back button.
  const canSplit = wideScreen && reviewW >= 900;
  const fileMode = openedFile ? (!wideScreen ? "full" : fileOpen.mode === "beside" && !canSplit ? "overlay" : fileOpen.mode) : null;
  const paneMax = reviewW - (showSide ? 372 : 0) - 520; // the manuscript keeps at least ~520px
  const paneWidth = Math.round(Math.min(Math.max(paneW, 320), Math.max(320, paneMax)));
  const viewerProps = openedFile && {
    jobId: id, file: openedFile, files, canSplit, onClose: closeFile, onError: (m) => toast(m, "error"),
    onMode: (m) => setFileOpen((o) => o && { ...o, mode: m }),
    onOpenPath: (p) => setFileOpen((o) => ({ path: p, mode: o ? o.mode : "overlay" })),
  };

  return (
    <>
      <header className="topbar">
        <Brand />
        {job && (
          <div className="article">
            <b title={job.input_name}>{doc ? doc.article : job.input_name}</b>
            <span>{(doc && doc.journal_name) || job.journal.toUpperCase()} · {job.stages.join(" + ")}</span>
          </div>
        )}
        <span className="spacer" />
        {done && (
          <div className="top-actions">
            <div className="seg hide-phone" role="tablist" aria-label="View">
              {[["doc", <ScrollText size={14} key="i" />, "Document"], ["list", <LayoutList size={14} key="i" />, "List"]].map(([k, icon, label]) => (
                <button key={k} className={view === k ? "on" : ""} onClick={() => setView(k)} role="tab" aria-selected={view === k}>
                  {view === k && <motion.span layoutId="segpill" className="seg-pill" transition={{ type: "spring", stiffness: 500, damping: 38 }} />}
                  {icon}{label}
                </button>
              ))}
            </div>
            <ProgressRing done={decided} total={items.length} />
            <button className="btn magic" onClick={() => setAp({ phase: "confirm" })} disabled={busy || !!ap}
              title="AI accepts the suggestions it is sure of and leaves the rest for you">
              <Plane size={15} /> <span className="hide-phone">Autopilot</span>
            </button>
            <button className="btn primary hide-phone" onClick={rerender} disabled={busy} title="Render the tracked and clean copies with your decisions">
              {busy ? <RefreshCw size={15} className="spin" /> : <Wand2 size={15} />} {busy ? "Rendering…" : "Apply decisions"}
            </button>
            <DownloadMenu id={id} final={final} setFinal={setFinal} onError={(m) => toast(m, "error")} />
          </div>
        )}
        <ThemeButton theme={theme} onClick={nextTheme} />
        {done && wideScreen && (
          <button className="btn ghost icon" onClick={() => setSideOpen(!sideOpen)} title={sideOpen ? "Hide panel" : "Show panel"} aria-label="Toggle panel">
            {sideOpen ? <PanelRightClose size={17} /> : <PanelRightOpen size={17} />}
          </button>
        )}
      </header>

      <div className="review" ref={reviewRef}>
        <main className={`stage ${wideScreen && stageW < 700 ? "narrow" : ""}`} ref={stageRef}>
          {error && !job && <div className="list-wrap"><div className="error-box">{error}</div></div>}
          {!job && !error && <div className="list-wrap muted">Loading…</div>}
          {job && job.status === "failed" && (
            <div className="list-wrap"><div className="card" style={{ padding: 20 }}><h2 style={{ marginTop: 0 }}>This manuscript could not be processed</h2><pre className="error-box">{job.error}</pre></div></div>
          )}
          <AnimatePresence mode="wait">
            {job && (job.status === "queued" || job.status === "running" || (job.status === "done" && !done)) && (
              <motion.div key="proc" exit={{ opacity: 0, scale: 0.985 }} transition={{ duration: 0.35 }}>
                <Processing job={job} />
              </motion.div>
            )}
            {done && (
              <motion.div key={`view-${view}`} initial={{ opacity: 0, y: sawRun ? 14 : 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: 0.4, ease: [0.2, 0.8, 0.2, 1] }}>
                {view === "doc" ? (
                  <DocView doc={doc} items={items} queries={queries} selId={selId} scrollKey={scrollKey} anim={anim}
                    reveal={reveal} wide={wide} article={doc.article} journal={doc.journal_name || job.journal.toUpperCase()}
                    onSelect={(i) => select(i)} onDirectEdit={directEdit} onDecideQuery={decideQuery} />
                ) : (
                  <ListView items={nav} selId={selId} onlyPending={onlyPending} onSelect={(i) => { setSelId(i); setPopOpen(false); }}
                    onDecide={(i, s) => { setSelId(i); decide([i], s); }} onEdit={(i, t) => { setSelId(i); editWording(i, t); }} />
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </main>
        <AnimatePresence>
          {done && fileMode === "beside" && (
            <motion.div key="fpane" className="fpane" style={{ width: paneWidth }}
              initial={{ opacity: 0, x: 48 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 48 }}
              transition={{ type: "spring", stiffness: 420, damping: 40 }}>
              <SplitHandle width={paneWidth} setWidth={setPaneW} max={paneMax} />
              <FileViewer {...viewerProps} mode="beside" />
            </motion.div>
          )}
        </AnimatePresence>
        {showSide && (
          <Sidebar tab={tab} setTab={setTab} items={items} queries={queries} report={report} counts={counts}
            layer={layer} setLayer={setLayer} onlyPending={onlyPending} setOnlyPending={setOnlyPending}
            nav={nav} selId={selId} onPick={(i) => { if (!wideScreen) setSheetOpen(false); if (view === "list") setSelId(i); else select(i, true); }}
            bulk={bulk} decideQuery={decideQuery} onPickQuery={pickQuery}
            files={files} filesError={filesError} activeFile={openedFile ? openedFile.path : null} canSplit={canSplit} onOpenFile={openFile}
            open={wideScreen ? true : sheetOpen} setOpen={setSheetOpen} />
        )}
      </div>
      <AnimatePresence>
        {done && openedFile && fileMode !== "beside" && <FileViewer key="fv" {...viewerProps} mode={fileMode} />}
      </AnimatePresence>
      <AnimatePresence>
        {ap && (
          <AutopilotSheet key="ap" phase={ap.phase} report={ap.report} error={ap.error} onRun={runAutopilot}
            onClose={() => setAp(null)} onUndo={undoAutopilot} onReview={reviewLeft}
            onDownload={() => download(id, "tracked").catch((e) => toast(e.message, "error"))} />
        )}
      </AnimatePresence>
      <AnimatePresence>
        {done && view === "doc" && popOpen && sel && (
          <Popover key="pop" item={sel} onClose={() => setPopOpen(false)} onNav={move}
            onDecide={(s) => { if (s === "suggested") decide([sel.id], s); else { setSelId(sel.id); decideSelectedFor(sel, s); } }}
            onEdit={(t) => editWording(sel.id, t)} />
        )}
      </AnimatePresence>
    </>
  );

  function decideSelectedFor(item, status) {
    // From the popover: decide, then move on to the next pending suggestion like the keyboard does.
    if (item.id === selId) decideSelected(status);
    else decide([item.id], status);
  }
}
