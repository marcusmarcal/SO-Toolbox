"""
routes_gop.py — GOP Analyzer Blueprint
SO-Toolbox v2.28.0

All /gop/* routes (run, upload, status, results, schedule, specs, overrides, delete).
GOP results JSON now includes a `username` field (from /me session) for every test —
"anonymous" when the request has no valid session.
Each result also includes a `workflow` field; specs are now stored per workflow
(dc_aminos_tp / rts / wb), each with its own specs JSON file on disk.

Registers all /gop/* routes (nginx strips /so-proxy prefix).
"""

import os
import re
import json
import uuid
import datetime
import subprocess
import threading
import tempfile
import shutil

from flask import Blueprint, request, jsonify, send_from_directory

from routes_auth import _get_session, _token_from_request

# ── Blueprint ─────────────────────────────────────────────────────────────
gop_bp = Blueprint('gop', __name__)

# ── Paths ─────────────────────────────────────────────────────────────────
_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
GOP_DIR    = os.path.join(_BASE_DIR, "gop-results")
SPECS_FILE = os.path.join(_BASE_DIR, "specs.json")
os.makedirs(GOP_DIR, exist_ok=True)

# ── Workflows ─────────────────────────────────────────────────────────────
# Each workflow has its own specs file. "dc_aminos_tp" keeps using the
# original specs.json so existing deployments don't lose their saved specs.
DEFAULT_WORKFLOW = "dc_aminos_tp"
WORKFLOW_SPECS_FILES = {
    "dc_aminos_tp": SPECS_FILE,
    "rts":          os.path.join(_BASE_DIR, "specs_rts.json"),
    "wb":           os.path.join(_BASE_DIR, "specs_wb.json"),
}


def _specs_file_for(workflow):
    """Return the specs.json path for a workflow, falling back to the default
    workflow's file for unknown/empty values."""
    return WORKFLOW_SPECS_FILES.get(workflow, SPECS_FILE)

# ── In-memory job stores ───────────────────────────────────────────────────
_gop_jobs      = {}
_gop_lock      = threading.Lock()
_gop_scheduled = {}
_gop_sched_lock = threading.Lock()


# ════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _get_username_from_request() -> str:
    """Return the logged-in username from the current request session,
    or 'anonymous' if there is no valid session."""
    session = _get_session(_token_from_request())
    if session:
        return session.get('username', 'anonymous')
    return 'anonymous'


def _get_user_and_role():
    """Return (username, role) for the current request.

    Reads from the auth session established by routes_auth.py.
    Returns ('anonymous', None) when there is no valid session.
    """
    session = _get_session(_token_from_request())
    if session:
        return session.get('username', 'anonymous'), session.get('role')
    return 'anonymous', None


def _check_password(req):
    """Validate X-Admin-Password header against ADMIN_PASSWORD in .env.
    Returns (ok: bool, error_response or None)."""
    env_path = os.path.join(_BASE_DIR, '.env')
    required = None
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('ADMIN_PASSWORD='):
                    required = line.split('=', 1)[1].strip()
                    break
    except Exception:
        pass
    if not required:
        return True, None
    provided = req.headers.get('X-Admin-Password', '')
    if provided == required:
        return True, None
    return False, (jsonify({'success': False, 'output': '❌ Invalid admin password.'}), 403)


# ════════════════════════════════════════════════════════════════════════════
#  SPECS
# ════════════════════════════════════════════════════════════════════════════

DEFAULT_SPECS = {
    "overall_br":   {"lo": 5.0,  "hi": 18.0, "pref_lo": 8.0,  "pref_hi": 15.0, "label": "Overall Bitrate (Mbps)"},
    "gop_size":     {"values": [30, 50], "tolerance": 3, "label": "GOP Size (frames)", "allow_seconds": True},
    "gop_type":     {"values": ["CLOSED", "OPEN"], "preferred": "CLOSED", "label": "GOP Type"},
    "b_frames":     {"values": ["absent", "present"], "preferred": "absent", "label": "B-Frames"},
    "idr":          {"required": True, "label": "IDR Frames"},
    "frame_size":   {"values": ["1280x720","1920x1080"], "preferred": "1920x1080", "label": "Frame Size"},
    "aspect_ratio": {"values": ["16:9"], "label": "Aspect Ratio"},
    "chroma":       {"values": ["4:2:0"], "label": "Chroma Subsampling"},
    "colour_range": {"values": ["limited", "full"], "preferred": "limited", "label": "Colour Range"},
    "scan_type":    {"values": ["progressive","interlaced","mbaff"], "preferred": "interlaced", "label": "Scan Type"},
    "bit_depth":    {"values": ["8"], "label": "Bit Depth"},
    "colour_gamut": {"values": ["unknown","bt709"], "preferred": "bt709", "label": "Colour Gamut"},
    "codec":        {"values": ["h264","hevc"], "preferred": "h264", "label": "Coding Algorithm"},
    "codec_level":  {"lo": 4.0, "hi": 4.2, "pref_lo": 4.1, "pref_hi": 4.2, "label": "CODEC Level"},
    "codec_profile":{"values": ["main","high","constrained baseline","baseline"], "preferred": "high", "label": "CODEC Profile"},
    "entropy":      {"values": ["CABAC"], "label": "Entropy"},
    "rate_ctrl_v":  {"values": ["VBR","CBR"], "preferred": "CBR", "label": "Rate Control (Video)"},
    "v_br":         {"lo": 5.0, "hi": 18.0, "pref_lo": 8.0, "pref_hi": 15.0, "label": "Video Bitrate (Mbps)"},
    "hdr_scheme":   {"values": ["SDR"], "label": "SDR/HDR Scheme"},
    "fps":          {"values": [25.0, 29.97, 30.0], "preferred": 25.0, "label": "Frame Rate",
                     "allow_50p_720": True},
    "a_codec":      {"values": ["AAC-LC","AAC-LATM","AAC-HE","MP1","MP2"], "preferred": "AAC-LC", "label": "Audio Coding"},
    "a_streams":    {"lo": 1, "hi": 32, "pref_lo": 2, "pref_hi": 2, "label": "Audio Streams"},
    "a_channels":   {"lo": 2, "hi": 2, "label": "Audio Channels"},
    "a_rate_ctrl":  {"values": ["VBR","CBR"], "preferred": "CBR", "label": "Audio Rate Control"},
    "a_sample_rate":{"lo": 44.1, "hi": 48.0, "pref_lo": 48.0, "pref_hi": 48.0, "label": "Sample Rate (kHz)"},
    "a_bits":       {"values": ["fltp","16","s16"], "preferred": "16", "label": "Audio Bits per Sample"},
    "a_br_kbps":    {"lo": 118, "hi": 512, "pref_lo": 256, "pref_hi": 256, "label": "Audio Bitrate (Kbps)"},
    # AV Sync & Timing — mode "inform" means never REJECT; "enforce" enables REJECTED status.
    "av_sync_warn": {"warn": 15.0,  "hard": 230.0, "mode": "inform", "label": "AV Sync Avg Offset (ms)"},
    "av_sync_max":  {"warn": 175.0, "hard": 230.0, "mode": "inform", "label": "AV Sync Max Offset (ms)"},
    "v_pts_jitter": {"warn": 5.0,   "hard": 10.0,  "mode": "inform", "label": "Video PTS Jitter (ms)"},
    "a_pts_jitter": {"warn": 5.0,   "hard": 10.0,  "mode": "inform", "label": "Audio PTS Jitter (ms)"},
}


def _load_specs(workflow=DEFAULT_WORKFLOW):
    """Load the specs file for a workflow and deep-merge with DEFAULT_SPECS
    so no field is lost. Preserves the ``_meta`` key (saved_by / saved_at)
    if present in the stored file."""
    import copy
    base = copy.deepcopy(DEFAULT_SPECS)
    specs_file = _specs_file_for(workflow)
    meta = {}
    if os.path.isfile(specs_file):
        try:
            with open(specs_file) as f:
                saved = json.load(f)
            # Extract and preserve _meta before merging
            meta = saved.pop("_meta", {})
            for key, val in saved.items():
                if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                    base[key].update(val)
                else:
                    base[key] = val
        except Exception:
            pass
    if meta:
        base["_meta"] = meta
    return base


