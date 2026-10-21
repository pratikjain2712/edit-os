"""Where job files live.

Each job has a local working folder (`<data_dir>/jobs/<id>/`, with `input/` and `out/`); the
pipeline always runs on local files. `Storage` keeps a durable copy:

  LocalStorage   the working folder is the durable copy (single host, docker volume)
  S3Storage      inputs and outputs are uploaded to s3://BUCKET/PREFIX<job id>/... after every run
                 and re-render, and downloaded again when a container without them needs them

`fetch_s3_object` reads an input straight from another bucket (the dashboard's), for jobs created
from an S3 key. boto3 takes credentials from the environment / the task's IAM role.
"""

from __future__ import annotations

import os

from .config import Settings


class LocalStorage:
    kind = "local"

    def __init__(self, root: str):
        self.root = root

    def job_dir(self, job_id: str) -> str:
        return os.path.join(self.root, "jobs", job_id)

    def save(self, job_id: str) -> None:
        """Persist the job folder (already durable for local storage)."""

    def restore(self, job_id: str) -> bool:
        """Make sure the job folder is present locally; False when there is nothing to restore."""
        return os.path.isdir(self.job_dir(job_id))


class S3Storage(LocalStorage):
    kind = "s3"

    def __init__(self, root: str, bucket: str, prefix: str = "edit-os/", region: str = "", client=None):
        super().__init__(root)
        self.bucket, self.prefix = bucket, prefix
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=region or None)
        self.s3 = client

    def _key(self, job_id: str, rel: str) -> str:
        return f"{self.prefix}{job_id}/{rel.replace(os.sep, '/')}"

    def save(self, job_id: str) -> None:
        base = self.job_dir(job_id)
        for dirpath, _, files in os.walk(base):
            for name in files:
                path = os.path.join(dirpath, name)
                self.s3.upload_file(path, self.bucket, self._key(job_id, os.path.relpath(path, base)),
                                    ExtraArgs={"ServerSideEncryption": "AES256"})

    def restore(self, job_id: str) -> bool:
        base = self.job_dir(job_id)
        if os.path.isdir(os.path.join(base, "out")):
            return True
        prefix = self._key(job_id, "")
        found = False
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                dest = os.path.join(base, obj["Key"][len(prefix):])
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                self.s3.download_file(self.bucket, obj["Key"], dest)
                found = True
        return found


def fetch_s3_object(bucket: str, key: str, dest: str, region: str = "", client=None) -> str:
    if client is None:
        import boto3

        client = boto3.client("s3", region_name=region or None)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    client.download_file(bucket, key, dest)
    return dest


def make_storage(settings: Settings) -> LocalStorage:
    if settings.s3_bucket:
        return S3Storage(settings.data_dir, settings.s3_bucket, settings.s3_prefix, settings.aws_region)
    return LocalStorage(settings.data_dir)

