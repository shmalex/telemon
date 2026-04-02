# Telemon

Linux background service that monitors system health and sends alerts to Telegram.

## What it monitors

| Check | Alert condition |
|---|---|
| Disk space | Free space on `/` drops below threshold |
| RAM usage | Usage exceeds threshold % |
| CPU usage | Usage exceeds threshold % |
| Swap usage | Usage exceeds threshold % |
| Load average | 1-min load average exceeds threshold |
| Disk I/O | Read or write throughput exceeds threshold MB/s |
| Systemd services | Service is not `active` (nginx, postgresql, …) |
| Docker containers | Container is stopped or not found |
| PM2 processes | Process is not `online` |
| Journal errors | New `journalctl` entries at priority 3 (error) or higher |

Alerts are throttled — the same alert is not repeated for 5 minutes (configurable).

---

## Project structure

```
telesystem/
├── src/
│   └── telemon.py   # main service
├── telemon.service          # systemd unit file (reference)
├── .env                     # local config (do not commit)
├── .env.example             # config template
├── install.sh               # deploy script
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

## Installation

```bash
sudo bash install.sh
```

The script will:
1. Install Python dependencies
2. Deploy `telemon.py` to `/app/monitor/`
3. Create state directory `/var/lib/system-monitor/`
4. Write and enable the `telemon` systemd service
5. Show service status on completion

---

## Updating an existing installation

Deploy only the script (no reinstall needed):

```bash
scp src/telemon.py scr:/app/telemon/telemon.py && ssh scr "systemctl restart telemon"
```

If you also changed `.env`:

```bash
scp .env scr:/app/monitor/.env && ssh scr "systemctl restart telemon"
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
