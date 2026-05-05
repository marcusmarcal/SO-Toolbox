# ═══════════════════════════════════════════════════════════
#  id3as DC Monitor — deployment notes
# ═══════════════════════════════════════════════════════════

# 1. Add to .env:
# ────────────────────────────────────────────────────────────
# id3as authentication — server-side only, never sent to browser
PRFAUTH=your-prfauth-token-here

# Add to TOOL_* list in .env:
TOOL_6=id3as-DC-Monitor.html|id3as DC Monitor|id3as channel & node monitoring|⛨|Monitoring|

# ────────────────────────────────────────────────────────────
# 2. Append proxy_id3as_patch.py into proxy.py
#    (before the `if __name__ == "__main__":` line)
# ────────────────────────────────────────────────────────────
#
#    cat proxy_id3as_patch.py >> proxy_tmp.py
#    # Then manually merge above the __main__ block, or:
#    head -n -3 proxy.py > proxy_new.py
#    cat proxy_id3as_patch.py >> proxy_new.py
#    echo "" >> proxy_new.py
#    echo "if __name__ == '__main__':" >> proxy_new.py
#    echo "    app.run(host='0.0.0.0', port=5050, threaded=True)" >> proxy_new.py
#    mv proxy_new.py proxy.py
#
# 3. Copy HTML to toolbox directory:
#    cp id3as-DC-Monitor.html /path/to/SO-Toolbox/
#
# 4. Restart proxy:
#    systemctl restart so-proxy
#
# 5. Verify endpoints work:
#    curl http://localhost:5050/id3as/ix/channels/default
#    curl http://localhost:5050/id3as/ix/flags/channels
#    curl http://localhost:5050/id3as/ix/running_events

# ────────────────────────────────────────────────────────────
# Proxy endpoint summary (id3as routes):
# ────────────────────────────────────────────────────────────
# GET /so-proxy/id3as/<dc>/channels/<variant>       → channel list (default | racing_uk)
# GET /so-proxy/id3as/<dc>/flags/channels           → active warnings
# GET /so-proxy/id3as/<dc>/running_events           → active events
# GET /so-proxy/id3as/<dc>/nodes                    → node list
# GET /so-proxy/id3as/<dc>/logs                     → today's system events
# GET /so-proxy/id3as/<dc>/logs/<y>/<m>/<d>         → logs for specific date
# GET /so-proxy/id3as/<dc>/channel/<id>/status      → single channel status
