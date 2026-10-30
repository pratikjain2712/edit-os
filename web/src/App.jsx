import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { ArrowRight, Check, ChevronDown, FileText, KeyRound, LogOut, Search, UploadCloud, Wand2, X } from "lucide-react";
import { api, getApiKey, reviewToken, setApiKey } from "./api.js";
import Review from "./Review.jsx";
import { ThemeButton, ToastProvider, useOutside, useTheme, useToast } from "./ui.jsx";

function route() {
  const m = window.location.pathname.match(/^\/review\/([A-Za-z0-9]+)/);
  return m ? { page: "review", id: m[1] } : { page: "home" };
}

export function go(path) {
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function Brand() {
  return (
    <a className="brand" href="/" onClick={(e) => { if (!reviewToken()) { e.preventDefault(); go("/"); } }}>
      <span className="brand-mark"><Wand2 size={16} /></span>
      <span className="brand-name">Edit OS</span>
    </a>
  );
}

function Login({ onDone, theme, nextTheme }) {
  const [user, setUser] = useState("");
  const [pw, setPw] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await api.login(user.trim(), pw);
      setApiKey(r.token);
      onDone(r.user);
    } catch (err) {
      setError(err.status === 401 ? "Wrong username or password." : err.message);
      setBusy(false);
    }
  }
  return (
    <>
      <header className="topbar"><Brand /><span className="spacer" /><ThemeButton theme={theme} onClick={nextTheme} /></header>
      <main className="login-wrap">
        <motion.form className="card login-card" onSubmit={submit} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
          <h2>Sign in</h2>
          <p className="muted small">Edit OS is in testing. Use the username and password you were given.</p>
          <label className="field"><span>Username</span>
            <input className="input" autoFocus autoComplete="username" value={user} onChange={(e) => setUser(e.target.value)} required />
          </label>
          <label className="field"><span>Password</span>
            <input className="input" type="password" autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} required />
          </label>
          {error && <div className="error-box">{error}</div>}
          <button className="btn primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        </motion.form>
      </main>
    </>
  );
}

function SignOut({ user }) {
  return (
    <button className="btn ghost sm" title={`Signed in as ${user}`}
      onClick={() => { setApiKey(""); window.location.href = "/"; }}>
      <LogOut size={15} /><span className="hide-phone">{user} · Sign out</span>
    </button>
  );
}

function KeyMenu() {
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState(getApiKey());
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  if (reviewToken()) return null;
  return (
    <div className="menu" ref={ref}>
      <button className="btn ghost sm" onClick={() => setOpen(!open)} title="API key"><KeyRound size={15} /><span className="hide-phone">API key</span></button>
      <AnimatePresence>
        {open && (
          <motion.form className="menu-panel" style={{ width: 300 }} initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }}
            onSubmit={(e) => { e.preventDefault(); setApiKey(key.trim()); window.location.reload(); }}>
            <div className="hint">Kept in this browser only. Review links from the dashboard carry their own token.</div>
            <div className="keybox">
              <input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder="X-API-Key" aria-label="API key" />
              <button className="btn primary sm" type="submit">Save</button>
            </div>
          </motion.form>
        )}
      </AnimatePresence>
    </div>
  );
}

