# SO-Toolbox — Server Rebuild Guide

Everything needed to rebuild the server from scratch on a new machine.
The application code lives in Git — this document covers everything that does NOT.

---

## 1. System packages

### CentOS / RHEL

```bash
yum install nginx git python3 python3-pip ffmpeg curl mtr -y
pip3 install flask flask-cors requests
```

### Debian / Ubuntu (incl. WSL)

```bash
apt update
apt install nginx git python3 python3-flask python3-requests ffmpeg curl mtr -y
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

### What the nginx config now includes (auth)

The nginx configs already contain everything below — no manual edits needed.
This section documents what was added and why.

**Sensitive file blocking** — `.env` and `users.json` both return 404:

```nginx
location ~ ^/(\.env|users\.json) {
    deny all;
    return 404;
}
```

**Auth check endpoint** — nginx calls `/so-proxy/_auth_check` silently before
serving any protected file. The Flask proxy validates the session cookie and
returns 200 (valid) or 401 (not authenticated):

```nginx
location = /so-proxy/_auth_check {
    internal;
    proxy_pass         http://127.0.0.1:5050/me;
    proxy_pass_request_body off;
    proxy_set_header   Content-Length "";
    proxy_set_header   Cookie $http_cookie;
}
```

**Public pages** — `login.html` and `users-admin.html` are served without
auth (they manage their own authentication):

```nginx
location = /login.html    { root /opt/web; }
location = /users-admin.html { root /opt/web; }
```

**Protected zone** — everything else requires a valid session; unauthenticated
requests are redirected to `/login.html?next=<original-url>`:

```nginx
location / {
    auth_request /so-proxy/_auth_check;
    error_page 401 = @login_redirect;
    try_files $uri $uri/ =404;
}

location @login_redirect {
    return 302 /login.html?next=$request_uri;
}
```

> `/so-proxy/*` is never subject to `auth_request` — it passes directly to
> Flask which manages its own auth (session tokens, admin password).

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

# id3as authentication — server-side only, never sent to browser
PRFAUTH=your-prfauth-token-here

# id3as DC hosts — server-side only, never hardcoded in source files
ID3AS_HOST_IX=id3as-ix.example.co.uk
ID3AS_HOST_EQ=id3as-eq.example.co.uk
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
>
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

| What                         | Where                                                                  | In Git?                   |
| ---------------------------- | ---------------------------------------------------------------------- | ------------------------- |
| App code (HTML, proxy.py)    | `/opt/web/`                                                            | ✅ Yes                    |
| nginx config (CentOS/RHEL)   | `/opt/web/nginx.conf` → `/etc/nginx/nginx.conf`                        | ✅ Yes                    |
| nginx config (Debian/Ubuntu) | `/opt/web/nginx-debian.conf` → `/etc/nginx/sites-available/so-toolbox` | ✅ Yes                    |
| systemd service file         | `/opt/web/so-proxy.service` → `/etc/systemd/system/`                   | ✅ Yes                    |
| `.env` (tools, credentials)  | `/opt/web/.env`                                                        | ❌ No — create manually   |
| SSL certificates             | `/etc/nginx/ssl/`                                                      | ❌ No — generate manually |
| Proxy logs                   | `journalctl -u so-proxy`                                               | ❌ No                     |

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

### srt-push service

```bash
sudo vi /etc/systemd/system/srt-push.service


-------------
[Unit]
Description=SRT Push Streaming Service (Chromium + FFmpeg)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/web/srt-push.py
Restart=always
RestartSec=5
User=root
Environment=DISPLAY=:99
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
-----------

sudo systemctl daemon-reload
sudo systemctl enable srt-push
sudo systemctl start srt-push

```

---

## 10. Authentication setup

### First run — seed the user database

`users.json` is NOT in Git. Create it manually before starting the proxy:

```bash
# Copy the committed template
cp /opt/web/users.json.template /opt/web/users.json

# Generate a SHA-256 hash for the initial admin password
echo -n "your-password-here" | sha256sum
# → e.g.  5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8

# Edit users.json and replace REPLACE_WITH_SHA256_OF_YOUR_PASSWORD
nano /opt/web/users.json
```

Alternatively, use the `users-admin.html` tool after the proxy is running —
it can create users (including the first admin) via the `POST /so-proxy/users`
endpoint protected by `ADMIN_PASSWORD`.

> ⚠️ `ADMIN_PASSWORD` in `.env` controls write access to the user database.
> It is independent of any user account — set it to a strong, unique value.

### .gitignore entries (add if not present)

```
users.json
```

`users.json` stores password hashes. It must never be committed.

### Protecting the main app (optional)

To redirect unauthenticated browsers to `login.html`, add to `proxy.py`:

```python
@app.route('/')
def index():
    token = request.cookies.get('sotb-session', '')
    if not _get_session(token):
        return redirect('/login.html')
    return send_from_directory('/opt/web', 'index.html')
```

Or enforce it at the nginx level with `auth_request` pointing to `/so-proxy/me`.

### Quick rebuild checklist (auth additions)

- [ ] Copy `users.json.template` → `users.json` and set initial admin hash
- [ ] Verify `users.json` is in `.gitignore`
- [ ] Add `users.json` block to nginx config and reload nginx
- [ ] Confirm `ADMIN_PASSWORD` is set in `.env`
- [ ] Open `users-admin.html` and create any additional users
- [ ] Test login at `/login.html`
