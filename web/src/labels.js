// Plain-language names for layers and rules (the raw rule id stays in tooltips).

export const LAYERS = {
  references: "References",
  structure: "Structure",
  language: "Language",
  house_style: "House style",
  tables_figures: "Tables & figures",
  numbers: "Numbers",
  query: "Queries",
};

export const RULE_LABELS = {
  ref_lookup: "Reference checked in PubMed / CrossRef",
  ref_other: "Reference format (book, website, report)",
  ref_journal: "Journal abbreviation (NLM)",
  ref_initials: "Author initials", ref_et_al: "et al. in references", ref_pages: "Page range",
  ref_year: "Year and volume", ref_issue: "Issue number", ref_title_case: "Reference title case",
  citations: "Citation style", renumber: "Reference renumbering",
  fig_citations: "Figure / table citation", fig_captions: "Figure / table caption",
  tables: "Table layout", table_cell_blanks: "Empty lines in table cells", tables_move: "Tables at the end",
  heading_tags: "Heading tag", sections_headings: "Heading", sections_abstract: "Abstract",
  sections_keywords: "Keywords", sections_back_matter: "Back matter",
  statistics: "Statistics spacing", guide_replace: "Style guide fix",
  spelling: "Spelling (British / American)", langtool: "Grammar and typography", abbrev: "Abbreviation",
  editor_edit: "Your edit",
};

const FRONT = {
  running_head: "Running head", article_type: "Article type", title: "Title", authors: "Author names",
  affiliations: "Affiliations", affiliation: "Affiliations", correspondence: "Correspondence",
  dates: "History dates", cite: "How to cite", label: "Label",
};

export const PARA_BREAK = String.fromCharCode(0x2029);

export const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, " ");

export function ruleLabel(reason) {
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

// The rule name shown for a suggestion: the /rules label, except where the reason itself says more
// (Claude's kind and note, a LanguageTool rule id).
export function whyLabel(item, rule) {
  const r = item.reason || "";
  if (/^(claude\.|front_matter: )/.test(r) || !rule || !rule.label) return ruleLabel(r);
  return rule.label;
}

const FMT_NAMES = { italic: "italic", bold: "bold", superscript: "superscript", subscript: "subscript", small_caps: "small caps" };
// A value property reads as its value: size 12 -> "12 pt", font -> its name, align left -> "align left".
const fmtValue = (k, v) => (k === "size" && typeof v === "number" ? `${v} pt` : k === "font" && typeof v === "string" ? v
  : k === "align" ? `align ${v}` : k === "line_spacing" ? (v === 2 ? "double spacing" : `line spacing ${v}`) : FMT_NAMES[k] || k);
export const fmtLabel = (f) => Object.entries(f || {}).filter(([, v]) => v).map(([k, v]) => fmtValue(k, v)).join(", ") ||
  Object.keys(f || {}).map((k) => `no ${FMT_NAMES[k] || k}`).join(", ");

export const show = (s) => (s || "").split(PARA_BREAK).join("¶ ").replace(/\t/g, "⇥");
export const formatOnly = (c) => !!c.format && c.old === c.new && c.end > c.start;

// Map pipeline progress messages to the stages shown while a job runs.
export const STAGES = [
  { key: "read", label: "Reading manuscript", hint: "Paragraphs, runs, tables and the reference list" },
  { key: "refs", label: "Checking references in PubMed", hint: "Every entry looked up in PubMed / CrossRef" },
  { key: "style", label: "Applying journal style", hint: "Front matter, headings, citations, house style" },
  { key: "copy", label: "Copy editing", hint: "Spelling, grammar, abbreviations, statistics" },
  { key: "write", label: "Writing tracked file", hint: "Tracked .docx, clean copy, queries, QC" },
];

// For each pipeline stage message: [index of the stage in progress, stages also in progress].
export function stageIndex(stage, status) {
  if (status === "done") return { at: STAGES.length, also: [] };
  switch (stage) {
    case "style rules and front matter": return { at: 1, also: [2] };
    case "author queries": return { at: 2, also: [] };
    case "copy-edit checkers": return { at: 3, also: [] };
    case "rendering outputs": return { at: 4, also: [] };
    default: return { at: 0, also: [] };
  }
}
