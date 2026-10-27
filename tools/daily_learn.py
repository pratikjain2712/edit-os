"""Daily learning: evaluate newly completed articles against the editors' files and propose what to change.

Run every afternoon by a scheduled Claude Code cloud session (fresh container, this repository checked out):

    python tools/daily_learn.py run [--journals ijo,jehp] [--max-new 30] [--backfill] [--dry-run]
                                    [--local-dir DIR] [--prefix learning/] [--workers 4] [--heldout-size 5]
    python tools/daily_learn.py apply-safe [--actions REPORT.json]

`run`:
1. lists the EditInk dashboard's articles (credentials: AWS Secrets Manager `edit-os/env`, keys
   EDITINK_DASHBOARD_USER / EDITINK_DASHBOARD_PASSWORD, else the same environment variables) and stores every new
   completed article that has the style editor's file (te.docx) in S3, server-side encrypted:
   s3://$EDITOS_S3_BUCKET/<prefix>articles/<id>/{original.zip,te.docx,ce.docx,meta.json}. The first run without
   --backfill only takes the last 7 days; --backfill stores (and queues) every past completed article.
   --local-dir DIR reads <DIR>/<id>/{original.zip,te.docx,ce.docx[,meta.json]} instead of the dashboard;
2. evaluates, in parallel processes, up to --max-new queued articles (journal priority: config `journals`, then
   newest first) and the fixed per-journal held-out set (<prefix>heldout.json; picked once per journal, never used
   for learning) with the repository's own tools: tools/evaluate.py (style closure, joined paragraphs),
   tools/eval_final.py (rule precision vs ce.docx, bigram closure of text / references / front matter),
   tools/eval_refs.py, tools/eval_frontmatter.py, tools/eval_format.py (the editors' formatting), the QC report
   (qc.check incl. content.*) and tools/defect_scan.py (defects of the rendered files themselves: headings not bold,
   queries on deleted text, paragraphs lost, tracked-change residue, ...). Results: <prefix>results/<id>.json;
3. learns (tools/learn.py; deterministic): autopilot hold / release candidates per journal from rule precision over
   the last `window` articles, editor-only edit patterns as new-rule proposals (never applied), stylesheet rules
   nearly all editors go against as owner questions, regressions against the previous run;
4. writes the report: <prefix>reports/<date>.md and .json (and reports/latest.json), the per-journal metrics
   history <prefix>history/<journal>.json, the state <prefix>state.json (which article was evaluated with which
   commit), and prints the markdown. --dry-run reads S3 but writes nothing there (the report goes to --out).

The pipeline's caches (Claude answers, PubMed / CrossRef lookups, LanguageTool) are SQLite files under
EDITOS_CACHE; they are restored from and saved to <prefix>cache/ so a fresh container does not pay for the same
Claude calls twice (--no-cache-sync to skip). LanguageTool is started when Java is available (--no-languagetool).

`apply-safe` applies only the autopilot hold / release patch of a report (default: <prefix>reports/latest.json) to
edit_os/styles/<journal>.yaml `autopilot.hold`, then runs the test suite; on failure the files are restored and the
exit code is 1. The scheduled session commits, pushes and deploys only when it exits 0. Holds this tool added are
listed in a marker comment in the style file; only those are ever released. Rules stay on when held.

Manuscripts are client-confidential: nothing here prints or reports manuscript text beyond masked snippets of at
most 8 words; credentials are never printed. See docs/daily_learning.md.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import glob
import gzip
import hashlib
import http.cookiejar
import json
import os
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import learn  # noqa: E402

SECRET_ID, REGION = "edit-os/env", "ap-south-1"
DASHBOARD = "https://dashboard.editink.in"
CONFIG = os.path.join(HERE, "daily_learn.yaml")
CACHE_FILES = ("ai_cache.sqlite", "http_cache.sqlite", "claude_copyedit.sqlite", "autopilot.sqlite",
               "languagetool_cache.sqlite", "journals.sqlite")


def log(msg: str) -> None:
    print(f"[daily_learn {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def load_config() -> dict:
    import yaml

    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def journal_of(article_id: str) -> str:
    import re

    from edit_os import style as style_mod

    return style_mod.resolve(re.split(r"[_-]", article_id)[0].lower())


# --- secrets ----------------------------------------------------------------------------------------

def load_secrets() -> dict:
    """The `edit-os/env` secret, with environment variables as the fallback for missing keys. Never printed."""
    sec: dict = {}
    try:
        import boto3

        sm = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", REGION))
        sec = json.loads(sm.get_secret_value(SecretId=SECRET_ID)["SecretString"])
    except Exception as e:  # noqa: BLE001 - fall back to the environment
        log(f"secret {SECRET_ID} not read ({type(e).__name__}); using environment variables")
    for k in ("EDITINK_DASHBOARD_USER", "EDITINK_DASHBOARD_PASSWORD", "EDITOS_ANTHROPIC_API_KEY",
              "EDITOS_S3_BUCKET"):
        if not sec.get(k) and os.environ.get(k):
            sec[k] = os.environ[k]
    if sec.get("EDITOS_ANTHROPIC_API_KEY") and not os.environ.get("EDITOS_ANTHROPIC_API_KEY"):
        os.environ["EDITOS_ANTHROPIC_API_KEY"] = sec["EDITOS_ANTHROPIC_API_KEY"]  # for the pipeline's Claude steps
    return sec


# --- S3 ----------------------------------------------------------------------------------------------

class Store:
    """<bucket>/<prefix>...; every object written server-side encrypted. `readonly`: writes are skipped."""

    def __init__(self, bucket: str, prefix: str, readonly: bool = False):
        import boto3

        self.s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", REGION))
        self.bucket, self.prefix, self.readonly = bucket, prefix.rstrip("/") + "/", readonly

    def key(self, path: str) -> str:
        return self.prefix + path

    def get_bytes(self, path: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self.key(path))["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def get_json(self, path: str, default=None):
        b = self.get_bytes(path)
        return json.loads(b) if b is not None else default

    def put_bytes(self, path: str, body: bytes, content_type: str = "application/octet-stream") -> None:
        if self.readonly:
            return
        self.s3.put_object(Bucket=self.bucket, Key=self.key(path), Body=body, ContentType=content_type,
                           ServerSideEncryption="AES256")

    def put_json(self, path: str, obj) -> None:
        self.put_bytes(path, json.dumps(obj, indent=1, ensure_ascii=False).encode(), "application/json")

    def put_file(self, path: str, local: str) -> None:
        if self.readonly:
            return
        self.s3.upload_file(local, self.bucket, self.key(path), ExtraArgs={"ServerSideEncryption": "AES256"})

    def get_file(self, path: str, local: str) -> bool:
        try:
            self.s3.download_file(self.bucket, self.key(path), local)
            return True
        except Exception:  # noqa: BLE001 - missing object
            return False

    def head(self, path: str) -> dict | None:
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=self.key(path))
        except Exception:  # noqa: BLE001
            return None


# --- sources: the dashboard, or a local folder --------------------------------------------------------

FILES = (("original.zip", "file_link"), ("te.docx", "te_upload_link"), ("ce.docx", "ce_update_link"))


class Dashboard:
    """The EditInk dashboard API: cookie login, the article overview, file downloads."""

    def __init__(self, user: str, password: str, base: str = DASHBOARD):
        ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get(
            "CURL_CA_BUNDLE")
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                                  urllib.request.HTTPSHandler(context=ctx))
        req = urllib.request.Request(self.base + "/api/auth/login", method="POST",
                                     data=json.dumps({"username": user, "password": password}).encode(),
                                     headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=60) as r:
            if r.status != 200:
                raise RuntimeError(f"dashboard login failed: HTTP {r.status}")

    def overview(self) -> list[dict]:
        with self.opener.open(self.base + "/api/reports/article-overview", timeout=120) as r:
            return json.loads(r.read())["data"]

    def fetch(self, rec: dict, dest: str) -> list[str]:
        """Download the article's files into `dest`; the names fetched."""
        os.makedirs(dest, exist_ok=True)
        got = []
        for name, field in FILES:
            key = rec.get(field)
            if not key:
                continue
            url = self.base + "/api/storage/view/" + urllib.parse.quote(key)
            with self.opener.open(url, timeout=300) as r, open(os.path.join(dest, name), "wb") as f:
                shutil.copyfileobj(r, f)
            got.append(name)
        return got


