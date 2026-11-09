"""One admin login for the draft site: a scrypt hash in the env and a signed cookie."""

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

# scrypt n=2**14: about 50 ms and 16 MB a check.
_N, _R, _P = 2**14, 8, 1
_MAXMEM = 64 * 1024 * 1024
SESSION_SECONDS = 30 * 24 * 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def hash_password(password: str) -> str:
    # ':' rather than '$' as a separator: docker compose would expand '$' in .env.
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM)
    return f"scrypt:{_N}:{_R}:{_P}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split(":")
        if scheme != "scrypt":
            return False
        expected = base64.urlsafe_b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.urlsafe_b64decode(salt),
            n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM, dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


class Sessions:
    """Stateless cookies. The password hash is part of the key, so changing the
    password logs everyone out; so does changing the secret."""

    def __init__(self, secret: str, password_hash: str):
        self._key = hashlib.sha256(f"{secret}\0{password_hash}".encode()).digest()

    def _sign(self, expires: int) -> str:
        return hmac.new(self._key, f"v1:{expires}".encode(), hashlib.sha256).hexdigest()

    def issue(self, now: float | None = None) -> str:
        expires = int(now if now is not None else time.time()) + SESSION_SECONDS
        return f"{expires}.{self._sign(expires)}"

    def valid(self, token: str | None, now: float | None = None) -> bool:
        if not token or "." not in token:
            return False
        expires, signature = token.split(".", 1)
        if not expires.isdigit():
            return False
        if int(expires) < (now if now is not None else time.time()):
            return False
        return hmac.compare_digest(signature, self._sign(int(expires)))


class Throttle:
    """Failed logins in a sliding window, per client address and in total. The total
    cap stops guessing spread over many addresses; it locks the owner out for a
    while too, which is acceptable for a one-person admin."""

    def __init__(self, per_ip: int = 10, total: int = 100, window: float = 900):
        self.per_ip, self.total, self.window = per_ip, total, window
        self._failures: deque[tuple[float, str]] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._failures and self._failures[0][0] < now - self.window:
            self._failures.popleft()

    def allowed(self, ip: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._prune(now)
            mine = sum(1 for _, who in self._failures if who == ip)
            return mine < self.per_ip and len(self._failures) < self.total

    def failed(self, ip: str, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._prune(now)
            self._failures.append((now, ip))
