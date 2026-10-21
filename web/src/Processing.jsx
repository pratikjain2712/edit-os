import React, { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { BookOpenText, Check, FileCheck2, Library, Loader2, PenLine, Ruler } from "lucide-react";
import { STAGES, stageIndex } from "./labels.js";
import { Sparkle, Ticker } from "./ui.jsx";

const ICONS = [BookOpenText, Library, Ruler, PenLine, FileCheck2];
// What the floating chips say during each stage: kinds of checks, never invented edits.
const CHIPS = [
  [["Paragraphs", "q"], ["Tables", "q"], ["References", "q"]],
  [["PubMed", "ins"], ["CrossRef", "ins"], ["DOI match", "ins"], ["Journal abbrev.", "ins"]],
  [["Front matter", "ins"], ["Headings", "ins"], ["Citations", "ins"], ["House style", "del"]],
  [["Spelling", "del"], ["Grammar", "ins"], ["Abbreviations", "q"], ["Statistics", "ins"]],
  [["Tracked .docx", "ins"], ["Query sheet", "q"], ["QC report", "ins"]],
];

// A deterministic page skeleton: title, front matter, sections of body lines.
function useSkeleton() {
  return useMemo(() => {
    const lines = [{ k: "t" }];
    let seed = 7;
    const rnd = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);
    for (let i = 0; i < 3; i++) lines.push({ k: "", w: 30 + rnd() * 30, c: true });
    for (let s = 0; s < 5; s++) {
      lines.push({ k: "h" });
      const n = 4 + Math.floor(rnd() * 5);
      for (let i = 0; i < n; i++) lines.push({ k: "", w: i === n - 1 ? 35 + rnd() * 40 : 88 + rnd() * 12 });
    }
    return lines;
  }, []);
}

export default function Processing({ job }) {
  const reduce = useReducedMotion();
  const lines = useSkeleton();
  const { at, also } = stageIndex(job.stage, job.status);
  const live = (job.summary && job.summary.live) || (job.summary && job.summary.changes
    ? { changes: job.summary.changes.total, queries: job.summary.queries.total, inserts: job.summary.front_matter_inserts.total } : null);
  const counts = live || { changes: 0, queries: 0, inserts: 0 };
  const [t0] = useState(() => Date.now());
  const [now, setNow] = useState(Date.now());
  const [chips, setChips] = useState([]);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, []);

  // Floating chips: a new one every ~0.9s, from the current stage's list.
  useEffect(() => {
    if (reduce || job.status === "done") return undefined;
    let n = 0;
    const t = setInterval(() => {
      const pool = CHIPS[Math.min(at, CHIPS.length - 1)];
      const [text, kind] = pool[n % pool.length];
      n += 1;
      const id = `${Date.now()}-${n}`;
      setChips((cs) => [...cs.slice(-5), { id, text, kind, x: 8 + ((n * 37) % 70), y: 12 + ((n * 53) % 72) }]);
      setTimeout(() => setChips((cs) => cs.filter((c) => c.id !== id)), 2700);
    }, 900);
    return () => clearInterval(t);
  }, [at, reduce, job.status]);

  // Lines that light up as proposals come in (one per few suggestions), coloured like edits.
  const lit = Math.min(Math.round(lines.length * 0.4), Math.round(Math.sqrt(counts.changes + counts.queries) * 1.4) + at);
  const litSet = useMemo(() => {
    const s = new Map();
    for (let i = 0; i < lit; i++) {
      const idx = (i * 7 + 3) % lines.length;
      s.set(idx, i % 5 === 0 ? "no" : i % 3 === 0 ? "" : "ok");
    }
    return s;
  }, [lit, lines.length]);
  const sparks = useMemo(() => Array.from({ length: 9 }, (_, i) => ({ x: (i * 41 + 11) % 92, y: (i * 29 + 7) % 90, d: (i * 0.37) % 2.4 })), []);
  const secs = Math.max(0, Math.round((now - t0) / 1000));

  return (
    <div className="proc">
      <div className="skel" aria-hidden="true">
        {lines.map((l, i) => (
          <div key={i} className={`ln ${l.k} ${litSet.has(i) ? `lit ${litSet.get(i)}` : ""}`}
            style={{ width: l.k ? undefined : `${l.w}%`, margin: l.c ? "0 auto 12px" : undefined, "--d": `${(i % 9) * 0.12}s` }} />
        ))}
        {job.status !== "done" && <div className="scan" />}
        {sparks.map((s, i) => (
          <span key={i} className="spark" style={{ left: `${s.x}%`, top: `${s.y}%`, "--d": `${s.d}s` }}><Sparkle size={i % 3 ? 9 : 13} /></span>
        ))}
        <div className="floaters">
          {chips.map((c) => <span key={c.id} className={`floater ${c.kind}`} style={{ left: `${c.x}%`, top: `${c.y}%` }}>{c.text}</span>)}
        </div>
      </div>

      <div className="proc-side">
        <div className="proc-title">
          <h2>{job.status === "queued" ? "Waiting for a free editor…" : job.status === "done" ? "All set — opening the document" : "Editing your manuscript"}</h2>
          <p>{job.input_name} · {job.journal.toUpperCase()} <span className="elapsed">· {Math.floor(secs / 60)}:{String(secs % 60).padStart(2, "0")}</span></p>
        </div>
        <div className="card stages">
          {STAGES.map((s, i) => {
            const state = i < at && !also.includes(i) ? "done" : i === at || also.includes(i) ? "now" : "";
            const Icon = ICONS[i];
            return (
              <motion.div key={s.key} className={`stage-row ${state}`} layout initial={{ opacity: 0, x: 10 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: i * 0.06 }}>
                <span className="ic">
                  <AnimatePresence mode="wait" initial={false}>
                    <motion.span key={state} initial={{ scale: 0.4, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0.4, opacity: 0 }} style={{ display: "grid" }}>
                      {state === "done" ? <Check size={16} /> : state === "now" ? <Loader2 size={16} className="spin" /> : <Icon size={16} />}
                    </motion.span>
                  </AnimatePresence>
                </span>
                <span><b>{s.label}</b><small>{s.hint}</small></span>
              </motion.div>
            );
          })}
        </div>
        <div className="counters">
          <div className="counter"><b><Ticker value={counts.changes} /></b><span>suggestions</span></div>
          <div className="counter"><b><Ticker value={counts.queries} /></b><span>author queries</span></div>
          <div className="counter"><b><Ticker value={counts.inserts} /></b><span>front matter</span></div>
        </div>
      </div>
    </div>
  );
}