class LocalSource:
    """A folder of <id>/{original.zip, te.docx, ce.docx[, meta.json]} standing in for the dashboard."""

    def __init__(self, root: str):
        self.root = root

    def overview(self) -> list[dict]:
        out = []
        for d in sorted(glob.glob(os.path.join(self.root, "*"))):
            if not os.path.isdir(d):
                continue
            aid = os.path.basename(d.rstrip("/"))
            meta = {}
            if os.path.exists(os.path.join(d, "meta.json")):
                with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                    meta = json.load(f)
            has = lambda n: os.path.exists(os.path.join(d, n))  # noqa: E731
            out.append({**meta, "article_id": aid, "status": meta.get("status", "completed"),
                        "file_link": "original.zip" if has("original.zip") else None,
                        "te_upload_link": "te.docx" if has("te.docx") else None,
                        "ce_update_link": "ce.docx" if has("ce.docx") else None})
        return out

    def fetch(self, rec: dict, dest: str) -> list[str]:
        os.makedirs(dest, exist_ok=True)
        got = []
        for name, _ in FILES:
            src = os.path.join(self.root, rec["article_id"], name)
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(dest, name))
                got.append(name)
        return got


def article_date(rec: dict) -> str:
    return str(rec.get("completion_date") or rec.get("ce_update_date") or rec.get("te_upload_date")
               or rec.get("processed_date") or "")[:10]


