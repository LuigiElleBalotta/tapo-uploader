"""Local accounts: password hashing and a small in-memory limiter for login attempts."""
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LENGTH = 8
# checked when the user does not exist, so an unknown name takes as long as a wrong password
_DUMMY_HASH = generate_password_hash("not-a-real-password", method="scrypt")


def hash_password(plain):
    if len(plain) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"the password must have at least {MIN_PASSWORD_LENGTH} characters")
    return generate_password_hash(plain, method="scrypt")


def verify_password(plain, hashed):
    return check_password_hash(hashed or _DUMMY_HASH, plain)


class LoginLimiter:
    """Too many failures for the same (address, username) in a window block further attempts, even with the
    right password. Kept in memory: a restart clears it, which is acceptable for a small trusted group."""

    def __init__(self, max_failures=5, window=60.0, clock=time.time):
        self.max_failures = max_failures
        self.window = window
        self.clock = clock
        self._failures = {}
        self._lock = threading.Lock()

    def _recent(self, key):
        cutoff = self.clock() - self.window
        recent = [stamp for stamp in self._failures.get(key, []) if stamp > cutoff]
        self._failures[key] = recent
        return recent

    def blocked(self, key):
        with self._lock:
            return len(self._recent(key)) >= self.max_failures

    def fail(self, key):
        with self._lock:
            self._recent(key).append(self.clock())

    def reset(self, key):
        with self._lock:
            self._failures.pop(key, None)
