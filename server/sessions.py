"""Sign-in for the review screen: named users with passwords (EDITOS_USERS="name:password,...") get a
signed session token that the server accepts wherever it accepts an API key. Stateless: the token is
"s1.<user>.<expiry>.<hmac>", so a restart keeps people signed in and changing the secret signs everyone out."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

PREFIX = "s1."


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _unb64(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def _sig(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def check_password(users: dict[str, str], name: str, password: str) -> bool:
    """Constant-time compare; an unknown name still costs one compare."""
    want = users.get(name.strip().lower())
    ok = hmac.compare_digest((want or "\0").encode(), password.encode())
    return want is not None and ok


def issue(secret: str, user: str, hours: float, now: float | None = None) -> str:
    body = f"{_b64(user)}.{int((now or time.time()) + hours * 3600)}"
    return f"{PREFIX}{body}.{_sig(secret, body)}"


def verify(secret: str, token: str | None, now: float | None = None) -> str | None:
    """The signed-in user, or None for anything forged, malformed or expired."""
    if not secret or not token or not token.startswith(PREFIX):
        return None
    try:
        user, exp, sig = token[len(PREFIX):].split(".")
        if not hmac.compare_digest(sig, _sig(secret, f"{user}.{exp}")) or int(exp) < (now or time.time()):
            return None
        return _unb64(user)
    except ValueError:
        return None
