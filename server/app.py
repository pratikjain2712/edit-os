"""FastAPI service: jobs, suggestions, decisions, re-render, downloads; serves the review screen.

Auth: `X-API-Key` (keys from EDITOS_API_KEYS) for everything; a job's review token (`X-Review-Token`
header or `?token=`) for that one job's review routes, so a dashboard can link an editor straight
to the review screen without handing out an API key. With no keys configured the API refuses all
requests, unless EDITOS_AUTH_DISABLED=1 (local development).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shutil
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from edit_os import autopilot, docview, pipeline, rule_sources, spend, style as style_mod
from edit_os.changes import Layer, Source, Status

from . import db, sessions
from .config import Settings
from .jobqueue import ThreadQueue
from .storage import LocalStorage, make_storage
from . import submission_files
from .worker import compact_summary, input_file, job_lock, out_dir, process, review_url

log = logging.getLogger("editos.api")

def download_name(input_name: str | None, stem: str, path: str) -> str:
    """The downloaded file named after the upload: "JEHP_73_26.zip" -> "JEHP_73_26_tracked.docx" (owner, 2026-10-29),
    not the manuscript's own "Manuscript_tracked.docx"."""
    base = os.path.basename(path)
    up = re.sub(r"[^\w.\- ]+", "_", os.path.splitext(os.path.basename(input_name or ""))[0]).strip(" ._")
    return up + base[len(stem):] if up and base.startswith(stem) else base


