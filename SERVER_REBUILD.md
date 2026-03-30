# SO-Toolbox — Server Rebuild Guide

Everything needed to rebuild the server from scratch on a new machine.
The application code lives in Git — this document covers everything that does NOT.

---

## 1. Requirements

- OS: CentOS / RHEL (tested on the MSP8050 environment)
- Python 3.6+
- nginx
- git

---

## 2. System packages

```bash
yum install nginx git python3 python3-pip -y
pip3 install flask flask-cors requests
```

---

## 3. Clone the repository

```bash
mkdir -p /opt/web
cd /opt/web
git clone https://github.com/marcusmarcal/SO-Toolbox.git .
```

---

## 4. SSL certificate (self-signed)

```bash
mkdir -p /etc/nginx/ssl

openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout /etc/nginx/ssl/selfsigned.key \
  -out    /etc/nginx/ssl/selfsigned.crt \
  -subj "/CN=localhost"
```

---

## 5. nginx configuration

Replace `/etc/nginx/nginx.conf` entirely with the clean version from the repo:

```bash
cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak   # backup just in case
cp /opt/web/nginx.conf /etc/nginx/nginx.conf

# Remove old conf.d/default.conf if it exists — everything is now in nginx.conf
rm -f /etc/nginx/conf.d/default.conf

nginx -t && systemctl enable nginx && systemctl start nginx
```

The `nginx.conf` in the repo includes:

- HTTPS on 443 (`default_server`) serving `/opt/web`
- HTTP on 80 redirecting to HTTPS
- `/phenix-proxy/` reverse-proxied to Flask on `127.0.0.1:5050`
- `/.env` blocked (returns 404)
- Includes `nginx_http_defaults.conf` for logging and proxy defaults

> The old `nginx.conf` had legacy Id3as/perform.local upstreams and servers — all removed.
> The useful `nginx_http_defaults.conf` is still included and must remain in `/etc/nginx/`.

---

## 6. .env file

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

## 7. Proxy as a systemd service

```bash
cp /opt/web/phenix-proxy.service /etc/systemd/system/phenix-proxy.service

systemctl daemon-reload
systemctl enable phenix-proxy
systemctl start phenix-proxy
```

Check status and logs:

```bash
systemctl status phenix-proxy
journalctl -u phenix-proxy -f
```

Restart after changes:

```bash
systemctl restart phenix-proxy
```

---

## 8. Verify everything works

```bash
# nginx serving HTTPS
curl -sk https://localhost/ | head -5

# Proxy config endpoint
curl -sk https://localhost/phenix-proxy/config

# Git pull endpoint
curl -sk -X POST https://localhost/phenix-proxy/git-pull

# .env is blocked (must return 404)
curl -sk https://localhost/.env
```

---

## 9. What lives where

| What | Where | In Git? |
|------|-------|---------|
| App code (HTML, proxy.py) | `/opt/web/` | ✅ Yes |
| nginx config | `/opt/web/nginx.conf` → `/etc/nginx/nginx.conf` | ✅ Yes |
| systemd service file | `/opt/web/phenix-proxy.service` → `/etc/systemd/system/` | ✅ Yes |
| `.env` (tools, credentials) | `/opt/web/.env` | ❌ No — create manually |
| SSL certificates | `/etc/nginx/ssl/` | ❌ No — generate manually |
| Proxy logs | `journalctl -u phenix-proxy` | ❌ No |

---

## 10. Quick rebuild checklist

- [ ] `yum install nginx git python3 python3-pip -y`
- [ ] `pip3 install flask flask-cors requests`
- [ ] `git clone https://github.com/marcusmarcal/SO-Toolbox.git /opt/web`
- [ ] Generate SSL certificate into `/etc/nginx/ssl/`
- [ ] `cp /opt/web/nginx.conf /etc/nginx/nginx.conf`
- [ ] `rm -f /etc/nginx/conf.d/default.conf`
- [ ] `nginx -t && systemctl enable nginx && systemctl start nginx`
- [ ] Create `/opt/web/.env` with credentials and tool list
- [ ] `cp /opt/web/phenix-proxy.service /etc/systemd/system/`
- [ ] `systemctl daemon-reload && systemctl enable phenix-proxy && systemctl start phenix-proxy`
- [ ] Verify with curl checks in Section 8
