# SO-Toolbox — Server Rebuild Guide

Everything needed to rebuild the server from scratch on a new machine.
The application code lives in Git — this document covers everything that does NOT.

---

## 1. System packages

### CentOS / RHEL

```bash
yum install nginx git python3 python3-pip ffmpeg curl -y
pip3 install flask flask-cors requests
```

### Debian / Ubuntu (incl. WSL)

```bash
apt update
apt install nginx git python3 python3-flask python3-requests ffmpeg curl  -y
apt install python3-pip -y
pip3 install flask-cors --break-system-packages
```

> On newer Debian/Ubuntu, `flask` and `requests` are available via `apt` as
> `python3-flask` and `python3-requests`. Use `apt` first to avoid pip conflicts.

---

## 2. Clone the repository

```bash
mkdir -p /opt/web
cd /opt/web
git clone https://github.com/marcusmarcal/SO-Toolbox.git .
```

---

## 3. SSL certificate (self-signed)

```bash
mkdir -p /etc/nginx/ssl

openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout /etc/nginx/ssl/selfsigned.key \
  -out    /etc/nginx/ssl/selfsigned.crt \
  -subj "/CN=localhost"
```

---

## 4. nginx configuration

### CentOS / RHEL

nginx uses `conf.d/` and a single `nginx.conf`. Replace it with the clean version from the repo:

```bash
cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak
cp /opt/web/nginx.conf /etc/nginx/nginx.conf
rm -f /etc/nginx/conf.d/default.conf
nginx -t && systemctl enable nginx && systemctl start nginx
```

> `nginx.conf` includes `nginx_http_defaults.conf` — this file must exist in `/etc/nginx/`.
> It is already present on the MSP machines. If missing, copy from `/opt/web/nginx_http_defaults.conf`.

---

### Debian / Ubuntu (incl. WSL)

nginx uses `sites-enabled/`. Remove the default site and add ours:

```bash
# Remove Debian default site
rm -f /etc/nginx/sites-enabled/default

# Install SO-Toolbox site
cp /opt/web/nginx-debian.conf /etc/nginx/sites-available/so-toolbox
ln -s /etc/nginx/sites-available/so-toolbox /etc/nginx/sites-enabled/so-toolbox

nginx -t && systemctl enable nginx && systemctl start nginx
```

> Do NOT replace `/etc/nginx/nginx.conf` on Debian — use `sites-available/` as above.

---

## 5. .env file

Create `/opt/web/.env` — this file is NOT in Git (intentionally).

```bash
nano /opt/web/.env
```

Template:

```env
APP_TITLE=SP SO Web Toolbox
APP_VERSION=1.5.0

# Tools — format: TOOL_n=file.html|Name|Description|icon|Category|BADGE
TOOL_1=monitor.html|Channel Monitor|PhenixRTS real-time health|📡|Monitoring|LIVE
TOOL_2=SRT-URI-Builder.html|SRT URI Builder|Build SRT connection strings|🔗|Streaming|

# SRT Builder config
SRT_PASSPHRASE=your-passphrase-here

# Admin password for sensitive actions (restart proxy, kill/delete MTR jobs)
# Leave blank to disable password protection
ADMIN_PASSWORD=your-admin-password-here
SRT_SERVER_1=10.x.x.x|Server Name 1
SRT_SERVER_2=10.x.x.x|Server Name 2

# PhenixRTS credentials (used by monitor.html via proxy)
# NOT exposed to browser — only read server-side by proxy.py
PHENIXRTS_APP_ID=your-app-id
PHENIXRTS_PASSWORD=your-password
```

> ⚠️ `SRT_PASSPHRASE`, `PHENIXRTS_APP_ID`, and `PHENIXRTS_PASSWORD` are sensitive.
> They are never sent to the browser — only `proxy.py` reads them server-side.

---

## 6. Proxy as a systemd service

```bash
cp /opt/web/so-proxy.service /etc/systemd/system/so-proxy.service

systemctl daemon-reload
systemctl enable so-proxy
systemctl start so-proxy
```

Check status and logs:

```bash
systemctl status so-proxy
journalctl -u so-proxy -f
```

Restart after changes:

```bash
systemctl restart so-proxy
```

> **WSL note:** `systemctl` may not work on WSL1. Use WSL2, or start manually:
> ```bash
> cd /opt/web && python3 proxy.py &
> ```

---

## 7. Verify everything works

```bash
# nginx serving HTTPS
curl -sk https://localhost/ | head -5

# Proxy config endpoint
curl -sk https://localhost/so-proxy/config

# Git pull endpoint
curl -sk -X POST https://localhost/so-proxy/git-pull

# .env is blocked (must return 404)
curl -sk https://localhost/.env
```

---

## 8. What lives where

| What | Where | In Git? |
|------|-------|---------|
| App code (HTML, proxy.py) | `/opt/web/` | ✅ Yes |
| nginx config (CentOS/RHEL) | `/opt/web/nginx.conf` → `/etc/nginx/nginx.conf` | ✅ Yes |
| nginx config (Debian/Ubuntu) | `/opt/web/nginx-debian.conf` → `/etc/nginx/sites-available/so-toolbox` | ✅ Yes |
| systemd service file | `/opt/web/so-proxy.service` → `/etc/systemd/system/` | ✅ Yes |
| `.env` (tools, credentials) | `/opt/web/.env` | ❌ No — create manually |
| SSL certificates | `/etc/nginx/ssl/` | ❌ No — generate manually |
| Proxy logs | `journalctl -u so-proxy` | ❌ No |

---

## 9. Quick rebuild checklist

### CentOS / RHEL
- [ ] `yum install nginx git python3 python3-pip -y`
- [ ] `pip3 install flask flask-cors requests`
- [ ] `git clone https://github.com/marcusmarcal/SO-Toolbox.git /opt/web`
- [ ] Generate SSL certificate into `/etc/nginx/ssl/`
- [ ] `cp /opt/web/nginx.conf /etc/nginx/nginx.conf && rm -f /etc/nginx/conf.d/default.conf`
- [ ] `nginx -t && systemctl enable nginx && systemctl start nginx`
- [ ] Create `/opt/web/.env`
- [ ] `cp /opt/web/so-proxy.service /etc/systemd/system/`
- [ ] `systemctl daemon-reload && systemctl enable so-proxy && systemctl start so-proxy`
- [ ] Verify with curl checks in Section 7

### Debian / Ubuntu / WSL
- [ ] `apt update && apt install nginx git python3 python3-flask python3-requests -y`
- [ ] `pip3 install flask-cors --break-system-packages`
- [ ] `git clone https://github.com/marcusmarcal/SO-Toolbox.git /opt/web`
- [ ] Generate SSL certificate into `/etc/nginx/ssl/`
- [ ] `rm -f /etc/nginx/sites-enabled/default`
- [ ] `cp /opt/web/nginx-debian.conf /etc/nginx/sites-available/so-toolbox`
- [ ] `ln -s /etc/nginx/sites-available/so-toolbox /etc/nginx/sites-enabled/so-toolbox`
- [ ] `nginx -t && systemctl enable nginx && systemctl start nginx`
- [ ] Create `/opt/web/.env`
- [ ] `cp /opt/web/so-proxy.service /etc/systemd/system/`
- [ ] `systemctl daemon-reload && systemctl enable so-proxy && systemctl start so-proxy`
- [ ] Verify with curl checks in Section 7