# --- evaluating one article (in a worker process) ------------------------------------------------------

def _usage() -> dict:
    from edit_os import ai_refs, autopilot, frontmatter
    from edit_os.copyedit import claude_pass

    calls = ai_refs.USAGE["calls"] + frontmatter.USAGE["calls"] + autopilot.USAGE["calls"] + sum(
        u["calls"] for u in claude_pass.USAGE.values())
    tin = ai_refs.USAGE["input_tokens"] + frontmatter.USAGE["input_tokens"] + autopilot.USAGE["input_tokens"]
    tout = ai_refs.USAGE["output_tokens"] + frontmatter.USAGE["output_tokens"] + autopilot.USAGE["output_tokens"]
    haiku_in, haiku_out = claude_pass.PRICES["claude-haiku-5-5"][:2]
    cost = (tin * haiku_in + tout * haiku_out) / 1e6 + claude_pass.cost()
    return {"calls": calls, "input_tokens": tin + sum(u["input_tokens"] for u in claude_pass.USAGE.values()),
            "output_tokens": tout + sum(u["output_tokens"] for u in claude_pass.USAGE.values()),
            "cost": round(cost, 4)}


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def evaluate_article(adir: str, journal: str, workdir: str, timeout: int = 900) -> dict:
    """Every metric of one article (see learn.RESULT). A step that fails is recorded in `crashes` (its error type
    only: messages can quote the manuscript) and the other steps still run."""
    import eval_final
    import eval_format
    import eval_frontmatter
    import eval_refs
    import evaluate
    from edit_os import style as style_mod

    t0, u0 = time.time(), _usage()
    aid = os.path.basename(adir.rstrip("/"))
    res: dict = {"article": aid, "journal": journal, "crashes": []}
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout)

    def step(name, fn):
        try:
            return fn()
        except _Timeout:
            res["crashes"].append({"step": name, "error": "Timeout"})
            raise
        except Exception as e:  # noqa: BLE001 - one broken step must not stop the others
            res["crashes"].append({"step": name, "error": type(e).__name__})
            return None

    has_ce = os.path.exists(os.path.join(adir, "ce.docx"))
    try:
        r = step("evaluate", lambda: evaluate.evaluate(adir, journal, join=True))
        if r:
            res["style"] = r
        od = step("pipeline", lambda: eval_final.run_article(adir, journal, workdir, "autopilot"))
        if od and has_ce:
            stat = collections.defaultdict(lambda: [0, 0, 0])
            bigrams = collections.defaultdict(collections.Counter)
            b = step("eval_final", lambda: eval_final.score_article(adir, od, stat, bigrams))
            if b:
                res["bigrams"] = b
                step("rules", lambda: res.update(_rules(adir, od)))
                step("missed_bigrams", lambda: res.update(missed_bigrams=_missed_bigrams(adir, od)))
        if od:
            step("patterns", lambda: res.update(patterns=_patterns(adir, od)))
            step("qc", lambda: res.update(qc=_qc(od)))
            step("defects", lambda: res.update(defects=_defects(od, aid, journal)))
        rows = step("eval_format", lambda: eval_format.article_rows(adir, journal))
        if rows:
            fe: dict = {}
            for row in rows:
                if row["side"] == "editor":
                    v = fe.setdefault(f"{row['label']}@{row['ctx']}", [0, 0])
                    v[0] += 1
                    v[1] += bool(row["hit"])
            res["format_editor"] = fe
        st = style_mod.load(journal)
        rf = step("eval_refs", lambda: eval_refs.score_article(adir, st))
        if rf is not None:
            res["refs"] = {k: rf[k] for k in ("refs", "dOT", "dUT", "exact_before", "exact_after")}
        got = step("eval_frontmatter", lambda: eval_frontmatter.article_fields(adir, journal))
        if got:
            ed, ours = got
            res["front"] = {f: [1, int(ours.get(f, "") == ed[f]), eval_frontmatter.similarity(ours.get(f, ""), ed[f])]
                            for f in eval_frontmatter.FIELDS if f in ed}
    except _Timeout:
        pass
    finally:
        signal.alarm(0)
    u1 = _usage()
    res["usage"] = {k: round(u1[k] - u0[k], 4) for k in u1}
    res["seconds"] = round(time.time() - t0, 1)
    return res


