import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { ArrowLeft, CalendarDays, Columns2, Download, Hash, Mail, Maximize2, RotateCcw, RotateCw, Scan, Star, Tag, X, ZoomIn, ZoomOut } from "lucide-react";
import { submission } from "./api.js";
import DocView from "./DocView.jsx";
import { KIND_LABEL, fileIcon, fmtSize } from "./FilesPanel.jsx";

// One submission file, read-only: image (zoom, rotate, fit), .docx page, PDF, metadata card, or a download.
// Shown in an overlay, in a pane beside the manuscript (wide screens), or full screen (phones).

function useBlob(jobId, path, enabled) {
  const [state, setState] = useState({ url: null, error: "" });
  useEffect(() => {
    if (!enabled) return undefined;
    let url = null;
    let alive = true;
    setState({ url: null, error: "" });
    submission.blobUrl(jobId, path).then((u) => {
      url = u;
      if (alive) setState({ url: u, error: "" }); else URL.revokeObjectURL(u);
    }).catch((e) => alive && setState({ url: null, error: e.message }));
    return () => { alive = false; if (url) URL.revokeObjectURL(url); };
  }, [jobId, path, enabled]);
  return state;
}

// --- image ------------------------------------------------------------------------------------

const ZOOMS = [0.1, 0.25, 0.33, 0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 3, 4, 6];

