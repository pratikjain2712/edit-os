import React, { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { BookOpen, ChevronDown, ExternalLink, FileText, Globe, Info, Mail, Sparkles, Wand2 } from "lucide-react";
import { show, whyLabel } from "./labels.js";

// "Why" for one suggestion: the rule (label + the stylesheet / guide line it implements, expandable) and
// a "Source" chip saying where its content came from (title page, XML field, PubMed record, checker, ...).
// `rule` is the /rules entry for item.rule_key (may be missing).

const SOURCE_ICONS = { pubmed: BookOpen, crossref: BookOpen, europepmc: BookOpen, openalex: BookOpen, semantic_scholar: BookOpen,
  doaj: BookOpen, checked_online: BookOpen, web: Globe, ai: Sparkles, cover_letter: Mail,
  checker: Wand2, journal_style: Wand2, renumber: Wand2 };

export function SourceChip({ p, small }) {
  if (!p || !p.label) return null;
  const Icon = SOURCE_ICONS[p.kind] || FileText;
  const tip = [p.file, p.field && p.kind !== "xml" ? p.field : "", p.detail].filter(Boolean).join(" · ");
  const body = <><Icon size={small ? 11 : 12} aria-hidden="true" /><span>{p.label}</span>{p.url && <ExternalLink size={11} aria-hidden="true" />}</>;
  const cls = `src-chip k-${p.kind}`;
  return p.url
    ? <a className={cls} href={p.url} target="_blank" rel="noopener noreferrer" title={tip || p.url} onClick={(e) => e.stopPropagation()}>{body}</a>
    : <span className={cls} title={tip || p.label}>{body}</span>;
}

function SecondCheck({ sc }) {
  if (!sc) return null;
  return <span className={`src-chip ${sc.verdict === "agreed" ? "k-ok" : "k-warn"}`} title={sc.why || ""}>
    <Sparkles size={11} aria-hidden="true" /><span>{sc.verdict === "agreed" ? "2nd check agreed" : "2nd check: review"}</span></span>;
}

const FIELDS = [["authors", "Authors"], ["title", "Title"], ["journal", "Journal"], ["year", "Year"], ["volume", "Volume"],
  ["issue", "Issue"], ["pages", "Pages"], ["doi", "DOI"], ["pmid", "PMID"]];

function RefCompare({ p }) {
  const rec = p.record || {};
  const fields = FIELDS.filter(([k]) => rec[k]);
  return (
    <div className="ref-cmp">
      <div><small>Author’s original</small><p>{p.original}</p></div>
      {p.styled && <div><small>Styled</small><p className="styled">{p.styled}</p></div>}
      {fields.length > 0 && (
        <div><small>{p.kind === "pubmed" ? "PubMed record" : p.kind === "crossref" ? "CrossRef record"
          : p.source_name ? `${p.source_name} record` : "Record"}{p.found_via ? ` (found via ${p.found_via})` : ""}</small>
          <dl>{fields.map(([k, label]) => <React.Fragment key={k}><dt>{label}</dt><dd>{rec[k]}</dd></React.Fragment>)}</dl>
        </div>
      )}
      {p.kind === "unverified" && p.detail && <div><small>Why not verified</small><p className="muted">{p.detail}</p></div>}
    </div>
  );
}

export default function Why({ item, rule, compact = false, hideLabel = false }) {
  const [open, setOpen] = useState(false);
  const [refOpen, setRefOpen] = useState(false);
  const p = item.provenance;
  const label = whyLabel(item, rule);
  const quote = rule && rule.quote;
  const desc = rule && rule.description;
  const isRef = p && (p.original || p.record);
  const stop = (f) => (e) => { e.stopPropagation(); f(); };
  return (
    <div className={`why ${compact ? "compact" : ""}`} onClick={(e) => e.stopPropagation()}>
      <div className="why-line">
        <Info size={13} className="why-ico" aria-hidden="true" />
        <span className="why-k">Why</span>
        {!hideLabel && <span className="why-rule">{label}</span>}
        {quote ? (
          <button className="why-peek" onClick={stop(() => setOpen(!open))} aria-expanded={open}
            title={`${rule.where}${rule.line ? `, line ${rule.line}` : ""}`}>
            <b>{rule.where === "Pre-editing guide" ? "Guide" : "Stylesheet"}:</b>
            {!open && <span className="why-snip">“{quote.replace(/^[^|]{1,60}\|\s*/, "")}”</span>}
            <ChevronDown size={12} className="why-chev" style={{ transform: open ? "rotate(180deg)" : "none" }} />
          </button>
        ) : hideLabel && !desc ? <span className="why-rule">{label}</span> : null}
        {!quote && desc && hideLabel && <span className="why-desc-inline">{desc}</span>}
      </div>
      <AnimatePresence initial={false}>
        {open && quote && (
          <motion.blockquote className="why-quote" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2, ease: [0.2, 0.8, 0.2, 1] }}>
            <span className="why-where">{rule.where}{rule.line ? ` · line ${rule.line}` : ""}</span>
            “{quote}”
          </motion.blockquote>
        )}
      </AnimatePresence>
      {!quote && desc && !compact && !hideLabel && <div className="why-desc">{desc}</div>}
      {p && (
        <div className="why-src">
          <span className="why-k">Source</span>
          <SourceChip p={p} />
          <SecondCheck sc={p && p.second_check} />
          {p && p.kind === "ai" && p.detail && <span className="why-note" title={p.detail}>“{p.detail}”</span>}
          {p && p.kind !== "ai" && p.kind !== "pubmed" && p.kind !== "crossref" && p.detail && !isRef && !compact &&
            <span className="why-note" title={p.detail}>“{show(p.detail)}”</span>}
          {isRef && (
            <button className="linkbtn" onClick={stop(() => setRefOpen(!refOpen))} aria-expanded={refOpen}>
              {refOpen ? "Hide comparison" : "Compare"}
            </button>
          )}
        </div>
      )}
      <AnimatePresence initial={false}>
        {refOpen && isRef && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }} style={{ overflow: "hidden" }}>
            <RefCompare p={p} />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