def _rules(adir: str, od: str) -> dict:
    """Rule precision against the final file, keyed by the autopilot hold key: every suggestion as if accepted
    (`rules`) and those autopilot accepted (`rules_autopilot`), with masked examples."""
    import eval_final
    from edit_os import pipeline
    from edit_os.changes import Status

    ce = eval_final.final_text(open(os.path.join(adir, "ce.docx"), "rb").read())
    ce_joined = eval_final._nz("¶ " + " ¶ ".join(ce) + " ¶")
    texts = pipeline.paragraph_texts(pipeline.source_path(od))
    _, cs, _, _ = pipeline.load_records(od)
    out = {"rules": {}, "rules_autopilot": {}, "examples": {}}
    by_para: dict = {}
    for c in cs.changes:
        if c.status in (Status.ACCEPTED, Status.SUGGESTED):
            by_para.setdefault(c.para_id, []).append(c)
    for pid, changes in by_para.items():
        for c, verdict, old_s, new_s in eval_final.judge_in_ours(changes, texts.get(pid, ""), ce_joined):
            key = learn.hold_key(c.reason)
            i = ("right", "wrong", "unclear").index(verdict)
            out["rules"].setdefault(key, [0, 0, 0])[i] += 1
            if c.status == Status.ACCEPTED:
                out["rules_autopilot"].setdefault(key, [0, 0, 0])[i] += 1
            ex = out["examples"].setdefault(key, {"right": [], "wrong": []})
            if verdict in ex and len(ex[verdict]) < 2:
                ex[verdict].append(learn.change_snippet(old_s, new_s))
    return out


def _missed_bigrams(adir: str, od: str, top: int = 30) -> dict:
    """Word bigrams of the final file that the editors added (not in the original) and our clean copy lacks."""
    import eval_final
    from edit_os import pipeline

    ce = eval_final.final_text(open(os.path.join(adir, "ce.docx"), "rb").read())
    orig = eval_final.plain_paras(pipeline.source_path(od))
    ours = eval_final.plain_paras(glob.glob(os.path.join(od, "*_clean.docx"))[0])
    g_ce, g_o, g_u = eval_final.grams(ce), eval_final.grams(orig), eval_final.grams(ours)
    missed = (g_ce - g_u) & (g_ce - g_o)
    masked = collections.Counter()
    for (a, b), n in missed.items():
        masked[f"{learn.mask_token(a)} {learn.mask_token(b)}"] += n
    return dict(masked.most_common(top))


def _patterns(adir: str, od: str) -> dict:
    """Editor text edits (te.docx, paragraph by paragraph) we did not make: {key: [missed, applied, kept, example]}."""
    import evaluate
    from edit_os import pipeline
    from edit_os.changes import Status

    _, cs, _, _ = pipeline.load_records(od)
    texts = pipeline.paragraph_texts(pipeline.source_path(od))
    by_text: dict = {}
    for pid, t in texts.items():
        by_text.setdefault(t.strip(), []).append(pid)
    accepted = [c for c in cs.changes if c.status == Status.ACCEPTED]
    out: dict = {}
    with open(os.path.join(adir, "te.docx"), "rb") as f:
        pairs = evaluate.tracked_pairs(f.read(), join=True)
    for before, after in pairs:
        ids = by_text.get(before.strip())
        if not before.strip() or not after.strip() or not ids:
            continue
        pid = ids.pop(0) if len(ids) > 1 else ids[0]
        o = texts[pid]
        u = evaluate.apply_text(o, [c for c in accepted if c.para_id == pid])
        for key, v in learn.paragraph_patterns(o.strip(), after.strip(), u.strip()).items():
            cur = out.setdefault(key, [0, 0, 0, None])
            for i in range(3):
                cur[i] += v[i]
            if cur[3] is None and v[0]:
                cur[3] = learn.pattern_label(key)
    return {k: v for k, v in out.items() if v[1]}


def _qc(od: str) -> dict:
    from edit_os import pipeline

    man = pipeline.load_manifest(od)
    with open(pipeline.file_paths(od, man["stem"])["qc_json"], encoding="utf-8") as f:
        q = json.load(f)["qc"]
    return {"passed": q["passed"], "errors": q["errors"], "warnings": q["warnings"],
            "failed": {c["check"]: c["severity"] for c in q["checks"] if not c["passed"]}}


def _defects(od: str, aid: str, journal: str) -> dict:
    """Output defects of the rendered files (tools/defect_scan.py): {check: [findings, severity]}, the number of
    error findings, and up to two masked locations per check."""
    import defect_scan

    found = defect_scan.scan(od, aid, journal)
    checks: dict = {}
    examples: dict = {}
    for f in found:
        cur = checks.setdefault(f.check, [0, f.severity])
        cur[0] += 1
        if f.severity == defect_scan.ERROR:
            cur[1] = defect_scan.ERROR
        ex = examples.setdefault(f.check, [])
        if len(ex) < 2 and f.loc:
            ex.append(f"p{f.para} {f.loc}" if f.para is not None else f.loc)
    return {"errors": sum(f.severity == defect_scan.ERROR for f in found), "checks": checks, "examples": examples}


