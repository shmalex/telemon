"""
diagnostics.py — LangGraph diagnostic workflow for telemon.

When an alert fires, instead of just sending a raw threshold message,
this graph runs a multi-step investigation and produces a structured
diagnosis with a recommended action.

Graph topology:

    collect_context
          │
     classify_alert
          │
    ┌─────┴──────┐
  load          disk_io      (other alerts skip straight to analyze)
    │               │
check_processes  check_disk_detail
    └─────┬──────┘
       analyze
          │
     format_report

State flows forward through nodes; each node adds its findings
to the shared DiagnosticState dict.

Required env:
  ANTHROPIC_API_KEY  or  OPENAI_API_KEY
  LLM_MODEL  (optional override)
"""

import logging
import os
import subprocess
import time
from typing import Literal

import psutil
from typing_extensions import TypedDict

log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL         = os.environ.get("LLM_MODEL", "gpt-4o-mini" if not ANTHROPIC_API_KEY else "claude-haiku-4-5-20251001")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class DiagnosticState(TypedDict):
    alert_type:     str   # "load" | "disk_io" | "memory" | "journal" | "other"
    alert_text:     str   # original alert message that triggered the workflow
    context:        str   # raw metrics gathered by collect_context
    extra:          str   # extra data gathered by the type-specific node
    analysis:       str   # LLM's interpretation
    report:         str   # final message ready to send to Telegram


# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------

def _get_llm():
    if ANTHROPIC_API_KEY:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=LLM_MODEL, api_key=ANTHROPIC_API_KEY, max_tokens=512)
    elif OPENAI_API_KEY:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=LLM_MODEL, api_key=OPENAI_API_KEY)
    else:
        raise RuntimeError("No LLM API key configured")


# ---------------------------------------------------------------------------
# Node: collect_context
# ---------------------------------------------------------------------------

def collect_context(state: DiagnosticState) -> DiagnosticState:
    """Snapshot current system metrics into state."""
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

    context = (
        f"CPU: {cpu:.1f}%  |  Load (1/5/15m): {load1:.2f}/{load5:.2f}/{load15:.2f}"
        f"  ({psutil.cpu_count()} CPUs)\n"
        f"RAM: {mem.percent:.1f}%  ({mem.used/1024**3:.1f}/{mem.total/1024**3:.1f} GB)\n"
        f"Swap: {swap.percent:.1f}%  ({swap.used/1024**3:.2f}/{swap.total/1024**3:.2f} GB)\n"
        f"Disk /: {disk.percent:.1f}%  ({disk.free/1024**3:.1f} GB free)\n"
        f"Disk I/O: read {read_mbps:.1f} MB/s  write {write_mbps:.1f} MB/s"
    )
    log.debug("diagnostics: context collected")
    return {**state, "context": context}


# ---------------------------------------------------------------------------
# Node: classify_alert
# ---------------------------------------------------------------------------

def classify_alert(state: DiagnosticState) -> DiagnosticState:
    """Derive alert_type from the alert text (simple keyword match, no LLM needed)."""
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
    log.debug("diagnostics: classified as '%s'", alert_type)
    return {**state, "alert_type": alert_type}


# ---------------------------------------------------------------------------
# Node: check_processes  (load branch)
# ---------------------------------------------------------------------------

def check_processes(state: DiagnosticState) -> DiagnosticState:
    """Collect top CPU-consuming processes for load alert context."""
    procs = sorted(
        psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
        key=lambda p: p.info.get("cpu_percent") or 0,
        reverse=True,
    )[:8]
    lines = [
        f"  {p.info['name']} (pid {p.info['pid']}): "
        f"cpu {p.info['cpu_percent']:.1f}%  mem {p.info['memory_percent']:.1f}%"
        for p in procs
    ]
    extra = "Top processes by CPU:\n" + "\n".join(lines)
    log.debug("diagnostics: process list collected")
    return {**state, "extra": extra}


# ---------------------------------------------------------------------------
# Node: check_disk_detail  (disk_io branch)
# ---------------------------------------------------------------------------

