import threading
import time


class QpsRateLimiter:
    """Simple QPS limiter: ensures at least 1/qps seconds between calls."""
    def __init__(self, qps: float = 4.0):
        self.min_interval = 1.0 / qps if qps > 0 else 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last = time.monotonic()

_limiters: dict[str, QpsRateLimiter] = {}


def get_rate_limiter(name: str, qps: float = 4.0) -> QpsRateLimiter:
    limiter = _limiters.get(name)
    if limiter is None:
        limiter = QpsRateLimiter(qps)
        _limiters[name] = limiter
    return limiter