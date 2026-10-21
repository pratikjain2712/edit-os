import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { Check, ChevronDown, ChevronUp, Pencil, RotateCcw, Sparkles, UserRound, X } from "lucide-react";
import { LAYERS, fmtLabel, formatOnly, ruleLabel, show } from "./labels.js";

const W = 340;

export default function Popover({ item, onDecide, onEdit, onClose, onNav }) {
  const ref = useRef(null);
  const [pos, setPos] = useState(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.new);

  useEffect(() => { setEditing(false); setDraft(item.new); }, [item.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const place = useCallback(() => {
    const el = document.querySelector(`.paper [data-cid="${item.id}"]`);
    const me = ref.current;
    const h = me ? me.offsetHeight : 180;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    if (!el) { setPos({ left: Math.max(12, (vw - W) / 2), top: Math.max(80, vh - h - 24), arrow: null }); return; }
    const r = el.getClientRects()[0] || el.getBoundingClientRect();
    let top = r.bottom + 10;
    let below = true;
    if (top + h > vh - 12 && r.top - h - 10 > 70) { top = r.top - h - 10; below = false; }
    const left = Math.min(Math.max(12, r.left + r.width / 2 - W / 2), vw - W - 12);
    setPos({ left, top, below, hidden: r.bottom < 56 || r.top > vh });
  }, [item.id]);

  useLayoutEffect(() => { place(); }, [place, editing, item.status, item.new]);
  useEffect(() => {
    let raf = 0;
    const on = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(place); };
    window.addEventListener("scroll", on, true);
    window.addEventListener("resize", on);
    const t = setTimeout(place, 420); // after a smooth scroll settles
    return () => { window.removeEventListener("scroll", on, true); window.removeEventListener("resize", on); cancelAnimationFrame(raf); clearTimeout(t); };
  }, [place]);

  const fmtOnly = formatOnly(item);
  const decided = item.status !== "suggested";
  return (
    <motion.div ref={ref} className="pop" role="dialog" aria-label="Suggestion"
      style={{ left: pos ? pos.left : -9999, top: pos ? pos.top : -9999, visibility: pos && pos.hidden ? "hidden" : "visible" }}
      initial={{ opacity: 0, scale: 0.96, y: 4 }} animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: 0.97 }}
      transition={{ type: "spring", stiffness: 520, damping: 34 }}
      onMouseDown={(e) => e.stopPropagation()}>
      <div className="pop-head">
        <span className={`dot l-${item.layer}`} />
        <span className="rule" title={`${item.reason} · ${item.para_id}`}>{ruleLabel(item.reason)}</span>
        {item.source === "ai" && <span className="pill ai"><Sparkles size={12} /> AI</span>}
        {item.direct && <span className="pill"><UserRound size={12} /> You</span>}
        {item.edited && <span className="pill">reworded</span>}
        <span className="spacer" />
        <button className="btn ghost sm icon" onClick={() => onNav(-1)} title="Previous (k)" aria-label="Previous"><ChevronUp size={15} /></button>
        <button className="btn ghost sm icon" onClick={() => onNav(1)} title="Next (j)" aria-label="Next"><ChevronDown size={15} /></button>
        <button className="btn ghost sm icon" onClick={onClose} title="Close (Esc)" aria-label="Close"><X size={15} /></button>
      </div>
      <div className="layer muted small" style={{ marginTop: 2 }}>{LAYERS[item.layer] || item.layer}
        {" · "}<span className={`status-chip ${item.status}`}>{item.status === "suggested" ? "pending" : item.status}</span></div>
      {!editing && (
        <div className="pop-diff">
          {item.kind === "insert" ? <ins>{show(item.new)}</ins>
            : fmtOnly ? <><span style={{ boxShadow: "inset 0 -2px 0 var(--fmt-line)" }}>{show(item.old)}</span><span className="arrow">→</span><span className="small muted" style={{ fontFamily: "var(--font-ui)" }}>{fmtLabel(item.format)}</span></>
              : <>{item.old && <del>{show(item.old)}</del>}{item.old && item.new && <span className="arrow">→</span>}{item.new && <ins>{show(item.new)}</ins>}
                {!item.new && <span className="small muted" style={{ fontFamily: "var(--font-ui)", marginLeft: 6 }}>delete</span>}</>}
        </div>
      )}
      {editing ? (
        <form className="pop-edit" onSubmit={(e) => { e.preventDefault(); onEdit(draft); setEditing(false); }}>
          <label className="small muted">{item.old ? <>Replace “{show(item.old).slice(0, 80)}” with</> : "Insert"}</label>
          <textarea value={draft} autoFocus rows={Math.min(6, 1 + Math.ceil(draft.length / 38))}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onEdit(draft); setEditing(false); }
              if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setEditing(false); }
            }} />
          <div className="pop-actions">
            <button className="btn sm ok solid" type="submit"><Check size={14} /> Save &amp; accept</button>
            <button className="btn sm ghost" type="button" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </form>
      ) : (
        <div className="pop-actions">
          {item.direct ? (
            item.status === "accepted"
              ? <button className="btn sm no" onClick={() => onDecide("rejected")}><RotateCcw size={14} /> Revert my edit</button>
              : <button className="btn sm ok" onClick={() => onDecide("accepted")}><Check size={14} /> Restore my edit</button>
          ) : (
            <>
              <button className={`btn sm ok ${item.status === "accepted" ? "solid" : ""}`} onClick={() => onDecide("accepted")} title="Accept (a)"><Check size={14} /> Accept</button>
              <button className={`btn sm no ${item.status === "rejected" ? "solid" : ""}`} onClick={() => onDecide("rejected")} title="Reject (r)"><X size={14} /> Reject</button>
              {!fmtOnly && <button className="btn sm" onClick={() => { setDraft(item.new); setEditing(true); }} title="Change the wording"><Pencil size={13} /> Edit</button>}
              {decided && <button className="btn sm ghost" onClick={() => onDecide("suggested")} title="Undo (u)"><RotateCcw size={13} /> Undo</button>}
            </>
          )}
        </div>
      )}
      <div className="pop-foot"><span><kbd>a</kbd> accept</span><span><kbd>r</kbd> reject</span><span><kbd>j</kbd>/<kbd>k</kbd> move</span><span><kbd>Esc</kbd></span></div>
    </motion.div>
  );
}
