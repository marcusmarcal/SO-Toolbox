# ═══════════════════════════════════════════════════════════
#  id3as DC Monitor — deployment notes
# ═══════════════════════════════════════════════════════════

# 1. Add to .env:
# ────────────────────────────────────────────────────────────
# id3as authentication — server-side only, never sent to browser
PRFAUTH=your-prfauth-token-here

# id3as DC hosts — server-side only, never hardcoded in source files
ID3AS_HOST_IX=id3as-ix.example.co.uk
ID3AS_HOST_EQ=id3as-eq.example.co.uk

# Add to TOOL_* list in .env:
TOOL_6=id3as-DC-Monitor.html|id3as DC Monitor|id3as channel & node monitoring|⛨|Monitoring|

# ────────────────────────────────────────────────────────────
# 2. Routes are loaded automatically via Blueprint — no changes to proxy.py needed.
#    proxy.py already contains:
#
#      from id3as_routes import id3as_bp
#      app.register_blueprint(id3as_bp)
#
#    id3as_routes.py must be present in the same directory as proxy.py.
# ────────────────────────────────────────────────────────────

# 3. Restart proxy:
#    systemctl restart so-proxy

# 4. Verify endpoints work:
#    curl http://localhost:5050/id3as/config
#    curl http://localhost:5050/id3as/ix/channels/default
#    curl http://localhost:5050/id3as/ix/flags/channels
#    curl http://localhost:5050/id3as/ix/running_events

# ────────────────────────────────────────────────────────────
# Proxy endpoint summary (id3as routes):
# ────────────────────────────────────────────────────────────
# GET /so-proxy/id3as/config                        → DC GUI base URLs (read from .env, used by browser)
# GET /so-proxy/id3as/<dc>/channels/<variant>       → channel list (default | racing_uk)
# GET /so-proxy/id3as/<dc>/flags/channels           → active warnings
# GET /so-proxy/id3as/<dc>/running_events           → active events
# GET /so-proxy/id3as/<dc>/nodes                    → node list
# GET /so-proxy/id3as/<dc>/logs                     → today's system events
# GET /so-proxy/id3as/<dc>/logs/<y>/<m>/<d>         → logs for specific date
# GET /so-proxy/id3as/<dc>/channel/<id>/status      → single channel status