def _worker(args):
    adir, journal, workdir, timeout = args
    try:
        return evaluate_article(adir, journal, workdir, timeout)
    except Exception as e:  # noqa: BLE001
        return {"article": os.path.basename(adir), "journal": journal,
                "crashes": [{"step": "worker", "error": type(e).__name__}]}


# --- caches -------------------------------------------------------------------------------------------

def cache_path(name: str) -> str:
    """Where the pipeline keeps a cache file: the reference caches next to the journal database (EDIT_OS_DATA),
    the copy-edit / autopilot ones under EDITOS_CACHE (both ~/.cache/edit-os by default)."""
    from edit_os import journals
    from edit_os.copyedit.cache import cache_dir

    if name in ("ai_cache.sqlite", "http_cache.sqlite", "journals.sqlite"):
        os.makedirs(os.path.dirname(journals.DEFAULT_DB), exist_ok=True)
        return os.path.join(os.path.dirname(journals.DEFAULT_DB), name)
    return cache_dir(name)


def restore_caches(store: Store) -> dict[str, float]:
    """Fetch the pipeline's SQLite caches from S3 where the container has none; {file: mtime} to detect changes."""
    for name in CACHE_FILES:
        local = cache_path(name)
        if os.path.exists(local):
            continue
        gz = local + ".gz"
        if store.get_file(f"cache/{name}.gz", gz):
            with gzip.open(gz, "rb") as fi, open(local, "wb") as fo:
                shutil.copyfileobj(fi, fo)
            os.remove(gz)
            log(f"cache restored: {name}")
    return {n: os.path.getmtime(cache_path(n)) for n in CACHE_FILES if os.path.exists(cache_path(n))}


def save_caches(store: Store, before: dict[str, float]) -> None:
    for name in CACHE_FILES:
        local = cache_path(name)
        if not os.path.exists(local) or before.get(name) == os.path.getmtime(local):
            continue
        with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as tmp:
            with open(local, "rb") as fi, gzip.open(tmp, "wb", compresslevel=5) as fo:
                shutil.copyfileobj(fi, fo)
        store.put_file(f"cache/{name}.gz", tmp.name)
        os.remove(tmp.name)
        log(f"cache saved: {name}")


def start_languagetool():
    from edit_os.copyedit import langtool

    if langtool.is_running() or not shutil.which("java"):
        return None
    try:
        return langtool.start_server()
    except Exception as e:  # noqa: BLE001
        log(f"LanguageTool not started ({type(e).__name__}); copy-edit runs without it")
        return None


# --- run --------------------------------------------------------------------------------------------

def priority(journal: str, order: list[str]) -> int:
    return order.index(journal) if journal in order else len(order)


def pick_heldout(ids: list[str], k: int) -> list[str]:
    """A fixed, reproducible choice: the k ids with the smallest hash."""
    return sorted(ids, key=lambda a: hashlib.sha256(a.encode()).hexdigest())[:k]