def _save_specs(incoming, workflow=DEFAULT_WORKFLOW, username=None):
    """Deep-merge incoming with defaults then save complete specs for a workflow.
    Stamps ``_meta`` with saved_by / saved_at."""
    import copy
    merged = copy.deepcopy(DEFAULT_SPECS)
    # Strip _meta from incoming — we stamp it server-side
    incoming_clean = {k: v for k, v in incoming.items() if k != "_meta"}
    for key, val in incoming_clean.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key].update(val)
        else:
            merged[key] = val
    merged["_meta"] = {
        "saved_by": username or "unknown",
        "saved_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    with open(_specs_file_for(workflow), "w") as f:
        json.dump(merged, f, indent=2)


# ════════════════════════════════════════════════════════════════════════════
#  CORE ANALYSIS
# ════════════════════════════════════════════════════════════════════════════

def _run_gop_on_file(job_id, ts_path, tag, url_display, started_at, workflow=DEFAULT_WORKFLOW):
    """Run GOP analysis on an already-captured/uploaded .ts file (no ffmpeg capture)."""
    log_lines = []

    def log(msg):
        log_lines.append(msg)
        with _gop_lock:
            if job_id in _gop_jobs:
                _gop_jobs[job_id]["log"] = list(log_lines)

    try:
        log(f"Analysing file: {url_display}")
        ts_size = os.path.getsize(ts_path)
        log(f"File size: {ts_size:,} bytes")

        with _gop_lock:
            _gop_jobs[job_id]["_ts_path"] = ts_path

        original_name = url_display.split("upload:", 1)[-1] if url_display.startswith("upload:") else None
        _run_gop_analysis(job_id, f"file://{ts_path}", 9999, "", tag,
                          _started_at=started_at, _original_name=original_name,
                          workflow=workflow)

    except Exception as e:
        log(f"ERROR: {e}")
        with _gop_lock:
            _gop_jobs[job_id].update({
                "status": "error", "log": log_lines,
                "ended_at": datetime.datetime.utcnow().isoformat() + "Z"
            })


def _run_gop_analysis(job_id, url, duration, passphrase, tag, _started_at=None, _original_name=None, workflow=DEFAULT_WORKFLOW):
    """Background: capture SRT stream, run ffprobe frame analysis, parse GOP structure."""
    log_lines = []

    def log(msg):
        log_lines.append(msg)
        with _gop_lock:
            if job_id in _gop_jobs:
                _gop_jobs[job_id]["log"] = list(log_lines)

    url_display = re.sub(r'[?&]passphrase=[^&]*', '', url).rstrip('?&')

    is_file_upload = url.startswith("file://")
    ts_path_from_upload = url[7:] if is_file_upload else None

    m_host = re.search(r'srt://([^:/?]+):(\d+)', url_display)
    if is_file_upload:
        url_host = "upload"
        url_port = ""
        url_display = "upload:" + (_original_name or os.path.basename(ts_path_from_upload or ""))
    else:
        url_host = m_host.group(1) if m_host else url_display
        url_port = m_host.group(2) if m_host else ""

    ts_path = None
    cap_returncode = 0

    if _started_at and job_id in _gop_jobs:
        with _gop_lock:
            _gop_jobs[job_id]["started_at"] = _started_at

    # Retrieve username stored in job (set at job creation time)
    with _gop_lock:
        username = _gop_jobs.get(job_id, {}).get("username", "anonymous")

    try:
        log(f"Starting GOP analysis for: {url_display}")

        if is_file_upload:
            ts_path = ts_path_from_upload
            ts_size = os.path.getsize(ts_path) if ts_path and os.path.isfile(ts_path) else 0
            log(f"Using uploaded file: {ts_size:,} bytes")
        else:
            log(f"Capture duration: {duration}s")
            with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp:
                ts_path = tmp.name

            log("Capturing stream with ffmpeg…")
            cap_cmd = [
                "ffmpeg", "-y",
                "-timeout", str((duration + 10) * 1000000),
                "-i", url,
                "-t", str(duration),
                "-c", "copy",
                "-f", "mpegts",
                ts_path
            ]
            try:
                cap_result = subprocess.run(
                    cap_cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=duration + 45
                )
                cap_returncode = cap_result.returncode
                cap_out = cap_result.stdout.decode(errors="replace")
                log(f"ffmpeg capture done (exit {cap_returncode})")
            except subprocess.TimeoutExpired:
                log("WARNING: ffmpeg timed out — analysing partial capture if available")
                cap_out = ""
                cap_returncode = -1

            ts_size = os.path.getsize(ts_path) if (ts_path and os.path.isfile(ts_path)) else 0
            log(f"Captured {ts_size:,} bytes")

        if ts_size < 500:
            log("ERROR: Capture produced no usable data. Is the stream reachable?")
            if not is_file_upload and 'cap_out' in dir():
                log(cap_out[-800:])
            ended_at = datetime.datetime.utcnow().isoformat() + "Z"
            err_result = {
                "url": url_display, "url_host": url_host, "url_port": url_port,
                "tag": tag, "username": username,
                "started_at": _gop_jobs.get(job_id, {}).get("started_at", ""),
                "ended_at": ended_at,
                "status": "failed",
                "error": "Stream unreachable or produced no data",
                "log": log_lines,
                "has_idr": False, "idr_count": 0, "total_frames": 0,
                "overall_status": "FAILED", "is_scheduled": False, "override": None,
            }
            ts_str   = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            safe_url = re.sub(r"[^\w\-]", "_", url_display)[:40]
            res_file = f"{ts_str}_{safe_url}_FAILED.json"
            try:
                with open(os.path.join(GOP_DIR, res_file), "w") as f:
                    json.dump(err_result, f, indent=2)
                log(f"Failure log saved: {res_file}")
            except Exception as ex:
                log(f"WARNING: Could not save failure log: {ex}")
            try:
                if ts_path and os.path.isfile(ts_path): os.remove(ts_path)
            except Exception: pass
            with _gop_lock:
                _gop_jobs[job_id].update({
                    "status": "failed", "log": log_lines,
                    "ended_at": ended_at,
                    "res_file": res_file,
                    "result": err_result,
                })
            return

        # ── Stream info ───────────────────────────────────────────────
        log("Running ffprobe for stream info…")
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", "-show_programs", ts_path
        ]
        probe_data = {}
        try:
            r = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            probe_data = json.loads(r.stdout.decode())
        except Exception as e:
            log(f"WARNING: ffprobe stream info failed: {e}")

        # ── Frame analysis ────────────────────────────────────────────
        log("Running ffprobe frame analysis (NAL/IDR detection)…")
        frame_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-select_streams", "v:0",
            "-show_frames",
            "-show_entries",
            "frame=pict_type,key_frame,pts_time,coded_picture_number,side_data_list",
            ts_path
        ]
        frames_data = []
        try:
            r = subprocess.run(frame_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
            fd = json.loads(r.stdout.decode())
            frames_data = fd.get("frames", [])
        except Exception as e:
            log(f"WARNING: Frame analysis failed: {e}")

        log(f"Analysed {len(frames_data)} video frames")

        # ── AV sync ───────────────────────────────────────────────────
        log("Running ffprobe for AV sync analysis…")
        av_sync = {"av_sync_min_ms": None, "av_sync_max_ms": None,
                   "av_sync_avg_ms": None, "av_sync_median_ms": None,
                   "v_pts_jitter_ms": None, "a_pts_jitter_ms": None}
        try:
            def _get_pts(f):
                for key in ("pts_time", "pkt_dts_time"):
                    val = f.get(key)
                    if val not in (None, "N/A"):
                        try:
                            return float(val)
                        except (ValueError, TypeError):
                            pass
                return None

            def _probe_pts(stream_spec):
                cmd = [
                    "ffprobe", "-v", "error", "-print_format", "json",
                    "-select_streams", stream_spec,
                    "-show_frames",
                    "-show_entries", "frame=pts_time,pkt_dts_time",
                    ts_path
                ]
                r = subprocess.run(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=60)
                stderr_out = r.stderr.decode(errors="replace").strip()
                if stderr_out:
                    log(f"ffprobe [{stream_spec}] stderr: {stderr_out[:200]}")
                frames = json.loads(r.stdout.decode()).get("frames", [])
                pts = sorted([t for f in frames for t in [_get_pts(f)] if t is not None])
                log(f"ffprobe [{stream_spec}]: {len(frames)} frames, {len(pts)} valid PTS")
                return pts

            v_pts = _probe_pts("v:0")
            a_pts = _probe_pts("a:0")

            if v_pts and a_pts:
                offsets = []
                a_idx = 0
                for vt in v_pts:
                    while a_idx + 1 < len(a_pts) and abs(a_pts[a_idx+1] - vt) < abs(a_pts[a_idx] - vt):
                        a_idx += 1
                    offsets.append(abs(vt - a_pts[a_idx]) * 1000)

                if offsets:
                    av_sync["av_sync_min_ms"]    = round(min(offsets), 2)
                    av_sync["av_sync_max_ms"]    = round(max(offsets), 2)
                    av_sync["av_sync_avg_ms"]    = round(sum(offsets)/len(offsets), 2)
                    s_off = sorted(offsets)
                    mid = len(s_off) // 2
                    av_sync["av_sync_median_ms"] = round(
                        s_off[mid] if len(s_off) % 2 else (s_off[mid-1]+s_off[mid])/2, 2)

            def _jitter(pts_list):
                if len(pts_list) < 2:
                    return 0.0
                diffs = [abs(pts_list[i+1] - pts_list[i]) for i in range(len(pts_list)-1)]
                avg = sum(diffs) / len(diffs)
                variations = [abs(d - avg) * 1000 for d in diffs]
                return round(sum(variations)/len(variations), 2)

            if len(v_pts) > 2:
                av_sync["v_pts_jitter_ms"] = _jitter(v_pts)
            if len(a_pts) > 2:
                av_sync["a_pts_jitter_ms"] = _jitter(a_pts)

            log(f"AV sync: min={av_sync['av_sync_min_ms']}ms max={av_sync['av_sync_max_ms']}ms "
                f"avg={av_sync['av_sync_avg_ms']}ms jitter V={av_sync['v_pts_jitter_ms']}ms "
                f"A={av_sync['a_pts_jitter_ms']}ms")
        except Exception as e:
            log(f"WARNING: AV sync analysis failed: {e}")

        # ── GOP parsing ───────────────────────────────────────────────
        gops = []
        current_gop = []
        idr_count = 0
        non_idr_keyframe_count = 0
        total_frames = len(frames_data)

        for frame in frames_data:
            ptype   = frame.get("pict_type", "?")
            is_key  = frame.get("key_frame", 0) == 1
            pts_t   = float(frame.get("pts_time", 0) or 0)
            is_idr  = is_key and ptype == "I"

            if is_key:
                if is_idr:
                    idr_count += 1
                else:
                    non_idr_keyframe_count += 1
                if current_gop:
                    gops.append(current_gop)
                current_gop = [{"type": ptype, "key": True, "idr": is_idr, "pts": pts_t}]
            else:
                current_gop.append({"type": ptype, "key": False, "idr": False, "pts": pts_t})

        if current_gop:
            gops.append(current_gop)

        complete_gops = gops[:-1] if len(gops) > 1 else gops

        def _is_open_gop(gop_list):
            for g in gop_list:
                if len(g) > 1:
                    types_in_gop = [f["type"] for f in g]
                    non_i = [t for t in types_in_gop if t != "I"]
                    if non_i and non_i[0] == "B":
                        return True
            return False

        gop_type = "OPEN" if _is_open_gop(complete_gops) else "CLOSED"

        gop_lengths  = [len(g) for g in complete_gops]
        gop_patterns = ["".join(f["type"] for f in g) for g in complete_gops]

        has_b_frames  = any(f["type"] == "B" for g in complete_gops for f in g)
        b_frame_count = sum(1 for g in complete_gops for f in g if f["type"] == "B")
        has_idr       = idr_count > 0

        avg_gop = round(sum(gop_lengths) / len(gop_lengths), 1) if gop_lengths else 0
        min_gop = min(gop_lengths) if gop_lengths else 0
        max_gop = max(gop_lengths) if gop_lengths else 0

        # ── Stream metadata ───────────────────────────────────────────
        streams    = probe_data.get("streams", [])
        fmt        = probe_data.get("format", {})
        vid        = next((s for s in streams if s.get("codec_type") == "video"), {})
        aud_list   = [s for s in streams if s.get("codec_type") == "audio"]
        aud        = aud_list[0] if aud_list else {}

        container   = fmt.get("format_long_name") or fmt.get("format_name", "unknown")
        file_dur    = float(fmt.get("duration", 0) or 0)
        file_br     = int(fmt.get("bit_rate", 0) or 0)
        num_prog    = int(fmt.get("nb_programs", 1) or 1)
        num_streams = int(fmt.get("nb_streams", len(streams)) or len(streams))

        v_codec     = vid.get("codec_name", "unknown")
        v_profile   = vid.get("profile", "unknown")
        v_level_raw = vid.get("level", 0)
        v_level_str = f"{v_level_raw/10:.1f}" if isinstance(v_level_raw, int) and v_level_raw > 9 else str(v_level_raw)
        v_level_f   = float(v_level_raw) / 10 if isinstance(v_level_raw, int) and v_level_raw > 9 else 0
        v_width     = vid.get("width", 0)
        v_height    = vid.get("height", 0)
        v_pix_fmt   = vid.get("pix_fmt", "unknown")
        v_b_frames  = vid.get("has_b_frames", 0)
        v_refs      = vid.get("refs", "?")
        v_br_raw    = vid.get("bit_rate")
        v_br        = int(v_br_raw or 0)
        a_br_raw    = aud.get("bit_rate")
        a_br        = int(a_br_raw or 0)
        if v_br == 0 and file_br:
            a_br_est = a_br if a_br else 192000 * len(aud_list)
            v_br = max(0, file_br - a_br_est)
        if a_br == 0 and file_br and v_br:
            a_br = max(0, file_br - v_br)
        v_color_sp  = vid.get("color_space", "unknown")
        v_color_tr  = vid.get("color_transfer", "unknown")
        v_field     = vid.get("field_order", "progressive")
        v_bits      = vid.get("bits_per_raw_sample") or vid.get("bits_per_coded_sample") or "?"

        scan_map = {"progressive":"progressive","tt":"interlaced","bb":"interlaced",
                    "tb":"interlaced","bt":"interlaced","unknown":"progressive"}
        v_scan = scan_map.get(v_field, v_field)

        r_fps_raw = vid.get("r_frame_rate", "0/1")
        def _fps_val(raw):
            try: n, d = raw.split("/"); return float(n)/float(d) if float(d) else 0
            except: return 0
        v_fps_val = _fps_val(r_fps_raw)

        v_fps_for_compliance = v_fps_val
        if v_scan == "interlaced" and v_fps_val > 30:
            v_fps_for_compliance = v_fps_val / 2
        v_fps_interlaced_note = ""
        if v_scan == "interlaced" and v_fps_val > 30:
            v_fps_interlaced_note = f" (50i→{v_fps_for_compliance:.3f}fps)"
        v_fps_str = f"{r_fps_raw} | {v_fps_val:.3f}{v_fps_interlaced_note}"

        dar = vid.get("display_aspect_ratio", "")
        if not dar and v_width and v_height:
            from math import gcd; g = gcd(v_width, v_height); dar = f"{v_width//g}:{v_height//g}"

        chroma_map  = {
            "yuv420p":  "4:2:0", "yuvj420p":  "4:2:0",
            "yuv422p":  "4:2:2", "yuvj422p":  "4:2:2",
            "yuv444p":  "4:4:4", "yuvj444p":  "4:4:4",
        }
        v_chroma       = chroma_map.get(v_pix_fmt, v_pix_fmt)
        v_full_range   = v_pix_fmt.startswith("yuvj")
        v_entropy   = "CABAC" if v_profile in ("High","Main","High 10","High 422","High 444") else "CAVLC"

        hdr_transfers = ("smpte2084", "smpte428")
        if v_color_tr in hdr_transfers:
            v_hdr = "HDR"
        elif v_color_tr == "arib-std-b67" and v_scan == "progressive":
            v_hdr = "HDR"
        else:
            v_hdr = "SDR"

        def _audio_display_name(codec, profile):
            c = (codec or "").lower()
            p = (profile or "").upper()
            if c == "aac":
                if "LATM" in p:     return "AAC-LATM"
                if "HE" in p:       return "AAC-HE"
                if "LD" in p:       return "AAC-LD"
                if "ELD" in p:      return "AAC-ELD"
                return f"AAC-{p}" if p and p != "?" and p != "UNKNOWN" else "AAC-LC"
            if c in ("mp1","mp2","mp3"): return c.upper()
            return codec.upper() if codec else "?"

        a_codec    = aud.get("codec_name", "unknown")
        a_profile  = aud.get("profile", "?")
        a_codec_display = _audio_display_name(a_codec, a_profile)
        a_ch       = aud.get("channels", 0)
        a_layout   = aud.get("channel_layout", "?")
        a_rate     = aud.get("sample_rate", "?")
        a_lang     = aud.get("tags", {}).get("language", "?")

        a_bps_raw  = aud.get("bits_per_raw_sample") or aud.get("bits_per_coded_sample")
        if a_bps_raw and int(a_bps_raw) > 0:
            a_bps  = str(int(a_bps_raw))
        elif a_codec in ("aac", "mp3", "mp2", "mp1", "opus", "vorbis"):
            a_bps  = "FLTP"
        else:
            a_bps  = "?"
        a_br_kbps  = round(a_br / 1000) if a_br else 0

        all_audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        programs = probe_data.get("programs", [])
        if programs:
            audio_indices = set()
            for prog in programs:
                for s in prog.get("streams", []):
                    if s.get("codec_type") == "audio":
                        audio_indices.add(s.get("index", id(s)))
            audio_track_count = len(audio_indices) if audio_indices else len(all_audio_streams)
        else:
            audio_track_count = len(all_audio_streams)

        v_rate_ctrl = "CBR" if file_br and v_br and abs(file_br - v_br) < file_br * 0.1 else "VBR"

        def _av_check(measured_ms, sp):
            warn  = float(sp.get("warn", 15.0))
            hard  = float(sp.get("hard", 230.0))
            mode  = sp.get("mode", "inform")  # "inform" = informational only, never affects overall
            if measured_ms is None:
                return ("UNKNOWN", "—", "Could not measure")
            m = round(measured_ms, 2)
            if mode == "inform":
                note = f"< {warn}ms preferred" if m < warn else (
                       f"< {hard}ms limit" if m < hard else f"Exceeds {hard}ms")
                return ("INFO", f"{m} ms", f"{note} (inform only)")
            if m < warn:
                return ("COMPLIANT", f"{m} ms", f"< {warn}ms preferred")
            if m < hard:
                return ("ACCEPTED", f"{m} ms", f"< {hard}ms limit; prefer < {warn}ms")
            return ("REJECTED", f"{m} ms", f"Exceeds hard limit of {hard}ms")

        # ── Compliance ────────────────────────────────────────────────
        specs = _load_specs(workflow)

        def _s(key):
            return specs.get(key, DEFAULT_SPECS.get(key, {}))

        def comply_range(measured, key):
            sp = _s(key)
            lo, hi = sp.get("lo", 0), sp.get("hi", float("inf"))
            plo = sp.get("pref_lo")
            phi = sp.get("pref_hi")
            if measured is None:
                return "UNKNOWN", "—", ""
            in_range = lo <= measured <= hi
            if not in_range:
                return "REJECTED", str(measured), f"Expected {lo}–{hi}"
            if plo is not None and phi is not None:
                if plo <= measured <= phi:
                    return "COMPLIANT", str(measured), ""
                return "ACCEPTED", str(measured), f"Preferred {plo}–{phi}"
            return "COMPLIANT", str(measured), ""

        def comply_enum_multi(measured, key):
            sp = _s(key)
            allowed    = [str(v).lower() for v in sp.get("values", [])]
            pref_raw   = sp.get("preferred", "")
            if isinstance(pref_raw, list):
                preferred = [str(p).lower() for p in pref_raw]
            else:
                preferred = [str(pref_raw).lower()] if pref_raw else []
            m = str(measured).strip().lower()
            if not allowed:
                return "UNKNOWN", measured, "No spec defined"
            if m not in allowed:
                return "REJECTED", measured, f"Expected one of {sp.get('values', [])}"
            if preferred and m in preferred:
                return "COMPLIANT", measured, ""
            if preferred:
                return "ACCEPTED", measured, f"Preferred {pref_raw}"
            return "COMPLIANT", measured, ""

        file_br_mbps = round(file_br / 1e6, 5) if file_br else 0
        v_br_mbps    = round(v_br   / 1e6, 5) if v_br   else 0
        a_br_kbps_f  = round(a_br   / 1000, 1) if a_br  else 0
        a_rate_khz   = round(float(a_rate) / 1000, 1) if str(a_rate).isdigit() else 0

        fps_eff    = v_fps_for_compliance
        fps_sp     = _s("fps")
        fps_values = [float(v) for v in fps_sp.get("values", [25.0, 29.97, 30.0])]
        fps_pref   = fps_sp.get("preferred", 25.0)
        allow_50p_720 = fps_sp.get("allow_50p_720", False)

        fps_to_check = fps_eff
        fps_50p_720_accepted = False
        if allow_50p_720 and v_height == 720 and abs(fps_eff - 50.0) < 0.5:
            fps_to_check = 50.0
            fps_values = list(fps_values) + [50.0]
            fps_50p_720_accepted = True

        fps_ok = any(abs(fps_to_check - f) < 0.1 for f in fps_values)
        fps_pref_ok = isinstance(fps_pref, (int, float)) and abs(fps_to_check - float(fps_pref)) < 0.1
        if not fps_ok:
            fps_status = "REJECTED"
        elif fps_pref_ok:
            fps_status = "COMPLIANT"
        else:
            fps_status = "ACCEPTED"
        fps_measured_str = f"{fps_eff:.3f}" + (" (50i→25fps)" if v_scan == "interlaced" and v_fps_val != fps_eff else "")
        if fps_50p_720_accepted:
            fps_measured_str += " [accepted: 50p @ 720p]"

        gop_sp      = _s("gop_size")
        gop_values  = [int(v) for v in gop_sp.get("values", [30, 50])]
        gop_tol     = int(gop_sp.get("tolerance", 3))
        allow_secs  = gop_sp.get("allow_seconds", True)

        if allow_secs and avg_gop > 0:
            fps_for_gop = fps_eff if fps_eff > 0 else 25.0
            gop_values_ext = list(gop_values)
            for secs in [1, 2]:
                gop_values_ext.append(round(fps_for_gop * secs))
            gop_values_check = gop_values_ext
        else:
            gop_values_check = gop_values

        gop_exact = any(abs(avg_gop - g) < 1 for g in gop_values_check)
        gop_near  = any(abs(avg_gop - g) <= gop_tol for g in gop_values_check)
        gop_status = "COMPLIANT" if gop_exact else ("ACCEPTED" if gop_near else "REJECTED")
        gop_expected_str = ", ".join(str(v) for v in gop_values)
        if allow_secs:
            gop_expected_str += " or 1s/2s"

        compliance = {
            "overall_br":   comply_range(file_br_mbps, "overall_br"),
            "gop_size":     (gop_status, str(avg_gop), f"Expected {gop_expected_str}"),
            "gop_type":     comply_enum_multi(gop_type, "gop_type"),
            "b_frames":     comply_enum_multi("absent" if not has_b_frames else "present", "b_frames"),
            "idr":          ("COMPLIANT" if has_idr else "REJECTED",
                             "Present" if has_idr else "ABSENT", "IDR frames required"),
            "frame_size":   comply_enum_multi(f"{v_width}x{v_height}", "frame_size"),
            "aspect_ratio": comply_enum_multi(dar, "aspect_ratio"),
            "chroma":       comply_enum_multi(v_chroma, "chroma"),
            "colour_range": (lambda res: (res[0], v_pix_fmt, res[2]))(
                                comply_enum_multi("full" if v_full_range else "limited", "colour_range")),
            "scan_type":    comply_enum_multi(v_scan, "scan_type"),
            "bit_depth":    comply_enum_multi(str(v_bits), "bit_depth"),
            "colour_gamut": comply_enum_multi(v_color_sp, "colour_gamut"),
            "codec":        comply_enum_multi(v_codec.lower() if v_codec else "", "codec"),
            "codec_level":  comply_range(v_level_f, "codec_level"),
            "codec_profile":comply_enum_multi(v_profile.lower() if v_profile else "", "codec_profile"),
            "entropy":      comply_enum_multi(v_entropy, "entropy"),
            "rate_ctrl_v":  comply_enum_multi(v_rate_ctrl, "rate_ctrl_v"),
            "v_br":         comply_range(v_br_mbps, "v_br"),
            "hdr_scheme":   comply_enum_multi(v_hdr, "hdr_scheme"),
            "fps":          (fps_status, fps_measured_str, f"Expected {fps_values}"),
            "a_codec":      comply_enum_multi(a_codec_display, "a_codec"),
            "a_streams":    comply_range(audio_track_count, "a_streams"),
            "a_channels":   comply_range(a_ch, "a_channels"),
            "a_rate_ctrl":  comply_enum_multi("VBR", "a_rate_ctrl"),
            "a_sample_rate":comply_range(a_rate_khz, "a_sample_rate"),
            "a_bits":       comply_enum_multi(a_bps.lower(), "a_bits"),
            "a_br_kbps":    comply_range(a_br_kbps_f, "a_br_kbps"),
            "av_sync_warn": _av_check(av_sync.get("av_sync_avg_ms"),  _s("av_sync_warn")),
            "av_sync_max":  _av_check(av_sync.get("av_sync_max_ms"),  _s("av_sync_max")),
            "v_pts_jitter": _av_check(av_sync.get("v_pts_jitter_ms"), _s("v_pts_jitter")),
            "a_pts_jitter": _av_check(av_sync.get("a_pts_jitter_ms"), _s("a_pts_jitter")),
        }

        statuses = [v[0] for v in compliance.values() if v[0] != "INFO"]
        if "REJECTED" in statuses:
            overall_status = "REJECTED"
        elif "ACCEPTED" in statuses:
            overall_status = "ACCEPTED"
        else:
            overall_status = "COMPLIANT"

        result = {
            "url": url_display, "url_host": url_host, "url_port": url_port,
            "tag": tag, "username": username, "workflow": workflow,
            "started_at": _gop_jobs[job_id].get("started_at",""),
            "file_size": ts_size, "file_dur": file_dur, "file_br": file_br,
            "file_br_mbps": file_br_mbps,
            "container": container, "num_programs": num_prog, "num_streams": num_streams,
            "have_video": 1 if vid else 0, "have_audio": len(aud_list),
            "v_codec": v_codec, "v_profile": v_profile, "v_level": v_level_str,
            "v_level_f": v_level_f, "v_width": v_width, "v_height": v_height,
            "v_fps": v_fps_str, "v_fps_val": v_fps_val, "v_fps_compliance": v_fps_for_compliance,
            "v_pix_fmt": v_pix_fmt, "v_b_frames": v_b_frames, "v_refs": v_refs,
            "v_br": v_br, "v_br_mbps": v_br_mbps,
            "v_color_sp": v_color_sp, "v_color_tr": v_color_tr,
            "v_color_combined": f"{v_color_sp} | {v_color_tr}",
            "v_field": v_field, "v_scan": v_scan,
            "v_bits": str(v_bits), "v_chroma": v_chroma, "v_dar": dar,
            "v_entropy": v_entropy, "v_hdr": v_hdr, "v_rate_ctrl": v_rate_ctrl,
            "a_codec": a_codec, "a_codec_display": a_codec_display,
            "a_profile": a_profile, "a_channels": a_ch,
            "a_layout": a_layout, "a_rate": a_rate, "a_rate_khz": a_rate_khz,
            "a_br": a_br, "a_br_kbps": a_br_kbps_f, "a_lang": a_lang,
            "a_bps": str(a_bps), "audio_tracks": audio_track_count,
            "has_idr": has_idr, "idr_count": idr_count,
            "non_idr_keyframes": non_idr_keyframe_count,
            "total_frames": total_frames, "has_b_frames": has_b_frames,
            "b_frame_count": b_frame_count, "gop_type": gop_type,
            "gop_count": len(complete_gops), "gop_avg": avg_gop,
            "gop_min": min_gop, "gop_max": max_gop,
            "gop_patterns": gop_patterns[:20],
            "gops": [[{"type":f["type"],"key":f["key"],"idr":f.get("idr",False)}
                       for f in g] for g in complete_gops[:20]],
            "compliance": compliance,
            "specs": specs,
            "overall_status": overall_status,
            "test_id": str(uuid.uuid4()),
            "av_sync_min_ms":    av_sync.get("av_sync_min_ms"),
            "av_sync_max_ms":    av_sync.get("av_sync_max_ms"),
            "av_sync_avg_ms":    av_sync.get("av_sync_avg_ms"),
            "av_sync_median_ms": av_sync.get("av_sync_median_ms"),
            "v_pts_jitter_ms":   av_sync.get("v_pts_jitter_ms"),
            "a_pts_jitter_ms":   av_sync.get("a_pts_jitter_ms"),
        }

        ts_str   = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        safe_url = re.sub(r"[^\w\-]", "_", url_display)[:40]
        res_file = f"{ts_str}_{safe_url}.json"
        ts_dest  = os.path.join(GOP_DIR, res_file.replace(".json", ".ts"))

        if ts_path and os.path.isfile(ts_path):
            try:
                if is_file_upload and os.path.dirname(ts_path) == GOP_DIR:
                    ts_dest  = ts_path
                    ts_saved = os.path.basename(ts_dest)
                else:
                    shutil.move(ts_path, ts_dest)
                    ts_saved = os.path.basename(ts_dest)
                log(f"TS saved: {ts_saved}")
            except Exception as e:
                log(f"WARNING: Could not save .ts file: {e}")
                ts_saved = None
        else:
            ts_saved = None

        result["ts_file"] = ts_saved
        result["override"] = None

        with open(os.path.join(GOP_DIR, res_file), "w") as f:
            json.dump(result, f, indent=2)
        log(f"Result saved: {res_file}")

        ended_at = datetime.datetime.utcnow().isoformat() + "Z"
        with _gop_lock:
            _gop_jobs[job_id].update({
                "status": "done", "ended_at": ended_at,
                "result": result, "res_file": res_file, "log": log_lines,
            })

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        try:
            if ts_path and os.path.isfile(ts_path): os.remove(ts_path)
        except Exception: pass

        ended_at = datetime.datetime.utcnow().isoformat() + "Z"
        err_result = {
            "url": url_display if 'url_display' in dir() else url,
            "url_host": url_host if 'url_host' in dir() else "",
            "url_port": url_port if 'url_port' in dir() else "",
            "tag": tag, "username": username, "workflow": workflow,
            "started_at": _gop_jobs.get(job_id, {}).get("started_at", ""),
            "ended_at": ended_at,
            "status": "error",
            "error": str(e),
            "log": log_lines,
            "has_idr": False, "idr_count": 0, "total_frames": 0,
            "overall_status": "ERROR", "is_scheduled": False, "override": None,
        }
        try:
            ts_str   = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            safe_url = re.sub(r"[^\w\-]", "_", err_result["url"])[:40]
            res_file = f"{ts_str}_{safe_url}_ERROR.json"
            with open(os.path.join(GOP_DIR, res_file), "w") as f:
                json.dump(err_result, f, indent=2)
            log_lines.append(f"Error log saved: {res_file}")
            err_result["log"] = log_lines
        except Exception as ex2:
            res_file = None
            log_lines.append(f"WARNING: Could not save error log: {ex2}")

        with _gop_lock:
            _gop_jobs[job_id].update({
                "status":   "error",
                "log":      log_lines,
                "ended_at": ended_at,
                "res_file": res_file,
                "result":   err_result,
            })


# ════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════════════════════

@gop_bp.route("/gop/run", methods=["POST"])
def gop_run():
    data       = request.get_json(silent=True) or {}
    url        = (data.get("url") or "").strip()
    duration   = min(int(data.get("duration") or 30), 120)
    passphrase = (data.get("passphrase") or "").strip()
    tag        = (data.get("tag") or "").strip()
    workflow   = (data.get("workflow") or DEFAULT_WORKFLOW).strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    if passphrase and "passphrase=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}passphrase={passphrase}"

    job_id     = str(uuid.uuid4())[:8]
    started_at = datetime.datetime.utcnow().isoformat() + "Z"
    username   = _get_username_from_request()

    with _gop_lock:
        _gop_jobs[job_id] = {
            "job_id":     job_id,
            "status":     "running",
            "started_at": started_at,
            "ended_at":   None,
            "url":        re.sub(r'[?&]passphrase=[^&]*', '', url).rstrip('?&'),
            "tag":        tag,
            "username":   username,
            "workflow":   workflow,
            "result":     None,
            "log":        [],
        }

    t = threading.Thread(target=_run_gop_analysis,
                         args=(job_id, url, duration, passphrase, tag),
                         kwargs={"workflow": workflow}, daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@gop_bp.route("/gop/upload", methods=["POST"])
def gop_upload():
    """Accept an uploaded .ts file and run GOP analysis on it (skips capture step)."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".ts"):
        return jsonify({"error": "Only .ts files are supported"}), 400

    tag      = (request.form.get("tag") or "").strip()
    workflow = (request.form.get("workflow") or DEFAULT_WORKFLOW).strip()
    username = _get_username_from_request()

    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False, dir=GOP_DIR) as tmp:
        ts_save_path = tmp.name
    f.save(ts_save_path)

    if os.path.getsize(ts_save_path) < 500:
        os.remove(ts_save_path)
        return jsonify({"error": "Uploaded file is empty or too small"}), 400

    job_id      = str(uuid.uuid4())[:8]
    started_at  = datetime.datetime.utcnow().isoformat() + "Z"
    url_display = f"upload:{f.filename}"

    with _gop_lock:
        _gop_jobs[job_id] = {
            "job_id":     job_id,
            "status":     "running",
            "started_at": started_at,
            "ended_at":   None,
            "url":        url_display,
            "tag":        tag,
            "username":   username,
            "workflow":   workflow,
            "result":     None,
            "log":        [],
        }

    t = threading.Thread(
        target=_run_gop_on_file,
        args=(job_id, ts_save_path, tag, url_display, started_at),
        kwargs={"workflow": workflow},
        daemon=True
    )
    t.start()
    return jsonify({"job_id": job_id})


@gop_bp.route("/gop/status/<job_id>", methods=["GET"])
def gop_status(job_id):
    with _gop_lock:
        job = _gop_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@gop_bp.route("/gop/jobs/running", methods=["GET"])
def gop_jobs_running():
    """List all in-progress jobs (status == 'running'), regardless of which
    client started them — HTML frontend, Chrome extension, or any other
    API caller. Log lines are omitted to keep the polling payload small."""
    with _gop_lock:
        running = [
            {k: v for k, v in job.items() if k != "log"}
            for job in _gop_jobs.values()
            if job.get("status") == "running"
        ]
    return jsonify(running)


@gop_bp.route("/gop/results", methods=["GET"])
def gop_results():
    files = sorted([f for f in os.listdir(GOP_DIR) if f.endswith(".json")], reverse=True)
    items = []
    for f in files[:500]:
        try:
            with open(os.path.join(GOP_DIR, f)) as fh:
                d = json.load(fh)
            override      = d.get("override")
            raw_status    = d.get("status", "done")
            raw_ov_status = d.get("overall_status", "UNKNOWN")
            if override:
                eff_status = "ACCEPTED (Override)"
            elif raw_status in ("failed", "error"):
                eff_status = raw_status.upper()
            else:
                eff_status = raw_ov_status

            v_fps_val        = d.get("v_fps_val", 0)
            v_fps_compliance = d.get("v_fps_compliance", v_fps_val)
            v_scan           = d.get("v_scan", "progressive")
            items.append({
                "file":             f,
                "url":              d.get("url", ""),
                "url_host":         d.get("url_host", ""),
                "url_port":         d.get("url_port", ""),
                "tag":              d.get("tag", ""),
                "username":         d.get("username", "anonymous"),
                "started_at":       d.get("started_at", ""),
                "ended_at":         d.get("ended_at", ""),
                "has_idr":          d.get("has_idr", False),
                "has_b_frames":     d.get("has_b_frames", False),
                "gop_type":         d.get("gop_type", ""),
                "gop_avg":          d.get("gop_avg", 0),
                "v_codec":          d.get("v_codec", ""),
                "v_width":          d.get("v_width", 0),
                "v_height":         d.get("v_height", 0),
                "v_fps_val":        v_fps_val,
                "v_fps_compliance": v_fps_compliance,
                "v_scan":           v_scan,
                "run_status":       raw_status,
                "overall_status":   eff_status,
                "override":         override,
                "error":            d.get("error", ""),
                "ts_file":          d.get("ts_file"),
                "is_scheduled":     d.get("is_scheduled", False),
                "log_count":        len(d.get("log", [])),
                "test_id":          d.get("test_id", ""),
            })
        except Exception:
            pass
    return jsonify(items)


@gop_bp.route("/gop/result/<path:filename>", methods=["GET"])
def gop_result_file(filename):
    return send_from_directory(GOP_DIR, filename)

@gop_bp.route("/gop/result/<path:filename>", methods=["PATCH"])
def gop_patch_result(filename):
    filepath = os.path.join(GOP_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "not found"}), 404
    with open(filepath, "r") as f:
        data = json.load(f)
    patch = request.get_json(silent=True) or {}
    if "tag" in patch:
        data["tag"] = patch["tag"]
    with open(filepath, "w") as f:
        json.dump(data, f)
    return jsonify({"ok": True})

@gop_bp.route("/gop/ts/<path:filename>", methods=["GET"])
def gop_ts_download(filename):
    """Download the .ts capture file for a GOP analysis result."""
    return send_from_directory(GOP_DIR, filename, as_attachment=True)


@gop_bp.route("/gop/override/<path:filename>", methods=["POST"])
def gop_override(filename):
    """Save override reason to the result JSON and update overall_status."""
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason is required"}), 400
    filepath = os.path.join(GOP_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    try:
        with open(filepath) as f:
            d = json.load(f)
        d["override"] = {
            "reason":     reason,
            "applied_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        d["overall_status"] = "ACCEPTED (Override)"
        with open(filepath, "w") as f:
            json.dump(d, f, indent=2)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@gop_bp.route("/gop/override/<path:filename>", methods=["DELETE"])
def gop_override_remove(filename):
    """Remove override from a result JSON."""
    filepath = os.path.join(GOP_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    try:
        with open(filepath) as f:
            d = json.load(f)
        d.pop("override", None)
        statuses = [v[0] for v in (d.get("compliance") or {}).values()]
        if "REJECTED" in statuses:     d["overall_status"] = "REJECTED"
        elif "ACCEPTED" in statuses:   d["overall_status"] = "ACCEPTED"
        else:                          d["overall_status"] = "COMPLIANT"
        with open(filepath, "w") as f:
            json.dump(d, f, indent=2)
        return jsonify({"success": True, "overall_status": d["overall_status"]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@gop_bp.route("/gop/delete/<path:filename>", methods=["DELETE"])
def gop_delete(filename):
    ok, err = _check_password(request)
    if not ok:
        return err
    filepath = os.path.join(GOP_DIR, filename)
    ts_path  = filepath.replace(".json", ".ts")
    deleted  = []
    for fp in [filepath, ts_path]:
        if os.path.isfile(fp):
            try: os.remove(fp); deleted.append(os.path.basename(fp))
            except Exception: pass
    if filepath.replace(".json","") + ".json" not in [os.path.join(GOP_DIR, d) for d in deleted]:
        if not os.path.isfile(filepath):
            return jsonify({"success": True, "deleted": deleted})
        return jsonify({"error": "Could not delete file"}), 500
    return jsonify({"success": True, "deleted": deleted})


# ── SCHEDULED JOBS ────────────────────────────────────────────────────────

@gop_bp.route("/gop/schedule", methods=["POST"])
def gop_schedule():
    """Schedule a GOP analysis for a future UTC time."""
    data       = request.get_json(silent=True) or {}
    url        = (data.get("url") or "").strip()
    run_at     = (data.get("run_at_utc") or "").strip()
    duration   = min(int(data.get("duration") or 30), 120)
    passphrase = (data.get("passphrase") or "").strip()
    tag        = (data.get("tag") or "").strip()
    workflow   = (data.get("workflow") or DEFAULT_WORKFLOW).strip()
    username   = _get_username_from_request()

    if not url or not run_at:
        return jsonify({"error": "url and run_at_utc are required"}), 400

    try:
        run_dt = datetime.datetime.fromisoformat(run_at.replace("Z",""))
    except ValueError:
        return jsonify({"error": "Invalid run_at_utc format (use ISO 8601)"}), 400

    if passphrase and "passphrase=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}passphrase={passphrase}"

    sched_id    = str(uuid.uuid4())[:8]
    url_display = re.sub(r'[?&]passphrase=[^&]*', '', url).rstrip('?&')

    with _gop_sched_lock:
        _gop_scheduled[sched_id] = {
            "sched_id":   sched_id,
            "url":        url_display,
            "url_full":   url,
            "run_at_utc": run_dt.isoformat() + "Z",
            "duration":   duration,
            "tag":        tag,
            "username":   username,
            "workflow":   workflow,
            "status":     "pending",
        }

    def _wait_and_run():
        import time as _time2
        now_utc = datetime.datetime.utcnow()
        delay   = (run_dt - now_utc).total_seconds()
        if delay > 0:
            _time2.sleep(max(0, delay))

        with _gop_sched_lock:
            sched = _gop_scheduled.get(sched_id, {})
            if sched.get("status") == "cancelled":
                return
            sched["status"] = "running"

        job_id     = str(uuid.uuid4())[:8]
        started_at = datetime.datetime.utcnow().isoformat() + "Z"
        with _gop_lock:
            _gop_jobs[job_id] = {
                "job_id":     job_id,
                "status":     "running",
                "started_at": started_at,
                "ended_at":   None,
                "url":        url_display,
                "tag":        tag,
                "username":   username,
                "workflow":   workflow,
                "result":     None,
                "log":        [],
            }
        with _gop_sched_lock:
            if sched_id in _gop_scheduled:
                _gop_scheduled[sched_id]["job_id"] = job_id

        _run_gop_analysis(job_id, url, duration, "", tag, workflow=workflow)

        with _gop_lock:
            job = _gop_jobs.get(job_id, {})
            res_file = job.get("res_file")
        if res_file:
            res_path = os.path.join(GOP_DIR, res_file)
            try:
                with open(res_path) as f:
                    d = json.load(f)
                d["is_scheduled"] = True
                d["sched_id"]     = sched_id
                with open(res_path, "w") as f:
                    json.dump(d, f, indent=2)
            except Exception:
                pass

        with _gop_sched_lock:
            if sched_id in _gop_scheduled:
                _gop_scheduled[sched_id]["status"] = "done"

    threading.Thread(target=_wait_and_run, daemon=True).start()
    return jsonify({"sched_id": sched_id, "run_at_utc": run_dt.isoformat() + "Z"})


@gop_bp.route("/gop/schedule", methods=["GET"])
def gop_schedule_list():
    with _gop_sched_lock:
        items = list(_gop_scheduled.values())
    return jsonify(items)


@gop_bp.route("/gop/schedule/<sched_id>/cancel", methods=["POST"])
def gop_schedule_cancel(sched_id):
    ok, err = _check_password(request)
    if not ok:
        return err
    with _gop_sched_lock:
        sched = _gop_scheduled.get(sched_id)
        if not sched:
            return jsonify({"error": "Scheduled job not found"}), 404
        if sched["status"] not in ("pending",):
            return jsonify({"error": f"Cannot cancel job with status '{sched['status']}'"}), 400
        sched["status"] = "cancelled"
    return jsonify({"success": True})


# ── SPECS ─────────────────────────────────────────────────────────────────

@gop_bp.route("/gop/specs", methods=["GET"])
def gop_specs_get():
    workflow = request.args.get("workflow") or DEFAULT_WORKFLOW
    return jsonify(_load_specs(workflow))


@gop_bp.route("/gop/specs", methods=["POST"])
def gop_specs_save():
    """Save specs for a workflow.

    Auth: requires admin or engineer role (checked via session).
    Stamps _meta.saved_by / _meta.saved_at on the stored file.
    """
    username, role = _get_user_and_role()
    if role not in ("admin", "engineer"):
        return jsonify({"success": False, "error": "Permission denied — admin or engineer role required"}), 403

    workflow = request.args.get("workflow") or DEFAULT_WORKFLOW
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No specs data provided"}), 400
    try:
        _save_specs(data, workflow, username=username)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@gop_bp.route("/gop/specs/reset", methods=["POST"])
def gop_specs_reset():
    """Reset specs for a workflow to defaults.

    Auth: requires admin or engineer role.
    """
    _, role = _get_user_and_role()
    if role not in ("admin", "engineer"):
        return jsonify({"success": False, "error": "Permission denied — admin or engineer role required"}), 403
    workflow = request.args.get("workflow") or DEFAULT_WORKFLOW
    try:
        specs_file = _specs_file_for(workflow)
        if os.path.isfile(specs_file):
            os.remove(specs_file)
        return jsonify({"success": True, "specs": DEFAULT_SPECS})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── WORKFLOW LABELS ───────────────────────────────────────────────────────
_WORKFLOW_LABELS_FILE = os.path.join(_BASE_DIR, "workflow_labels.json")
_DEFAULT_WORKFLOW_LABELS = {
    "dc_aminos_tp": "DC - Aminos and TP",
    "rts":          "RTS",
    "wb":           "W&B",
}


def _load_workflow_labels():
    labels = dict(_DEFAULT_WORKFLOW_LABELS)
    if os.path.isfile(_WORKFLOW_LABELS_FILE):
        try:
            with open(_WORKFLOW_LABELS_FILE) as f:
                labels.update(json.load(f))
        except Exception:
            pass
    return labels


def _save_workflow_labels(labels):
    with open(_WORKFLOW_LABELS_FILE, "w") as f:
        json.dump(labels, f, indent=2)


@gop_bp.route("/gop/workflows", methods=["GET"])
def gop_workflows_get():
    return jsonify(_load_workflow_labels())


@gop_bp.route("/gop/workflows/rename", methods=["POST"])
def gop_workflows_rename():
    """Rename a workflow label.

    Auth: requires admin or engineer role.
    """
    _, role = _get_user_and_role()
    if role not in ("admin", "engineer"):
        return jsonify({"success": False, "error": "Permission denied — admin or engineer role required"}), 403
    data     = request.get_json(silent=True) or {}
    workflow = (data.get("workflow") or "").strip()
    label    = (data.get("label") or "").strip()
    if not workflow or not label:
        return jsonify({"error": "workflow and label are required"}), 400
    if workflow not in WORKFLOW_SPECS_FILES:
        return jsonify({"error": f"Unknown workflow: {workflow}"}), 400
    try:
        labels = _load_workflow_labels()
        labels[workflow] = label
        _save_workflow_labels(labels)
        return jsonify({"success": True, "labels": labels})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── RE-EVALUATE WITH DIFFERENT WORKFLOW ──────────────────────────────────────

def _reeval_compliance(stored: dict, specs: dict) -> tuple:
    """Re-run the compliance checks on a stored result dict using new specs.

    Returns (compliance_dict, overall_status_str).

    All measured values are read directly from the stored result — no ffprobe
    is re-run.  Only the compliance rules (specs) change.
    """
    def _s(key):
        return specs.get(key, DEFAULT_SPECS.get(key, {}))

    def comply_range(measured, key):
        sp = _s(key)
        lo, hi = sp.get("lo", 0), sp.get("hi", float("inf"))
        plo = sp.get("pref_lo")
        phi = sp.get("pref_hi")
        if measured is None:
            return "UNKNOWN", "—", ""
        in_range = lo <= measured <= hi
        if not in_range:
            return "REJECTED", str(measured), f"Expected {lo}–{hi}"
        if plo is not None and phi is not None:
            if plo <= measured <= phi:
                return "COMPLIANT", str(measured), ""
            return "ACCEPTED", str(measured), f"Preferred {plo}–{phi}"
        return "COMPLIANT", str(measured), ""

    def comply_enum_multi(measured, key):
        sp = _s(key)
        allowed   = [str(v).lower() for v in sp.get("values", [])]
        pref_raw  = sp.get("preferred", "")
        if isinstance(pref_raw, list):
            preferred = [str(p).lower() for p in pref_raw]
        else:
            preferred = [str(pref_raw).lower()] if pref_raw else []
        m = str(measured).strip().lower()
        if not allowed:
            return "UNKNOWN", measured, "No spec defined"
        if m not in allowed:
            return "REJECTED", measured, f"Expected one of {sp.get('values', [])}"
        if preferred and m in preferred:
            return "COMPLIANT", measured, ""
        if preferred:
            return "ACCEPTED", measured, f"Preferred {pref_raw}"
        return "COMPLIANT", measured, ""

    def _av_check(measured_ms, sp):
        warn = float(sp.get("warn", 15.0))
        hard = float(sp.get("hard", 230.0))
        mode = sp.get("mode", "inform")
        if measured_ms is None:
            return ("UNKNOWN", "—", "Could not measure")
        m = round(measured_ms, 2)
        if mode == "inform":
            note = (f"< {warn}ms preferred" if m < warn else
                    f"< {hard}ms limit" if m < hard else f"Exceeds {hard}ms")
            return ("INFO", f"{m} ms", f"{note} (inform only)")
        if m < warn:
            return ("COMPLIANT", f"{m} ms", f"< {warn}ms preferred")
        if m < hard:
            return ("ACCEPTED", f"{m} ms", f"< {hard}ms limit; prefer < {warn}ms")
        return ("REJECTED", f"{m} ms", f"Exceeds hard limit of {hard}ms")

    r = stored

    # ── Pull measured values from stored result ──────────────────────────────
    file_br_mbps = r.get("file_br_mbps", 0)
    v_br_mbps    = r.get("v_br_mbps", 0)
    a_br_kbps_f  = r.get("a_br_kbps", 0)
    a_rate_khz   = r.get("a_rate_khz", 0)
    avg_gop      = r.get("gop_avg", 0)
    gop_type     = r.get("gop_type", "CLOSED")
    has_b_frames = r.get("has_b_frames", False)
    has_idr      = r.get("has_idr", False)
    v_width      = r.get("v_width", 0)
    v_height     = r.get("v_height", 0)
    dar          = r.get("v_dar", "")
    v_chroma     = r.get("v_chroma", "")
    v_pix_fmt    = r.get("v_pix_fmt", "")
    v_full_range = v_pix_fmt.startswith("yuvj") if v_pix_fmt else False
    v_scan       = r.get("v_scan", "progressive")
    v_bits       = str(r.get("v_bits", ""))
    v_color_sp   = r.get("v_color_sp", "unknown")
    v_codec      = r.get("v_codec", "")
    v_level_f    = r.get("v_level_f", 0)
    v_profile    = r.get("v_profile", "")
    v_entropy    = r.get("v_entropy", "")
    v_rate_ctrl  = r.get("v_rate_ctrl", "VBR")
    v_hdr        = r.get("v_hdr", "SDR")
    fps_eff      = r.get("v_fps_compliance", r.get("v_fps_val", 0))
    a_codec_display = r.get("a_codec_display", r.get("a_codec", ""))
    a_ch         = r.get("a_channels", 0)
    a_bps        = str(r.get("a_bps", ""))
    audio_tracks = r.get("audio_tracks", 0)

    # ── FPS compliance ───────────────────────────────────────────────────────
    fps_sp     = _s("fps")
    fps_values = [float(v) for v in fps_sp.get("values", [25.0, 29.97, 30.0])]
    fps_pref   = fps_sp.get("preferred", 25.0)
    allow_50p_720 = fps_sp.get("allow_50p_720", False)
    fps_to_check = fps_eff
    fps_50p_720_accepted = False
    if allow_50p_720 and v_height == 720 and abs(fps_eff - 50.0) < 0.5:
        fps_to_check = 50.0
        fps_values = list(fps_values) + [50.0]
        fps_50p_720_accepted = True
    fps_ok = any(abs(fps_to_check - f) < 0.1 for f in fps_values)
    fps_pref_ok = isinstance(fps_pref, (int, float)) and abs(fps_to_check - float(fps_pref)) < 0.1
    fps_status = "REJECTED" if not fps_ok else ("COMPLIANT" if fps_pref_ok else "ACCEPTED")
    fps_measured_str = f"{fps_eff:.3f}"
    if v_scan == "interlaced" and r.get("v_fps_val", fps_eff) != fps_eff:
        fps_measured_str += " (50i→25fps)"
    if fps_50p_720_accepted:
        fps_measured_str += " [accepted: 50p @ 720p]"

    # ── GOP size compliance ──────────────────────────────────────────────────
    gop_sp     = _s("gop_size")
    gop_values = [int(v) for v in gop_sp.get("values", [30, 50])]
    gop_tol    = int(gop_sp.get("tolerance", 3))
    allow_secs = gop_sp.get("allow_seconds", True)
    if allow_secs and avg_gop > 0:
        gop_values_check = list(gop_values) + [round(fps_eff * s) for s in [1, 2] if fps_eff > 0]
    else:
        gop_values_check = gop_values
    gop_exact  = any(abs(avg_gop - g) < 1 for g in gop_values_check)
    gop_near   = any(abs(avg_gop - g) <= gop_tol for g in gop_values_check)
    gop_status = "COMPLIANT" if gop_exact else ("ACCEPTED" if gop_near else "REJECTED")
    gop_expected_str = ", ".join(str(v) for v in gop_values)
    if allow_secs:
        gop_expected_str += " or 1s/2s"


    compliance = {
        "overall_br":   comply_range(file_br_mbps, "overall_br"),
        "gop_size":     (gop_status, str(avg_gop), f"Expected {gop_expected_str}"),
        "gop_type":     comply_enum_multi(gop_type, "gop_type"),
        "b_frames":     comply_enum_multi("absent" if not has_b_frames else "present", "b_frames"),
        "idr":          ("COMPLIANT" if has_idr else "REJECTED",
                         "Present" if has_idr else "ABSENT", "IDR frames required"),
        "frame_size":   comply_enum_multi(f"{v_width}x{v_height}", "frame_size"),
        "aspect_ratio": comply_enum_multi(dar, "aspect_ratio"),
        "chroma":       comply_enum_multi(v_chroma, "chroma"),
        "colour_range": (lambda res: (res[0], v_pix_fmt, res[2]))(
                            comply_enum_multi("full" if v_full_range else "limited", "colour_range")),
        "scan_type":    comply_enum_multi(v_scan, "scan_type"),
        "bit_depth":    comply_enum_multi(v_bits, "bit_depth"),
        "colour_gamut": comply_enum_multi(v_color_sp, "colour_gamut"),
        "codec":        comply_enum_multi(v_codec.lower() if v_codec else "", "codec"),
        "codec_level":  comply_range(v_level_f, "codec_level"),
        "codec_profile":comply_enum_multi(v_profile.lower() if v_profile else "", "codec_profile"),
        "entropy":      comply_enum_multi(v_entropy, "entropy"),
        "rate_ctrl_v":  comply_enum_multi(v_rate_ctrl, "rate_ctrl_v"),
        "v_br":         comply_range(v_br_mbps, "v_br"),
        "hdr_scheme":   comply_enum_multi(v_hdr, "hdr_scheme"),
        "fps":          (fps_status, fps_measured_str, f"Expected {fps_values}"),
        "a_codec":      comply_enum_multi(a_codec_display, "a_codec"),
        "a_streams":    comply_range(audio_tracks, "a_streams"),
        "a_channels":   comply_range(a_ch, "a_channels"),
        "a_rate_ctrl":  comply_enum_multi("VBR", "a_rate_ctrl"),
        "a_sample_rate":comply_range(a_rate_khz, "a_sample_rate"),
        "a_bits":       comply_enum_multi(a_bps.lower(), "a_bits"),
        "a_br_kbps":    comply_range(a_br_kbps_f, "a_br_kbps"),
        "av_sync_warn": _av_check(r.get("av_sync_avg_ms"),  _s("av_sync_warn")),
        "av_sync_max":  _av_check(r.get("av_sync_max_ms"),  _s("av_sync_max")),
        "v_pts_jitter": _av_check(r.get("v_pts_jitter_ms"), _s("v_pts_jitter")),
        "a_pts_jitter": _av_check(r.get("a_pts_jitter_ms"), _s("a_pts_jitter")),
    }

    statuses = [v[0] for v in compliance.values() if v[0] != "INFO"]
    if "REJECTED" in statuses:
        overall_status = "REJECTED"
    elif "ACCEPTED" in statuses:
        overall_status = "ACCEPTED"
    else:
        overall_status = "COMPLIANT"

    return compliance, overall_status


@gop_bp.route("/gop/reeval/<path:filename>", methods=["GET"])
def gop_reeval(filename):
    """Re-evaluate a stored result against a different workflow's specs.

    Does NOT modify the stored JSON — returns the re-evaluated result only.

    Query params
    ------------
      workflow  str  (required) Target workflow key (dc_aminos_tp / rts / wb)

    Response
    --------
      200  full result dict with overridden compliance / overall_status / specs
      400  { error: "workflow parameter required" }
      404  { error: "Result not found" }
      500  { error: "..." }
    """
    target_workflow = (request.args.get("workflow") or "").strip()
    if not target_workflow:
        return jsonify({"error": "workflow parameter required"}), 400

    filepath = os.path.join(GOP_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "Result not found"}), 404

    try:
        with open(filepath) as f:
            stored = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Could not read result: {e}"}), 500

    specs = _load_specs(target_workflow)

    try:
        compliance, overall_status = _reeval_compliance(stored, specs)
    except Exception as e:
        return jsonify({"error": f"Re-evaluation failed: {e}"}), 500

    import copy
    out = copy.deepcopy(stored)
    out["compliance"]     = compliance
    out["overall_status"] = overall_status
    out["specs"]          = specs
    out["reeval_workflow"] = target_workflow
    out["reeval_label"]   = _load_workflow_labels().get(target_workflow, target_workflow)

    return jsonify(out)


# ── CHANGE WORKFLOW OF HISTORICAL ENTRY ──────────────────────────────────────

@gop_bp.route("/gop/result/<path:filename>/workflow", methods=["PATCH"])
def gop_change_workflow(filename):
    """Permanently change the workflow of a stored result.

    Requires admin or engineer role. Re-runs compliance against the new
    workflow's specs and appends an entry to workflow_change_log.

    Request body (JSON)
    -------------------
      { "workflow": "<workflow_key>" }

    Response
    --------
      200  { "success": true, "overall_status": "<new status>" }
      400  { "error": "workflow field required" }
      403  { "error": "Permission denied …" }
      404  { "error": "Result not found" }
      500  { "error": "..." }
    """
    username, role = _get_user_and_role()
    if role not in ("admin", "engineer"):
        return jsonify({"error": "Permission denied — admin or engineer role required"}), 403

    body = request.get_json(force=True, silent=True) or {}
    new_workflow = (body.get("workflow") or "").strip()
    if not new_workflow:
        return jsonify({"error": "workflow field required"}), 400

    filepath = os.path.join(GOP_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "Result not found"}), 404

    try:
        with open(filepath) as f:
            result = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Could not read result: {e}"}), 500

    old_workflow = result.get("workflow", "unknown")
    specs = _load_specs(new_workflow)

    try:
        compliance, overall_status = _reeval_compliance(result, specs)
    except Exception as e:
        return jsonify({"error": f"Re-evaluation failed: {e}"}), 500

    result["workflow"]       = new_workflow
    result["compliance"]     = compliance
    result["overall_status"] = overall_status
    result["specs"]          = specs

    log_entry = {
        "from": old_workflow,
        "to":   new_workflow,
        "by":   username,
        "at":   datetime.datetime.utcnow().isoformat() + "Z",
    }
    result.setdefault("workflow_change_log", []).append(log_entry)

    try:
        with open(filepath, "w") as f:
            json.dump(result, f, indent=2)
    except Exception as e:
        return jsonify({"error": f"Failed to save result: {e}"}), 500

    return jsonify({"success": True, "overall_status": overall_status})


# ════════════════════════════════════════════════════════════════════════════
#  REGISTRATION
# ════════════════════════════════════════════════════════════════════════════

def register_routes(app) -> None:
    """Call this from proxy.py exactly like the other blueprint modules."""
    app.register_blueprint(gop_bp)
