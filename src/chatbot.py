"""
chatbot.py — Telegram chatbot that answers questions about server health.

Uses a LangChain tool-calling agent backed by Claude (Anthropic).
Runs as a daemon thread inside the main telemon process.

The bot only responds to messages from the configured TELEGRAM_CHAT_ID,
so it shares the same Telegram bot as the alert channel.

Required env vars:
  ANTHROPIC_API_KEY  — Claude API key
  TELEGRAM_BOT_TOKEN — same as telemon
  TELEGRAM_CHAT_ID   — same as telemon

Optional:
  LLM_MODEL          — Claude model ID (default: claude-haiku-4-5-20251001)
"""

import logging
import os
import subprocess
import threading
import time

import psutil
import requests

log = logging.getLogger(__name__)

BOT_TOKEN         = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID           = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
LLM_MODEL         = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")

SYSTEM_PROMPT = (
    "You are a Linux server monitoring assistant. "
    "You have tools to query real-time system metrics. "
    "Answer concisely. Always call the relevant tool(s) before answering — "
    "never guess metrics. Respond in the same language the user wrote in."
)


# ---------------------------------------------------------------------------
# LangChain tools
# ---------------------------------------------------------------------------

def _build_tools():
    from langchain.tools import tool

    @tool
    def get_system_metrics() -> str:
        """Get current CPU usage, RAM, load average, swap, and disk space on /."""
        load1, load5, load15 = os.getloadavg()
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        cpu  = psutil.cpu_percent(interval=1)
        swap = psutil.swap_memory()
        return (
            f"CPU: {cpu:.1f}%\n"
            f"Load avg (1/5/15m): {load1:.2f} / {load5:.2f} / {load15:.2f}"
            f"  ({psutil.cpu_count()} CPUs)\n"
            f"RAM: {mem.percent:.1f}%  "
            f"({mem.used / 1024**3:.1f} / {mem.total / 1024**3:.1f} GB)\n"
            f"Swap: {swap.percent:.1f}%  "
            f"({swap.used / 1024**3:.2f} / {swap.total / 1024**3:.2f} GB)\n"
            f"Disk /: {disk.percent:.1f}%  ({disk.free / 1024**3:.1f} GB free)"
        )

    @tool
    def get_disk_io() -> str:
        """Measure disk read/write throughput over 1 second and return MB/s."""
        io1 = psutil.disk_io_counters()
        time.sleep(1)
        io2 = psutil.disk_io_counters()
        read_mbps  = (io2.read_bytes  - io1.read_bytes)  / 1024 / 1024
        write_mbps = (io2.write_bytes - io1.write_bytes) / 1024 / 1024
        return f"Disk I/O: read {read_mbps:.1f} MB/s, write {write_mbps:.1f} MB/s"

    @tool
    def get_top_processes() -> str:
        """Return the top 10 processes by CPU usage."""
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                procs.append(p.info)
            except Exception:
                pass
        procs.sort(key=lambda x: x.get("cpu_percent") or 0, reverse=True)
        lines = [
            f"{p['name']} (pid {p['pid']}): "
            f"CPU {p['cpu_percent']:.1f}%, RAM {p['memory_percent']:.1f}%"
            for p in procs[:10]
        ]
        return "\n".join(lines) or "No process data."

    @tool
    def get_recent_errors(n: int = 15) -> str:
        """Return the last N system journal entries at error priority (3) or above."""
        try:
            result = subprocess.run(
                ["journalctl", "-b", "-p", "3", "-n", str(n), "--no-pager", "-o", "short"],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout.strip() or "No recent errors."
        except Exception as exc:
            return f"Failed to read journal: {exc}"

    @tool
    def get_docker_status() -> str:
        """Return the status of all Docker containers (running and stopped)."""
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout.strip() or "No containers found."
        except Exception as exc:
            return f"Docker not available: {exc}"

    return [get_system_metrics, get_disk_io, get_top_processes,
            get_recent_errors, get_docker_status]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

def _build_agent():
    from langchain_anthropic import ChatAnthropic
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    llm   = ChatAnthropic(model=LLM_MODEL, api_key=ANTHROPIC_API_KEY, max_tokens=1024)
    tools = _build_tools()

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])

    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, max_iterations=6, verbose=False)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def _send_reply(chat_id: int | str, text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": text[:4096]},
            timeout=15,
        )
    except Exception as exc:
        log.error("Chatbot: failed to send reply: %s", exc)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def _poll_loop() -> None:
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — chatbot disabled")
        return

    log.info("Chatbot polling started (model: %s)", LLM_MODEL)

    try:
        agent = _build_agent()
    except Exception as exc:
        log.error("Chatbot: failed to build agent: %s", exc)
        return

    url    = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

    # Drain any updates that accumulated while the service was stopped
    # so we don't answer stale messages after a restart.
    try:
        resp = requests.get(url, params={"timeout": 0, "offset": -1}, timeout=10)
        updates = resp.json().get("result", [])
        offset = updates[-1]["update_id"] + 1 if updates else 0
    except Exception:
        offset = 0

    while True:
        try:
            resp    = requests.get(url, params={"timeout": 30, "offset": offset}, timeout=40)
            updates = resp.json().get("result", [])
        except Exception as exc:
            log.error("Chatbot: polling error: %s", exc)
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1

            msg     = upd.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            text    = msg.get("text", "").strip()

            if not text or not chat_id:
                continue
            if str(chat_id) != str(CHAT_ID):
                continue   # only respond to the configured chat

            log.info("Chatbot query: %s", text[:120])

            try:
                result = agent.invoke({"input": text})
                answer = result.get("output", "No answer.")
            except Exception as exc:
                log.error("Chatbot: agent error: %s", exc)
                answer = f"⚠️ Error: {exc}"

            _send_reply(chat_id, answer)


def start_chatbot_thread() -> None:
    """Start the chatbot polling loop as a background daemon thread."""
    t = threading.Thread(target=_poll_loop, name="chatbot", daemon=True)
    t.start()
