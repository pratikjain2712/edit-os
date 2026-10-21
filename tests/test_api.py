"""API service with FastAPI's TestClient: upload, background job, decisions, re-render, downloads, auth."""

import io
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from edit_os import docmodel, style
from server.app import create_app
from server.config import Settings
from server.storage import S3Storage

from test_pipeline import TEXT, make_docx, offline_copyedit, offline_style  # noqa: F401  (fixture)

KEY = {"X-API-Key": "test-key"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    orig = style.load
    st = offline_style()
    monkeypatch.setattr(style, "load", lambda code: st if code.lower() == "ijo" else orig(code))
    settings = Settings(data_dir=str(tmp_path / "data"), api_keys=["test-key"], workers=1,
                        public_url="https://editos.example")
    with TestClient(create_app(settings)) as c:
        yield c


def upload(client, tmp_path, name="ms.docx", journal="ijo"):
    path = make_docx(tmp_path / name)
    with open(path, "rb") as f:
        r = client.post("/api/jobs", headers=KEY, data={"journal": journal, "external_id": "IJO_1"},
                        files={"file": (name, f, "application/octet-stream")})
    return r


def wait(client, job_id, headers=KEY, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_auth_required(client, tmp_path):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", headers={"X-API-Key": "wrong"}).status_code == 401
    assert upload(client, tmp_path).status_code == 202
    r = client.post("/api/jobs", data={"journal": "ijo"}, files={"file": ("a.docx", b"x")})
    assert r.status_code == 401


def test_no_keys_configured_refuses(tmp_path):
    with TestClient(create_app(Settings(data_dir=str(tmp_path / "d")))) as c:
        assert c.get("/api/jobs", headers=KEY).status_code == 503


def test_rejects_bad_input(client):
    r = client.post("/api/jobs", headers=KEY, data={"journal": "ijo"}, files={"file": ("a.pdf", b"x")})
    assert r.status_code == 422
    r = client.post("/api/jobs", headers=KEY, data={"journal": "nope"}, files={"file": ("a.docx", b"x")})
    assert r.status_code == 422


def test_job_flow(client, tmp_path):
    r = upload(client, tmp_path)
    assert r.status_code == 202, r.text
    created = r.json()
    job_id = created["id"]
    assert created["review_url"].startswith(f"https://editos.example/review/{job_id}?token=")
    job = wait(client, job_id)
    assert job["status"] == "done", job["error"]
    assert job["summary"]["changes"]["total"] > 0 and job["external_id"] == "IJO_1"
    assert client.get("/api/jobs?external_id=IJO_1", headers=KEY).json()[0]["id"] == job_id

    # Suggestions with paragraph context, grouped by layer.
    ch = client.get(f"/api/jobs/{job_id}/changes", headers=KEY).json()
    assert "language" in ch["layers"]
    colour = next(i for i in ch["items"] if i["old"] == "colour")
    assert colour["status"] == "suggested" and colour["context"]["before"].endswith("The ")
    assert "lesion" in colour["context"]["after"]
    rand = next(i for i in ch["items"] if i["old"] == "randomised")
    qs = client.get(f"/api/jobs/{job_id}/queries", headers=KEY).json()["items"]
    missing = next(q for q in qs if q["check"] == "references.missing_entry")
    assert missing["anchor"] == "[3]"

    # Decisions persist.
    r = client.post(f"/api/jobs/{job_id}/decisions", headers=KEY,
                    json={"ids": [colour["id"]], "status": "accepted", "editor": "te1"})
    assert r.json()["updated"] == 1
    client.post(f"/api/jobs/{job_id}/decisions", headers=KEY, json={"ids": [rand["id"]], "status": "rejected"})
    client.post(f"/api/jobs/{job_id}/decisions", headers=KEY, json={"ids": [missing["key"]], "status": "rejected"})
    assert client.post(f"/api/jobs/{job_id}/decisions", headers=KEY,
                       json={"ids": ["nope"], "status": "accepted"}).status_code == 422
    ch = client.get(f"/api/jobs/{job_id}/changes", headers=KEY).json()
    status = {i["id"]: i["status"] for i in ch["items"]}
    assert status[colour["id"]] == "accepted" and status[rand["id"]] == "rejected"

    # Bulk accept by layer only touches pending ones.
    r = client.post(f"/api/jobs/{job_id}/decisions", headers=KEY, json={"layer": "language", "status": "accepted"})
    assert rand["id"] not in r.json()["ids"]
    ch = client.get(f"/api/jobs/{job_id}/changes?layer=language", headers=KEY).json()
    assert {i["status"] for i in ch["items"]} == {"accepted", "rejected"}

    # Re-render reflects the decisions.
    rep = client.post(f"/api/jobs/{job_id}/render", headers=KEY).json()
    assert rep["queries"]["rejected"] == 1
    r = client.get(f"/api/jobs/{job_id}/files/clean", headers=KEY)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxml")
    p = tmp_path / "clean.docx"
    p.write_bytes(r.content)
    text = docmodel.load(str(p)).paragraphs[2].text
    assert "color of the lesion" in text and "randomised" in text
    comments = zipfile.ZipFile(io.BytesIO(client.get(f"/api/jobs/{job_id}/files/tracked", headers=KEY).content)) \
        .read("word/comments.xml").decode()
    assert "has no entry" not in comments
    for kind in ("queries", "qc_report", "qc_json", "changes"):
        assert client.get(f"/api/jobs/{job_id}/files/{kind}", headers=KEY).status_code == 200, kind
    assert client.get(f"/api/jobs/{job_id}/qc", headers=KEY).json()["qc"]["checks"]


def test_review_token_scoped_to_its_job(client, tmp_path):
    a = upload(client, tmp_path, "a.docx").json()
    b = upload(client, tmp_path, "b.docx").json()
    tok = {"X-Review-Token": a["review_token"]}
    assert wait(client, a["id"], headers=tok)["status"] == "done"
    assert "review_token" not in client.get(f"/api/jobs/{a['id']}", headers=tok).json()
    assert client.get(f"/api/jobs/{a['id']}/changes", headers=tok).status_code == 200
    assert client.get(f"/api/jobs/{a['id']}/files/clean?token={a['review_token']}").status_code == 200
    assert client.get(f"/api/jobs/{b['id']}", headers=tok).status_code == 401
    assert client.get("/api/jobs", headers=tok).status_code == 401
    wait(client, b["id"])


def test_failed_job_reports_error(client, tmp_path):
    r = client.post("/api/jobs", headers=KEY, data={"journal": "ijo"}, files={"file": ("bad.docx", b"not a docx")})
    job = wait(client, r.json()["id"])
    assert job["status"] == "failed" and job["error"]
    assert client.get(f"/api/jobs/{job['id']}/changes", headers=KEY).status_code == 409


class FakeS3:
    def __init__(self):
        self.objects = {}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.objects[(bucket, key)] = open(path, "rb").read()

    def download_file(self, bucket, key, dest):
        with open(dest, "wb") as f:
            f.write(self.objects[(bucket, key)])

    def get_paginator(self, name):
        objs = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for b, k in objs if b == Bucket and k.startswith(Prefix)]}
        return P()


def test_s3_storage_round_trip(tmp_path):
    fake = FakeS3()
    s = S3Storage(str(tmp_path / "a"), "bucket", "eo/", client=fake)
    os.makedirs(s.job_dir("j1") + "/out")
    open(s.job_dir("j1") + "/out/x.json", "w").write("{}")
    s.save("j1")
    assert ("bucket", "eo/j1/out/x.json") in fake.objects
    s2 = S3Storage(str(tmp_path / "b"), "bucket", "eo/", client=fake)
    assert s2.restore("j1") and os.path.exists(s2.job_dir("j1") + "/out/x.json")
    assert not s2.restore("missing")
