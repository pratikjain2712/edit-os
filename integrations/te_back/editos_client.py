"""Minimal Edit OS client for the TE dashboard (te_back). Standard library only.

Copy this file into te_back (e.g. te_back/services/editos_client.py). Configure with env vars:

    EDITOS_URL        https://editos.internal.example     (the Edit OS API)
    EDITOS_API_KEY    one of the keys in Edit OS's EDITOS_API_KEYS (from AWS Secrets Manager)
    EDITOS_CALLBACK_SECRET  same value as Edit OS's EDITOS_CALLBACK_SECRET (optional)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Callable

# transport(method, url, headers, body) -> (status, response bytes); swappable for tests.
Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]


def _urllib_transport(timeout: float) -> Transport:
    def send(method, url, headers, body):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    return send


class EditOSError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Edit OS returned {status}: {detail}")
        self.status = status


class EditOSClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 30,
                 transport: Transport | None = None):
        self.base_url = (base_url or os.environ.get("EDITOS_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("EDITOS_API_KEY", "")
        if not self.base_url or not self.api_key:
            raise ValueError("EDITOS_URL and EDITOS_API_KEY must be set")
        self.send = transport or _urllib_transport(timeout)

    def _call(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None) -> dict:
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        status, data = self.send(method, self.base_url + path, headers, body)
        if status >= 400:
            try:
                detail = json.loads(data).get("detail", data[:200])
            except ValueError:
                detail = data[:200]
            raise EditOSError(status, str(detail))
        return json.loads(data)

    def submit_s3(self, bucket: str, key: str, journal: str, external_id: str = "", callback_url: str = "") -> dict:
        """Start a job on a file already in S3 (the dashboard's article zip). Edit OS reads it with its
        own IAM role, so the bucket policy must allow s3:GetObject for that role."""
        body = json.dumps({"bucket": bucket, "key": key, "journal": journal, "external_id": external_id,
                           "callback_url": callback_url}).encode()
        return self._call("POST", "/api/jobs/from-s3", body, "application/json")

    def submit_file(self, filename: str, data: bytes, journal: str, external_id: str = "",
                    callback_url: str = "") -> dict:
        """Start a job by uploading the .docx / .zip bytes."""
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("journal", journal), ("external_id", external_id), ("callback_url", callback_url)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{os.path.basename(filename)}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return self._call("POST", "/api/jobs", b"".join(parts), f"multipart/form-data; boundary={boundary}")

    def status(self, job_id: str) -> dict:
        return self._call("GET", f"/api/jobs/{job_id}")

    def jobs_for(self, external_id: str) -> list[dict]:
        return self._call("GET", f"/api/jobs?external_id={urllib.request.quote(external_id)}")


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check the X-EditOS-Signature header of a completion callback."""
    if not secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[7:])
