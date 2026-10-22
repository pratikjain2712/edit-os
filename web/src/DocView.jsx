import React, { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Check, CornerDownLeft, MessageSquareText, RotateCcw, X } from "lucide-react";
import { PARA_BREAK, formatOnly, ruleLabel } from "./labels.js";

// --- text model (mirrors edit_os.docview.apply on the server) ----------------------------------

export function overlaps(a, b, c, d) {
  if (a === b && c === d) return false;
  if (a === b) return c < a && a < d;
  if (c === d) return a < c && c < b;
  return a < d && c < b;
}

/** Text changes shown inline: accepted first (they make the current text), then pending, then
 * rejected, skipping any that collide with one already placed. */
export function placeChanges(changes) {
  const rank = { accepted: 0, suggested: 1, rejected: 2 };
  const cands = changes.filter((c) => c.kind !== "insert" && !formatOnly(c));
  const byStart = (a, b) => a.start - b.start || a.end - b.end;
  const accepted = cands.filter((c) => c.status === "accepted").sort(byStart);
  const chosen = [];
  let lastEnd = -1;
  for (const c of accepted) { // same greedy rule as the server
    if (c.start < lastEnd) continue;
    chosen.push(c);
    lastEnd = c.end;
  }
  const rest = cands.filter((c) => c.status !== "accepted").sort((a, b) => rank[a.status] - rank[b.status] || byStart(a, b));
  for (const c of rest) if (!chosen.some((o) => overlaps(c.start, c.end, o.start, o.end))) chosen.push(c);
  return chosen.sort(byStart);
}

/** The paragraph's text with accepted changes applied: what the editor edits. */
export function currentText(text, changes) {
  let out = "";
  let pos = 0;
  for (const c of placeChanges(changes).filter((c) => c.status === "accepted")) {
    out += text.slice(pos, c.start) + c.new;
    pos = c.end;
  }
  return out + text.slice(pos);
}

const FMT_KEYS = ["bold", "italic", "superscript", "subscript", "small_caps"];

function runAt(runs) {
  const starts = [];
  let pos = 0;
  for (const r of runs) { starts.push(pos); pos += r.text.length; }
  return (i) => {
    let k = 0;
    for (let j = 0; j < starts.length; j++) if (starts[j] <= i) k = j;
    return runs[k] ? runs[k].format : {};
  };
}

function Styled({ fmt, children, ...rest }) {
  const cls = [fmt.bold && "b", fmt.italic && "i", fmt.small_caps && "sc"].filter(Boolean).join(" ");
  let node = <span className={cls || undefined} {...rest}>{children}</span>;
  if (fmt.superscript) node = <sup>{node}</sup>;
  else if (fmt.subscript) node = <sub>{node}</sub>;
  return node;
}

