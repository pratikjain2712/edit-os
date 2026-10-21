"""The te_back client against the real API (TestClient as transport), and callback signatures."""

import hashlib
import hmac
import json

from integrations.te_back.editos_client import EditOSClient, EditOSError, verify_signature
from test_api import client, wait  # noqa: F401  (fixture)
from test_pipeline import make_docx, offline_copyedit  # noqa: F401  (fixture)


def transport_for(tc):
    def send(method, url, headers, body):
        r = tc.request(method, url.replace("http://editos", ""), headers=headers, content=body)
        return r.status_code, r.content
    return send


def test_client_submits_and_polls(client, tmp_path):
    c = EditOSClient("http://editos", "test-key", transport=transport_for(client))
    data = open(make_docx(tmp_path / "IJO_9.docx"), "rb").read()
    job = c.submit_file("IJO_9.docx", data, "IJO", external_id="IJO_9")
    assert job["review_url"].endswith(job["review_token"])
    assert wait(client, job["id"])["status"] == "done"
    assert c.status(job["id"])["summary"]["changes"]["total"] > 0
    assert c.jobs_for("IJO_9")[0]["id"] == job["id"]
    bad = EditOSClient("http://editos", "wrong", transport=transport_for(client))
    try:
        bad.status(job["id"])
        raise AssertionError("expected 401")
    except EditOSError as e:
        assert e.status == 401


def test_callback_signature():
    body = json.dumps({"job_id": "x"}).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature("s3cret", body, sig)
    assert not verify_signature("s3cret", body + b" ", sig)
    assert not verify_signature("s3cret", body, None)
