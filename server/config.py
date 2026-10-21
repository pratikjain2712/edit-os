"""Settings from the environment. No secrets in code: API keys, the callback secret and AWS
credentials come from the environment (in AWS: Secrets Manager -> ECS task environment, and the
task's IAM role for S3)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass
class Settings:
    data_dir: str = "./data"              # SQLite job database + per-job working folders
    api_keys: list[str] = field(default_factory=list)  # X-API-Key values accepted (service-to-service, admins)
    auth_disabled: bool = False           # local development only
    workers: int = 2                      # in-process pipeline threads
    max_upload_mb: int = 50
    public_url: str = ""                  # where the review screen is reachable, for review links
    web_dir: str = ""                     # built review screen (web/dist); served at / when present
    s3_bucket: str = ""                   # when set, job inputs and outputs are kept in S3 too
    s3_prefix: str = "edit-os/"
    aws_region: str = ""
    callback_secret: str = ""             # signs completion callbacks (HMAC-SHA256)
    stages: tuple[str, ...] = ("style", "copyedit")

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_web = os.path.join(here, "web", "dist")
        return cls(
            data_dir=env.get("EDITOS_DATA_DIR", "./data"),
            api_keys=_list(env.get("EDITOS_API_KEYS")),
            auth_disabled=env.get("EDITOS_AUTH_DISABLED", "") == "1",
            workers=int(env.get("EDITOS_WORKERS", "2")),
            max_upload_mb=int(env.get("EDITOS_MAX_UPLOAD_MB", "50")),
            public_url=env.get("EDITOS_PUBLIC_URL", "").rstrip("/"),
            web_dir=env.get("EDITOS_WEB_DIR", default_web if os.path.isdir(default_web) else ""),
            s3_bucket=env.get("EDITOS_S3_BUCKET", ""),
            s3_prefix=env.get("EDITOS_S3_PREFIX", "edit-os/"),
            aws_region=env.get("AWS_REGION", env.get("AWS_DEFAULT_REGION", "")),
            callback_secret=env.get("EDITOS_CALLBACK_SECRET", ""),
            stages=tuple(_list(env.get("EDITOS_STAGES")) or ("style", "copyedit")),
        )
