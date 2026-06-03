# Aurum — Deployment Guide
# gold.samvgarcia.com · Hetzner CAX11 · nginx + SSL

---

## Architecture on the server

```
Internet
    │
    ▼
nginx (port 80/443)          ← handles HTTPS, SSL cert, domain routing
    │
    │  proxies to
    ▼
Aurum container (port 8080)  ← FastAPI + WebSocket + trading loop
    │
    ▼
/opt/aurum/data/trading.db   ← SQLite, persists forever outside container
```

nginx lives on the host (not in Docker). It handles all the SSL complexity
and forwards clean HTTP to your app. This is the standard production setup.

---

## Part 1 — DNS (do this first, takes ~10 min to propagate)

In your domain registrar (wherever samvgarcia.com is managed):

Add an A record:
- Name: `gold`
- Value: `YOUR_HETZNER_IP`
- TTL: 300

This makes `gold.samvgarcia.com` point to your server.
Confirm it's working before proceeding:
```bash
ping gold.samvgarcia.com
# Should resolve to your Hetzner IP
```

---

## Part 2 — Server setup (one-time)

```bash
ssh root@YOUR_HETZNER_IP

# Install Docker
curl -fsSL https://get.docker.com | sh

# Install docker-compose plugin
apt-get install -y docker-compose-plugin

# Install nginx and certbot (for SSL)
apt-get install -y nginx certbot python3-certbot-nginx

# Create app directory
mkdir -p /opt/aurum/data /opt/aurum/logs
cd /opt/aurum

# Clone your repo
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git .

# Create .env (manually — never in git)
nano .env
```

`.env` contents:
```
OANDA_API_KEY=your-key-here
OANDA_ACCOUNT_ID=your-account-id
OANDA_ENV=practice
PAPER_MODE=true
DB_PATH=/app/data/trading.db
LOG_LEVEL=INFO
```

---

## Part 3 — nginx config

```bash
# Create the Aurum nginx site config
nano /etc/nginx/sites-available/aurum
```

Paste this:
```nginx
server {
    listen 80;
    server_name gold.samvgarcia.com;

    # Certbot will add the SSL block below this automatically
    # Leave this here for the initial cert request

    location / {
        proxy_pass http://localhost:8080;
        proxy_http_version 1.1;

        # Critical for WebSocket to work through nginx
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Keep WebSocket connections alive
        proxy_read_timeout 86400;
    }
}
```

```bash
# Enable the site
ln -s /etc/nginx/sites-available/aurum /etc/nginx/sites-enabled/
nginx -t          # test config — should say "syntax is ok"
systemctl reload nginx
```

---

## Part 4 — SSL certificate (free, auto-renews)

```bash
# Get the cert — certbot edits nginx config automatically
certbot --nginx -d gold.samvgarcia.com

# Follow prompts:
# - Enter your email
# - Agree to terms
# - Choose option 2 (redirect HTTP to HTTPS)

# Test auto-renewal
certbot renew --dry-run
```

After this, `gold.samvgarcia.com` serves over HTTPS.
Certbot adds a cron job to renew the cert automatically every 90 days.

---

## Part 5 — docker-compose.yml

```yaml
version: "3.9"

services:
  aurum:
    build: .
    container_name: aurum
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:8080"   # only accessible from localhost — nginx proxies to it
                                  # NOT exposed to the public internet directly
    env_file:
      - .env
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    environment:
      - DB_PATH=/app/data/trading.db
```

Note `127.0.0.1:8080:8080` — the app port is bound to localhost only.
The outside world reaches it only through nginx (HTTPS). This is correct.

---

## Part 6 — Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY . .

EXPOSE 8080

CMD ["python", "-m", "xauusd_system.main"]
```

---

## Part 7 — First deploy

```bash
cd /opt/aurum

# Build and start
docker compose up -d --build

# Watch logs — confirm it starts cleanly
docker compose logs -f aurum

# Test locally on the server first
curl http://localhost:8080/api/bars?count=3

# Then test through nginx + HTTPS
curl https://gold.samvgarcia.com/api/bars?count=3

# Open in browser
# https://gold.samvgarcia.com
```

---

## Part 8 — Git-based auto-deploy

```bash
# Create deploy script
cat > /opt/aurum/deploy.sh << 'EOF'
#!/bin/bash
set -e
cd /opt/aurum

CURRENT=$(git rev-parse HEAD)
git fetch origin main
LATEST=$(git rev-parse origin/main)

if [ "$CURRENT" != "$LATEST" ]; then
  echo "[aurum deploy] New commit detected: $LATEST"
  git pull origin main
  docker compose up -d --build
  echo "[aurum deploy] Done at $(date)"
else
  echo "[aurum deploy] No changes at $(date)"
fi
EOF
chmod +x /opt/aurum/deploy.sh

# Auto-deploy every 5 minutes
(crontab -l 2>/dev/null; echo "*/5 * * * * /opt/aurum/deploy.sh >> /opt/aurum/logs/deploy.log 2>&1") | crontab -
```

### Your daily workflow

```
Edit code locally
      ↓
git commit -m "..."
git push origin main
      ↓
Server detects new commit within 5 min
      ↓
Pulls + rebuilds container (~30s downtime)
      ↓
trading.db untouched — all trades still there
      ↓
https://gold.samvgarcia.com live again
```

For instant deploy (don't wait 5 min):
```bash
ssh root@YOUR_HETZNER_IP "/opt/aurum/deploy.sh"
```

---

## Part 9 — Firewall (Hetzner panel)

In Hetzner console → Firewalls → create a firewall for the server:

| Direction | Protocol | Port | Source          |
|-----------|----------|------|-----------------|
| Inbound   | TCP      | 22   | Your IP only    |
| Inbound   | TCP      | 80   | 0.0.0.0/0       |
| Inbound   | TCP      | 443  | 0.0.0.0/0       |

Port 8080 is NOT in this list — it's only accessible via localhost now.
Port 22 is restricted to your IP — nobody else can SSH in.

---

## Part 10 — Verify persistence after a deploy

```bash
# After your second deploy, confirm the DB survived
docker compose exec aurum sqlite3 /app/data/trading.db \
  "SELECT COUNT(*) as trades FROM trades; SELECT COUNT(*) as snapshots FROM equity_curve;"
```

---

## .gitignore

```
.env
data/
logs/
__pycache__/
*.pyc
.venv/
*.egg-info/
node_modules/
```

---

## Summary — what runs where

| Thing              | Where              | Managed by        |
|--------------------|--------------------|-------------------|
| Aurum bot + API    | Docker container   | docker-compose    |
| SQLite database    | Host `/opt/aurum/data/` | Volume mount |
| nginx + SSL        | Host directly      | systemd + certbot |
| SSL cert renewal   | Host               | certbot cron      |
| Auto-deploy        | Host               | cron → deploy.sh  |
| Domain DNS         | Your registrar     | A record          |