def cmd_run(a) -> int:
    t0 = time.time()
    cfg = load_config()
    lcfg = {**learn.DEFAULTS, **(cfg.get("learn") or {})}
    order = [j.lower() for j in cfg.get("journals", [])]
    only = {learn_j.strip().lower() for learn_j in a.journals.split(",")} if a.journals else None
    if only:
        from edit_os import style as style_mod

        only = {style_mod.resolve(j) for j in only}
    sec = load_secrets()
    bucket = a.bucket or sec.get("EDITOS_S3_BUCKET")
    if not bucket:
        log("no S3 bucket (EDITOS_S3_BUCKET in the secret or --bucket)")
        return 2
    store = Store(bucket, a.prefix, readonly=a.dry_run)
    head = commit()
    today = dt.date.today().isoformat()
    state = store.get_json("state.json", {"articles": {}})
    arts = state.setdefault("articles", {})
    heldout = store.get_json("heldout.json", {})
    cache_mtimes = {} if a.no_cache_sync else restore_caches(store)

    # 1. new articles from the dashboard (or a local folder) into S3
    if a.local_dir:
        source = LocalSource(a.local_dir)
    elif sec.get("EDITINK_DASHBOARD_USER") and sec.get("EDITINK_DASHBOARD_PASSWORD"):
        source = Dashboard(sec["EDITINK_DASHBOARD_USER"], sec["EDITINK_DASHBOARD_PASSWORD"])
    else:
        source = None
        log("no dashboard credentials (EDITINK_DASHBOARD_USER / _PASSWORD); evaluating what S3 already holds")
    stored = 0
    work = tempfile.mkdtemp(prefix="daily_learn_")
    if source is not None:
        listing = source.overview()
        first = not arts
        cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
        for rec in listing:
            aid = rec.get("article_id")
            if not aid or rec.get("status") != "completed" or not rec.get("te_upload_link") or not rec.get("file_link"):
                continue
            j = journal_of(aid)
            if only and j not in only:
                continue
            cur = arts.get(aid)
            if cur and not (a.backfill and cur.get("status") == "baseline"):
                continue
            if first and not a.backfill and not a.local_dir and article_date(rec) < cutoff:
                arts[aid] = {"journal": j, "date": article_date(rec), "status": "baseline"}
                continue
            dest = os.path.join(work, "src", aid)
            try:
                names = source.fetch(rec, dest)
            except Exception as e:  # noqa: BLE001
                log(f"{aid}: download failed ({type(e).__name__})")
                continue
            for n in names:
                store.put_file(f"articles/{aid}/{n}", os.path.join(dest, n))
            store.put_json(f"articles/{aid}/meta.json", rec)
            arts[aid] = {"journal": j, "date": article_date(rec), "status": "queued", "files": names,
                         "stored_at": today}
            stored += 1
        log(f"{stored} new article(s) stored")

    def local(aid: str) -> str | None:
        """The article's folder in the work dir (from the source download or from S3)."""
        d = os.path.join(work, "src", aid)
        if os.path.exists(os.path.join(d, "te.docx")):
            return d
        os.makedirs(d, exist_ok=True)
        ok = True
        for n in arts[aid].get("files") or ["original.zip", "te.docx", "ce.docx"]:
            ok &= store.get_file(f"articles/{aid}/{n}", os.path.join(d, n)) or n == "ce.docx"
        return d if ok else None

    # 2. held-out sets (fixed once per journal) and today's queue
    by_j = collections.defaultdict(list)
    for aid, s in arts.items():
        if s.get("status") != "baseline" and (not only or s["journal"] in only):
            by_j[s["journal"]].append(aid)
    k = a.heldout_size
    for j, ids in by_j.items():
        if j not in heldout and len(ids) >= 2 * k:
            heldout[j] = pick_heldout(ids, k)
            log(f"held-out set for {j}: {len(heldout[j])} article(s)")
    held_ids = {x for ids in heldout.values() for x in ids}
    for aid in held_ids:
        if aid in arts:
            arts[aid]["status"] = "heldout"
    queue = [aid for aid, s in arts.items() if s.get("status") == "queued" and aid not in held_ids
             and (not only or s["journal"] in only)]
    queue.sort(key=lambda x: (priority(arts[x]["journal"], order), _neg(arts[x].get("date") or "")))
    queue = queue[:a.max_new]
    held_today = []
    reuse: dict = {}
    for j, ids in heldout.items():
        if only and j not in only:
            continue
        for aid in ids:
            prev = store.get_json(f"results/{aid}.json")
            if prev and prev.get("commit") == head and not a.reeval_heldout:
                reuse[aid] = dict(prev, reused=True)
            else:
                held_today.append(aid)

    # 3. evaluate in parallel
    lt = None if a.no_languagetool else start_languagetool()
    jobs = []
    for aid in queue + held_today:
        d = local(aid)
        if d:
            jobs.append((d, arts[aid]["journal"], os.path.join(work, "out"), a.timeout))
        else:
            log(f"{aid}: files missing in S3")
    log(f"evaluating {len(queue)} new + {len(held_today)} held-out article(s) ({len(reuse)} held-out reused) "
        f"with {a.workers} worker(s)")
    results: dict = dict(reuse)
    with cf.ProcessPoolExecutor(max_workers=a.workers) as ex:
        for r in ex.map(_worker, jobs):
            aid = r["article"]
            r.update(commit=head, date=arts[aid].get("date"), evaluated_at=dt.datetime.now().isoformat(timespec="seconds"),
                     heldout=aid in held_ids)
            results[aid] = r
            store.put_json(f"results/{aid}.json", r)
            arts[aid].update(commit=head, evaluated_at=today)
            if aid not in held_ids:
                arts[aid]["status"] = "done"
            log(f"{aid}: {r.get('seconds', 0)}s" + (f", crashed: {[c['step'] for c in r['crashes']]}"
                                                     if r.get("crashes") else ""))
    if lt is not None:
        lt.terminate()

    # 4. learn and report
    rep = build_report(store, cfg, lcfg, order, arts, heldout, results, queue, head, today, only)
    rep.update(stored_count=stored, seconds=round(time.time() - t0, 1),
               queued_count=sum(1 for s in arts.values() if s.get("status") == "queued"))
    md = learn.render_markdown(rep)
    rep["markdown"] = md
    store.put_bytes(f"reports/{today}.md", md.encode(), "text/markdown; charset=utf-8")
    store.put_json(f"reports/{today}.json", rep)
    store.put_json("reports/latest.json", rep)
    for j, js in rep["journals"].items():
        hist = store.get_json(f"history/{j}.json", [])
        hist = [h for h in hist if h.get("date") != today] + [{"date": today, "commit": head,
                                                                "heldout_ids": heldout.get(j, []),
                                                                "heldout": js.get("heldout"), "new": js.get("new"),
                                                                "window": js.get("window")}]
        store.put_json(f"history/{j}.json", hist[-365:])
    state["heldout_size"] = k
    store.put_json("heldout.json", heldout)
    state["last_run"] = {"date": today, "commit": head}
    store.put_json("state.json", state)
    if not a.no_cache_sync:
        save_caches(store, cache_mtimes)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, f"{today}.json"), "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        with open(os.path.join(a.out, f"{today}.md"), "w", encoding="utf-8") as f:
            f.write(md)
    shutil.rmtree(work, ignore_errors=True)
    print(md)
    return 0