function ImageView({ jobId, file, keysGlobal, hostRef }) {
  const { url, error } = useBlob(jobId, file.path, true);
  const [nat, setNat] = useState(null);
  const [box, setBox] = useState({ w: 0, h: 0 });
  const [zoom, setZoom] = useState("fit");
  const [rot, setRot] = useState(0);
  const canvas = useRef(null);
  useEffect(() => { setZoom("fit"); setRot(0); setNat(null); }, [file.path]);
  useLayoutEffect(() => {
    const el = canvas.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(() => setBox({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, [url]);
  const swap = rot % 180 !== 0;
  const fit = nat && box.w ? Math.min(2, (box.w - 32) / (swap ? nat.h : nat.w), (box.h - 32) / (swap ? nat.w : nat.h)) : 1;
  const scale = zoom === "fit" ? fit : zoom;
  const step = useCallback((dir) => {
    setZoom((z) => {
      const cur = z === "fit" ? fit : z;
      const next = dir > 0 ? ZOOMS.find((x) => x > cur + 1e-3) : [...ZOOMS].reverse().find((x) => x < cur - 1e-3);
      return next ?? cur;
    });
  }, [fit]);
  const rotate = useCallback((d) => setRot((r) => (r + d + 360) % 360), []);

  useEffect(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (!keysGlobal && !(hostRef.current && hostRef.current.contains(document.activeElement))) return;
      if (e.key === "+" || e.key === "=") step(1);
      else if (e.key === "-") step(-1);
      else if (e.key === "0") setZoom(1);
      else if (e.key === "f") setZoom("fit");
      else if (e.key === "r" || e.key === "R") rotate(e.shiftKey ? -90 : 90);
      else return;
      e.preventDefault();
      e.stopPropagation();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [step, rotate, keysGlobal, hostRef]);

  const onWheel = (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    step(e.deltaY < 0 ? 1 : -1);
  };
  useEffect(() => {
    const el = canvas.current;
    if (!el) return undefined;
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  });

  if (error) return <div className="fv-msg error-box">{error}</div>;
  const w = nat ? nat.w * scale : 0;
  const h = nat ? nat.h * scale : 0;
  return (
    <div className="fv-image">
      <div className="fv-canvas" ref={canvas} onDoubleClick={() => setZoom(zoom === "fit" ? 1 : "fit")}>
        {!url && <div className="fv-spinner" aria-label="Loading" />}
        {url && (
          <div className="fv-imgbox" style={{ width: swap ? h : w, height: swap ? w : h, visibility: nat ? "visible" : "hidden" }}>
            <img src={url} alt={file.label || file.name} draggable={false}
              onLoad={(e) => setNat({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
              style={{ width: w || undefined, height: h || undefined, transform: `translate(-50%, -50%) rotate(${rot}deg)` }} />
          </div>
        )}
      </div>
      <div className="fv-tools" role="toolbar" aria-label="Image tools">
        <button className="btn sm ghost icon" onClick={() => step(-1)} title="Zoom out (−)" aria-label="Zoom out"><ZoomOut size={15} /></button>
        <button className="btn sm ghost fv-pct" onClick={() => setZoom(1)} title="Actual size (0)">{Math.round(scale * 100)}%</button>
        <button className="btn sm ghost icon" onClick={() => step(1)} title="Zoom in (+)" aria-label="Zoom in"><ZoomIn size={15} /></button>
        <button className={`btn sm ghost icon ${zoom === "fit" ? "on" : ""}`} onClick={() => setZoom("fit")} title="Fit (f)" aria-label="Fit to view"><Scan size={15} /></button>
        <span className="fv-sep" />
        <button className="btn sm ghost icon" onClick={() => rotate(-90)} title="Rotate left (Shift+R)" aria-label="Rotate left"><RotateCcw size={15} /></button>
        <button className="btn sm ghost icon" onClick={() => rotate(90)} title="Rotate right (r)" aria-label="Rotate right"><RotateCw size={15} /></button>
        {nat && <span className="fv-dim">{nat.w} × {nat.h}</span>}
      </div>
    </div>
  );
}

// --- pdf ----------------------------------------------------------------------------------------

function PdfView({ jobId, file }) {
  const { url, error } = useBlob(jobId, file.path, true);
  if (error) return <div className="fv-msg error-box">{error}</div>;
  if (!url) return <div className="fv-msg"><div className="fv-spinner" /></div>;
  return <iframe className="fv-pdf" src={url} title={file.name} />;
}

// --- metadata card --------------------------------------------------------------------------------

const initials = (name) => name.split(/\s+/).filter(Boolean).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
const fmtDate = (d) => {
  const t = new Date(`${d}T00:00:00`);
  return Number.isNaN(t.getTime()) ? d : t.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
};

function MetaCard({ card, files, onOpenPath }) {
  const byName = Object.fromEntries((files || []).map((f) => [f.name, f]));
  return (
    <div className="meta-card">
      <div className="mc-top">
        <div className="mc-chips">
          {card.article_type && <span className="pill ai">{card.article_type}</span>}
          <span className="pill">{card.source}</span>
        </div>
        {card.title ? <h2 className="mc-title">{card.title}</h2> : <h2 className="mc-title muted">No title in this file</h2>}
        {card.running_title && <p className="mc-rt"><span>Running title</span> {card.running_title}</p>}
        <div className="mc-ids">
          {card.article_id && <span><Hash size={12} /> {card.article_id}</span>}
          {card.journal && <span>{card.journal.toUpperCase()}</span>}
          {card.doi && <span>DOI {card.doi}</span>}
          {card.word_count && <span>{card.word_count} words</span>}
        </div>
      </div>

      {card.dates.length > 0 && (
        <section className="mc-sec">
          <h4><CalendarDays size={14} /> Dates</h4>
          <ol className="mc-dates">
            {card.dates.map((d) => <li key={d.label}><small>{d.label}</small><b>{fmtDate(d.date)}</b></li>)}
          </ol>
        </section>
      )}

      {card.authors.length > 0 && (
        <section className="mc-sec">
          <h4>Authors <span className="count">{card.authors.length}</span></h4>
          <ol className="mc-authors">
            {card.authors.map((a, i) => (
              <li key={i} className={a.corresponding ? "corr" : ""}>
                <span className="mc-av" aria-hidden="true">{initials(a.name) || i + 1}</span>
                <span className="mc-who">
                  <b>{a.prefix ? `${a.prefix} ` : ""}{a.name}{a.corresponding && <span className="mc-corr" title="Corresponding author"><Star size={11} /> Corresponding</span>}</b>
                  {a.affiliation && <small>{a.affiliation}</small>}
                  {(a.email || a.orcid) && <small className="mc-mail">{a.email}{a.email && a.orcid ? " · " : ""}{a.orcid && `ORCID ${a.orcid}`}</small>}
                </span>
              </li>
            ))}
          </ol>
        </section>
      )}

      {card.corresponding && (
        <section className="mc-sec mc-corrbox">
          <h4><Mail size={14} /> Corresponding author</h4>
          <p><b>{card.corresponding.prefix ? `${card.corresponding.prefix} ` : ""}{card.corresponding.name}</b>
            {card.corresponding.address && <><br />{card.corresponding.address}</>}
            {card.corresponding.email && <><br /><span className="mc-mail">{card.corresponding.email}</span></>}</p>
        </section>
      )}

      {card.keywords.length > 0 && (
        <section className="mc-sec">
          <h4><Tag size={14} /> Keywords</h4>
          <div className="mc-kw">{card.keywords.map((k) => <span key={k} className="pill">{k}</span>)}</div>
        </section>
      )}

      {card.figures.length > 0 && (
        <section className="mc-sec">
          <h4>Images listed</h4>
          <ul className="mc-figs">
            {card.figures.map((f, i) => (
              <li key={i}>
                {byName[f.file] ? <button className="linkbtn" onClick={() => onOpenPath(byName[f.file].path)}>{f.file}</button> : <b>{f.file}</b>}
                {f.type && <span className="muted small"> · {f.type}</span>}
                {f.legend && <p>{f.legend}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {card.extra.length > 0 && (
        <section className="mc-sec">
          <dl className="mc-extra">{card.extra.map((x) => <React.Fragment key={x.label}><dt>{x.label}</dt><dd>{x.value}</dd></React.Fragment>)}</dl>
        </section>
      )}
    </div>
  );
}

// --- the viewer -------------------------------------------------------------------------------------

function Body({ jobId, file, files, onOpenPath, keysGlobal, hostRef, onDownload }) {
  const [v, setV] = useState(null);
  const [error, setError] = useState("");
  const needsView = file.view === "docx" || file.view === "metadata";
  useEffect(() => {
    if (!needsView) return undefined;
    let alive = true;
    setV(null);
    setError("");
    submission.view(jobId, file.path).then((r) => alive && setV(r)).catch((e) => alive && setError(e.message));
    return () => { alive = false; };
  }, [jobId, file.path, needsView]);

  if (file.view === "image") return <ImageView jobId={jobId} file={file} keysGlobal={keysGlobal} hostRef={hostRef} />;
  if (file.view === "pdf") return <PdfView jobId={jobId} file={file} />;
  if (needsView) {
    if (error) return <div className="fv-msg error-box">{error}</div>;
    if (!v) return <div className="fv-msg"><div className="fv-spinner" aria-label="Loading" /></div>;
    if (v.view === "docx") {
      return (
        <div className="fv-doc">
          <DocView doc={v} items={[]} queries={[]} selId={null} scrollKey={0} anim={{}} reveal={false} wide={false}
            article={file.name} journal={KIND_LABEL[file.kind]} readOnly onSelect={() => {}} onDirectEdit={async () => false} onDecideQuery={() => {}} />
        </div>
      );
    }
    if (v.view === "metadata") return <div className="fv-scroll"><MetaCard card={v.card} files={files} onOpenPath={onOpenPath} /></div>;
  }
  const Icon = fileIcon(file);
  const why = ["pptx", "ppt"].includes(file.ext) ? "PowerPoint files can't be previewed here."
    : ["zip", "rar", "7z"].includes(file.ext) ? "Archives inside the submission are not opened here."
      : (v && v.error) || "This file type can't be previewed here.";
  return (
    <div className="fv-msg fv-dl">
      <span className={`fv-bigicon k-${file.kind}`}><Icon size={30} /></span>
      <b>{file.name}</b>
      <span className="muted small">{file.ext.toUpperCase()} · {fmtSize(file.size)}</span>
      <p className="muted">{why}</p>
      <button className="btn primary" onClick={onDownload}><Download size={15} /> Download</button>
    </div>
  );
}

/**
 * mode: "overlay" (sheet over the review screen), "beside" (pane next to the manuscript) or "full" (phones).
 */
export default function FileViewer({ jobId, file, files, mode, canSplit, onClose, onMode, onOpenPath, onError }) {
  const host = useRef(null);
  const Icon = fileIcon(file);
  const download = () => submission.download(jobId, file).catch((e) => onError && onError(e.message));
  const modal = mode !== "beside";

  // Overlay / full screen: Esc closes, and the review screen's keys (j/k/a/r/u) stay off while it is open.
  useEffect(() => {
    if (!modal) return undefined;
    const h = (e) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
      e.stopPropagation(); // capture on window: the review screen's own key handler never sees it
    };
    window.addEventListener("keydown", h, true);
    return () => window.removeEventListener("keydown", h, true);
  }, [modal, onClose]);
  useEffect(() => { if (modal && host.current) host.current.focus({ preventScroll: true }); }, [modal, file.path]);

  const head = (
    <div className="fv-head">
      {mode === "full" ? (
        <button className="btn ghost icon" onClick={onClose} aria-label="Back to the manuscript" title="Back"><ArrowLeft size={18} /></button>
      ) : null}
      <span className={`ficon k-${file.kind}`}><Icon size={16} /></span>
      <span className="fv-title">
        <b title={file.name}>{file.kind === "figure" || file.kind === "table" ? file.label : file.name}</b>
        <small>{KIND_LABEL[file.kind]}{file.kind === "figure" || file.kind === "table" ? ` · ${file.name}` : ""} · {fmtSize(file.size)}</small>
      </span>
      <span className="spacer" />
      {canSplit && mode === "overlay" && (
        <button className="btn sm" onClick={() => onMode("beside")} title="Open beside manuscript"><Columns2 size={14} /> <span>Beside manuscript</span></button>
      )}
      {mode === "beside" && (
        <button className="btn sm ghost icon" onClick={() => onMode("overlay")} title="Enlarge" aria-label="Enlarge"><Maximize2 size={15} /></button>
      )}
      <button className="btn sm ghost icon" onClick={download} title="Download original" aria-label="Download original"><Download size={15} /></button>
      {mode !== "full" && <button className="btn sm ghost icon" onClick={onClose} title="Close (Esc)" aria-label="Close"><X size={16} /></button>}
    </div>
  );
  const body = <Body jobId={jobId} file={file} files={files} onOpenPath={onOpenPath} keysGlobal={modal} hostRef={host} onDownload={download} />;

  if (mode === "beside") {
    return (
      <div className="fv fv-beside" ref={host} tabIndex={-1} aria-label={`${file.name} beside the manuscript`}>
        {head}
        <div className="fv-body">{body}</div>
      </div>
    );
  }
  return (
    <motion.div className={`fv-scrim ${mode}`} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <motion.div className={`fv fv-${mode} card`} ref={host} tabIndex={-1} role="dialog" aria-modal="true" aria-label={file.name}
        initial={mode === "full" ? { x: "100%" } : { opacity: 0, y: 18, scale: 0.97 }}
        animate={mode === "full" ? { x: 0 } : { opacity: 1, y: 0, scale: 1 }}
        exit={mode === "full" ? { x: "100%" } : { opacity: 0, y: 10, scale: 0.98 }}
        transition={{ type: "spring", stiffness: 380, damping: 34 }}>
        {head}
        <div className="fv-body">{body}</div>
      </motion.div>
    </motion.div>
  );
}
