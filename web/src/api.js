// Thin client for the Edit OS API. Auth: an API key (kept in this browser) or a job's review token
// (from the ?token= in a review link the dashboard hands out).

const KEY_STORE = "editos.apiKey";

export function getApiKey() {
  try { return localStorage.getItem(KEY_STORE) || ""; } catch { return ""; }
}
export function setApiKey(k) {
  try { localStorage.setItem(KEY_STORE, k); } catch { /* private mode */ }
}
const EDITOR_STORE = "editos.editor";

// The editor's name signs the file ("Technical Editing done on <date>:- <name>") and is recorded with each
// decision. A dashboard link can pass it as ?editor=Name; otherwise the editor types it once.
export function getEditor() {
  const fromLink = new URLSearchParams(window.location.search).get("editor");
  if (fromLink) setEditor(fromLink);
  try { return (fromLink || localStorage.getItem(EDITOR_STORE) || "").trim(); } catch { return (fromLink || "").trim(); }
}
export function setEditor(name) {
  try { localStorage.setItem(EDITOR_STORE, name.trim()); } catch { /* private mode */ }
}
export function reviewToken() {
  return new URLSearchParams(window.location.search).get("token") || "";
}

function headers(extra = {}) {
  const h = { ...extra };
  const token = reviewToken();
  if (token) h["X-Review-Token"] = token;
  const key = getApiKey();
  if (key) h["X-API-Key"] = key;
  const editor = getEditor();
  if (editor) h["X-Editor"] = encodeURIComponent(editor);
  return h;
}

async function call(method, path, body) {
  const opts = { method, headers: headers() };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = r.status;
    throw err;
  }
  return r.json();
}

export const api = {
  login: (username, password) => call("POST", "/api/login", { username, password }),
  me: () => call("GET", "/api/me"),
  journals: () => call("GET", "/api/journals"),
  jobs: () => call("GET", "/api/jobs?limit=30"),
  spend: (month) => call("GET", `/api/spend?month=${encodeURIComponent(month)}&limit=5000`),
  job: (id) => call("GET", `/api/jobs/${id}`),
  upload: (file, journal, confirmJournal = false) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("journal", journal);
    if (confirmJournal) fd.append("confirm_journal", "true");
    return call("POST", "/api/jobs", fd);
  },
  changes: (id) => call("GET", `/api/jobs/${id}/changes`),
  rules: (id) => call("GET", `/api/jobs/${id}/rules`),
  queries: (id) => call("GET", `/api/jobs/${id}/queries`),
  decide: (id, body) => call("POST", `/api/jobs/${id}/decisions`, body),
  edit: (id, itemId, text) => call("POST", `/api/jobs/${id}/edit`, { id: itemId, new: text }),
  render: (id, final) => call("POST", `/api/jobs/${id}/render${final ? "?final=true" : ""}`),
  qc: (id) => call("GET", `/api/jobs/${id}/qc`),
  document: (id) => call("GET", `/api/jobs/${id}/document`),
  directEdit: (id, body) => call("POST", `/api/jobs/${id}/changes`, body),
  autopilot: (id) => call("POST", `/api/jobs/${id}/autopilot`),
  undoAutopilot: (id) => call("POST", `/api/jobs/${id}/autopilot/undo`),
};

export async function download(id, kind) {
  // The edited files carry the sign-off "Technical Editing done on <date>:- <name>": never "???" in a file that may
  // go to the client, so ask for the name first when none is set.
  if ((kind === "tracked" || kind === "clean") && !getEditor()) {
    const v = window.prompt("Your name, for the sign-off (\"Technical Editing done on <date>:- <name>\")", "");
    if (!v || !v.trim()) throw new Error("Add your name first: it signs the edited file");
    setEditor(v.trim());
  }
  const r = await fetch(`/api/jobs/${id}/files/${kind}`, { headers: headers() });
  if (!r.ok) throw new Error(`Download failed (${r.status})`);
  const blob = await r.blob();
  const cd = r.headers.get("content-disposition") || "";
  const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const name = m ? decodeURIComponent(m[1]) : `${id}_${kind}`;
  const url = URL.createObjectURL(blob);
  if (kind === "qc_report") {
    window.open(url, "_blank", "noopener");
  } else {
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// --- submission files (title page, figures, XML, forms, ...) -----------------------------------

const subPath = (p) => p.split("/").map(encodeURIComponent).join("/");

export const submission = {
  list: (id) => call("GET", `/api/jobs/${id}/submission`),
  view: (id, path) => call("GET", `/api/jobs/${id}/submission/view/${subPath(path)}`),
  /** An object URL for the file (images, PDFs); the caller revokes it. */
  async blobUrl(id, path) {
    const r = await fetch(`/api/jobs/${id}/submission/raw/${subPath(path)}`, { headers: headers() });
    if (!r.ok) {
      let msg = `Could not open the file (${r.status})`;
      try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
      throw new Error(msg);
    }
    return URL.createObjectURL(await r.blob());
  },
  /** Save the file exactly as the author sent it. */
  async download(id, file) {
    const r = await fetch(`/api/jobs/${id}/submission/raw/${subPath(file.path)}?original=true`, { headers: headers() });
    if (!r.ok) throw new Error(`Download failed (${r.status})`);
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = file.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  },
};
