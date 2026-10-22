import React, { memo, useEffect, useRef, useState } from "react";
import { Check, ListX, Pencil, RotateCcw, Sparkles, UserRound, X } from "lucide-react";
import { LAYERS, fmtLabel, formatOnly, show, whyLabel } from "./labels.js";
import Why from "./Why.jsx";

function Diff({ item, expanded }) {
  const { before, after, paragraph } = item.context;
  if (item.kind === "insert") {
    return (
      <div className="diff">
        <div className="muted small" style={{ fontFamily: "var(--font-ui)" }}>
          {item.position === "start" ? "New line at the start of the article" : `New paragraph ${item.where} “${show(paragraph).slice(0, 60)}…”`}
        </div>
        <ins>{show(item.new)}</ins>
      </div>
    );
  }
  const mid = formatOnly(item) ? <span className="fmtx">{show(item.old)}</span>
    : <>{item.old && <del>{show(item.old)}</del>}{item.new && <ins>{show(item.new)}</ins>}</>;
  if (expanded) return <div className="diff">{show(paragraph.slice(0, item.start))}{mid}{show(paragraph.slice(item.end))}</div>;
  return (
    <div className="diff">
      {before.length >= 60 ? "…" : ""}{show(before.slice(-60))}{mid}{show(after.slice(0, 60))}{after.length > 60 ? "…" : ""}
    </div>
  );
}

const Card = memo(function Card({ item, selected, onSelect, onDecide, onEdit, rule, likeCount, onRejectLike }) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.new);
  const ref = useRef(null);
  useEffect(() => { if (selected && ref.current) ref.current.scrollIntoView({ block: "nearest" }); }, [selected]);
  const stop = (f) => (e) => { e.stopPropagation(); f(); };
  return (
    <article ref={ref} className={`ccard ${item.status} ${selected ? "sel" : ""}`} onClick={() => onSelect(item.id)}>
      <div className="meta">
        <span className={`dot l-${item.layer}`} />
        <span className="muted">{LAYERS[item.layer] || item.layer}</span>
        <span className="rule" title={`${item.reason} · ${item.para_id}`}>{whyLabel(item, rule)}</span>
        {item.format && <span className="pill">{fmtLabel(item.format)}</span>}
        {item.source === "ai" && <span className="pill ai"><Sparkles size={11} /> AI</span>}
        {item.direct && <span className="pill"><UserRound size={11} /> You</span>}
        {item.edited && <span className="pill">reworded</span>}
        <span className="spacer" />
        <span className={`pill ${item.status}`}>{item.status === "suggested" ? "pending" : item.status}</span>
      </div>
      <Diff item={item} expanded={expanded} />
      {!item.direct && <Why item={item} rule={rule} compact hideLabel />}
      {editing && (
        <form className="pop-edit" onClick={(e) => e.stopPropagation()} onSubmit={(e) => { e.preventDefault(); onEdit(item.id, draft); setEditing(false); }}>
          <label className="small muted">{item.old ? <>Replace “{show(item.old)}” with</> : "Insert"}</label>
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={Math.min(6, 1 + Math.ceil(draft.length / 90))} autoFocus />
          <div className="pop-actions">
            <button className="btn ok solid sm" type="submit"><Check size={14} /> Save and accept</button>
            <button className="btn ghost sm" type="button" onClick={() => { setDraft(item.new); setEditing(false); }}>Cancel</button>
          </div>
        </form>
      )}
      <div className="actions">
        <button className="btn ok sm" onClick={stop(() => onDecide(item.id, "accepted"))} title="Accept (a)"><Check size={14} /> Accept</button>
        <button className="btn no sm" onClick={stop(() => onDecide(item.id, "rejected"))} title="Reject (r)"><X size={14} /> Reject</button>
        {item.status !== "suggested" && <button className="btn ghost sm" onClick={stop(() => onDecide(item.id, "suggested"))} title="Undo (u)"><RotateCcw size={13} /> Undo</button>}
        {!formatOnly(item) && !editing && !item.direct && <button className="btn sm" onClick={stop(() => { setDraft(item.new); setEditing(true); })}><Pencil size={13} /> Edit</button>}
        {item.kind !== "insert" && <button className="linkbtn" onClick={stop(() => setExpanded(!expanded))}>{expanded ? "less context" : "whole paragraph"}</button>}
        {item.status === "suggested" && likeCount > 1 && onRejectLike && !item.direct &&
          <button className="linkbtn like" onClick={stop(() => onRejectLike(item))} title="Reject every pending suggestion made by this rule"><ListX size={12} /> Reject all {likeCount} like this</button>}
      </div>
    </article>
  );
});

export default function ListView({ items, selId, onSelect, onDecide, onEdit, onlyPending, rules = {}, likeCounts = {}, onRejectLike }) {
  const [limit, setLimit] = useState(150);
  return (
    <div className="list-wrap">
      {items.length === 0 && <p className="muted">Nothing {onlyPending ? "pending " : ""}here.</p>}
      {items.slice(0, limit).map((item) => (
        <Card key={item.id} item={item} selected={item.id === selId} onSelect={onSelect} onDecide={onDecide} onEdit={onEdit}
          rule={rules[item.rule_key]} likeCount={likeCounts[item.rule_key] || 0} onRejectLike={onRejectLike} />
      ))}
      {items.length > limit && <button className="btn" onClick={() => setLimit(limit + 150)}>Show {Math.min(150, items.length - limit)} more</button>}
    </div>
  );
}
