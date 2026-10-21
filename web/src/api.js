// Thin client for the Edit OS API. Auth: an API key (kept in this browser) or a job's review token
// (from the ?token= in a review link the dashboard hands out).

const KEY_STORE = "editos.apiKey";

export function getApiKey() {
  try { return localStorage.getItem(KEY_STORE) || ""; } catch { return ""; }
}
export function setApiKey(k) {
  try { localStorage.setItem(KEY_STORE, k); } catch { /* private mode */ }
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
  journals: () => call("GET", "/api/journals"),
  jobs: () => call("GET", "/api/jobs?limit=30"),
  job: (id) => call("GET", `/api/jobs/${id}`),
  upload: (file, journal) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("journal", journal);
    return call("POST", "/api/jobs", fd);
  },
  changes: (id) => call("GET", `/api/jobs/${id}/changes`),
  queries: (id) => call("GET", `/api/jobs/${id}/queries`),
  decide: (id, body) => call("POST", `/api/jobs/${id}/decisions`, body),
  edit: (id, itemId, text) => call("POST", `/api/jobs/${id}/edit`, { id: itemId, new: text }),
  render: (id, final) => call("POST", `/api/jobs/${id}/render${final ? "?final=true" : ""}`),
  qc: (id) => call("GET", `/api/jobs/${id}/qc`),
  document: (id) => call("GET", `/api/jobs/${id}/document`),
  directEdit: (id, body) => call("POST", `/api/jobs/${id}/changes`, body),
};

export async function download(id, kind) {
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
