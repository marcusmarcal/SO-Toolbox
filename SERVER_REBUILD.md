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
```

```bash
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

File: `/etc/nginx/conf.d/default.conf`

```nginx
server {
    listen 443 ssl;
    server_name _;
    root /opt/web;
    index index.html;

    ssl_certificate     /etc/nginx/ssl/selfsigned.crt;
    ssl_certificate_key /etc/nginx/ssl/selfsigned.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # Block direct access to .env
    location = /.env {
        deny all;
        return 404;
    }

    # Reverse proxy to Flask (port 5050)
    location /phenix-proxy/ {
        proxy_pass http://127.0.0.1:5050/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_pass_request_body on;
        proxy_pass_request_headers on;
    }

    # Serve static files
    location / {
        try_files $uri $uri/ =404;
    }
}

# Redirect HTTP → HTTPS
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}
```

```bash
nginx -t && nginx -s reload
# or if starting fresh:
systemctl enable nginx
systemctl start nginx
```

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
# NOT exposed to browser — only read by proxy.py
PHENIXRTS_APP_ID=your-app-id
PHENIXRTS_PASSWORD=your-password
```

> ⚠️ `SRT_PASSPHRASE`, `PHENIXRTS_APP_ID`, and `PHENIXRTS_PASSWORD` are sensitive.
> They are never sent to the browser — only `proxy.py` reads them server-side.

---

## 7. Start the proxy

```bash
cd /opt/web
python3 proxy.py &
```

To keep it running after logout, use `nohup`:

```bash
nohup python3 proxy.py > /var/log/phenix-proxy.log 2>&1 &
```

To check if it's running:

```bash
ps aux | grep proxy.py
curl -sk http://127.0.0.1:5050/config
```

---

## 8. Verify everything works

```bash
# nginx serving HTTPS
curl -sk https://localhost/ | head -5

# Proxy reachable via nginx
curl -sk https://localhost/phenix-proxy/config

# Git pull endpoint
curl -sk -X POST https://localhost/phenix-proxy/git-pull

# .env is blocked
curl -sk https://localhost/.env  # should return 404
```

---

## 9. What lives where

| What | Where | In Git? |
|------|-------|---------|
| App code (HTML, proxy.py) | `/opt/web/` | ✅ Yes |
| `.env` (tools, credentials) | `/opt/web/.env` | ❌ No — create manually |
| nginx config | `/etc/nginx/conf.d/default.conf` | ❌ No — see Section 5 |
| SSL certificates | `/etc/nginx/ssl/` | ❌ No — generate manually |
| Proxy logs | `/var/log/phenix-proxy.log` | ❌ No |

---

## 10. Quick rebuild checklist

- [ ] Install packages (`nginx`, `python3`, `pip3`, `git`)
- [ ] `pip3 install flask flask-cors requests`
- [ ] Clone repo to `/opt/web`
- [ ] Generate SSL certificate
- [ ] Write nginx config → `nginx -t && systemctl start nginx`
- [ ] Create `/opt/web/.env` with credentials and tool list
- [ ] Start proxy: `nohup python3 proxy.py > /var/log/phenix-proxy.log 2>&1 &`
- [ ] Verify with curl checks above
