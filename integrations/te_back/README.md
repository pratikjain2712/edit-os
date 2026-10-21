# Edit OS <-> TE dashboard (te_back)

Goal: when an article moves to `assigned_TE`, Edit OS starts editing it automatically, and the
assigned TE gets a "Review in Edit OS" link next to the article. This folder holds the code te_back
would add; te_back itself is not modified here.

## Flow

```
te_back                                   Edit OS
-------                                   -------
article.status = 'assigned_TE'  --+
db.session.commit()               |
start_edit_os_jobs(article_ids) --+--> POST /api/jobs/from-s3
                                  |      {bucket, key: article.s3_key, journal: article.journal_code,
                                  |       external_id: article.article_id, callback_url}
EditOSJob(article_id, job_id,  <--+    202 {id, status, review_url (with per-job token)}
          review_url) saved
                                        worker: S3 GetObject -> pipeline.edit -> outputs to S3
POST /api/editos/callback  <------------ {job_id, external_id, status, qc_passed, review_url}
  (X-EditOS-Signature: HMAC)
TE dashboard: GET /api/editos/articles/<article_id>  ->  {status, reviewUrl}
  "Review in Edit OS" button opens reviewUrl (the review screen; token scoped to that one job)
```

If the callback cannot reach te_back, te_back can poll `GET /api/jobs/{job_id}` (or
`GET /api/jobs?external_id=<article_id>`) with its API key.

## Files

- `editos_client.py` – standard-library client: `submit_s3`, `submit_file`, `status`, `jobs_for`,
  `verify_signature` for callbacks. Copy to `te_back/services/editos_client.py`.
- `editos_hook.py` – `define_model(db)` (the `editos_jobs` table), `start_edit_os_jobs(...)` (called after
  the status change) and `make_blueprint(...)` (callback + review-link routes). Copy to
  `te_back/services/editos_hook.py`.

## Proposed patch to te_back

```diff
--- a/models.py
+++ b/models.py
@@ (end of file)
+from services.editos_hook import define_model
+EditOSJob = define_model(db)
```

```diff
--- a/services/te_assignment_service.py
+++ b/services/te_assignment_service.py
@@ def create_te_assignment_from_folder_upload(...)   (after the 'assigned_TE' loop)
             # Mark assignment as completed
             assignment.assignment_status = 'completed'
             self.db.session.commit()
+
+            # Start Edit OS on the newly assigned articles (never blocks or fails the assignment)
+            try:
+                from services.editos_hook import start_edit_os_jobs
+                from models import EditOSJob
+                start_edit_os_jobs(article_ids, self.ArticleFile, EditOSJob, self.db)
+            except Exception as e:
+                logger.warning(f"Edit OS hook failed: {e}")
@@ def complete_te_assignment(self, assignment_id)   (same, after its commit)
+            try:
+                from services.editos_hook import start_edit_os_jobs
+                from models import EditOSJob
+                start_edit_os_jobs(assignment.article_ids, self.ArticleFile, EditOSJob, self.db)
+            except Exception as e:
+                logger.warning(f"Edit OS hook failed: {e}")
```

```diff
--- a/app.py
+++ b/app.py
+from services.editos_hook import make_blueprint
+from models import EditOSJob
+app.register_blueprint(make_blueprint(EditOSJob, db, require_login=<te_back's JWT decorator>))
```

The review-link route must only answer the TE assigned to the article (`article.assigned_to ==
current_user.id`) or an admin; wrap it with te_back's existing permission decorator
(`utils/permissions.py`).

Migration: one table, `editos_jobs` (see `define_model`), created like `migrate_qc_tables.py` does.

Frontend (te_frontend): on the TE dashboard row, call `GET /api/editos/articles/<articleId>` and show
"Edit OS: running / Review / failed"; the Review button opens `reviewUrl` in a new tab.

## Configuration in te_back (environment, from Secrets Manager — never in code)

| Variable | Value |
|---|---|
| `EDITOS_ENABLED` | `1` to turn the hook on |
| `EDITOS_URL` | Edit OS API base URL (internal ALB / service URL) |
| `EDITOS_API_KEY` | one of Edit OS's `EDITOS_API_KEYS` |
| `EDITOS_CALLBACK_URL` | `https://<te_back>/api/editos/callback` |
| `EDITOS_CALLBACK_SECRET` | same value as in Edit OS |
| `AWS_S3_BUCKET` | already used by te_back (`services/s3_service.py`) |

Edit OS reads the article zip with its own IAM role: add `s3:GetObject` for that role on the
dashboard bucket's article prefix. No credentials are passed between the systems.

## Notes

- `journal_code` is sent lower-cased; Edit OS maps article-id prefixes such as `Neurol-India` to its
  style files (`edit_os/style.py: ALIASES`) and rejects unknown codes with 422 (logged, assignment
  unaffected).
- One job per article: re-assigning to a different TE keeps the existing review; failed jobs are retried
  on the next assignment.
- The TE's Word workflow is unchanged: the tracked .docx, clean .docx and query sheet are downloadable
  from the review screen (or `GET /api/jobs/{id}/files/{tracked|clean|queries|qc_report}`), and could be
  copied back to the article's S3 folder by te_back on the `done` callback.
- Committed credentials in te_back / te_frontend must be rotated before any of this goes live; see
  `docs/deploy-aws.md`.