/** Render one paragraph's text with its suggestions as tracked changes. */
function renderText(p, changes, queries, anim, revealIdx) {
  const text = p.text;
  const fmtAt = runAt(p.runs);
  const placed = placeChanges(changes);
  const fmts = changes.filter((c) => c.kind !== "insert" && formatOnly(c));
  const qs = queries.filter((q) => q.end > q.start && q.status !== "rejected");
  const cuts = new Set([0, text.length]);
  let pos = 0;
  for (const r of p.runs) { cuts.add(pos); pos += r.text.length; }
  for (const c of [...placed, ...fmts]) { cuts.add(c.start); cuts.add(c.end); }
  for (const q of qs) { cuts.add(q.start); cuts.add(q.end); }
  const sorted = [...cuts].filter((x) => x >= 0 && x <= text.length).sort((a, b) => a - b);

  // Leaves of the source text between s and e, grouped under formatting-change wrappers.
  const source = (s, e, cur, key) => {
    const out = [];
    let group = null;
    const flush = () => {
      if (!group) return;
      const c = group.c;
      out.push(
        <span key={`f${c.id}-${group.s}`} className={`chg fmt ${c.status}`} data-cid={c.id} style={{ "--i": revealIdx(c.id) }}>{group.nodes}</span>);
      group = null;
    };
    for (let i = 0; i < sorted.length - 1; i++) {
      const a = Math.max(sorted[i], s);
      const b = Math.min(sorted[i + 1], e);
      if (b <= a) continue;
      let fmt = { ...fmtAt(a) };
      const fc = fmts.find((c) => c.start <= a && b <= c.end && c.status !== "rejected");
      if (fc && fc.status === "accepted") for (const k of FMT_KEYS) if (k in fc.format) fmt[k] = fc.format[k];
      const qk = qs.filter((q) => q.start <= a && b <= q.end).map((q) => q.key).join(" ");
      let leaf = <Styled key={`${key}-${a}`} fmt={fmt} data-c={cur == null ? undefined : cur + (a - s)}>{text.slice(a, b)}</Styled>;
      if (qk) leaf = <span key={`${key}-q${a}`} className="qa" data-q={qk}>{leaf}</span>;
      const wrap = fmts.find((c) => c.start <= a && b <= c.end);
      if (wrap) {
        if (group && group.c !== wrap) flush();
        if (!group) group = { c: wrap, s: a, nodes: [] };
        group.nodes.push(leaf);
      } else {
        flush();
        out.push(leaf);
      }
    }
    flush();
    return out;
  };

  const inserted = (c, base) => {
    const fmt = { ...fmtAt(Math.max(0, c.start - (c.start === c.end ? 1 : 0))), ...(c.format || {}) };
    const parts = c.new.split(PARA_BREAK);
    return parts.map((t, i) => (
      <React.Fragment key={`n${c.id}${i}`}>
        {i > 0 && <><span className="pb">¶</span><br /></>}
        {t && <Styled fmt={fmt} data-c={base == null ? undefined : base + parts.slice(0, i).join(PARA_BREAK).length + (i ? 1 : 0)}>{t}</Styled>}
      </React.Fragment>
    ));
  };

  const nodes = [];
  let at = 0;
  let cur = 0;
  for (const c of placed) {
    if (c.start > at) { nodes.push(...source(at, c.start, cur, `s${at}`)); cur += c.start - at; }
    const a = anim[c.id];
    const common = { key: c.id, "data-cid": c.id, style: { "--i": revealIdx(c.id) } };
    if (a === "accepting") {
      nodes.push(<span {...common} className="chg suggested">
        {c.old && <del className="collapse">{source(c.start, c.end, null, `o${c.id}`)}</del>}
        {c.new && <ins>{inserted(c, null)}</ins>}
      </span>);
      cur += c.new.length;
    } else if (a === "rejecting") {
      nodes.push(<span {...common} className="chg suggested">
        {source(c.start, c.end, null, `o${c.id}`)}
        {c.new && <ins className="collapse">{inserted(c, null)}</ins>}
      </span>);
      cur += c.end - c.start;
    } else if (c.status === "accepted") {
      nodes.push(<span {...common} className={`chg accepted ${c.direct ? "direct" : ""} ${a === "settle" ? "settle" : ""}`}
        title={c.direct ? "Your edit" : `Accepted: ${ruleLabel(c.reason)}`}>{inserted(c, cur)}{!c.new && <span data-c={cur} />}</span>);
      cur += c.new.length;
    } else if (c.status === "rejected") {
      nodes.push(<span {...common} className={`chg rejected ${a === "settle" ? "reverted" : ""}`} title={`Rejected: ${ruleLabel(c.reason)}`}>
        {source(c.start, c.end, cur, `o${c.id}`)}{c.start === c.end && <span data-c={cur} />}</span>);
      cur += c.end - c.start;
    } else {
      nodes.push(<span {...common} className="chg suggested" title={ruleLabel(c.reason)}>
        {c.old && <del>{source(c.start, c.end, cur, `o${c.id}`)}</del>}
        {c.new && <ins>{inserted(c, null)}</ins>}
      </span>);
      cur += c.end - c.start;
    }
    at = c.end;
  }
  if (at < text.length) nodes.push(...source(at, text.length, cur, `s${at}`));
  return nodes;
}

// --- one paragraph ---------------------------------------------------------------------------

function caretOffset(e, root, fallback) {
  let node = null;
  let off = 0;
  if (document.caretRangeFromPoint) {
    const r = document.caretRangeFromPoint(e.clientX, e.clientY);
    if (r) { node = r.startContainer; off = r.startOffset; }
  } else if (document.caretPositionFromPoint) {
    const r = document.caretPositionFromPoint(e.clientX, e.clientY);
    if (r) { node = r.offsetNode; off = r.offset; }
  }
  const el = node && (node.nodeType === 3 ? node.parentElement : node);
  const host = el && el.closest("[data-c]");
  if (!host || !root.contains(host)) return fallback;
  return Number(host.getAttribute("data-c")) + (node.nodeType === 3 ? off : 0);
}

