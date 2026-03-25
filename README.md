# CampWatch

Monitors GDay Parks availability and alerts you via browser notification + email.

## Quick Start

### 1. Configure email

Edit `docker-compose.yml` and set your Gmail credentials:

```yaml
SMTP_USER: your.gmail@gmail.com
SMTP_PASS: xxxx-xxxx-xxxx-xxxx   # Gmail App Password (NOT your real password)
NOTIFY_EMAIL: your.gmail@gmail.com
```

**Gmail App Password:** go to https://myaccount.google.com/apppasswords
Create one called "CampWatch". Paste the 16-char code as SMTP_PASS.

### 2. Build and start

```bash
docker compose up -d --build
```

### 3. Open the web UI

http://localhost:5000

### 4. Add a watch

1. Find the park code from a GDay Parks URL.
   Example: `gdayparks.com.au/...?propertyCode=VWBT` → code is **VWBT**
2. Type the code in the "Add Watch" box, click **Look up**
3. Select accommodation / site type
4. Set check-in and check-out dates
5. Click **Add Watch**

CampWatch checks every 30 minutes. When available it will:
- Show a browser desktop popup
- Send an email

## Common commands

```bash
docker compose up -d        # start
docker compose down         # stop
docker compose logs -f      # tail logs
docker compose restart      # restart after config change
```

## How it works

- Calls `exp-api.gdaygroup.com.au` directly — no scraping, clean JSON API
- A site is available when it appears in the `available[]` list in the response
- Alerts only fire on transition to available (not every poll)
- Data stored in a Docker volume (SQLite) — persists across restarts

## Changing poll interval

Edit `docker-compose.yml`:
```yaml
POLL_INTERVAL_MINUTES: 15
```
Then: `docker compose restart`
