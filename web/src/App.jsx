import React, { useEffect, useState } from "react";
import { api, getApiKey, reviewToken, setApiKey } from "./api.js";
import Review from "./Review.jsx";

function route() {
  const m = window.location.pathname.match(/^\/review\/([A-Za-z0-9]+)/);
  return m ? { page: "review", id: m[1] } : { page: "home" };
}

export function go(path) {
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function KeyBox() {
  const [key, setKey] = useState(getApiKey());
  const [saved, setSaved] = useState(false);
  if (reviewToken()) return null;
  return (
    <form className="keybox" onSubmit={(e) => { e.preventDefault(); setApiKey(key.trim()); setSaved(true); window.location.reload(); }}>
      <label htmlFor="apikey">API key</label>
      <input id="apikey" type="password" value={key} onChange={(e) => { setKey(e.target.value); setSaved(false); }} placeholder="X-API-Key" />
      <button className="btn small" type="submit">{saved ? "Saved" : "Save"}</button>
    </form>
  );
}

function StatusPill({ status }) {
  return <span className={`pill ${status}`}>{status}</span>;
}

function Home() {
  const [journals, setJournals] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [journal, setJournal] = useState("ijo");
  const [file, setFile] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => api.jobs().then(setJobs).catch((e) => setError(e.message));
  useEffect(() => {
    api.journals().then(setJournals).catch((e) => setError(e.message));
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, []);

  async function submit(e) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const job = await api.upload(file, journal);
      go(`/review/${job.id}`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="home">
      <section className="card">
        <h2>New manuscript</h2>
        <form className="upload" onSubmit={submit}>
          <label>Manuscript (.docx) or submission (.zip)
            <input type="file" accept=".docx,.zip" onChange={(e) => setFile(e.target.files[0] || null)} />
          </label>
          <label>Journal
            <select value={journal} onChange={(e) => setJournal(e.target.value)}>
              {journals.map((j) => <option key={j.code} value={j.code}>{j.code.toUpperCase()} · {j.name}</option>)}
            </select>
          </label>
          <button className="btn primary" disabled={!file || busy}>{busy ? "Uploading…" : "Run Edit OS"}</button>
        </form>
        {error && <p className="error">{error}</p>}
      </section>
      <section className="card">
        <h2>Recent jobs</h2>
        {jobs.length === 0 ? <p className="muted">No jobs yet.</p> : (
          <table className="jobs">
            <thead><tr><th>File</th><th>Journal</th><th>Status</th><th>Changes</th><th>QC</th><th>Created</th></tr></thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id} onClick={() => go(`/review/${j.id}`)}>
                  <td>{j.input_name}</td>
                  <td>{j.journal.toUpperCase()}</td>
                  <td><StatusPill status={j.status} /> <span className="muted">{j.status === "running" ? j.stage : ""}</span></td>
                  <td>{j.summary ? j.summary.changes.total : "–"}</td>
                  <td>{j.summary ? (j.summary.qc.passed ? "passed" : "blocked") : "–"}</td>
                  <td className="muted">{j.created_at.replace("T", " ").replace("Z", "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}

export default function App() {
  const [r, setR] = useState(route());
  useEffect(() => {
    const on = () => setR(route());
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  return (
    <div className="app">
      <header className="top">
        <a className="brand" href="/" onClick={(e) => { if (!reviewToken()) { e.preventDefault(); go("/"); } }}>Edit OS</a>
        <span className="muted">Style &amp; copy-edit review</span>
        <span className="spacer" />
        <KeyBox />
      </header>
      {r.page === "review" ? <Review id={r.id} /> : <Home />}
    </div>
  );
}