function ParaEditor({ p, base, caret, onSave, onCancel }) {
  const [val, setVal] = useState(base);
  const [state, setState] = useState("editing"); // editing | saving | failed
  const ref = useRef(null);
  const done = useRef(false);
  const size = () => { const t = ref.current; if (t) { t.style.height = "auto"; t.style.height = `${t.scrollHeight}px`; } };
  useLayoutEffect(() => {
    size();
    const t = ref.current;
    if (t) { t.focus({ preventScroll: true }); const c = Math.min(caret ?? base.length, base.length); t.setSelectionRange(c, c); }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const save = async () => {
    if (done.current || state === "saving") return;
    if (val === base) { done.current = true; onCancel(); return; }
    setState("saving");
    const ok = await onSave(val);
    if (ok) done.current = true;
    else setState("failed");
  };
  return (
    <div className="editing" onClick={(e) => e.stopPropagation()}>
      <textarea ref={ref} className="para-edit" value={val} spellCheck
        aria-label={`Edit paragraph ${p.id}`}
        onChange={(e) => { setVal(e.target.value.replace(/[\r\n]+/g, (m) => (base.includes("\n") ? m : " "))); setState("editing"); size(); }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save(); }
          else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done.current = true; onCancel(); }
        }}
        onBlur={() => { if (state !== "failed") save(); }} />
      <div className="edit-bar">
        {state === "saving" ? <><span className="saving-dot" /> Saving…</> : state === "failed" ? <span style={{ color: "var(--no)" }}>Not saved — fix or press Esc</span> : <span>Your correction · pending suggestions hidden while you type</span>}
        <button className="btn sm primary" onMouseDown={(e) => e.preventDefault()} onClick={save} disabled={state === "saving"}><CornerDownLeft size={13} /> Save</button>
        <button className="btn sm ghost" onMouseDown={(e) => e.preventDefault()} onClick={() => { done.current = true; onCancel(); }}>Cancel</button>
      </div>
    </div>
  );
}

const sameArr = (a, b) => a === b || (a.length === b.length && a.every((x, i) => x === b[i]));

const Para = memo(function Para({ p, changes, queries, sel, anim, revealIdx, editing, onEditStart, onEditEnd, onDirectEdit, readOnly, Tag = "div" }) {
  const ref = useRef(null);
  const cls = ["blk", p.kind, p.kind === "heading" ? `h${Math.min(p.level ?? 1, 6)}` : "", !readOnly && !["empty", "image"].includes(p.kind) ? "editable" : ""].join(" ");
  useEffect(() => {
    if (!ref.current) return;
    ref.current.querySelectorAll(".chg.sel").forEach((n) => n.classList.remove("sel"));
    if (sel) ref.current.querySelectorAll(`[data-cid="${sel}"]`).forEach((n) => n.classList.add("sel"));
  });
  const base = useMemo(() => currentText(p.text, changes), [p.text, changes]);
  if (editing) {
    return (
      <Tag ref={ref} className={cls} data-pid={p.id}>
        <ParaEditor p={p} base={base} caret={editing.caret}
          onCancel={onEditEnd}
          onSave={async (text) => { const ok = await onDirectEdit(p.id, base, text); if (ok) onEditEnd(); return ok; }} />
      </Tag>
    );
  }
  return (
    <Tag ref={ref} className={cls} data-pid={p.id}
      onClick={(e) => {
        if (readOnly || e.target.closest("[data-cid]") || p.kind === "empty" || p.kind === "image") return;
        if (window.getSelection && String(window.getSelection()).length) return; // selecting text to copy
        onEditStart(p.id, caretOffset(e, ref.current, base.length));
      }}>
      {p.image && <span className="img-ph" aria-label="image" />}
      {renderText(p, changes, queries, anim, revealIdx)}
    </Tag>
  );
}, (a, b) => a.p === b.p && sameArr(a.changes, b.changes) && sameArr(a.queries, b.queries) && a.sel === b.sel &&
  a.animKey === b.animKey && a.editing === b.editing && a.revealIdx === b.revealIdx);

// --- front-matter insertions -----------------------------------------------------------------

function Insert({ item, revealIdx }) {
  const runs = item.runs || [[item.new, {}]];
  if (item.status === "rejected") {
    return <div><span className="fm rejected" data-cid={item.id}><X size={12} /> {ruleLabel(item.reason)} line left out</span></div>;
  }
  return (
    <div className={`fm ${item.status}`} data-cid={item.id} style={{ "--i": revealIdx(item.id) }} title={ruleLabel(item.reason)}>
      {item.status === "suggested" && <span className="fm-tag">{ruleLabel(item.reason).replace("Front matter · ", "")}</span>}
      {runs.map(([t, f], i) => <Styled key={i} fmt={f || {}}>{t.split(PARA_BREAK).join("\n")}</Styled>)}
    </div>
  );
}

