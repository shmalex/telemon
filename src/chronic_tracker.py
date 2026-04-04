"""
chronic_tracker.py — tracks recurring alert types and classifies them
as ACUTE, CHRONIC, or RESOLVED.

State machine per alert_type:
    ACUTE   → normal: full diagnostic report sent
    CHRONIC → repeated issue: short hourly summary sent, full reports suppressed
    RESOLVED → silence for CHRONIC_RESOLVE_HOURS → reset back to ACUTE

Transitions:
    ACUTE    → CHRONIC   when count >= CHRONIC_THRESHOLD within CHRONIC_WINDOW_HOURS
    CHRONIC  → RESOLVED  when silence >= CHRONIC_RESOLVE_HOURS
    RESOLVED → ACUTE     on next occurrence (automatic)

State is persisted to CHRONIC_STATE_FILE so it survives restarts.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CHRONIC_STATE_FILE    = os.environ.get(
    "CHRONIC_STATE_FILE", "/var/lib/system-monitor/chronic_state.json"
)
CHRONIC_THRESHOLD     = int(os.environ.get("CHRONIC_THRESHOLD",    "5"))   # events before → chronic
CHRONIC_WINDOW_HOURS  = float(os.environ.get("CHRONIC_WINDOW_HOURS", "6")) # window to count events
CHRONIC_RESOLVE_HOURS = float(os.environ.get("CHRONIC_RESOLVE_HOURS", "6"))# silence → resolved
CHRONIC_SUMMARY_INTERVAL = int(os.environ.get("CHRONIC_SUMMARY_INTERVAL", "3600"))  # 1h


@dataclass
class AlertTypeState:
    status:          str   = "acute"   # "acute" | "chronic" | "resolved"
    count_window:    int   = 0         # events in current window
    count_today:     int   = 0         # total events today
    chronic_since:   float = 0.0       # timestamp when went chronic
    last_seen:       float = 0.0       # timestamp of last event
    last_summary:    float = 0.0       # timestamp of last hourly summary
    peak_label:      str   = ""        # e.g. "536 MB/s read"
    window_events:   list  = field(default_factory=list)  # timestamps in window


class ChronicTracker:
    def __init__(self):
        self._states: dict[str, AlertTypeState] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            path = Path(CHRONIC_STATE_FILE)
            if path.exists():
                raw = json.loads(path.read_text())
                for key, data in raw.items():
                    s = AlertTypeState(**data)
                    self._states[key] = s
                log.debug("chronic_tracker: loaded %d states", len(self._states))
        except Exception as exc:
            log.warning("chronic_tracker: could not load state: %s", exc)

    def _save(self) -> None:
        try:
            path = Path(CHRONIC_STATE_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: asdict(v) for k, v in self._states.items()}
            path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            log.warning("chronic_tracker: could not save state: %s", exc)

    # ── state access ─────────────────────────────────────────────────────────

    def _get(self, alert_type: str) -> AlertTypeState:
        if alert_type not in self._states:
            self._states[alert_type] = AlertTypeState()
        return self._states[alert_type]

    # ── main API ─────────────────────────────────────────────────────────────

    def record(self, alert_type: str, peak_label: str = "") -> str:
        """
        Record a new occurrence of alert_type.
        Returns the current status: "acute" | "chronic".

        Call this every time an alert fires. Based on the return value,
        the caller decides whether to run full diagnostics (acute)
        or suppress (chronic).
        """
        now = time.time()
        s   = self._get(alert_type)

        # Auto-resolve: if we were chronic but haven't seen this in a while
        if s.status == "chronic":
            if now - s.last_seen > CHRONIC_RESOLVE_HOURS * 3600:
                self._resolve(alert_type, s, now)

        # Update window: keep only events within CHRONIC_WINDOW_HOURS
        cutoff = now - CHRONIC_WINDOW_HOURS * 3600
        s.window_events = [t for t in s.window_events if t > cutoff]
        s.window_events.append(now)
        s.count_window = len(s.window_events)

        # Update today count (reset at midnight)
        if s.last_seen and _is_new_day(s.last_seen, now):
            s.count_today = 0
        s.count_today += 1

        s.last_seen = now
        if peak_label:
            s.peak_label = peak_label

        # Transition: acute → chronic
        if s.status == "acute" and s.count_window >= CHRONIC_THRESHOLD:
            s.status        = "chronic"
            s.chronic_since = now
            s.last_summary  = now
            log.info("chronic_tracker: '%s' → CHRONIC (%d events in %.0fh)",
                     alert_type, s.count_window, CHRONIC_WINDOW_HOURS)

        self._save()
        return s.status

    def _resolve(self, alert_type: str, s: AlertTypeState, now: float) -> None:
        duration = now - s.chronic_since
        log.info("chronic_tracker: '%s' → RESOLVED (lasted %.0f min, %d events)",
                 alert_type, duration / 60, s.count_today)
        s.status       = "resolved"   # caller checks and resets to acute
        s.chronic_since = 0.0

    def get_transition_message(self, alert_type: str) -> str | None:
        """
        Returns a Telegram message if a state transition just happened,
        else None. Call right after record().
        """
        s = self._get(alert_type)

        if s.status == "chronic" and s.last_seen == s.chronic_since:
            # just went chronic this call
            duration_h = CHRONIC_WINDOW_HOURS
            return (
                f"⚠️ Chronic issue detected: *{alert_type}*\n"
                f"Occurred {s.count_window}× in the last {duration_h:.0f}h.\n"
                f"Peak: {s.peak_label or '—'}\n"
                f"Switching to summary mode — full reports suppressed.\n"
                f"Hourly summary will continue until 6h of silence."
            )

        if s.status == "resolved":
            duration = time.time() - (s.last_seen + CHRONIC_RESOLVE_HOURS * 3600)
            msg = (
                f"✅ Chronic issue resolved: *{alert_type}*\n"
                f"Total events today: {s.count_today}\n"
                f"Peak: {s.peak_label or '—'}\n"
                f"Next occurrence will be treated as a new incident."
            )
            # reset to acute
            self._states[alert_type] = AlertTypeState()
            self._save()
            return msg

        return None

    def get_hourly_summaries(self) -> list[tuple[str, str]]:
        """
        Returns list of (alert_type, message) for all chronic types
        that are due for an hourly summary. Call every monitoring cycle.
        """
        now     = time.time()
        results = []

        for alert_type, s in list(self._states.items()):
            if s.status != "chronic":
                continue

            # Auto-resolve check
            if now - s.last_seen > CHRONIC_RESOLVE_HOURS * 3600:
                duration_str = _fmt_duration(now - s.chronic_since)
                msg = (
                    f"✅ Chronic issue resolved: *{alert_type}*\n"
                    f"Lasted {duration_str} | Total events today: {s.count_today}\n"
                    f"Peak: {s.peak_label or '—'}\n"
                    f"Next occurrence will be treated as a new incident."
                )
                self._states[alert_type] = AlertTypeState()
                self._save()
                results.append((alert_type, msg))
                continue

            if now - s.last_summary < CHRONIC_SUMMARY_INTERVAL:
                continue

            # Count events in the last hour
            hour_ago     = now - 3600
            events_1h    = sum(1 for t in s.window_events if t > hour_ago)
            ongoing_for  = _fmt_duration(now - s.chronic_since)

            msg = (
                f"📋 Chronic: *{alert_type}*\n"
                f"Last hour: {events_1h} event(s) | Today: {s.count_today}\n"
                f"Peak: {s.peak_label or '—'} | Ongoing for: {ongoing_for}"
            )
            s.last_summary = now
            self._save()
            results.append((alert_type, msg))

        return results

    def is_chronic(self, alert_type: str) -> bool:
        return self._get(alert_type).status == "chronic"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_new_day(ts_old: float, ts_new: float) -> bool:
    from datetime import datetime
    return datetime.fromtimestamp(ts_old).date() != datetime.fromtimestamp(ts_new).date()


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}min {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}min"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_tracker: ChronicTracker | None = None


def get_tracker() -> ChronicTracker:
    global _tracker
    if _tracker is None:
        _tracker = ChronicTracker()
    return _tracker
