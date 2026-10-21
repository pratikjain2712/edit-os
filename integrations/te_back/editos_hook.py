"""The te_back side of the integration: what runs when articles move to "assigned_TE".

Proposed placement (see README.md): call `start_edit_os_jobs(article_ids, ...)` in
services/te_assignment_service.py right after the loops that set `article.status = 'assigned_TE'`
and commit (both in the folder-upload path and in `complete_te_assignment`), and register
`editos_bp` (the callback route) in app.py. Nothing here may break an assignment: every failure is
logged and the article stays assigned as before.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from .editos_client import EditOSClient, EditOSError, verify_signature

log = logging.getLogger("editos")


def define_model(db):
    """The one new table te_back needs (create it with a migration like migrate_qc_tables.py)."""

    class EditOSJob(db.Model):
        __tablename__ = "editos_jobs"
        id = db.Column(db.Integer, primary_key=True)
        article_id = db.Column(db.String(50), db.ForeignKey("article_files.article_id"), index=True, nullable=False)
        job_id = db.Column(db.String(32), unique=True, nullable=False)
        status = db.Column(db.String(20), default="queued", nullable=False)  # queued | running | done | failed
        review_url = db.Column(db.Text)   # carries a per-job review token: show it to the assigned TE only
        qc_passed = db.Column(db.Boolean)
        error = db.Column(db.Text)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

        def to_dict(self):
            return {"articleId": self.article_id, "jobId": self.job_id, "status": self.status,
                    "reviewUrl": self.review_url, "qcPassed": self.qc_passed, "error": self.error}

    return EditOSJob


def start_edit_os_jobs(article_ids, ArticleFile, EditOSJob, db, client: EditOSClient | None = None,
                       callback_url: str | None = None) -> list[str]:
    """Submit each assigned article to Edit OS once; returns the job ids created."""
    if os.environ.get("EDITOS_ENABLED", "0") != "1":
        return []
    try:
        client = client or EditOSClient()
    except ValueError as e:
        log.warning("Edit OS not configured: %s", e)
        return []
    bucket = os.environ.get("AWS_S3_BUCKET", "")
    callback_url = callback_url or os.environ.get("EDITOS_CALLBACK_URL", "")
    created = []
    for article_id in article_ids:
        article = ArticleFile.query.filter_by(article_id=article_id).first()
        if article is None or article.status != "assigned_TE" or not article.s3_key or not article.journal_code:
            continue
        if EditOSJob.query.filter_by(article_id=article_id).filter(EditOSJob.status != "failed").first():
            continue  # already submitted (re-assignment to another TE keeps the same review)
        try:
            job = client.submit_s3(bucket, article.s3_key, article.journal_code.lower(),
                                   external_id=article_id, callback_url=callback_url)
        except (EditOSError, OSError) as e:
            log.warning("Edit OS submit failed for %s: %s", article_id, e)
            continue
        db.session.add(EditOSJob(article_id=article_id, job_id=job["id"], status=job["status"],
                                 review_url=job.get("review_url")))
        created.append(job["id"])
    db.session.commit()
    return created


def make_blueprint(EditOSJob, db, require_login=None):
    """Flask blueprint: the completion callback, and the TE's link to the review screen."""
    from flask import Blueprint, abort, jsonify, redirect, request

    bp = Blueprint("editos", __name__, url_prefix="/api/editos")

    @bp.post("/callback")
    def callback():
        body = request.get_data()
        if not verify_signature(os.environ.get("EDITOS_CALLBACK_SECRET", ""), body,
                                request.headers.get("X-EditOS-Signature")):
            abort(401)
        data = json.loads(body)
        job = EditOSJob.query.filter_by(job_id=data.get("job_id")).first_or_404()
        job.status, job.error, job.qc_passed = data.get("status"), data.get("error"), data.get("qc_passed")
        if data.get("review_url"):
            job.review_url = data["review_url"]
        db.session.commit()
        return jsonify(ok=True)

    def review(article_id):
        job = EditOSJob.query.filter_by(article_id=article_id).order_by(EditOSJob.id.desc()).first_or_404()
        if request.args.get("redirect") == "1" and job.review_url:
            return redirect(job.review_url)
        return jsonify(job.to_dict())

    # te_back's own auth decorator (e.g. the JWT check used by te_dashboard_routes) must wrap this:
    # only the TE assigned to the article (or an admin) may receive the review URL.
    view = require_login(review) if require_login else review
    bp.add_url_rule("/articles/<article_id>", "review", view, methods=["GET"])
    return bp