function JournalPicker({ journals, value, onChange }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [hi, setHi] = useState(0);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  const cur = journals.find((j) => j.code === value);
  const list = useMemo(() => {
    const s = q.trim().toLowerCase();
    const rank = (j) => (j.code === s ? 0 : j.code.startsWith(s) ? 1 : j.name.toLowerCase().startsWith(s) ? 2 : 3);
    return journals.filter((j) => !s || j.code.includes(s) || j.name.toLowerCase().includes(s))
      .sort((a, b) => (s ? rank(a) - rank(b) : 0));
  }, [journals, q]);
  const pick = (j) => { onChange(j.code); setQ(""); setOpen(false); };
  return (
    <div className="combo" ref={ref}>
      <Search size={16} className="combo-icon" />
      <input role="combobox" aria-expanded={open} aria-label="Journal"
        value={open ? q : cur ? `${cur.name}` : value.toUpperCase()}
        placeholder={cur ? cur.name : "Search journals"}
        onFocus={() => { setOpen(true); setHi(0); }}
        onChange={(e) => { setQ(e.target.value); setOpen(true); setHi(0); }}
        onKeyPress={(e) => { if (e.key === "Enter") e.preventDefault(); }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(h + 1, list.length - 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, 0)); }
          else if (e.key === "Enter") { e.preventDefault(); if (open && list[hi]) pick(list[hi]); }
          else if (e.key === "Escape") setOpen(false);
        }} />
      <ChevronDown size={16} className="combo-icon" style={{ left: "auto", right: 12 }} />
      <AnimatePresence>
        {open && (
          <motion.div className="combo-list" role="listbox" initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.15 }}>
            {list.length === 0 && <div className="muted small" style={{ padding: 10 }}>No journal matches “{q}”.</div>}
            {list.map((j, i) => (
              <button type="button" key={j.code} role="option" aria-selected={j.code === value} className={i === hi ? "on" : ""}
                onMouseEnter={() => setHi(i)} onClick={() => pick(j)}>
                <code>{j.code.toUpperCase()}</code><span style={{ flex: 1 }}>{j.name}</span>
                {j.code === value && <Check size={15} color="var(--accent)" />}
              </button>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// The journal a file name starts with: "IJO_702_26.zip" -> ijo, "jehp_1044_26.docx" -> jehp,
// "Neurol-India-D-26-00445.zip" -> ni. The longest matching prefix wins ("ijoem_..." is not IJO).
export function journalFromName(name, journals) {
  const n = (name || "").toLowerCase();
  let best = null;
  for (const j of journals) {
    for (const p of j.prefixes || [j.code]) {
      if (n.startsWith(p) && /^[\s_\-.]/.test(n.slice(p.length)) && (!best || p.length > best.len)) best = { code: j.code, len: p.length };
    }
  }
  return best && best.code;
}

const ago = (iso) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
};

function Home() {
  const toast = useToast();
  const [journals, setJournals] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [month, setMonth] = useState(null);  // this month's Claude spend (/api/spend)
  const [journal, setJournal] = useState("ijo");
  const [file, setFile] = useState(null);
  const [over, setOver] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef(null);

  const load = useCallback(() => api.jobs().then(setJobs).catch((e) => setError(e.message)), []);
  useEffect(() => {
    api.journals().then(setJournals).catch((e) => setError(e.message));
    api.spend(new Date().toISOString().slice(0, 7)).then(setMonth).catch(() => {});
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load]);

  const take = (f) => {
    if (!f) return;
    if (!/\.(docx|zip)$/i.test(f.name)) { toast("Choose a .docx manuscript or a submission .zip", "error"); return; }
    setFile(f);
    const code = journalFromName(f.name, journals);
    if (code) setJournal(code);
  };
  // A file picked before the journal list arrived: read its name again once the list is in (JEHP_73_26 ran as IJO).
  useEffect(() => {
    const code = file && journalFromName(file.name, journals);
    if (code) setJournal(code);
  }, [journals]); // eslint-disable-line react-hooks/exhaustive-deps

  async function submit(e) {
    e.preventDefault();
    if (!file) { input.current?.click(); return; }
    // The file name names another journal than the one chosen: ask before running it with the wrong style.
    const named = journalFromName(file.name, journals);
    if (named && named !== journal &&
        !window.confirm(`The file name says ${named.toUpperCase()}, but ${journal.toUpperCase()} is selected.\n\nRun it as ${journal.toUpperCase()} anyway?`)) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const job = await api.upload(file, journal, Boolean(named && named !== journal));
      go(`/review/${job.id}`);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <main className="home">
      <motion.div className="hero" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
        <h1>Drop a manuscript.<br /><em>Review every edit on the page.</em></h1>
        <p>Journal style, references checked in PubMed, and a careful copy edit — each one a suggestion you accept, reject or reword in place.</p>
      </motion.div>

      <motion.form className="card upload-card" onSubmit={submit} initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5, delay: 0.08 }}>
        <div className={`drop ${over ? "over" : ""}`} role="button" tabIndex={0}
          onClick={() => input.current?.click()}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.current?.click(); } }}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); take(e.dataTransfer.files[0]); }}>
          <input ref={input} type="file" accept=".docx,.zip" hidden onChange={(e) => take(e.target.files[0])} />
          <AnimatePresence mode="wait">
            {file ? (
              <motion.div key="f" initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }} style={{ display: "grid", gap: 10, justifyItems: "center" }}>
                <span className="file-chip"><FileText size={18} color="var(--accent)" />{file.name}
                  <span className="muted small">{(file.size / 1024).toFixed(0)} KB</span>
                  <button type="button" className="btn ghost sm icon" aria-label="Remove file" onClick={(e) => { e.stopPropagation(); setFile(null); }}><X size={14} /></button>
                </span>
                <span className="muted small">Click or drop to replace</span>
              </motion.div>
            ) : (
              <motion.div key="e" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} style={{ display: "grid", gap: 8, justifyItems: "center" }}>
                <motion.span className="drop-icon" animate={over ? { y: -4, scale: 1.06 } : { y: 0, scale: 1 }}><UploadCloud size={24} /></motion.span>
                <b>Drop the manuscript here</b>
                <span className="muted small">A .docx manuscript, or the submission .zip with its title page and XML</span>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
        <div className="upload-row">
          <label className="field"><span>Journal</span>
            <JournalPicker journals={journals} value={journal} onChange={setJournal} />
          </label>
          <button className="btn magic go-btn" disabled={busy}>
            {busy ? "Uploading…" : <>Run Edit OS <ArrowRight size={16} /></>}
          </button>
        </div>
        {error && <div className="error-box">{error}</div>}
      </motion.form>

      <section>
        <h3 className="section-title">Recent manuscripts</h3>
        {month && month.articles > 0 && (
          <p className="muted small">
            This month: {month.articles} article{month.articles > 1 ? "s" : ""} · Claude ₹{month.inr.toFixed(0)}
            {Object.entries(month.by_journal).map(([j, t]) => ` · ${j.toUpperCase()} ₹${t.inr_per_article.toFixed(2)}/article${t.inr_per_page != null ? `, ₹${t.inr_per_page.toFixed(2)}/page` : ""}`).join("")}
            {` (at ₹${month.usd_inr}/$)`}
          </p>
        )}
        {jobs.length === 0 ? <p className="muted">Nothing here yet — your first manuscript will show up here.</p> : (
          <div className="jobs-list">
            {jobs.map((j, i) => (
              <motion.button key={j.id} className="job-row" onClick={() => go(`/review/${j.id}`)}
                initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: Math.min(i, 10) * 0.03 }}>
                <span className="ficon"><FileText size={17} /></span>
                <span style={{ minWidth: 0 }}>
                  <b>{j.input_name}</b>
                  <span className="muted small">{j.journal.toUpperCase()} · {ago(j.created_at)}{j.status === "running" ? ` · ${j.stage}` : ""}</span>
                </span>
                <span className="muted small hide-phone">
                  {j.summary && j.summary.changes ? `${j.summary.changes.total} suggestions · QC ${j.summary.qc.passed ? "passed" : "blocked"}` : ""}
                  {j.spend && j.spend.inr != null ? ` · Claude ₹${j.spend.inr.toFixed(2)}${j.spend.inr_per_page != null ? ` (₹${j.spend.inr_per_page.toFixed(2)}/page)` : ""}` : ""}
                </span>
                <span className={`pill ${j.status}`}>{j.status}</span>
              </motion.button>
            ))}
          </div>
        )}
      </section>
    </main>
  );
}

export default function App() {
  const [r, setR] = useState(route());
  const [theme, nextTheme] = useTheme();
  // Who is signed in: undefined while checking, null when a sign-in is needed, "" for an API key.
  const [user, setUser] = useState(reviewToken() ? "" : undefined);
  useEffect(() => {
    if (reviewToken()) return;
    api.me().then((m) => setUser(m.user)).catch((e) => setUser(e.status === 401 ? null : ""));
  }, []);
  useEffect(() => {
    const on = () => setR(route());
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  return (
    <ToastProvider>
      <div className="app">
        {user === undefined ? null : user === null ? (
          <Login onDone={setUser} theme={theme} nextTheme={nextTheme} />
        ) : r.page === "review" ? (
          <Review id={r.id} theme={theme} nextTheme={nextTheme} />
        ) : (
          <>
            <header className="topbar">
              <Brand />
              <span className="spacer" />
              {user ? <SignOut user={user} /> : <KeyMenu />}
              <ThemeButton theme={theme} onClick={nextTheme} />
            </header>
            <Home />
          </>
        )}
      </div>
    </ToastProvider>
  );
}