_SIGNED = "_signed.json"  # the last render's final flag and signing editor
DOWNLOADS = {  # kind -> (pipeline file kind, media type)
    "tracked": ("tracked", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "clean": ("clean", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "queries": ("queries", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "qc_report": ("qc_html", "text/html"),
    "qc_json": ("qc_json", "application/json"),
    "changes": ("changes", "application/json"),
}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def journal_codes() -> dict[str, str]:
    out = {}
    for name in sorted(os.listdir(style_mod.STYLES_DIR)):
        if name.endswith(".yaml") and name != "base.yaml":
            code = name[:-5]
            out[code] = style_mod.load(code).name
    return out


# --- request bodies ---------------------------------------------------------------------------

class S3Job(BaseModel):
    bucket: str
    key: str
    journal: str
    external_id: str = ""
    callback_url: str = ""
    stages: list[str] | None = None


class Login(BaseModel):
    username: str
    password: str


class Edit(BaseModel):
    id: str = Field(description="change or front-matter insert id")
    new: str = Field(description="the editor's wording; the suggestion is accepted with it")
    editor: str = ""


class DirectEdit(BaseModel):
    para_id: str
    base: str = Field(description="the paragraph's text as the editor saw it (accepted changes applied)")
    text: str = Field(description="the paragraph's text as the editor wants it")
    editor: str = ""


class Decision(BaseModel):
    ids: list[str] = Field(default_factory=list, description="change, front-matter insert or query ids")
    status: Literal["accepted", "rejected", "suggested"]
    layer: str | None = Field(None, description="bulk: every still-pending change in this layer")
    editor: str = ""


def _ed(x_editor: str | None) -> str:
    """The X-Editor header: the review screen sends the name URL-encoded (headers are Latin-1 only)."""
    return urllib.parse.unquote(x_editor).strip() if x_editor else ""


def create_app(settings: Settings | None = None, queue=None) -> FastAPI:
    settings = settings or Settings.from_env()
    os.makedirs(settings.data_dir, exist_ok=True)
    store = db.JobStore(os.path.join(settings.data_dir, "jobs.sqlite"))
    storage: LocalStorage = make_storage(settings)
    queue = queue or ThreadQueue(lambda job_id: process(job_id, settings, store, storage), settings.workers)
    para_cache: dict[str, tuple[float, dict[str, str]]] = {}
    doc_cache: dict[str, tuple[float, dict]] = {}

    @asynccontextmanager
    async def lifespan(app):
        for job_id in store.unfinished():  # resume jobs a restart interrupted
            store.update(job_id, status=db.QUEUED, stage="queued")
            queue.submit(job_id)
        yield
        queue.shutdown()

    app = FastAPI(title="Edit OS", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.store, app.state.queue = settings, store, queue

    # --- auth -------------------------------------------------------------------------------

    session_secret = settings.session_secret or (settings.api_keys[0] if settings.api_keys else "")

    def _key_ok(key: str | None) -> bool:
        """An API key, or a sign-in session token (sent in the same header)."""
        if key and key.startswith(sessions.PREFIX):
            return sessions.verify(session_secret, key) is not None
        return bool(key) and any(hmac.compare_digest(key, k) for k in settings.api_keys)

    def _check_configured():
        if not settings.auth_disabled and not settings.api_keys and not settings.users:
            raise HTTPException(503, "No API keys configured (set EDITOS_API_KEYS)")

    def require_key(x_api_key: str | None = Header(None)) -> str:
        _check_configured()
        if settings.auth_disabled or _key_ok(x_api_key):
            return "api"
        raise HTTPException(401, "Missing or invalid X-API-Key")

    def require_job(job_id: str, x_api_key: str | None = Header(None),
                    x_review_token: str | None = Header(None), token: str | None = Query(None)) -> dict:
        _check_configured()
        job = store.get(job_id)
        allowed = settings.auth_disabled or _key_ok(x_api_key)
        if not allowed and job is not None:
            t = x_review_token or token
            allowed = bool(t) and hmac.compare_digest(t, job["review_token"])
        if not allowed:
            raise HTTPException(401, "Missing or invalid X-API-Key / review token")
        if job is None:
            raise HTTPException(404, "No such job")
        return job

    def require_done(job: dict = Depends(require_job)) -> dict:
        if job["status"] != db.DONE:
            raise HTTPException(409, f"Job is {job['status']}")
        if not storage.restore(job["id"]):
            raise HTTPException(410, "Job files are no longer available")
        return job

    # --- helpers ----------------------------------------------------------------------------

    def public(job: dict, with_token: bool = False) -> dict:
        d = {k: job[k] for k in ("id", "status", "stage", "journal", "stages", "input_name", "external_id",
                                 "error", "summary", "created_at", "updated_at")}
        d["links"] = {k: f"/api/jobs/{job['id']}/files/{k}" for k in DOWNLOADS} if job["status"] == db.DONE else {}
        sp = spend.load(out_dir(storage, job["id"]))  # what Claude cost for this article (edit_os/spend.py)
        d["spend"] = {k: sp[k] for k in ("usd", "words", "pages", "usd_per_page") if k in sp} if sp else None
        if with_token:
            d["review_token"] = job["review_token"]
            d["review_url"] = review_url(settings, job)
        return d

    def merged(job_id: str):
        """Saved records with the editor's decisions applied."""
        outdir = out_dir(storage, job_id)
        man, changes, inserts, qdicts = pipeline.load_records(outdir)
        dec = store.decisions(job_id)
        for item in list(changes.changes) + list(inserts):
            if item.id in dec:
                item.status = Status(dec[item.id]["status"])
                own = dec[item.id].get("new_text")
                if own is not None:  # the editor's wording replaces the suggestion's
                    if hasattr(item, "text"):
                        item.text = own
                    else:
                        item.new = own
                        item.source = Source.EDITOR
        for q in qdicts:
            if q["key"] in dec:
                q["status"] = dec[q["key"]]["status"]
        return outdir, man, changes, inserts, qdicts

    def paragraphs(job_id: str) -> dict[str, str]:
        src = pipeline.source_path(out_dir(storage, job_id))
        mtime = os.path.getmtime(src)
        hit = para_cache.get(job_id)
        if hit is None or hit[0] != mtime:
            hit = para_cache[job_id] = (mtime, pipeline.paragraph_texts(src))
        return hit[1]

    def document_view(job_id: str) -> dict:
        src = pipeline.source_path(out_dir(storage, job_id))
        mtime = os.path.getmtime(src)
        hit = doc_cache.get(job_id)
        if hit is None or hit[0] != mtime:
            hit = doc_cache[job_id] = (mtime, docview.document(src))
        return hit[1]

    def _create(journal: str, input_name: str, stages, **kw) -> dict:
        if style_mod.resolve(journal) not in journal_codes():
            raise HTTPException(422, f"Unknown journal code {journal!r}")
        stages = tuple(stages or settings.stages)
        if set(stages) - set(pipeline.STAGES):
            raise HTTPException(422, f"stages must be among {pipeline.STAGES}")
        return store.create(journal.lower(), stages, input_name, **kw)

    # --- routes -----------------------------------------------------------------------------

    @app.get("/api/health")
    def health():
        return {"ok": True, "storage": storage.kind}

    @app.post("/api/login")
    def login(body: Login):
        if not settings.users or not session_secret:
            raise HTTPException(404, "Sign-in is not set up on this server")
        if not sessions.check_password(settings.users, body.username, body.password):
            time.sleep(1)  # slows password guessing
            raise HTTPException(401, "Wrong username or password")
        user = body.username.strip().lower()
        return {"token": sessions.issue(session_secret, user, settings.session_hours), "user": user}

    @app.get("/api/me")
    def me(x_api_key: str | None = Header(None)):
        _check_configured()
        user = sessions.verify(session_secret, x_api_key)
        if user:
            return {"user": user, "kind": "session"}
        if settings.auth_disabled or _key_ok(x_api_key):
            return {"user": "", "kind": "api"}
        raise HTTPException(401, "Sign in first")

    @app.get("/api/journals", dependencies=[Depends(require_key)])
    def journals():
        prefixes = {}
        for alias, code in style_mod.ALIASES.items():
            prefixes.setdefault(code, []).append(alias)
        return [{"code": c, "name": n, "prefixes": [c] + prefixes.get(c, [])} for c, n in journal_codes().items()]

    @app.post("/api/jobs", status_code=202, dependencies=[Depends(require_key)])
    def upload(file: UploadFile = File(...), journal: str = Form(...), external_id: str = Form(""),
               callback_url: str = Form(""), stages: str = Form("")):
        name = _SAFE_NAME.sub("_", os.path.basename(file.filename or ""))
        if not name.lower().endswith((".docx", ".zip")):
            raise HTTPException(422, "Upload a .docx manuscript or a submission .zip")
        job = _create(journal, name, [s for s in stages.split(",") if s], external_id=external_id,
                      callback_url=callback_url)
        dest = os.path.join(storage.job_dir(job["id"]), "input", name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        limit = settings.max_upload_mb * 1024 * 1024
        size = 0
        with open(dest, "wb") as f:
            while chunk := file.file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    f.close()
                    shutil.rmtree(storage.job_dir(job["id"]), ignore_errors=True)
                    store.update(job["id"], status=db.FAILED, stage="failed", error="upload too large")
                    raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB")
                f.write(chunk)
        queue.submit(job["id"])
        return public(store.get(job["id"]), with_token=True)

    @app.post("/api/jobs/from-s3", status_code=202, dependencies=[Depends(require_key)])
    def from_s3(body: S3Job):
        if not body.key.lower().endswith((".docx", ".zip")):
            raise HTTPException(422, "key must point to a .docx or .zip")
        job = _create(body.journal, os.path.basename(body.key), body.stages, input_ref=f"s3://{body.bucket}/{body.key}",
                      external_id=body.external_id, callback_url=body.callback_url)
        queue.submit(job["id"])
        return public(job, with_token=True)

    @app.get("/api/jobs", dependencies=[Depends(require_key)])
    def list_jobs(limit: int = 50, external_id: str | None = None):
        return [public(j) for j in store.list(min(limit, 500), external_id)]

    def _parts(sp: dict) -> dict:
        parts: dict[str, float] = {}
        for step in sp.get("steps", []):
            for k, v in step.get("parts", {}).items():
                parts[k] = round(parts.get(k, 0) + v.get("usd", 0), 4)
        return parts

    @app.get("/api/spend", dependencies=[Depends(require_key)])
    def spend_report(month: str | None = None, limit: int = 5000):
        """What Claude cost, per article and in total: `month` "2026-10" (else every job kept), by journal, with
        words, pages of 300 words and the cost per page. Server costs (AWS) are fixed per month and not in here."""
        rows, tot = [], {}
        for j in store.list(min(limit, 20000)):
            if month and not str(j.get("created_at", "")).startswith(month):
                continue
            sp = spend.load(out_dir(storage, j["id"]))
            if not sp:
                continue
            rows.append({"id": j["id"], "input_name": j["input_name"], "journal": j["journal"],
                         "created_at": j["created_at"], "usd": sp.get("usd", 0), "words": sp.get("words"),
                         "pages": sp.get("pages"), "usd_per_page": sp.get("usd_per_page"),
                         "parts": _parts(sp)})
            t = tot.setdefault(j["journal"], {"articles": 0, "usd": 0.0, "pages": 0.0})
            t["articles"] += 1
            t["usd"] += sp.get("usd", 0)
            t["pages"] += sp.get("pages") or 0
        for t in tot.values():
            t["usd"] = round(t["usd"], 2)
            t["pages"] = round(t["pages"], 1)
            t["usd_per_article"] = round(t["usd"] / t["articles"], 3)
            t["usd_per_page"] = round(t["usd"] / t["pages"], 4) if t["pages"] else None
        all_usd = sum(t["usd"] for t in tot.values())
        return {"month": month, "articles": len(rows), "usd": round(all_usd, 2), "by_journal": tot, "jobs": rows}

    @app.get("/api/jobs/{job_id}")
    def job_status(job: dict = Depends(require_job), x_api_key: str | None = Header(None)):
        return public(job, with_token=_key_ok(x_api_key) or settings.auth_disabled)

    @app.get("/api/jobs/{job_id}/changes")
    def list_changes(job: dict = Depends(require_done), layer: str | None = None, status: str | None = None,
                     context: int = 120):
        outdir, man, changes, inserts, _ = merged(job["id"])
        texts = paragraphs(job["id"])
        checks = _second_checks(job["id"])
        items = []
        for c in changes.changes:
            t = texts.get(c.para_id, "")
            d = c.to_dict()
            if c.source == Source.AI and c.id in checks:
                d["provenance"] = {**(c.provenance or {"kind": "ai", "label": "Claude copy edit"}),
                                   "second_check": checks[c.id]}
            items.append({**d, "kind": "change", "rule_key": rule_sources.key_for(c.reason, c.provenance),
                          "edited": c.source == Source.EDITOR and c.reason != docview.DIRECT_EDIT,
                          "direct": c.reason == docview.DIRECT_EDIT,
                          "context": {"before": t[max(0, c.start - context):c.start], "after": t[c.end:c.end + context],
                                      "paragraph": t}})
        first_id = next(iter(texts), None)
        dec = store.decisions(job["id"])
        for n in inserts:
            t = texts.get(n.anchor, "")
            position = "start" if n.anchor == first_id and n.where == "before" else n.where
            items.append({"id": n.id, "kind": "insert", "para_id": n.anchor, "start": 0, "end": 0, "old": "",
                          "new": n.text, "layer": getattr(n, "layer", Layer.STRUCTURE).value,
                          "source": n.source.value,
                          "reason": f"front_matter: {n.role}", "status": n.status.value, "format": None,
                          "provenance": n.provenance,
                          "rule_key": n.reason if n.reason != "front_matter" else f"front_matter:{n.role}",
                          "where": n.where, "position": position, "edited": dec.get(n.id, {}).get("new_text") is not None,
                          "runs": [[t, f] for t, f in n.runs] if dec.get(n.id, {}).get("new_text") is None else None,
                          "context": {"before": "", "after": "", "paragraph": t}})
        order = {pid: i for i, pid in enumerate(texts)}
        items.sort(key=lambda d: (order.get(d["para_id"], 10 ** 9), d["start"]))
        layers: dict[str, dict[str, int]] = {}
        for d in items:
            row = layers.setdefault(d["layer"], {"total": 0, "suggested": 0, "accepted": 0, "rejected": 0})
            row["total"] += 1
            row[d["status"]] += 1
        if layer:
            items = [d for d in items if d["layer"] == layer]
        if status:
            items = [d for d in items if d["status"] == status]
        return {"job_id": job["id"], "journal": man["journal"], "layers": layers, "items": items}

    def _second_checks(job_id: str) -> dict[str, dict]:
        """Autopilot's second Claude check of Claude's copy edits, from its saved report (no model call here)."""
        path = _autopilot_path(job_id)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                rep = json.load(f)
        except (OSError, ValueError):
            return {}
        if "undone_at" in rep:
            return {}
        out = {d["id"]: {"verdict": "review", "why": re.sub(r"^second check:?\s*", "", d.get("why", ""))}
               for d in rep.get("left_items", []) if d.get("category") == "ai" and "second check" in d.get("why", "")}
        for item_id, dec in store.decisions(job_id).items():
            if dec.get("editor") == autopilot.EDITOR and dec.get("status") == "accepted":
                out.setdefault(item_id, {"verdict": "agreed", "why": "second check agreed"})
        return out

    @app.get("/api/jobs/{job_id}/rules")
    def list_rules(job: dict = Depends(require_done)):
        """The rule behind each kind of suggestion in the job, keyed by `rule_key` (as in /changes items): a plain
        label and description, and the stylesheet or pre-editing guide line it implements (`quote`, `where`, `line`),
        resolved at runtime from EDITOS_STYLESHEETS / EDITOS_PREEDIT_GUIDE; `quote` is null when none is configured or
        no line matches."""
        _, man, changes, inserts, _ = merged(job["id"])
        keys = {rule_sources.key_for(c.reason, c.provenance) for c in changes.changes}
        keys |= {n.reason if n.reason != "front_matter" else f"front_matter:{n.role}" for n in inserts}
        res = rule_sources.resolver(man["journal"])
        return {"job_id": job["id"], "journal": man["journal"], "configured": rule_sources.configured(),
                "rules": {k: res.rule(k) for k in sorted(keys)}}

    @app.get("/api/jobs/{job_id}/queries")
    def list_queries(job: dict = Depends(require_done)):
        _, _, _, _, qdicts = merged(job["id"])
        texts = paragraphs(job["id"])
        out = []
        for q in qdicts:
            t = texts.get(q["para_id"], "")
            if q.get("insert_id"):  # on a front-matter paragraph Edit OS inserts
                out.append({**q, "anchor": q.get("quote", ""), "paragraph": q.get("quote", "")})
                continue
            out.append({**q, "anchor": t[q["start"]:q["end"]] if q["end"] > q["start"] else "",
                        "paragraph": t})
        return {"job_id": job["id"], "items": out}

    @app.post("/api/jobs/{job_id}/decisions")
    def decide(body: Decision, job: dict = Depends(require_done), x_editor: str | None = Header(None)):
        _, _, changes, inserts, qdicts = merged(job["id"])
        kinds = {c.id: "change" for c in changes.changes} | {n.id: "insert" for n in inserts} | \
                {q["key"]: "query" for q in qdicts}
        unknown = [i for i in body.ids if i not in kinds]
        if unknown:
            raise HTTPException(422, f"Unknown ids: {unknown[:10]}")
        ids = list(body.ids)
        if body.layer:
            pending = [c.id for c in changes.changes if c.layer.value == body.layer and c.status == Status.SUGGESTED]
            if body.layer == "structure":
                pending += [n.id for n in inserts if n.status == Status.SUGGESTED]
            ids += [i for i in pending if i not in ids]
        n = store.decide(job["id"], [(i, kinds[i]) for i in ids], body.status, body.editor or _ed(x_editor) or "")
        return {"updated": n, "ids": ids, "status": body.status}

    @app.post("/api/jobs/{job_id}/edit")
    def edit(body: Edit, job: dict = Depends(require_done), x_editor: str | None = Header(None)):
        """Accept a suggestion with the editor's own wording (the original text it replaces stays the same)."""
        _, _, changes, inserts, _ = merged(job["id"])
        kinds = {c.id: "change" for c in changes.changes} | {n.id: "insert" for n in inserts}
        if body.id not in kinds:
            raise HTTPException(422, f"Unknown id {body.id!r}")
        if "\u2029" in body.new or (kinds[body.id] == "change" and "\n" in body.new):
            raise HTTPException(422, "The new text must stay within one paragraph")
        store.edit(job["id"], body.id, kinds[body.id], body.new, body.editor or _ed(x_editor) or "")
        return {"id": body.id, "status": "accepted", "new": body.new}

    @app.get("/api/jobs/{job_id}/document")
    def get_document(job: dict = Depends(require_done)):
        """The manuscript as received, paragraph by paragraph, for the page view: kind (heading with
        level, front matter, list, table cell with table/row/col, caption, reference, body) and runs
        with bold / italic / superscript / subscript. Suggestions come from /changes (same offsets)."""
        outdir = out_dir(storage, job["id"])
        man = pipeline.load_manifest(outdir)
        return {"job_id": job["id"], "journal": man["journal"], "journal_name": man.get("journal_name"),
                "article": man["stem"], **document_view(job["id"])}

    @app.post("/api/jobs/{job_id}/changes", status_code=201)
    def direct_edit(body: DirectEdit, job: dict = Depends(require_done), x_editor: str | None = Header(None)):
        """The editor's own correction typed into a paragraph. Diffed word by word against the paragraph's
        text and stored as accepted editor changes in the job's records (earlier direct edits of the
        paragraph are replaced). 409 when it touches an accepted suggestion or the text is stale;
        pending suggestions it overlaps are set aside (rejected) and listed."""
        editor = body.editor or _ed(x_editor) or ""
        with job_lock(job["id"]):
            outdir = out_dir(storage, job["id"])
            texts = paragraphs(job["id"])
            if body.para_id not in texts:
                raise HTTPException(422, f"Unknown paragraph {body.para_id!r}")
            _, _, merged_changes, _, _ = merged(job["id"])
            try:
                plan = docview.plan_edit(body.para_id, texts[body.para_id], merged_changes.changes, body.base,
                                         body.text, editor)
            except docview.EditInvalid as e:
                raise HTTPException(422, str(e))
            except docview.EditConflict as e:
                raise HTTPException(409, str(e))
            man, raw, inserts, qdicts = pipeline.load_records(outdir)
            gone = {c.id for c in plan.replaced}
            for c in raw.changes:
                if c.id in gone:
                    c.status = Status.REJECTED
            raw.changes += plan.changes
            pipeline.save_records(outdir, changes=raw)
            if gone:  # a decision on an old edit (e.g. undo + redo) must not bring it back
                store.decide(job["id"], [(i, "change") for i in gone], "rejected", editor)
            if plan.superseded:
                store.decide(job["id"], [(c.id, "change") for c in plan.superseded], "rejected", editor)
            storage.save(job["id"])
        return {"para_id": body.para_id, "created": [c.to_dict() for c in plan.changes],
                "replaced": sorted(gone), "superseded": [c.id for c in plan.superseded]}

    def _render(job_id: str, final: bool = False, editor: str | None = None) -> dict:
        """Render with the current decisions; call under the job's lock. `editor` (else whoever decided last,
        autopilot aside) signs the technical editing at the top of the file."""
        outdir, _, changes, inserts, qdicts = merged(job_id)
        pipeline.save_records(outdir, changes, inserts, qdicts)
        editor = editor or store.last_editor(job_id, skip=(autopilot.EDITOR,)) or None
        out = pipeline.render_outputs(outdir, include_pending=not final, editor=editor)
        with open(os.path.join(outdir, _SIGNED), "w", encoding="utf-8") as f:  # what the files were rendered with
            json.dump({"final": final, "editor": editor or ""}, f)
        storage.save(job_id)
        store.update(job_id, summary=compact_summary(out.summary))
        return out.summary

    @app.post("/api/jobs/{job_id}/render")
    def rerender(job: dict = Depends(require_done), final: bool = False, x_editor: str | None = Header(None)):
        """Render tracked/clean copies, query sheet and QC report with the current decisions.
        `final=true` leaves undecided suggestions out of the tracked copy too. The editor (X-Editor, else the
        last one to decide) signs the technical editing at the top of the file."""
        with job_lock(job["id"]):
            return _render(job["id"], final, _ed(x_editor))

    def _autopilot_path(job_id: str) -> str:
        return os.path.join(out_dir(storage, job_id), "_autopilot_report.json")

    @app.post("/api/jobs/{job_id}/autopilot")
    def run_autopilot(job: dict = Depends(require_done)):
        """Safe autopilot (edit_os/autopilot.py): accept the undecided suggestions it is sure of (rules,
        verified references, checkers; Claude's copy edits only when a second Claude check agrees),
        leave the rest pending, re-render and run QC. Decisions are recorded with editor "autopilot"."""
        with job_lock(job["id"]):
            outdir, man, changes, inserts, _ = merged(job["id"])
            dec = store.decisions(job["id"])
            # Undecided: no decision, or put back to pending (an undo); never the editor's own wording.
            todo = [c for c in changes.changes if c.status == Status.SUGGESTED and c.id not in dec
                    or dec.get(c.id, {}).get("status") == "suggested" and dec[c.id].get("new_text") is None]
            todo_ins = [n for n in inserts if n.status == Status.SUGGESTED and n.id not in dec
                        or dec.get(n.id, {}).get("status") == "suggested" and dec[n.id].get("new_text") is None]
            with spend.track() as led:  # the second check's Claude calls go on the article's bill
                p = autopilot.plan(todo, todo_ins, style_mod.load(man["journal"]), texts=paragraphs(job["id"]))
            if led.parts:
                spend.record(out_dir(storage, job["id"]), "autopilot", led)
            kinds = {c.id: "change" for c in todo} | {n.id: "insert" for n in todo_ins}
            store.decide(job["id"], [(i, kinds[i]) for i in p.accept], "accepted", autopilot.EDITOR)
            summary = _render(job["id"])
            rep = autopilot.report(p, {**{c.id: c for c in todo}, **{n.id: n for n in todo_ins}}, summary, db.now())
            with open(_autopilot_path(job["id"]), "w", encoding="utf-8") as f:
                json.dump(rep, f, indent=2, ensure_ascii=False)
            storage.save(job["id"])
        return rep

    @app.get("/api/jobs/{job_id}/autopilot")
    def autopilot_report(job: dict = Depends(require_done)):
        path = _autopilot_path(job["id"])
        if not os.path.exists(path):
            raise HTTPException(404, "Autopilot has not run on this job")
        with open(path, encoding="utf-8") as f:
            rep = json.load(f)
        if "undone_at" in rep:
            raise HTTPException(404, "Autopilot was undone")
        return rep

    @app.post("/api/jobs/{job_id}/autopilot/undo")
    def undo_autopilot(job: dict = Depends(require_done)):
        """Put everything autopilot accepted back to pending and re-render."""
        with job_lock(job["id"]):
            items = store.decided_by(job["id"], autopilot.EDITOR, "accepted")
            store.decide(job["id"], items, "suggested", autopilot.EDITOR)
            ids = [i for i, _ in items]
            summary = _render(job["id"])
            with open(_autopilot_path(job["id"]), "w", encoding="utf-8") as f:  # kept, not deleted: S3 copies stay
                json.dump({"undone_at": db.now(), "undone": len(ids)}, f)
            storage.save(job["id"])
        return {"undone": len(ids), "ids": ids, "pending": summary["changes"]["pending"]}

    @app.get("/api/jobs/{job_id}/qc")
    def qc_report(job: dict = Depends(require_done)):
        outdir = out_dir(storage, job["id"])
        with open(pipeline.file_paths(outdir, pipeline.load_manifest(outdir)["stem"])["qc_json"], encoding="utf-8") as f:
            return json.load(f)

    @app.get("/api/jobs/{job_id}/files/{kind}")
    def download(kind: str, job: dict = Depends(require_done), x_editor: str | None = Header(None)):
        if kind not in DOWNLOADS:
            raise HTTPException(404, f"kind must be one of {sorted(DOWNLOADS)}")
        outdir = out_dir(storage, job["id"])
        editor = _ed(x_editor)
        if editor and kind in ("tracked", "clean"):
            # Signed with the downloading editor's name: a file rendered without one ("... done on <date>:- ???")
            # or by someone else is rendered again, as it was (final or not), before it leaves.
            try:
                with open(os.path.join(outdir, _SIGNED), encoding="utf-8") as f:
                    signed = json.load(f)
            except (OSError, ValueError):
                signed = {"final": False, "editor": ""}
            if signed.get("editor") != editor:
                with job_lock(job["id"]):
                    _render(job["id"], bool(signed.get("final")), editor)
        fkind, media = DOWNLOADS[kind]
        stem = pipeline.load_manifest(outdir)["stem"]
        path = pipeline.file_paths(outdir, stem)[fkind]
        if not os.path.exists(path):
            raise HTTPException(404, "Not rendered yet")
        disposition = "inline" if kind == "qc_report" else "attachment"
        return FileResponse(path, media_type=media, filename=download_name(job.get("input_name"), stem, path),
                            content_disposition_type=disposition)

    # --- submission files (title page, figures, XML, forms, ...) ----------------------------

    def submission_input(job: dict = Depends(require_job)) -> str:
        if not storage.restore(job["id"]):
            raise HTTPException(410, "Job files are no longer available")
        try:
            return input_file(storage, job, settings)
        except FileNotFoundError:
            raise HTTPException(410, "The submission is no longer available")

    def _sub(fn, *args):
        try:
            return fn(*args)
        except (submission_files.NotFound, KeyError):
            raise HTTPException(404, "No such file in the submission")
        except submission_files.Refused as e:
            raise HTTPException(422, str(e))

    @app.get("/api/jobs/{job_id}/submission")
    def submission_list(job: dict = Depends(require_job), src: str = Depends(submission_input)):
        """The files of the submission as received (zip members, or the single .docx): path, name, kind
        (manuscript | title_page | figure | table | xml | metadata | form | cover_letter | other), size, media
        type, how the review screen shows it (`view`), and which one is the manuscript."""
        return {"job_id": job["id"], "input_name": job["input_name"], "files": submission_files.list_files(src)}

    @app.get("/api/jobs/{job_id}/submission/raw/{path:path}")
    def submission_raw(path: str, src: str = Depends(submission_input), original: bool = False):
        """One file, by its exact path in the listing. Images and PDFs inline (TIFF / BMP converted to PNG),
        everything else as an attachment; sandboxed so nothing in it can run on this origin. `original=true`:
        the file exactly as sent, as an attachment."""
        data, media, inline, name = _sub(submission_files.read, src, path, not original)
        disp = "inline" if inline else "attachment"
        ascii_name = re.sub(r'[^\x20-\x7e]|["\\]', "_", name)  # header values are latin-1; filename* has the real one
        headers = {"Content-Disposition": f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
                   "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"}
        if media != "application/pdf":  # Chrome's PDF viewer does not open in a sandboxed document
            headers["Content-Security-Policy"] = "sandbox"
        if not inline and (media.startswith("text/") or "xml" in media or "svg" in media or "javascript" in media):
            media = "application/octet-stream"  # never rendered as a page on this origin
        return Response(data, media_type=media, headers=headers)

    @app.get("/api/jobs/{job_id}/submission/view/{path:path}")
    def submission_view(path: str, src: str = Depends(submission_input)):
        """A viewable form: `docx` with paragraphs like /document, `metadata` with a card parsed from the XML or
        article_metadata.json, or `image` / `pdf` / `download` (fetch /raw)."""
        return _sub(submission_files.view, src, path)

    # --- review screen ----------------------------------------------------------------------

    if settings.web_dir and os.path.isdir(settings.web_dir):
        index = os.path.join(settings.web_dir, "index.html")
        assets = os.path.join(settings.web_dir, "assets")
        if os.path.isdir(assets):
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        @app.get("/review/{rest:path}", response_class=HTMLResponse, include_in_schema=False)
        def spa(rest: str = ""):
            return FileResponse(index, media_type="text/html")

    return app