def check_disk_detail(state: DiagnosticState) -> DiagnosticState:
    """Collect per-disk I/O stats and iotop-style process info."""
    extra_parts = []

    # Per-partition stats
    try:
        per_disk = psutil.disk_io_counters(perdisk=True)
        lines = [
            f"  {name}: read {c.read_bytes/1024**3:.1f} GB  write {c.write_bytes/1024**3:.1f} GB"
            for name, c in per_disk.items()
        ]
        extra_parts.append("Disk counters (total since boot):\n" + "\n".join(lines))
    except Exception as exc:
        extra_parts.append(f"Per-disk stats unavailable: {exc}")

    # iotop snapshot if available
    try:
        result = subprocess.run(
            ["iotop", "-b", "-n", "1", "-P", "-o"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            extra_parts.append("iotop snapshot:\n" + result.stdout.strip()[:600])
    except Exception:
        pass  # iotop is optional

    extra = "\n\n".join(extra_parts) or "No extra disk detail available."
    log.debug("diagnostics: disk detail collected")
    return {**state, "extra": extra}


# ---------------------------------------------------------------------------
# Node: analyze  (all branches converge here)
# ---------------------------------------------------------------------------

def analyze(state: DiagnosticState) -> DiagnosticState:
    """Ask the LLM to interpret the collected data and suggest a root cause."""
    llm = _get_llm()

    prompt = (
        f"You are a Linux sysadmin assistant. An automated monitor fired this alert:\n"
        f"---\n{state['alert_text']}\n---\n\n"
        f"Current system metrics:\n{state['context']}\n"
    )
    if state.get("extra"):
        prompt += f"\nAdditional detail:\n{state['extra']}\n"
    prompt += (
        "\nIn 3-5 sentences: what is the most likely cause of this alert, "
        "and what is the single most useful action to investigate or fix it? "
        "Be specific and concise. Do not repeat the metrics verbatim."
    )

    response = llm.invoke(prompt)
    analysis = response.content if hasattr(response, "content") else str(response)
    log.debug("diagnostics: LLM analysis complete (%d chars)", len(analysis))
    return {**state, "analysis": analysis}


# ---------------------------------------------------------------------------
# Node: format_report
# ---------------------------------------------------------------------------

def format_report(state: DiagnosticState) -> DiagnosticState:
    """Assemble the final Telegram message."""
    report = (
        f"🔍 Diagnostic report\n"
        f"{'─' * 30}\n"
        f"{state['alert_text']}\n"
        f"{'─' * 30}\n"
        f"📊 Snapshot\n{state['context']}\n"
        f"{'─' * 30}\n"
        f"🤖 Analysis\n{state['analysis']}"
    )
    return {**state, "report": report}


# ---------------------------------------------------------------------------
# Routing — conditional edge after classify_alert
# ---------------------------------------------------------------------------

def _route(state: DiagnosticState) -> Literal["check_processes", "check_disk_detail", "analyze"]:
    t = state["alert_type"]
    if t == "load":
        return "check_processes"
    if t == "disk_io":
        return "check_disk_detail"
    return "analyze"


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------

def build_graph():
    from langgraph.graph import StateGraph, END

    g = StateGraph(DiagnosticState)

    g.add_node("collect_context",    collect_context)
    g.add_node("classify_alert",     classify_alert)
    g.add_node("check_processes",    check_processes)
    g.add_node("check_disk_detail",  check_disk_detail)
    g.add_node("analyze",            analyze)
    g.add_node("format_report",      format_report)

    g.set_entry_point("collect_context")
    g.add_edge("collect_context", "classify_alert")

    g.add_conditional_edges(
        "classify_alert",
        _route,
        {
            "check_processes":   "check_processes",
            "check_disk_detail": "check_disk_detail",
            "analyze":           "analyze",
        },
    )

    g.add_edge("check_processes",   "analyze")
    g.add_edge("check_disk_detail", "analyze")
    g.add_edge("analyze",           "format_report")
    g.add_edge("format_report",     END)

    return g.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_graph = None


def run_diagnostic(alert_text: str) -> str:
    """Run the diagnostic graph for the given alert text.

    Returns the formatted report string, or the original alert_text
    if diagnostics are not configured / fail.
    """
    global _graph

    if not ANTHROPIC_API_KEY and not OPENAI_API_KEY:
        return alert_text   # diagnostics disabled — pass through unchanged

    try:
        if _graph is None:
            _graph = build_graph()

        initial: DiagnosticState = {
            "alert_type": "",
            "alert_text": alert_text,
            "context":    "",
            "extra":      "",
            "analysis":   "",
            "report":     "",
        }
        result = _graph.invoke(initial)
        return result["report"]

    except Exception as exc:
        log.error("diagnostics: graph failed: %s", exc)
        return alert_text   # fallback: original alert