// --- author queries in the margin --------------------------------------------------------------

function QueryNote({ q, onDecide, hot }) {
  const enter = () => document.querySelectorAll(`[data-q~="${q.key}"]`).forEach((n) => n.classList.add("hot"));
  const leave = () => document.querySelectorAll(`[data-q~="${q.key}"]`).forEach((n) => n.classList.remove("hot"));
  return (
    <div className={`note ${q.status} ${hot ? "hot" : ""}`} onMouseEnter={enter} onMouseLeave={leave} data-note={q.key}>
      <div className="note-head"><MessageSquareText size={13} color="var(--warn)" /><b>{q.id || "AQ"}</b><span>{ruleLabel((q.check || "").split(".")[0])}</span></div>
      {q.status !== "rejected" && <p>{q.text}</p>}
      <div className="pop-actions">
        {q.status === "rejected" ? (
          <button className="btn sm ghost" onClick={() => onDecide(q.key, "suggested")}><RotateCcw size={13} /> Restore query</button>
        ) : (
          <>
            <button className={`btn sm ${q.status === "accepted" ? "ok solid" : "ok"}`} onClick={() => onDecide(q.key, q.status === "accepted" ? "suggested" : "accepted")}>
              <Check size={13} /> {q.status === "accepted" ? "Kept" : "Keep"}</button>
            <button className="btn sm no" onClick={() => onDecide(q.key, "rejected")}><X size={13} /> Drop</button>
          </>
        )}
      </div>
    </div>
  );
}

function Margin({ queries, onDecide, gridRef, layoutKey }) {
  const [pos, setPos] = useState({});
  const ref = useRef(null);
  const layout = useCallback(() => {
    const grid = gridRef.current;
    const box = ref.current;
    if (!grid || !box) return;
    const top0 = box.getBoundingClientRect().top;
    const want = [];
    for (const q of queries) {
      const anchor = grid.querySelector(`[data-q~="${q.key}"]`) || grid.querySelector(`[data-pid="${q.para_id}"]`);
      const note = box.querySelector(`[data-note="${q.key}"]`);
      if (!anchor || !note) continue;
      want.push({ key: q.key, y: anchor.getBoundingClientRect().top - top0 - 6, h: note.offsetHeight });
    }
    want.sort((a, b) => a.y - b.y);
    const out = {};
    let floor = 0;
    for (const w of want) {
      const y = Math.max(w.y, floor);
      out[w.key] = y;
      floor = y + w.h + 10;
    }
    setPos(out);
  }, [queries, gridRef]);
  useLayoutEffect(() => { layout(); }, [layout, layoutKey]);
  useEffect(() => {
    const ro = new ResizeObserver(() => layout());
    if (gridRef.current) ro.observe(gridRef.current);
    return () => ro.disconnect();
  }, [layout, gridRef]);
  return (
    <aside className="margin" ref={ref} aria-label="Author queries">
      {queries.map((q) => (
        <div key={q.key} style={{ position: "absolute", left: 0, right: 0, top: 0, transform: `translateY(${pos[q.key] ?? 0}px)`, opacity: pos[q.key] == null ? 0 : 1, transition: "transform .35s var(--ease), opacity .3s" }}>
          <QueryNote q={q} onDecide={onDecide} />
        </div>
      ))}
    </aside>
  );
}

// --- the page --------------------------------------------------------------------------------

