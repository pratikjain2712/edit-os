import React from "react";
import { motion } from "framer-motion";
import { Braces, Columns2, File, FileArchive, FileCode2, FileImage, FileSignature, FileSpreadsheet, FileText, IdCard, Mail, Presentation, QrCode, ScrollText, Table2 } from "lucide-react";

// The submission's own files (GET /api/jobs/{id}/submission), listed in the side panel's Files tab.

export const KIND_LABEL = {
  manuscript: "Manuscript", title_page: "Title page", figure: "Figure", table: "Table", xml: "XML",
  metadata: "Metadata", form: "Form", cover_letter: "Cover letter", other: "Other",
};
const GROUPS = [
  ["manuscript", "Manuscript"], ["title_page", "Title page"], ["figure", "Figures"], ["table", "Tables"],
  ["cover_letter", "Letters"], ["form", "Forms"], ["metadata", "Metadata"], ["xml", "Metadata"], ["other", "Other files"],
];

export function fileIcon(f) {
  const e = f.ext;
  if (f.label === "QR code") return QrCode;
  if (f.kind === "manuscript") return ScrollText;
  if (f.kind === "title_page") return IdCard;
  if (f.kind === "cover_letter") return Mail;
  if (f.kind === "form") return FileSignature;
  if (["pptx", "ppt"].includes(e)) return Presentation;
  if (["xlsx", "xls", "csv"].includes(e)) return FileSpreadsheet;
  if (f.kind === "table") return Table2;
  if (f.view === "image" || f.kind === "figure") return FileImage;
  if (e === "json") return Braces;
  if (e === "xml") return FileCode2;
  if (["zip", "rar", "7z"].includes(e)) return FileArchive;
  if (["docx", "doc", "pdf", "txt"].includes(e)) return FileText;
  return File;
}

export function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

export default function FilesPanel({ files, error, active, canSplit, onOpen }) {
  if (error) return <div className="error-box small">{error}</div>;
  if (!files) {
    return <div className="files-list">{[0, 1, 2, 3].map((i) => <div key={i} className="frow skel-row" />)}</div>;
  }
  if (!files.length) return <p className="muted">No files in this submission.</p>;
  const groups = [];
  for (const [kind, title] of GROUPS) {
    const fs = files.filter((f) => f.kind === kind);
    if (!fs.length) continue;
    const g = groups.find((x) => x.title === title);
    if (g) g.files.push(...fs);
    else groups.push({ title, files: fs });
  }
  let n = 0;
  return (
    <div className="files-list">
      <p className="muted small" style={{ margin: 0 }}>
        Everything the author submitted · {files.length} file{files.length === 1 ? "" : "s"}. Read-only.
      </p>
      {groups.map((g) => (
        <section key={g.title} className="fgroup" aria-label={g.title}>
          <h4>{g.title}<span>{g.files.length}</span></h4>
          {g.files.map((f) => {
            const Icon = fileIcon(f);
            const on = active === f.path;
            return (
              <motion.div key={f.path} className={`frow ${on ? "on" : ""}`} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.25, delay: Math.min(n++, 14) * 0.025 }}>
                <button className="fmain" onClick={() => onOpen(f, "overlay")} title={`Open ${f.name}`}>
                  <span className={`ficon k-${f.kind}`}><Icon size={17} /></span>
                  <span className="fname">
                    <b>{f.kind === "figure" || f.kind === "table" ? f.label : f.name}</b>
                    <small>{f.kind === "figure" || f.kind === "table" ? `${f.name} · ` : ""}{f.ext.toUpperCase()} · {fmtSize(f.size)}</small>
                  </span>
                </button>
                {f.manuscript ? <span className="pill ai" title="This is the file being edited">editing</span>
                  : canSplit && (
                    <button className="btn sm ghost icon fside" onClick={() => onOpen(f, "beside")} title="Open beside manuscript" aria-label={`Open ${f.name} beside the manuscript`}>
                      <Columns2 size={15} />
                    </button>
                  )}
              </motion.div>
            );
          })}
        </section>
      ))}
    </div>
  );
}
