"""
Operational plumbing for the HTTP server: Prometheus metrics and
per-principal rate limiting. Both are in-process and dependency-free;
multi-replica deployments typically also limit at the gateway, and
Prometheus scrapes every replica.
"""
from __future__ import annotations

import threading
import time
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field

from mnemosyne.domain.exceptions import MnemosyneError

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


@dataclass
class Metrics:
    """A minimal Prometheus text-format registry. Labels use the route
    *template* (`/api/v1/memories/{memory_id}`), never the raw path, so
    label cardinality stays bounded."""

    requests: dict[tuple[str, str, int], int] = field(default_factory=lambda: defaultdict(int))
    latency_counts: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    latency_sums: dict[tuple[str, str], float] = field(default_factory=lambda: defaultdict(float))
    rate_limited: int = 0
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, method: str, route: str, status: int, seconds: float) -> None:
        with self._lock:
            self.requests[(method, route, status)] += 1
            key = (method, route)
            buckets = self.latency_counts.setdefault(key, [0] * (len(LATENCY_BUCKETS) + 1))
            buckets[bisect_left(LATENCY_BUCKETS, seconds)] += 1
            self.latency_sums[key] += seconds

    def render(self) -> str:
        lines = [
            "# HELP mnemosyne_http_requests_total HTTP requests by method, route and status.",
            "# TYPE mnemosyne_http_requests_total counter",
        ]
        with self._lock:
            for (method, route, status), count in sorted(self.requests.items()):
                lines.append(
                    f'mnemosyne_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}'
                )
            lines += [
                "# HELP mnemosyne_http_request_duration_seconds HTTP request latency.",
                "# TYPE mnemosyne_http_request_duration_seconds histogram",
            ]
            for (method, route), buckets in sorted(self.latency_counts.items()):
                labels = f'method="{method}",route="{route}"'
                cumulative = 0
                for bound, count in zip(LATENCY_BUCKETS, buckets, strict=False):
                    cumulative += count
                    bucket = f'{labels},le="{bound}"'
                    lines.append(f"mnemosyne_http_request_duration_seconds_bucket{{{bucket}}} {cumulative}")
                cumulative += buckets[-1]
                lines.append(f'mnemosyne_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {cumulative}')
                lines.append(f"mnemosyne_http_request_duration_seconds_count{{{labels}}} {cumulative}")
                lines.append(
                    f"mnemosyne_http_request_duration_seconds_sum{{{labels}}} {self.latency_sums[(method, route)]:.6f}"
                )
            lines += [
                "# HELP mnemosyne_rate_limited_total Requests rejected by the rate limiter.",
                "# TYPE mnemosyne_rate_limited_total counter",
                f"mnemosyne_rate_limited_total {self.rate_limited}",
                "# HELP mnemosyne_uptime_seconds Seconds since the process started.",
                "# TYPE mnemosyne_uptime_seconds gauge",
                f"mnemosyne_uptime_seconds {time.time() - self.started_at:.0f}",
            ]
        return "\n".join(lines) + "\n"


class RateLimitedError(MnemosyneError):
    def __init__(self, retry_after: float) -> None:
        super().__init__("rate limit exceeded", details={"retry_after": retry_after})
        self.retry_after = retry_after


class RateLimiter:
    """Token bucket per principal: `per_minute` sustained, with a burst
    of the same size. Thread-safe; 0 disables limiting."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        if self.per_minute <= 0:
            return
        rate = self.per_minute / 60.0
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.per_minute), now))
            tokens = min(float(self.per_minute), tokens + (now - last) * rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                raise RateLimitedError(retry_after=round((1.0 - tokens) / rate, 2))
            self._buckets[key] = (tokens - 1.0, now)
