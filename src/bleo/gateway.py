"""Gateway health-aware rotation and failover for bleo.

Modeled on pi-freeflow-rebase's relay-state.ts / relay.ts:

* Sticky primary gateway stays first until it errors, then rolls to next healthy
* Status-code-specific cooldowns (429=90s, 504=60s, 5xx=45s, socket=30s, 402=6h)
* Consecutive-failure escalation (up to 4x base cooldown)
* EWMA latency tracking with challenger promotion ahead of sticky head
* Healthy gateways first, cooling gateways at the tail
* Hedged failover: fire next candidate after HEDGE_GRACE_MS if no headers
* Disk persistence to ~/.bleo/gateway-state.json

Zero third-party dependencies — pure Python standard library.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── Cooldown defaults (milliseconds) ──────────────────────────────────
COOLDOWN_SOCKET_MS = 30_000      # socket / network / 502 / 503
COOLDOWN_429_MS = 90_000         # rate limit
COOLDOWN_504_MS = 60_000         # gateway timeout
COOLDOWN_5XX_MS = 45_000         # other server errors
COOLDOWN_402_MS = 6 * 60 * 60 * 1000  # disabled deployment (~6 hours)
MAX_RETRY_AFTER_MS = 6 * 60 * 60 * 1000  # cap for Retry-After values

# ── EWMA latency tracking ─────────────────────────────────────────────
EWMA_ALPHA = 0.2
EWMA_FLOOR = 5                   # samples needed before a gateway is "measured"
EWMA_MARGIN_MS = 100
EWMA_MARGIN_RATIO = 0.15

# ── Hedged failover ───────────────────────────────────────────────────
HEDGE_GRACE_MS = 8000            # fire next candidate if no headers within 8s
MAX_ESCALATION_MULTIPLIER = 4    # consecutive-failure backoff cap

# ── Disk persistence ──────────────────────────────────────────────────
STATE_DIR = Path.home() / ".bleo"
STATE_FILE = STATE_DIR / "gateway-state.json"


def _now() -> float:
    return time.time()


@dataclass
class GatewayHealth:
    """Per-gateway health state, mirroring pi-freeflow-rebase RelayHealth."""

    url: str
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_failure_time: float = 0.0
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    failure_count: int = 0
    recovering: bool = False
    last_latency_ms: Optional[float] = None
    ewma_ms: Optional[float] = None
    ewma_samples: int = 0
    success_count: int = 0
    last429_at: Optional[float] = None

    def is_cooled_down(self, now: float) -> bool:
        return now >= self.cooldown_until

    def record_success(self, latency_ms: Optional[float] = None) -> None:
        """Clear failure state and record a successful request."""
        self.consecutive_failures = 0
        self.cooldown_until = 0.0
        self.last_failure_time = 0.0
        self.last_status = None
        self.last_error = None
        self.recovering = False
        self.success_count += 1
        if latency_ms is not None:
            sample = round(latency_ms)
            if self.ewma_ms is None:
                self.ewma_ms = sample
            else:
                self.ewma_ms = round(EWMA_ALPHA * sample + (1 - EWMA_ALPHA) * self.ewma_ms)
            self.ewma_samples += 1
            self.last_latency_ms = sample

    def record_failure(self, status: Optional[int], error: Optional[str],
                       retry_after_ms: float = 0.0) -> None:
        """Mark a failure with status-code-specific cooldown and escalation."""
        now = _now()
        self.consecutive_failures += 1
        self.last_failure_time = now
        self.last_status = status
        self.last_error = error
        self.failure_count += 1
        self.recovering = True

        if status == 429:
            cooldown_ms = COOLDOWN_429_MS
        elif status == 504:
            cooldown_ms = COOLDOWN_504_MS
        elif status == 402:
            cooldown_ms = COOLDOWN_402_MS
        elif status is not None and status >= 500:
            cooldown_ms = COOLDOWN_5XX_MS
        else:
            cooldown_ms = COOLDOWN_SOCKET_MS

        multiplier = min(MAX_ESCALATION_MULTIPLIER, self.consecutive_failures)
        effective_retry = min(MAX_RETRY_AFTER_MS, max(0.0, retry_after_ms))
        self.cooldown_until = now + max(cooldown_ms * multiplier, effective_retry) / 1000.0


class GatewayPool:
    """Health-aware gateway rotation with sticky primary, cooldown, and hedged failover."""

    def __init__(
        self,
        gateways: list[str],
        *,
        base_cooldown: float = 30.0,
        max_cooldown: float = 6 * 60 * 60,
        max_failures: int = 10,
        hedge_grace_ms: float = HEDGE_GRACE_MS,
        persist: bool = True,
    ) -> None:
        if not gateways:
            raise ValueError("at least one gateway is required")
        self._gateways = [GatewayHealth(url=g) for g in gateways]
        self._base_cooldown = base_cooldown
        self._max_cooldown = max_cooldown
        self._max_failures = max_failures
        self._hedge_grace_ms = hedge_grace_ms
        self._persist = persist
        self._lock = threading.Lock()
        self._sticky_index = 0
        self._spread_cursor = 0
        self._recent_429_timestamps: list[float] = []
        self._last_429_warn = 0.0
        self._429_burst_window_ms = 60_000
        self._429_burst_threshold = 5

        if persist:

    # ── persistence ──────────────────────────────────────────────────

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            states = data.get("gateways", {})
            for gw in self._gateways:
                if gw.url in states:
                    s = states[gw.url]
                    gw.consecutive_failures = s.get("consecutive_failures", 0)
                    gw.cooldown_until = s.get("cooldown_until", 0.0)
                    gw.last_failure_time = s.get("last_failure_time", 0.0)
                    gw.last_status = s.get("last_status")
                    gw.last_error = s.get("last_error")
                    gw.failure_count = s.get("failure_count", 0)
                    gw.recovering = s.get("recovering", False)
                    gw.last_latency_ms = s.get("last_latency_ms")
                    gw.ewma_ms = s.get("ewma_ms")
                    gw.ewma_samples = s.get("ewma_samples", 0)
                    gw.success_count = s.get("success_count", 0)
                    gw.last429_at = s.get("last429_at")
            self._sticky_index = data.get("sticky_index", 0)
            self._spread_cursor = data.get("spread_cursor", 0)
            self._recent_429_timestamps = data.get("recent_429_timestamps", [])
            self._last_429_warn = data.get("last_429_warn", 0.0)
        except (json.JSONDecodeError, OSError):
            pass

    def _save_state(self) -> None:
        if not self._persist:
            return
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "gateways": {
                    gw.url: {
                        "consecutive_failures": gw.consecutive_failures,
                        "cooldown_until": gw.cooldown_until,
                        "last_failure_time": gw.last_failure_time,
                        "last_status": gw.last_status,
                        "last_error": gw.last_error,
                        "failure_count": gw.failure_count,
                        "recovering": gw.recovering,
                        "last_latency_ms": gw.last_latency_ms,
                        "ewma_ms": gw.ewma_ms,
                        "ewma_samples": gw.ewma_samples,
                        "success_count": gw.success_count,
                        "last429_at": gw.last429_at,
                    }
                    for gw in self._gateways
                },
                "sticky_index": self._sticky_index,
                "spread_cursor": self._spread_cursor,
                "recent_429_timestamps": self._recent_429_timestamps,
                "last_429_warn": self._last_429_warn,
            }
            STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass


    # ── health / selection ───────────────────────────────────────────

    def _is_healthy(self, gw: GatewayHealth, now: float) -> bool:
        return gw.is_cooled_down(now)

    def _ewma_challenger(self, band: list[GatewayHealth]) -> list[GatewayHealth]:
        """Promote a measured challenger ahead of sticky head if significantly faster."""
        if len(band) <= 1:
            return band
        head = band[0]
        rest = band[1:]
        floored = [g for g in rest if (g.ewma_samples or 0) >= EWMA_FLOOR]
        unknown = [g for g in rest if (g.ewma_samples or 0) < EWMA_FLOOR]

        if head.ewma_ms is not None:
            best_challenger = None
            best_ewma = float("inf")
            for g in floored:
                if g.recovering:
                    continue
                if g.ewma_ms is not None and g.ewma_ms < best_ewma:
                    best_ewma = g.ewma_ms
                    best_challenger = g
            if best_challenger is not None:
                margin = max(EWMA_MARGIN_MS, EWMA_MARGIN_RATIO * head.ewma_ms)
                if head.ewma_ms - best_ewma > margin:
                    return [best_challenger, head] + [g for g in rest if g is not best_challenger] + unknown
        return [head] + floored + unknown

    def _ordered(self, now: float) -> list[GatewayHealth]:
        """Return gateways ordered: sticky primary first, healthy next, cooling at tail."""
        gateways = self._gateways
        total = len(gateways)

        raw_ordered = [gateways[(self._sticky_index + i) % total] for i in range(total)]

        healthy = [g for g in raw_ordered if self._is_healthy(g, now)]
        cooling = [g for g in raw_ordered if not self._is_healthy(g, now)]

        healthy = self._ewma_challenger(healthy)

        return healthy + cooling

    def select(self, now: Optional[float] = None) -> Optional[GatewayHealth]:
        """Return the best healthy gateway, or None if all are cooled down."""
        if now is None:
            now = _now()
        ordered = self._ordered(now)
        available = [g for g in ordered if self._is_healthy(g, now)]
        if not available:
            return None
        return available[0]

    def record_success(self, gateway: GatewayHealth, latency_ms: Optional[float] = None) -> None:
        gateway.record_success(latency_ms)
        with self._lock:
            self._save_state()

    def record_failure(self, gateway: GatewayHealth, status: Optional[int],
                       error: Optional[str], retry_after_ms: float = 0.0) -> None:
        gateway.record_failure(status, error, retry_after_ms)
        with self._lock:
            self._sticky_index = (self._sticky_index + 1) % len(self._gateways)
            self._save_state()

            self._load_state()