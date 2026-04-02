"""
Telemon — Linux background service that monitors system health
and sends alerts to Telegram when issues are detected.

Monitors:
  - Disk space
  - RAM usage
  - CPU usage
  - Swap usage
  - System load average
  - Disk I/O throughput
  - Systemd service health (nginx, postgresql, …)
  - Docker container health
  - System journal errors (journalctl priority 0–3)

Configuration is loaded from a .env file in the project root.
Alerts are throttled via per-check cooldowns to avoid spamming Telegram.
"""

import json
import logging
import os
import signal
import socket
import statistics
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from io import BytesIO
from pathlib import Path

import matplotlib.pyplot as plt
import psutil
import requests

# ---------------------------------------------------------------------------
# Load .env  (python-dotenv if available, plain parser as fallback)
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path) -> None:
    """Parse KEY=VALUE lines from *path* and populate os.environ."""
    if not path.exists():
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:   # env already wins over .env
                os.environ[key] = value


try:
    from dotenv import load_dotenv as _dotenv_load
    _dotenv_load(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
HOST_PREFIX = os.environ.get("HOST_PREFIX", "")
HOSTNAME    = HOST_PREFIX + '-' + socket.gethostname() if HOST_PREFIX else socket.gethostname()

CHECK_INTERVAL = _env_int("CHECK_INTERVAL", 10)    # seconds between cycles
ALERT_COOLDOWN = _env_int("ALERT_COOLDOWN", 300)   # seconds before repeating an alert

# Absolute path — correct when running as a systemd service
STATE_FILE = "/var/lib/system-monitor/last_error_time.txt"

# Telegram retry settings
MAX_RETRIES     = 5
INITIAL_BACKOFF = 10   # seconds (doubled each retry)

# Thresholds
DISK_THRESHOLD_GB    = _env_float("DISK_THRESHOLD_GB",    50)
MEMORY_THRESHOLD_PCT = _env_float("MEMORY_THRESHOLD_PCT", 90)
CPU_THRESHOLD_PCT    = _env_float("CPU_THRESHOLD_PCT",    95)
SWAP_THRESHOLD_PCT   = _env_float("SWAP_THRESHOLD_PCT",   80)
LOAD_THRESHOLD       = _env_float("LOAD_THRESHOLD",       10.0)
DISK_IO_READ_MBPS    = _env_float("DISK_IO_READ_MBPS",    200)
DISK_IO_WRITE_MBPS   = _env_float("DISK_IO_WRITE_MBPS",   100)

# Adaptive baseline — alert only when value is SPIKE_MULTIPLIER× above rolling median
# ADAPTIVE_WINDOW samples × CHECK_INTERVAL seconds = history window
# e.g. 30 × 10s = 5 min of history
SPIKE_MULTIPLIER = _env_float("SPIKE_MULTIPLIER", 1.5)
ADAPTIVE_WINDOW  = _env_int("ADAPTIVE_WINDOW",   30)

# Watchdog targets
WATCHED_SERVICES   = _env_list("WATCHED_SERVICES",   "")
WATCHED_CONTAINERS = _env_list("WATCHED_CONTAINERS", "")
WATCHED_PM2        = _env_list("WATCHED_PM2",        "")
PM2_USER           = os.environ.get("PM2_USER", "")

# Journal error filters
# Units in this list are ignored entirely (e.g. ssh.service — mostly bot noise)
IGNORED_UNITS = _env_list(
    "IGNORED_UNITS",
    "ssh.service,sshd.service",
)
# Errors whose MESSAGE contains any of these substrings are silently skipped
IGNORED_PATTERNS = _env_list(
    "IGNORED_PATTERNS",
    ",".join([
        "kex_exchange_identification",
        "Connection reset by peer",
        "Disconnected from invalid user",
        "Disconnected from authenticating user",
        "Invalid user",
        "Failed password",
        "Did not receive identification string",
        "banner exchange: Connection from",
    ]),
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alert cooldown tracker
# ---------------------------------------------------------------------------

_last_alert_times: dict[str, float] = {}
_down_since: dict[str, float] = {}       # key → timestamp when it went down

# Rolling histories for adaptive baseline checks
_load_history:      deque[float] = deque(maxlen=ADAPTIVE_WINDOW)
_disk_io_r_history: deque[float] = deque(maxlen=ADAPTIVE_WINDOW)
_disk_io_w_history: deque[float] = deque(maxlen=ADAPTIVE_WINDOW)


def _adaptive_threshold(history: deque, static_threshold: float) -> float:
    """Return alert threshold that adapts to the recent baseline.

    Requires at least 1/3 of the window filled before adapting.
    Until then falls back to the static threshold.
    Threshold = max(static, rolling_median) * SPIKE_MULTIPLIER
    """
    min_samples = max(5, ADAPTIVE_WINDOW // 3)
    if len(history) < min_samples:
        return static_threshold
    baseline = statistics.median(history)
    return max(static_threshold, baseline) * SPIKE_MULTIPLIER


def _is_on_cooldown(alert_key: str) -> bool:
    return (time.time() - _last_alert_times.get(alert_key, 0)) < ALERT_COOLDOWN


def _mark_alert_sent(alert_key: str) -> None:
    _last_alert_times[alert_key] = time.time()


def _format_downtime(seconds: float) -> str:
    """Return human-readable downtime like '3 min 25s'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins} min {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins} min {secs}s"


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def _telegram_post(url: str, **kwargs) -> bool:
    """POST to the Telegram API with exponential back-off on HTTP 429."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(url, **kwargs, timeout=15)
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                retry_after = (
                    resp.json()
                    .get("parameters", {})
                    .get("retry_after", INITIAL_BACKOFF * (2 ** attempt))
                )
                log.warning(
                    "Telegram rate-limit: waiting %ds (attempt %d/%d)",
                    retry_after, attempt + 1, MAX_RETRIES,
                )
                time.sleep(retry_after)
            else:
                log.error("Telegram error %d: %s", resp.status_code, resp.text[:200])
                return False
        except requests.RequestException as exc:
            log.error("Telegram request exception: %s", exc)
            return False
    log.error("Telegram: gave up after %d retries", MAX_RETRIES)
    return False


def send_message(text: str) -> bool:
    """Send a plain-text message to the configured Telegram chat."""
    body = f"[{HOSTNAME}]\n{text}"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    return _telegram_post(url, json={"chat_id": CHAT_ID, "text": body[:4096]})


def send_message_with_chart(text: str) -> bool:
    """Send a text message followed by the current RAM usage pie chart."""
    if not send_message(text):
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    buf = _build_memory_chart()
    try:
        return _telegram_post(
            url,
            data={"chat_id": CHAT_ID},
            files={"photo": ("memory.png", buf, "image/png")},
        )
    finally:
        buf.close()


def _build_memory_chart() -> BytesIO:
    """Render a RAM usage pie chart and return it as an in-memory PNG."""
    mem     = psutil.virtual_memory()
    used_mb = mem.used  / 1024 / 1024
    free_mb = mem.free  / 1024 / 1024
    total_mb = mem.total / 1024 / 1024

    plt.figure(figsize=(6, 4))
    plt.pie(
        [used_mb, free_mb],
        explode=(0.1, 0),
        labels=["Used", "Free"],
        colors=["#ff9999", "#66b3ff"],
        autopct="%1.1f%%",
        startangle=90,
    )
    plt.title(f"RAM usage — {HOSTNAME}\nTotal: {total_mb:.0f} MB")
    plt.axis("equal")

    buf = BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    return buf


# ---------------------------------------------------------------------------
# Monitors — each returns an alert string or None
# ---------------------------------------------------------------------------

def check_disk_space() -> str | None:
    """Alert when free space on '/' falls below DISK_THRESHOLD_GB."""
    if _is_on_cooldown("disk"):
        return None

    disk    = psutil.disk_usage("/")
    free_gb = disk.free / 1024 ** 3
    if free_gb >= DISK_THRESHOLD_GB:
        return None

    try:
        df_out = subprocess.run(
            ["df", "-h"], capture_output=True, text=True, check=True
        ).stdout
    except subprocess.CalledProcessError as exc:
        df_out = f"(df -h failed: {exc})"

    _mark_alert_sent("disk")
    return (
        f"⚠️ Low disk space!\n"
        f"Free: {free_gb:.1f} GB  |  Threshold: {DISK_THRESHOLD_GB} GB\n\n"
        f"{df_out}"
    )


def check_memory() -> str | None:
    """Alert when RAM usage exceeds MEMORY_THRESHOLD_PCT."""
    if _is_on_cooldown("memory"):
        return None

    mem = psutil.virtual_memory()
    if mem.percent < MEMORY_THRESHOLD_PCT:
        return None

    _mark_alert_sent("memory")
    return (
        f"⚠️ High RAM usage: {mem.percent:.1f}%\n"
        f"Used: {mem.used / 1024**3:.1f} GB  |  "
        f"Total: {mem.total / 1024**3:.1f} GB"
    )


def check_cpu() -> str | None:
    """Alert when CPU usage exceeds CPU_THRESHOLD_PCT."""
    if _is_on_cooldown("cpu"):
        return None

    cpu_pct = psutil.cpu_percent(interval=1)
    if cpu_pct < CPU_THRESHOLD_PCT:
        return None

    load1, load5, load15 = os.getloadavg()
    _mark_alert_sent("cpu")
    return (
        f"🔥 High CPU usage: {cpu_pct:.1f}%\n"
        f"Load avg (1/5/15 min): {load1:.2f} / {load5:.2f} / {load15:.2f}"
    )


def check_swap() -> str | None:
    """Alert when swap usage exceeds SWAP_THRESHOLD_PCT."""
    if _is_on_cooldown("swap"):
        return None

    swap = psutil.swap_memory()
    if swap.total == 0 or swap.percent < SWAP_THRESHOLD_PCT:
        return None

    _mark_alert_sent("swap")
    return (
        f"⚠️ High swap usage: {swap.percent:.1f}%\n"
        f"Used: {swap.used / 1024**3:.2f} GB  |  "
        f"Total: {swap.total / 1024**3:.2f} GB"
    )


def check_load_average() -> str | None:
    """Alert when load average spikes significantly above the rolling baseline."""
    load1, load5, load15 = os.getloadavg()
    _load_history.append(load1)

    threshold = _adaptive_threshold(_load_history, LOAD_THRESHOLD)

    if _is_on_cooldown("load") or load1 < threshold:
        return None

    baseline = statistics.median(_load_history)
    _mark_alert_sent("load")
    return (
        f"🔥 High load average: {load1:.2f}  (logical CPUs: {psutil.cpu_count()})\n"
        f"Load avg (1/5/15 min): {load1:.2f} / {load5:.2f} / {load15:.2f}\n"
        f"Adaptive baseline: {baseline:.1f}  →  threshold: {threshold:.1f}"
    )


# --- Disk I/O ---

_prev_io = None   # psutil.disk_io_counters() snapshot
_prev_io_time: float = 0.0


def check_disk_io() -> str | None:
    """Alert when sustained disk throughput exceeds configured thresholds."""
    global _prev_io, _prev_io_time

    current_io   = psutil.disk_io_counters()
    current_time = time.time()

    if _prev_io is None:
        # First call — store baseline, nothing to compare yet
        _prev_io      = current_io
        _prev_io_time = current_time
        return None

    elapsed = current_time - _prev_io_time
    if elapsed < 1:
        return None

    read_mbps  = (current_io.read_bytes  - _prev_io.read_bytes)  / elapsed / 1024 / 1024
    write_mbps = (current_io.write_bytes - _prev_io.write_bytes) / elapsed / 1024 / 1024

    _prev_io      = current_io
    _prev_io_time = current_time

    _disk_io_r_history.append(read_mbps)
    _disk_io_w_history.append(write_mbps)

    read_threshold  = _adaptive_threshold(_disk_io_r_history, DISK_IO_READ_MBPS)
    write_threshold = _adaptive_threshold(_disk_io_w_history, DISK_IO_WRITE_MBPS)

    if read_mbps < read_threshold and write_mbps < write_threshold:
        return None

    if _is_on_cooldown("disk_io"):
        return None

    r_baseline = statistics.median(_disk_io_r_history)
    w_baseline = statistics.median(_disk_io_w_history)
    _mark_alert_sent("disk_io")
    return (
        f"💾 High disk I/O!\n"
        f"Read:  {read_mbps:.1f} MB/s  (baseline: {r_baseline:.0f}, threshold: {read_threshold:.0f} MB/s)\n"
        f"Write: {write_mbps:.1f} MB/s  (baseline: {w_baseline:.0f}, threshold: {write_threshold:.0f} MB/s)"
    )


# --- Service watchdog ---

def check_services() -> list[str]:
    """Alert when any watched systemd service is not active, notify when it recovers."""
    if not WATCHED_SERVICES:
        return []

    messages = []
    for svc in WATCHED_SERVICES:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", svc],
            capture_output=True,
        )
        key = f"svc:{svc}"
        is_down = result.returncode != 0

        if is_down:
            if key not in _down_since:
                _down_since[key] = time.time()
            if not _is_on_cooldown(key):
                _mark_alert_sent(key)
                messages.append(f"🚨 Service DOWN: {svc}")
        else:
            if key in _down_since:
                duration = _format_downtime(time.time() - _down_since.pop(key))
                _last_alert_times.pop(key, None)   # reset cooldown so next failure alerts immediately
                messages.append(f"✅ Service recovered: {svc}\nWas down ~{duration} (±{CHECK_INTERVAL}s)")

    return messages


# --- Docker container watchdog ---

def check_docker_containers() -> list[str]:
    """Alert when any watched Docker container is not running, notify when it recovers."""
    if not WATCHED_CONTAINERS:
        return []

    messages = []
    for name in WATCHED_CONTAINERS:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
        )
        key = f"docker:{name}"
        is_missing = result.returncode != 0
        is_down = is_missing or result.stdout.strip() != "true"

        if is_down:
            if key not in _down_since:
                _down_since[key] = time.time()
            if not _is_on_cooldown(key):
                _mark_alert_sent(key)
                status = "not found" if is_missing else "stopped"
                messages.append(f"🐳 Container DOWN: {name} ({status})")
        else:
            if key in _down_since:
                duration = _format_downtime(time.time() - _down_since.pop(key))
                _last_alert_times.pop(key, None)
                messages.append(f"✅ Container recovered: {name}\nWas down ~{duration} (±{CHECK_INTERVAL}s)")

    return messages


# --- PM2 process watchdog ---

def check_pm2_processes() -> list[str]:
    """Alert when any watched PM2 process is not online, notify when it recovers."""
    if not WATCHED_PM2 or not PM2_USER:
        return []

    try:
        result = subprocess.run(
            ["su", "-", PM2_USER, "-c", "pm2 jlist"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        processes = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        log.error("pm2 jlist timed out for user %s", PM2_USER)
        return []
    except (json.JSONDecodeError, Exception) as exc:
        log.error("Failed to get PM2 process list for user %s: %s", PM2_USER, exc)
        return []

    status_map = {
        p["name"]: p.get("pm2_env", {}).get("status", "unknown")
        for p in processes
    }

    messages = []
    for name in WATCHED_PM2:
        key = f"pm2:{name}"
        status = status_map.get(name)
        is_down = status is None or status != "online"

        if is_down:
            if key not in _down_since:
                _down_since[key] = time.time()
            if not _is_on_cooldown(key):
                _mark_alert_sent(key)
                detail = "not found in PM2" if status is None else f"status: {status}"
                messages.append(f"⚙️ PM2 process DOWN: {name} ({detail})")
        else:
            if key in _down_since:
                duration = _format_downtime(time.time() - _down_since.pop(key))
                _last_alert_times.pop(key, None)
                messages.append(f"✅ PM2 process recovered: {name}\nWas down ~{duration} (±{CHECK_INTERVAL}s)")

    return messages


# ---------------------------------------------------------------------------
# System journal reader
# ---------------------------------------------------------------------------

def _get_last_timestamp() -> str:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    try:
        return open(STATE_FILE).read().strip()
    except FileNotFoundError:
        log.info("State file not found — will track only new errors from now on")
        return ""


def _save_last_timestamp(ts: str) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        fh.write(ts)


def _is_filtered(unit: str, message: str) -> bool:
    """Return True if this journal entry should be silently skipped."""
    if unit in IGNORED_UNITS:
        return True
    msg_lower = message.lower()
    return any(p.lower() in msg_lower for p in IGNORED_PATTERNS)


def get_journal_errors() -> list[str]:
    """Return new priority-3 (error) journal entries since the last run."""
    last_ts = _get_last_timestamp()
    cmd = ["journalctl", "-b", "-p", "3", "-o", "json"]

    if last_ts:
        try:
            since = datetime.fromtimestamp(int(last_ts) / 1_000_000).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            cmd += ["--since", since]
        except (ValueError, TypeError) as exc:
            log.error("Invalid timestamp in state file (%s): %s", last_ts, exc)
            send_message(f"State file has an invalid timestamp, resetting: {exc}")
            _save_last_timestamp("")
            return []

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        log.error("journalctl failed: %s", exc)
        send_message(f"Failed to read system journal: {exc}")
        return []

    errors: list[str] = []
    latest_ts = last_ts or "0"

    for raw in result.stdout.splitlines():
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue

        ts   = entry.get("__REALTIME_TIMESTAMP", "")
        msg  = entry.get("MESSAGE", "(no message)")
        unit = entry.get("_SYSTEMD_UNIT", "unknown unit")

        if ts and ts > latest_ts:
            latest_ts = ts
            if _is_filtered(unit, msg):
                continue
            when = datetime.fromtimestamp(int(ts) / 1_000_000).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            errors.append(f"🛑 System error [{when}]\nUnit: {unit}\n{msg}")

    if latest_ts and latest_ts != last_ts:
        _save_last_timestamp(latest_ts)

    return errors


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def _handle_shutdown(sig, _frame):
    log.info("Received signal %d — shutting down", sig)
    send_message("🛑 System monitor stopped.")
    sys.exit(0)


signal.signal(signal.SIGINT,  _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# (check_function, send_ram_chart_with_alert)
THRESHOLD_CHECKS = [
    (check_disk_space,        True),   # disk alert includes RAM chart for context
    (check_memory,            False),
    (check_cpu,               False),
    (check_swap,              False),
    (check_load_average,      False),
    (check_disk_io,           False),
    (check_services,          False),
    (check_docker_containers, False),
    (check_pm2_processes,     False),
]


def main():
    log.info("System monitor starting on %s", HOSTNAME)

    if not BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN is not set — messages will not be sent")

    send_message(
        f"🚀 System monitor started.\n"
        f"Watching services: {', '.join(WATCHED_SERVICES) or '—'}\n"
        f"Watching containers: {', '.join(WATCHED_CONTAINERS) or '—'}\n"
        f"Watching PM2 ({PM2_USER}): {', '.join(WATCHED_PM2) or '—'}"
    )

    while True:
        # --- Threshold and watchdog checks ---
        for check_fn, with_chart in THRESHOLD_CHECKS:
            result = check_fn()
            # Watchdog functions return list[str]; threshold checks return str | None
            msgs = result if isinstance(result, list) else ([result] if result else [])
            sender = send_message_with_chart if with_chart else send_message
            for msg in msgs:
                sender(msg)

        # --- Journal errors (plain text — a RAM chart per error would be noisy) ---
        for error_msg in get_journal_errors():
            send_message(error_msg)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
