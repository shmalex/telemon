# Telemon

Linux background service that monitors system health and sends alerts to Telegram.

## What it monitors

| Check | Alert condition |
|---|---|
| Disk space | Free space on `/` drops below threshold |
| RAM usage | Usage exceeds threshold % |
| CPU usage | Usage exceeds threshold % |
| Swap usage | Usage exceeds threshold % |
| Load average | 1-min load spikes above adaptive baseline |
| Disk I/O | Read or write throughput spikes above adaptive baseline |
| Systemd services | Service is not `active` (nginx, postgresql, …) |
| Docker containers | Container is stopped or not found |
| PM2 processes | Process is not `online` |
| Journal errors | New `journalctl` entries at priority 3 (error) or higher |

Alerts are throttled per-check. Load and disk I/O use a **rolling median baseline** — alerts only fire when values spike significantly above recent normal (configurable multiplier).

Every 8 hours a **digest chart** is sent to Telegram with 24h graphs of load, CPU and disk I/O.

---

## Project structure

```
telesystem/
├── src/
│   ├── telemon.py       # main service — monitoring loop
│   ├── chatbot.py       # LangChain chatbot — answers questions via Telegram
│   └── diagnostics.py  # LangGraph workflow — enriches alerts with LLM analysis
├── docs/
│   └── diagnostics-graph.md  # Mermaid diagram of the diagnostics workflow
├── telemon.service      # systemd unit file (reference)
├── .env                 # local config (do not commit)
├── .env.example         # config template
├── install.sh           # deploy script
└── README.md
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
nano .env
```

Key variables:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=-1001234567890

WATCHED_SERVICES=nginx,postgresql
WATCHED_CONTAINERS=nginx-prod,postgres-db,redis-cache

PM2_USER=ivan
WATCHED_PM2=checker_api,cron_service,notification_api
```

Useful lookups:
```bash
# Docker container names
docker ps --format '{{.Names}}'

# PM2 process names
pm2 status
```

Full list of variables and their defaults is in [.env.example](.env.example).

---

## Chatbot

When `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set, a LangChain agent starts alongside the monitor and answers questions sent to the bot in Telegram.

```
ANTHROPIC_API_KEY=sk-ant-...   # or
OPENAI_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini          # optional override

# Chat where the bot listens (private chat or group — NOT a channel)
CHATBOT_CHAT_ID=123456789
```

> **Note:** Telegram channels are send-only. The bot can only receive messages from a private chat or a group. Find your personal `chat_id` by messaging [@userinfobot](https://t.me/userinfobot).

Example questions to send:
```
что сейчас с сервером?
какие процессы грузят CPU?
покажи последние ошибки
что с докером?
```

---

## LangGraph diagnostics

When an alert fires for load average or disk I/O, a LangGraph workflow runs before the message is sent. It collects extra context, runs a type-specific investigation, asks the LLM for a root-cause analysis, and sends an enriched report instead of a raw threshold message.

See [docs/diagnostics-graph.md](docs/diagnostics-graph.md) for the workflow diagram.

Requires `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. Falls back to plain alert text if not configured.

---

## Installation

```bash
sudo bash install.sh
```

The script will:
1. Install Python dependencies
2. Install chatbot/diagnostics dependencies if an LLM API key is set
3. Deploy `telemon.py`, `chatbot.py`, `diagnostics.py` to `/app/telemon/`
4. Create state directory `/var/lib/system-monitor/`
5. Write and enable the `telemon` systemd service
6. Show service status on completion

---

## Updating an existing installation

Deploy only the scripts (no reinstall needed):

```bash
scp src/telemon.py src/chatbot.py src/diagnostics.py scr:/app/telemon/ && ssh scr "systemctl restart telemon"
```

If you also changed `.env`:

```bash
scp .env scr:/app/telemon/.env && ssh scr "systemctl restart telemon"
```

Full reinstall (changed `telemon.service` or `install.sh`):

```bash
sudo bash install.sh
```

---

## Managing the service

```bash
# Status
systemctl status telemon

# Follow logs in real time
journalctl -u telemon -f

# Restart after config change
systemctl restart telemon

# Stop / start
systemctl stop telemon
systemctl start telemon
```

---

## Useful commands

```bash
# View recent system errors (priority 3 and above)
sudo journalctl -b 0 -r -p 3

# Trigger a test journal error
sudo python3 -c "
import logging
from logging.handlers import SysLogHandler
logger = logging.getLogger('TestApp')
logger.setLevel(logging.ERROR)
logger.addHandler(SysLogHandler(address='/dev/log'))
logger.error('Test error message')
"

# Trigger a test service failure
sudo systemctl start nonexistent.service
```