def _neg(date: str) -> str:
    """Sort key: newest first."""
    return "".join(chr(255 - ord(ch)) for ch in date)


def build_report(store, cfg, lcfg, order, arts, heldout, results, queue, head, today, only) -> dict:
    from edit_os import style as style_mod

    pinned_cfg = cfg.get("pinned") or {}
    rep: dict = {"date": today, "commit": head, "prefix": store.prefix, "config": lcfg, "journals": {},
                 "autopilot": {}, "proposals": [], "owner_questions": [], "regressions": [], "crashes": [],
                 "qc_blocked": [], "safe_actions": {"autopilot": {}}}
    usage = collections.Counter()
    for r in results.values():
        if not r.get("reused"):
            usage.update(r.get("usage") or {})
    rep["usage"] = {k: round(v, 4) for k, v in usage.items()}
    journals = sorted({s["journal"] for s in arts.values() if s.get("status") == "done"} | set(heldout)
                      | {r["journal"] for r in results.values()})
    if only:
        journals = [j for j in journals if j in only]
    held_ids = {x for ids in heldout.values() for x in ids}
    for j in journals:
        js: dict = {"priority": priority(j, order)}
        h = [results[x] for x in heldout.get(j, []) if x in results]
        new = [results[x] for x in queue if x in results and results[x]["journal"] == j]
        js["heldout"] = learn.journal_metrics(h) if h else None
        js["new"] = learn.journal_metrics(new) if new else None
        hist = store.get_json(f"history/{j}.json", [])
        prev = next((e for e in reversed(hist) if e.get("date") != today and e.get("heldout")), None)
        js["heldout_previous"] = prev.get("heldout") if prev else None
        if js["heldout"]:
            rep["regressions"] += learn.regressions(js["heldout"], js["heldout_previous"], j, lcfg,
                                                    same_set=bool(prev) and prev.get("heldout_ids") == heldout.get(j))
        # the learning window: the latest evaluated articles of the journal, held-out excluded
        done = [aid for aid, s in arts.items() if s["journal"] == j and s.get("status") == "done"
                and aid not in held_ids]
        done.sort(key=lambda x: (arts[x].get("date") or "", arts[x].get("evaluated_at") or ""))
        win = []
        for aid in done[-lcfg["window"]:]:
            r = results.get(aid) or store.get_json(f"results/{aid}.json")
            if r:
                win.append(r)
        js["window"] = learn.journal_metrics(win) if win else None
        rep["journals"][j] = js
        if not win:
            continue
        stats = learn.rule_stats(win)
        examples: dict = {}
        for r in win:
            for key, ex in (r.get("examples") or {}).items():
                e = examples.setdefault(key, {"right": [], "wrong": []})
                for v in ("right", "wrong"):
                    e[v] = (e[v] + ex.get(v, []))[:2]
        path = os.path.join(style_mod.STYLES_DIR, f"{j}.yaml")
        hold = list(style_mod.load(j).get("autopilot.hold") or [])
        learner = learn.learner_holds(open(path, encoding="utf-8").read()) if os.path.exists(path) else []
        ap = learn.autopilot_decisions(stats, hold, learner, pinned_cfg.get(j, []), lcfg, examples)
        rep["autopilot"][j] = ap
        if (ap["add"] or ap["release"]) and os.path.exists(path):
            rep["safe_actions"]["autopilot"][j] = {
                "file": os.path.relpath(path, ROOT), "add": ap["add"], "release": ap["release"],
                "hold_before": hold, "hold_after": learn.new_hold(hold, ap["add"], ap["release"]),
                "learner_after": [x for x in learner if x not in ap["release"]] + ap["add"]}
        rep["proposals"] += learn.proposals(win, j, lcfg)
        rep["owner_questions"] += learn.owner_questions(stats, j, lcfg)
    rep["proposals"].sort(key=lambda p: (priority(p["journal"], order), -p["articles"], -p["missed"]))
    for aid, r in results.items():
        for c in r.get("crashes") or []:
            rep["crashes"].append({"article": aid, **c})
        if aid in queue and r.get("qc") and not r["qc"].get("passed"):
            rep["qc_blocked"].append({"article": aid, "checks": sorted(c for c, s in r["qc"]["failed"].items()
                                                                       if s == "error")})
    rep["new_count"] = sum(1 for x in queue if x in results)
    rep["heldout_count"] = sum(1 for x in results if x in held_ids)
    rep["crash_count"] = sum(1 for r in results.values() if r.get("crashes"))
    if rep["crash_count"]:
        rep["regressions"].append({"kind": "crash", "what": f"{rep['crash_count']} article(s) with a crashed step "
                                   "(see Crashes)"})
    return rep


