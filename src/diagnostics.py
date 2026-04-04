"""
diagnostics.py — LangGraph diagnostic workflow for telemon.

Features:
  1. Iterative investigation loop — the LLM decides what to gather next
     and keeps looping until it has enough data (max 4 iterations).
  2. Persistent alert memory — stores the last 50 alerts to a JSON file
     so the agent can detect recurring patterns across restarts.

Graph topology:

    load_history
         │
    collect_context
         │
       decide  ◄─────────────────────┐
         │                           │  loop (max 4×)
    ┌────┴──────────────────────┐    │
    │  gather_processes         │────┘
    │  gather_disk_detail       │────┘
    │  gather_journal           │────┘
    │  gather_docker            │────┘
    └────────────┬──────────────┘
              "done"
                 │
          detect_pattern    ← looks at alert_history for recurring issues
                 │
             analyze        ← LLM final synthesis
                 │
           format_report
                 │
           save_history     ← persists to JSON
                 │
                END

Required env:  ANTHROPIC_API_KEY  or  OPENAI_API_KEY
Optional env:  LLM_MODEL, DIAGNOSTICS_HISTORY_FILE, DIAGNOSTICS_MAX_ITER
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

import psutil
from typing_extensions import TypedDict

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL         = os.environ.get(
    "LLM_MODEL",
    "claude-haiku-4-5-20251001" if ANTHROPIC_API_KEY else "gpt-4o-mini",
)
HISTORY_FILE = os.environ.get(
    "DIAGNOSTICS_HISTORY_FILE",
    "/var/lib/system-monitor/alert_history.json",
)
MAX_ITER = int(os.environ.get("DIAGNOSTICS_MAX_ITER", "4"))
HISTORY_KEEP = 50   # max alerts to keep in the JSON file


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class DiagnosticState(TypedDict):
    # ── current alert ────────────────────────────────────────────────────
    alert_text:  str
    alert_type:  str        # "load" | "disk_io" | "memory" | "journal" | "other"
    peak_label:  str        # short human-readable peak value, e.g. "536 MB/s read"

    # ── iterative investigation ───────────────────────────────────────────
    context:     str        # baseline snapshot from collect_context
    gathered:    list       # list of (label, data) tuples added each iteration
    iterations:  int        # guard against infinite loops
    next_action: str        # LLM decision: what to gather next | "done"

    # ── synthesis ────────────────────────────────────────────────────────
    pattern_note: str       # recurring-pattern note from detect_pattern
    analysis:     str       # LLM root-cause analysis
    report:       str       # final Telegram message

    # ── persistent memory ────────────────────────────────────────────────
    alert_history: list     # list[dict] loaded from / saved to HISTORY_FILE


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def _get_llm():
    from llm_logger import LLMFileLogger
    callbacks = [LLMFileLogger()]
    if ANTHROPIC_API_KEY:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=LLM_MODEL, api_key=ANTHROPIC_API_KEY,
                             max_tokens=600, callbacks=callbacks)
    elif OPENAI_API_KEY:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=LLM_MODEL, api_key=OPENAI_API_KEY,
                          callbacks=callbacks)
    raise RuntimeError("No LLM API key configured (ANTHROPIC_API_KEY or OPENAI_API_KEY)")


# ---------------------------------------------------------------------------
# Node: load_history
# ---------------------------------------------------------------------------

def load_history(state: DiagnosticState) -> DiagnosticState:
    """Load alert history from the JSON file on disk."""
    try:
        path = Path(HISTORY_FILE)
        if path.exists():
            history = json.loads(path.read_text())
            log.debug("diagnostics: loaded %d historical alerts", len(history))
        else:
            history = []
    except Exception as exc:
        log.warning("diagnostics: could not load history: %s", exc)
        history = []
    return {**state, "alert_history": history}


# ---------------------------------------------------------------------------
# Node: collect_context
# ---------------------------------------------------------------------------

def collect_context(state: DiagnosticState) -> DiagnosticState:
    """Snapshot current system metrics."""
    load1, load5, load15 = os.getloadavg()
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu  = psutil.cpu_percent(interval=1)
    swap = psutil.swap_memory()

    io1 = psutil.disk_io_counters()
    time.sleep(1)
    io2 = psutil.disk_io_counters()
    read_mbps  = (io2.read_bytes  - io1.read_bytes)  / 1024 / 1024
    write_mbps = (io2.write_bytes - io1.write_bytes) / 1024 / 1024

    # Basic alert type classification (no LLM needed)
    text = state["alert_text"].lower()
    if "load average" in text:
        alert_type = "load"
    elif "disk i/o" in text:
        alert_type = "disk_io"
    elif "ram" in text or "memory" in text:
        alert_type = "memory"
    elif "system error" in text or "journal" in text:
        alert_type = "journal"
    else:
        alert_type = "other"

    context = (
        f"CPU: {cpu:.1f}%  |  Load (1/5/15m): {load1:.2f}/{load5:.2f}/{load15:.2f}"
        f"  ({psutil.cpu_count()} CPUs)\n"
        f"RAM: {mem.percent:.1f}%  ({mem.used/1024**3:.1f}/{mem.total/1024**3:.1f} GB)\n"
        f"Swap: {swap.percent:.1f}%  ({swap.used/1024**3:.2f}/{swap.total/1024**3:.2f} GB)\n"
        f"Disk /: {disk.percent:.1f}%  ({disk.free/1024**3:.1f} GB free)\n"
        f"Disk I/O: read {read_mbps:.1f} MB/s  write {write_mbps:.1f} MB/s"
    )
    # Extract a short peak label from the alert text for chronic tracker
    import re as _re
    peak_label = ""
    m = _re.search(r"Read:\s+([\d.]+\s*MB/s)", state["alert_text"])
    if m:
        peak_label = f"{m.group(1)} read"
    else:
        m = _re.search(r"load average:\s+([\d.]+)", state["alert_text"], _re.IGNORECASE)
        if m:
            peak_label = f"load {m.group(1)}"

    log.debug("diagnostics: context collected, type=%s peak=%s", alert_type, peak_label)
    return {**state, "context": context, "alert_type": alert_type,
            "peak_label": peak_label, "gathered": [], "iterations": 0, "next_action": ""}


# ---------------------------------------------------------------------------
# Node: decide  (the loop controller)
# ---------------------------------------------------------------------------

_AVAILABLE_ACTIONS = {
    "gather_processes":   "top CPU/RAM consuming processes",
    "gather_disk_detail": "per-disk I/O stats and iotop snapshot",
    "gather_journal":     "recent system journal errors",
    "gather_docker":      "docker container statuses",
    "done":               "enough data collected, proceed to analysis",
}


def decide(state: DiagnosticState) -> DiagnosticState:
    """Ask the LLM what to investigate next, or whether we have enough."""
    if state["iterations"] >= MAX_ITER:
        log.debug("diagnostics: max iterations reached, forcing done")
        return {**state, "next_action": "done"}

    # Already-gathered labels — exclude from available actions to prevent repeats
    already_labels = {label for label, _ in state["gathered"]}
    available = {k: v for k, v in _AVAILABLE_ACTIONS.items()
                 if k not in already_labels or k == "done"}

    already = "\n".join(
        f"  - {label}" for label, _ in state["gathered"]
    ) or "  (nothing yet)"

    actions_desc = "\n".join(f"  {k}: {v}" for k, v in available.items())

    prompt = (
        f"Alert: {state['alert_text']}\n\n"
        f"System snapshot:\n{state['context']}\n\n"
        f"Already gathered:\n{already}\n\n"
        f"Available next actions:\n{actions_desc}\n\n"
        f"Reply with EXACTLY one action name from the list above. "
        f"Choose 'done' if you have enough to diagnose the root cause."
    )

    try:
        llm    = _get_llm()
        resp   = llm.invoke(prompt)
        action = resp.content.strip().lower().split()[0]
        if action not in available:
            action = "done"
    except Exception as exc:
        log.error("diagnostics: decide failed: %s", exc)
        action = "done"

    log.debug("diagnostics: iteration %d → %s", state["iterations"] + 1, action)
    return {**state, "next_action": action, "iterations": state["iterations"] + 1}


# ---------------------------------------------------------------------------
# Data-gathering nodes
# ---------------------------------------------------------------------------

def gather_processes(state: DiagnosticState) -> DiagnosticState:
    procs = sorted(
        psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
        key=lambda p: p.info.get("cpu_percent") or 0,
        reverse=True,
    )[:10]
    lines = [
        f"  {p.info['name']} (pid {p.info['pid']}): "
        f"cpu {p.info['cpu_percent']:.1f}%  mem {p.info['memory_percent']:.1f}%"
        for p in procs
    ]
    data = "Top processes by CPU:\n" + "\n".join(lines)
    return {**state, "gathered": state["gathered"] + [("processes", data)]}


def gather_disk_detail(state: DiagnosticState) -> DiagnosticState:
    parts = []
    try:
        per_disk = psutil.disk_io_counters(perdisk=True)
        lines = [
            f"  {name}: read {c.read_bytes/1024**3:.1f} GB  write {c.write_bytes/1024**3:.1f} GB"
            for name, c in per_disk.items()
        ]
        parts.append("Disk counters (since boot):\n" + "\n".join(lines))
    except Exception as exc:
        parts.append(f"Per-disk stats unavailable: {exc}")
    try:
        r = subprocess.run(["iotop", "-b", "-n", "1", "-P", "-o"],
                           capture_output=True, text=True, timeout=5)
        if r.stdout.strip():
            parts.append("iotop snapshot:\n" + r.stdout.strip()[:500])
    except Exception:
        pass
    data = "\n\n".join(parts) or "No disk detail available."
    return {**state, "gathered": state["gathered"] + [("disk_detail", data)]}


def gather_journal(state: DiagnosticState) -> DiagnosticState:
    try:
        r = subprocess.run(
            ["journalctl", "-b", "-p", "3", "-n", "20", "--no-pager", "-o", "short"],
            capture_output=True, text=True, timeout=10,
        )
        data = "Recent journal errors:\n" + (r.stdout.strip() or "(none)")
    except Exception as exc:
        data = f"Journal unavailable: {exc}"
    return {**state, "gathered": state["gathered"] + [("journal", data)]}


def gather_docker(state: DiagnosticState) -> DiagnosticState:
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
            capture_output=True, text=True, timeout=10,
        )
        data = "Docker containers:\n" + (r.stdout.strip() or "(none)")
    except Exception as exc:
        data = f"Docker unavailable: {exc}"
    return {**state, "gathered": state["gathered"] + [("docker", data)]}


# ---------------------------------------------------------------------------
# Node: detect_pattern
# ---------------------------------------------------------------------------

def detect_pattern(state: DiagnosticState) -> DiagnosticState:
    """Look at alert history for recurring patterns."""
    history = state.get("alert_history", [])
    if len(history) < 3:
        return {**state, "pattern_note": ""}

    # Count same alert_type in last 24h
    now = time.time()
    recent = [
        h for h in history
        if h.get("alert_type") == state["alert_type"]
        and now - h.get("ts", 0) < 86400
    ]

    if len(recent) < 2:
        return {**state, "pattern_note": ""}

    # Ask LLM to summarise the pattern
    summaries = "\n".join(
        f"  [{datetime.fromtimestamp(h['ts']).strftime('%H:%M')}] {h.get('alert_text', '')[:80]}"
        for h in recent[-5:]
    )
    prompt = (
        f"These are the last {len(recent)} occurrences of '{state['alert_type']}' alerts "
        f"in the past 24 hours:\n{summaries}\n\n"
        f"In one sentence: describe the pattern (e.g. frequency, time-of-day, trend)."
    )
    try:
        llm  = _get_llm()
        resp = llm.invoke(prompt)
        note = f"⚠️ Pattern ({len(recent)}× in 24h): {resp.content.strip()}"
    except Exception as exc:
        log.warning("diagnostics: pattern detection failed: %s", exc)
        note = f"⚠️ This alert type fired {len(recent)} times in the last 24h."

    log.debug("diagnostics: pattern note: %s", note)
    return {**state, "pattern_note": note}


# ---------------------------------------------------------------------------
# Node: analyze
# ---------------------------------------------------------------------------

def analyze(state: DiagnosticState) -> DiagnosticState:
    """LLM final root-cause analysis."""
    gathered_text = "\n\n".join(
        f"[{label}]\n{data}" for label, data in state["gathered"]
    ) or "(only baseline snapshot)"

    prompt = (
        f"Alert: {state['alert_text']}\n\n"
        f"System snapshot:\n{state['context']}\n\n"
        f"Investigated data:\n{gathered_text}\n\n"
    )
    if state.get("pattern_note"):
        prompt += f"Recurring pattern note: {state['pattern_note']}\n\n"

    prompt += (
        "In 3-5 sentences: what is the most likely root cause, "
        "and what is the single most useful action to investigate or fix it? "
        "Be specific. Do not repeat metrics verbatim."
    )

    try:
        llm      = _get_llm()
        resp     = llm.invoke(prompt)
        analysis = resp.content.strip()
    except Exception as exc:
        log.error("diagnostics: analyze failed: %s", exc)
        analysis = f"LLM analysis unavailable: {exc}"

    return {**state, "analysis": analysis}


# ---------------------------------------------------------------------------
# Node: format_report
# ---------------------------------------------------------------------------

def format_report(state: DiagnosticState) -> DiagnosticState:
    gathered_summary = "\n".join(
        f"  • {label}" for label, _ in state["gathered"]
    ) or "  • baseline only"

    parts = [
        "🔍 Diagnostic report",
        "─" * 30,
        state["alert_text"],
        "─" * 30,
        f"📊 Snapshot\n{state['context']}",
        f"🔎 Investigated ({state['iterations']} step(s))\n{gathered_summary}",
    ]
    if state.get("pattern_note"):
        parts.append(state["pattern_note"])
    parts += [
        "─" * 30,
        f"🤖 Analysis\n{state['analysis']}",
    ]
    return {**state, "report": "\n".join(parts)}


# ---------------------------------------------------------------------------
# Node: save_history
# ---------------------------------------------------------------------------

def save_history(state: DiagnosticState) -> DiagnosticState:
    """Append this alert to the persistent JSON history file."""
    entry = {
        "ts":         time.time(),
        "alert_type": state["alert_type"],
        "alert_text": state["alert_text"][:120],
        "analysis":   state["analysis"][:200],
    }
    history = (state.get("alert_history") or []) + [entry]
    history = history[-HISTORY_KEEP:]   # keep last N

    try:
        path = Path(HISTORY_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, indent=2))
        log.debug("diagnostics: history saved (%d entries)", len(history))
    except Exception as exc:
        log.warning("diagnostics: could not save history: %s", exc)

    return {**state, "alert_history": history}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _route_decide(state: DiagnosticState) -> Literal[
    "gather_processes", "gather_disk_detail",
    "gather_journal",   "gather_docker",
    "detect_pattern",
]:
    action = state.get("next_action", "done")
    if action in ("gather_processes", "gather_disk_detail",
                  "gather_journal",   "gather_docker"):
        return action
    return "detect_pattern"


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------

def build_graph():
    from langgraph.graph import StateGraph, END

    g = StateGraph(DiagnosticState)

    # nodes
    g.add_node("load_history",       load_history)
    g.add_node("collect_context",    collect_context)
    g.add_node("decide",             decide)
    g.add_node("gather_processes",   gather_processes)
    g.add_node("gather_disk_detail", gather_disk_detail)
    g.add_node("gather_journal",     gather_journal)
    g.add_node("gather_docker",      gather_docker)
    g.add_node("detect_pattern",     detect_pattern)
    g.add_node("analyze",            analyze)
    g.add_node("format_report",      format_report)
    g.add_node("save_history",       save_history)

    # entry
    g.set_entry_point("load_history")
    g.add_edge("load_history",    "collect_context")
    g.add_edge("collect_context", "decide")

    # iterative loop: decide → gather_* → decide (cycle!)
    g.add_conditional_edges(
        "decide",
        _route_decide,
        {
            "gather_processes":   "gather_processes",
            "gather_disk_detail": "gather_disk_detail",
            "gather_journal":     "gather_journal",
            "gather_docker":      "gather_docker",
            "detect_pattern":     "detect_pattern",   # exit loop
        },
    )

    # all gather nodes loop back to decide
    for gather_node in ("gather_processes", "gather_disk_detail",
                        "gather_journal",   "gather_docker"):
        g.add_edge(gather_node, "decide")

    # after loop
    g.add_edge("detect_pattern", "analyze")
    g.add_edge("analyze",        "format_report")
    g.add_edge("format_report",  "save_history")
    g.add_edge("save_history",   END)

    return g.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_graph = None


def run_diagnostic(alert_text: str) -> list[str]:
    """
    Run the diagnostic graph for an alert.

    Returns a list of messages to send to Telegram:
    - For ACUTE alerts: [full diagnostic report]
    - For CHRONIC alerts: [transition message] or [] (suppressed)
    - Also may include chronic hourly summaries from the tracker.
    """
    from chronic_tracker import get_tracker

    tracker    = get_tracker()
    alert_type = _classify_alert_type(alert_text)

    # Extract peak label from text before running graph
    import re as _re
    peak_label = ""
    m = _re.search(r"Read:\s+([\d.]+\s*MB/s)", alert_text)
    if m:
        peak_label = f"{m.group(1)} read"
    else:
        m = _re.search(r"load average:\s+([\d.]+)", alert_text, _re.IGNORECASE)
        if m:
            peak_label = f"load {m.group(1)}"

    status = tracker.record(alert_type, peak_label)
    transition_msg = tracker.get_transition_message(alert_type)

    messages = []

    if status == "chronic":
        # Just became chronic — send transition message once
        if transition_msg:
            messages.append(transition_msg)
        # Suppress full diagnostic report
        return messages

    # ACUTE — run full graph
    if not ANTHROPIC_API_KEY and not OPENAI_API_KEY:
        return [alert_text]

    global _graph
    try:
        if _graph is None:
            _graph = build_graph()

        initial: DiagnosticState = {
            "alert_text":    alert_text,
            "alert_type":    alert_type,
            "peak_label":    peak_label,
            "context":       "",
            "gathered":      [],
            "iterations":    0,
            "next_action":   "",
            "pattern_note":  "",
            "analysis":      "",
            "report":        "",
            "alert_history": [],
        }
        result = _graph.invoke(initial)
        messages.append(result["report"])
    except Exception as exc:
        log.error("diagnostics: graph failed: %s", exc)
        messages.append(alert_text)

    return messages


def get_chronic_summaries() -> list[str]:
    """Return pending hourly chronic summaries. Call every monitoring cycle."""
    from chronic_tracker import get_tracker
    return [msg for _, msg in get_tracker().get_hourly_summaries()]


def _classify_alert_type(text: str) -> str:
    t = text.lower()
    if "load average" in t:
        return "load"
    if "disk i/o" in t:
        return "disk_io"
    if "ram" in t or "memory" in t:
        return "memory"
    if "system error" in t or "journal" in t:
        return "journal"
    return "other"