export default function DocView({ doc, items, queries, selId, scrollKey, anim, reveal, wide, article, journal,
  onSelect, onDirectEdit, onDecideQuery, readOnly = false }) {
  // readOnly: a page with no suggestions and no editing (submission files such as the title page)
  const gridRef = useRef(null);
  const [editing, setEditing] = useState(null); // {pid, caret}

  const byPara = useMemo(() => {
    const m = {};
    for (const it of items) {
      if (it.kind === "insert") continue;
      (m[it.para_id] = m[it.para_id] || []).push(it);
    }
    return m;
  }, [items]);
  const inserts = useMemo(() => {
    const m = { start: [] };
    for (const it of items) {
      if (it.kind !== "insert") continue;
      if (it.position === "start") m.start.push(it);
      else (m[`${it.where}:${it.para_id}`] = m[`${it.where}:${it.para_id}`] || []).push(it);
    }
    return m;
  }, [items]);
  const qByPara = useMemo(() => {
    const m = {};
    for (const q of queries) (m[q.para_id] = m[q.para_id] || []).push(q);
    return m;
  }, [queries]);
  const reveals = useMemo(() => {
    const m = {};
    let n = 0;
    for (const it of items) if (it.status === "suggested") m[it.id] = Math.min(n++, 45);
    return m;
  }, [items]);
  const revealIdx = useCallback((id) => reveals[id] ?? 0, [reveals]);
  const sel = useMemo(() => items.find((i) => i.id === selId), [items, selId]);
  const statusKey = useMemo(() => items.map((i) => i.status[0] + (i.new || "").length).join(""), [items]);
  const selPid = sel ? sel.para_id : null;

  // Scroll the selected suggestion into view (keyboard / sidebar navigation).
  useEffect(() => {
    if (!scrollKey || !selId || !gridRef.current) return;
    const el = gridRef.current.querySelector(`[data-cid="${selId}"]`);
    if (el) {
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      el.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
    }
  }, [scrollKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const startEdit = useCallback((pid, caret) => setEditing({ pid, caret }), []);
  const endEdit = useCallback(() => setEditing(null), []);
  const onClick = (e) => {
    const t = e.target.closest("[data-cid]");
    if (t) { e.stopPropagation(); onSelect(t.getAttribute("data-cid")); }
  };

  const EMPTY = useRef([]).current;
  const para = (p, Tag) => {
    const ch = byPara[p.id] || EMPTY;
    const animKey = ch.map((c) => anim[c.id] || "").join("|");
    return (
      <Para key={p.id} p={p} Tag={Tag} changes={ch} queries={qByPara[p.id] || EMPTY}
        sel={selPid === p.id ? selId : null} anim={anim} animKey={animKey} revealIdx={revealIdx}
        editing={editing && editing.pid === p.id ? editing : null}
        onEditStart={startEdit} onEditEnd={endEdit} onDirectEdit={onDirectEdit} readOnly={readOnly} />
    );
  };

  // Blocks: paragraphs, with the cells of a table gathered into one table.
  const blocks = useMemo(() => {
    const out = [];
    for (const p of doc.paragraphs) {
      if (p.kind === "table" && p.cell) {
        const last = out[out.length - 1];
        const t = last && last.table === p.cell.table ? last : null;
        const tb = t || { table: p.cell.table, rows: [] };
        if (!t) out.push(tb);
        const row = (tb.rows[p.cell.row] = tb.rows[p.cell.row] || []);
        (row[p.cell.col] = row[p.cell.col] || []).push(p);
      } else out.push(p);
    }
    return out;
  }, [doc]);

  const inlineNotes = (pid) => (!wide && qByPara[pid] ? (
    <div className="note-inline">{qByPara[pid].map((q) => <QueryNote key={q.key} q={q} onDecide={onDecideQuery} />)}</div>
  ) : null);

  return (
    <div className={`doc-wrap ${wide ? "with-margin" : ""}`}>
      <div className={`doc-grid ${wide ? "with-margin" : ""}`} ref={gridRef}>
        <article className={`paper ${reveal ? "reveal" : ""}`} onClickCapture={onClick} style={{ whiteSpace: "pre-wrap" }}>
          <div className="page-label"><span>{article}</span><span>{journal}</span></div>
          {inserts.start.map((it) => <Insert key={it.id} item={it} revealIdx={revealIdx} />)}
          {blocks.map((b) => {
            if (b.table) {
              return (
                <div className="tbl-wrap" key={b.table}>
                  <table className="tbl"><tbody>
                    {b.rows.map((row, r) => (
                      <tr key={r}>{(row || []).map((cell, c) => <td key={c}>{(cell || []).map((p) => para(p, "div"))}</td>)}</tr>
                    ))}
                  </tbody></table>
                </div>
              );
            }
            return (
              <React.Fragment key={b.id}>
                {(inserts[`before:${b.id}`] || []).map((it) => <Insert key={it.id} item={it} revealIdx={revealIdx} />)}
                {para(b, "div")}
                {inlineNotes(b.id)}
                {(inserts[`after:${b.id}`] || []).map((it) => <Insert key={it.id} item={it} revealIdx={revealIdx} />)}
              </React.Fragment>
            );
          })}
        </article>
        {wide && <Margin queries={queries} onDecide={onDecideQuery} gridRef={gridRef} layoutKey={`${statusKey}:${editing ? editing.pid : ""}:${Object.keys(anim).length}`} />}
      </div>
    </div>
  );
}