# --- apply-safe -----------------------------------------------------------------------------------------

def apply_actions(actions: dict, styles_dir: str | None = None) -> dict[str, tuple[str, str]]:
    """Apply the autopilot hold / release patch; {path: (old text, new text)} of the files changed. The change is
    recomputed against the current file (the owner may have edited it since the report)."""
    import yaml

    from edit_os import style as style_mod

    sd = styles_dir or style_mod.STYLES_DIR
    changed = {}
    for j, act in (actions.get("autopilot") or {}).items():
        path = os.path.join(sd, f"{j}.yaml")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        base_path = os.path.join(sd, "base.yaml")
        with open(base_path, encoding="utf-8") as f:
            base = yaml.safe_load(f) or {}
        own = (yaml.safe_load(text) or {}).get("autopilot") or {}
        hold = list(own.get("hold") if "hold" in own else (base.get("autopilot") or {}).get("hold") or [])
        learner = learn.learner_holds(text)
        add = [x for x in act.get("add", []) if not learn.held(x, hold)]
        release = [x for x in act.get("release", []) if x in learner]  # only holds this tool added
        if not add and not release:
            continue
        new_hold = learn.new_hold(hold, add, release)
        new_learner = [x for x in learner if x not in release] + add
        new = learn.patch_hold_yaml(text, new_hold, new_learner)
        check = (yaml.safe_load(new) or {}).get("autopilot", {}).get("hold")
        if check != new_hold:
            raise RuntimeError(f"{j}: patched hold list does not read back")
        changed[path] = (text, new)
    for path, (_, new) in changed.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
    return changed


def cmd_apply_safe(a) -> int:
    if a.actions:
        with open(a.actions, encoding="utf-8") as f:
            rep = json.load(f)
    else:
        sec = load_secrets()
        rep = Store(a.bucket or sec["EDITOS_S3_BUCKET"], a.prefix).get_json("reports/latest.json")
        if not rep:
            log("no report in S3")
            return 2
    actions = rep.get("safe_actions", rep)
    changed = apply_actions(actions)
    if not changed:
        print("apply-safe: nothing to change")
        return 0
    for path in changed:
        print(f"apply-safe: patched {os.path.relpath(path, ROOT)}")
    if a.no_tests:
        return 0
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x"], cwd=ROOT)
    if r.returncode != 0:
        for path, (old, _) in changed.items():
            with open(path, "w", encoding="utf-8") as f:
                f.write(old)
        print("apply-safe: tests failed; style files restored")
        return 1
    print("apply-safe: tests green")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="store new articles, evaluate, learn, report")
    r.add_argument("--journals", help="comma-separated journal codes (default: all)")
    r.add_argument("--max-new", type=int, default=30, help="new articles evaluated per run")
    r.add_argument("--backfill", action="store_true", help="store and queue every past completed article")
    r.add_argument("--dry-run", action="store_true", help="write nothing to S3 (see --out)")
    r.add_argument("--local-dir", help="read articles from DIR/<id>/ instead of the dashboard")
    r.add_argument("--prefix", default="learning/")
    r.add_argument("--bucket")
    r.add_argument("--workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    r.add_argument("--heldout-size", type=int, default=5)
    r.add_argument("--reeval-heldout", action="store_true", help="evaluate the held-out set even at the same commit")
    r.add_argument("--timeout", type=int, default=900, help="seconds per article")
    r.add_argument("--no-cache-sync", action="store_true")
    r.add_argument("--no-languagetool", action="store_true")
    r.add_argument("--out", help="also write the report (.md, .json) into this local folder")
    s = sub.add_parser("apply-safe", help="apply the autopilot hold / release patch and run the tests")
    s.add_argument("--actions", help="a report JSON (default: <prefix>reports/latest.json in S3)")
    s.add_argument("--prefix", default="learning/")
    s.add_argument("--bucket")
    s.add_argument("--no-tests", action="store_true")
    a = ap.parse_args(argv)
    return cmd_run(a) if a.cmd == "run" else cmd_apply_safe(a)


if __name__ == "__main__":
    sys.exit(main())
