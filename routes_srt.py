"""
SRT Ingest Router Blueprint
Handles SRT ingest routes: single destination and multi-destination (port range).
Streams ffmpeg stderr stats (bitrate, speed, time) via SSE per job.
"""

import re
import subprocess
import threading
import os
import signal
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Generator
from flask import Blueprint, request, jsonify, render_template, Response, stream_with_context

srt_bp = Blueprint("srt_bp", __name__, url_prefix="/srt")

# Track running ffmpeg processes: { job_id: { process, config, stats_buf } }
_running_jobs: dict = {}
_jobs_lock = threading.Lock()
_job_counter = 0

# CBR defaults (Mbps) — overridable per request
CBR_DEFAULT_MBPS = 8.0
CBR_BUFSIZE_FACTOR = 2  # bufsize = bitrate * factor
TS_SOURCE_DIR = "/opt/web/gop-results"


def _next_job_id() -> int:
    global _job_counter
    _job_counter += 1
    return _job_counter


def _build_ffmpeg_cmd(
    input_file: str,
    host: str,
    port: int,
    passphrase: str,
    bitrate_mbps: float = CBR_DEFAULT_MBPS,
    passthrough: bool = False,
) -> list:
    """Build ffmpeg command for a single SRT destination.

    passthrough=True: copy streams without re-encoding (for .ts sources).
    passthrough=False: full CBR transcode with libx264/aac.
    """
    srt_url = f"srt://{host}:{port}?passphrase={passphrase}" if passphrase else f"srt://{host}:{port}"

    if passthrough:
        return [
            "ffmpeg", "-stream_loop", "-1",
            "-fflags", "+genpts+discardcorrupt",
            "-re",
            "-i", input_file,
            "-map", "0:v:0",
            "-map", "0:a:0",
            "-c:v", "copy",
            "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",
            "-f", "mpegts",
            "-muxdelay", "0",
            "-muxpreload", "0",
            srt_url,
        ]

    vbr = f"{bitrate_mbps}M"
    bufsize = f"{bitrate_mbps * CBR_BUFSIZE_FACTOR}M"
    return [
        "ffmpeg", "-stream_loop", "-1", "-re",
        "-i", input_file,
        "-map", "0:v:0",
        "-map", "0:a:0",
        # Video — strict CBR
        "-c:v", "libx264",
        "-x264-params", "force-cfr=1:pic-struct=1",
        "-bf", "0",
        "-flags", "+cgop",
        "-r", "25",
        "-g", "50",
        "-keyint_min", "50",
        "-sc_threshold", "0",
        "-b:v", vbr,
        "-minrate", vbr,
        "-maxrate", vbr,
        "-bufsize", bufsize,
        # Audio
        "-c:a", "aac",
        "-ar", "48000",
        "-ac", "2",
        # Scale + container
        "-vf", "scale=1920:1080",
        "-f", "mpegts",
        "-muxdelay", "0",
        "-muxpreload", "0",
        srt_url,
    ]


# Regex to parse ffmpeg progress lines:
# frame=  120 fps= 25 q=28.0 size=    1536kB time=00:00:04.80 bitrate=2621.4kbits/s speed=   1x
_FFMPEG_RE = re.compile(
    r"frame=\s*(\d+).*?fps=\s*([\d.]+).*?size=\s*([\d.]+\w+).*?"
    r"time=([\d:.]+).*?bitrate=\s*([\d.]+\w+/s).*?speed=\s*([\d.]+)x",
    re.S,
)


def _parse_ffmpeg_line(line: str) -> Optional[dict]:
    """Extract stats from a ffmpeg progress stderr line."""
    m = _FFMPEG_RE.search(line)
    if not m:
        return None
    return {
        "frame": int(m.group(1)),
        "fps": float(m.group(2)),
        "size": m.group(3),
        "time": m.group(4),
        "bitrate": m.group(5),
        "speed": m.group(6),
        "utc": datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3] + " UTC",
        "ts": time.time(),
    }


def _launch_job(
    input_file: str,
    host: str,
    port: int,
    passphrase: str,
    bitrate_mbps: float = CBR_DEFAULT_MBPS,
    passthrough: bool = False,
) -> dict:
    """Launch a single ffmpeg process and register it."""
    job_id = _next_job_id()
    cmd = _build_ffmpeg_cmd(input_file, host, port, passphrase, bitrate_mbps, passthrough)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    job = {
        "id": job_id,
        "host": host,
        "port": port,
        "pid": process.pid,
        "bitrate_mbps": bitrate_mbps,
        "passthrough": passthrough,
        "mode": "passthrough" if passthrough else "transcode",
        "status": "running",
        "process": process,
        "cmd": " ".join(cmd),
        # Ring buffer: last 300 stat samples (~5 min at 1 sample/s)
        "stats_buf": deque(maxlen=300),
        "last_stat": None,
    }

    with _jobs_lock:
        _running_jobs[job_id] = job

    def _read_stderr(jid: int, proc: subprocess.Popen) -> None:
        """Read ffmpeg stderr, parse progress lines into stats_buf."""
        buf = ""
        for chunk in iter(lambda: proc.stderr.read(256), ""):
            buf += chunk
            # ffmpeg progress lines end with \r
            while "\r" in buf or "\n" in buf:
                sep = "\r" if "\r" in buf else "\n"
                line, buf = buf.split(sep, 1)
                stat = _parse_ffmpeg_line(line)
                if stat:
                    with _jobs_lock:
                        if jid in _running_jobs:
                            _running_jobs[jid]["stats_buf"].append(stat)
                            _running_jobs[jid]["last_stat"] = stat
        proc.wait()
        with _jobs_lock:
            if jid in _running_jobs:
                _running_jobs[jid]["status"] = (
                    "finished" if proc.returncode == 0 else "error"
                )

    threading.Thread(target=_read_stderr, args=(job_id, process), daemon=True).start()
    return job


def _job_info(job: dict) -> dict:
    """Serialisable snapshot of a job (no subprocess object)."""
    return {
        "id": job["id"],
        "host": job["host"],
        "port": job["port"],
        "pid": job["pid"],
        "bitrate_mbps": job["bitrate_mbps"],
        "passthrough": job["passthrough"],
        "mode": job["mode"],
        "status": job["status"],
        "last_stat": job.get("last_stat"),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@srt_bp.route("/")
def srt_tool():
    """Serve the SRT ingest HTML tool."""
    return render_template("srt_tool.html")


@srt_bp.route("/ingest/single", methods=["POST"])
def ingest_single():
    """
    Start a single SRT ingest.
    Body JSON: { host, port, passphrase, input_file?, bitrate_mbps? }
    """
    data = request.get_json(force=True)
    host = data.get("host", "").strip()
    port = int(data.get("port", 0))
    passphrase = data.get("passphrase", "").strip()
    input_file = data.get("input_file", "test.mp4").strip()
    bitrate_mbps = float(data.get("bitrate_mbps", CBR_DEFAULT_MBPS))
    passthrough = bool(data.get("passthrough", False))

    if not host or not port:
        return jsonify({"error": "host and port are required"}), 400
    if not os.path.isfile(input_file):
        return jsonify({"error": f"Input file not found: {input_file}"}), 400

    job = _launch_job(input_file, host, port, passphrase, bitrate_mbps, passthrough)
    return jsonify({"message": "Ingest started", "job": _job_info(job)}), 201


@srt_bp.route("/ingest/multi", methods=["POST"])
def ingest_multi():
    """
    Start ingest to multiple SRT destinations (port range).
    Body JSON: { host, port_start, port_end, passphrase, input_file?, bitrate_mbps? }
    """
    data = request.get_json(force=True)
    host = data.get("host", "").strip()
    port_start = int(data.get("port_start", 0))
    port_end = int(data.get("port_end", 0))
    passphrase = data.get("passphrase", "").strip()
    input_file = data.get("input_file", "test.mp4").strip()
    bitrate_mbps = float(data.get("bitrate_mbps", CBR_DEFAULT_MBPS))
    passthrough = bool(data.get("passthrough", False))

    if not host or not port_start or not port_end:
        return jsonify({"error": "host, port_start and port_end are required"}), 400
    if port_start > port_end:
        return jsonify({"error": "port_start must be <= port_end"}), 400
    if (port_end - port_start) > 99:
        return jsonify({"error": "Port range limited to 100 destinations"}), 400
    if not os.path.isfile(input_file):
        return jsonify({"error": f"Input file not found: {input_file}"}), 400

    jobs = []
    for port in range(port_start, port_end + 1):
        job = _launch_job(input_file, host, port, passphrase, bitrate_mbps, passthrough)
        jobs.append(_job_info(job))

    return jsonify({
        "message": f"Ingest started to {len(jobs)} destinations",
        "jobs": jobs,
    }), 201


@srt_bp.route("/jobs", methods=["GET"])
def list_jobs():
    """List all known ingest jobs and their current status."""
    with _jobs_lock:
        jobs = [_job_info(j) for j in _running_jobs.values()]
    return jsonify({"jobs": jobs})


@srt_bp.route("/jobs/<int:job_id>", methods=["GET"])
def get_job(job_id: int):
    """Get status of a specific job."""
    with _jobs_lock:
        job = _running_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(_job_info(job))


@srt_bp.route("/jobs/<int:job_id>/stop", methods=["POST"])
def stop_job(job_id: int):
    """Stop (SIGTERM) a running ingest job."""
    with _jobs_lock:
        job = _running_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "running":
        return jsonify({"error": "Job is not running"}), 400

    try:
        os.kill(job["pid"], signal.SIGTERM)
        job["status"] = "stopping"
    except ProcessLookupError:
        job["status"] = "finished"

    return jsonify({"message": "Stop signal sent", "job": _job_info(job)})


@srt_bp.route("/jobs/stop-all", methods=["POST"])
def stop_all_jobs():
    """Stop all running ingest jobs."""
    stopped = []
    with _jobs_lock:
        for job in _running_jobs.values():
            if job["status"] == "running":
                try:
                    os.kill(job["pid"], signal.SIGTERM)
                    job["status"] = "stopping"
                    stopped.append(job["id"])
                except ProcessLookupError:
                    job["status"] = "finished"

    return jsonify({"message": f"Stopped {len(stopped)} jobs", "stopped_ids": stopped})



@srt_bp.route("/sources", methods=["GET"])
def list_sources():
    sources = [{"file": "test.mp4", "type": "mp4"}]

    if os.path.isdir(TS_SOURCE_DIR):
        ts_files = sorted(
            [f for f in os.listdir(TS_SOURCE_DIR) if f.lower().endswith(".ts")],
            reverse=True  # mais recente primeiro (baseado no nome)
        )

        for f in ts_files:
            sources.append({
                "file": os.path.join(TS_SOURCE_DIR, f),
                "name": f,
                "type": "ts",
            })

    return jsonify({"sources": sources})



@srt_bp.route("/jobs/<int:job_id>/stats", methods=["GET"])
def job_stats_sse(job_id: int):
    """
    SSE stream: sends a JSON stat event every second for the given job.
    Clients connect once per job and receive live bitrate/fps/frame data.
    """
    with _jobs_lock:
        job = _running_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    @stream_with_context
    def _generate():
        last_idx = 0
        while True:
            with _jobs_lock:
                j = _running_jobs.get(job_id)
            if not j:
                yield "event: done\ndata: {}\n\n"
                break

            buf = list(j["stats_buf"])
            new = buf[last_idx:]
            last_idx = len(buf)

            if new:
                import json
                for stat in new:
                    yield f"data: {json.dumps(stat)}\n\n"

            if j["status"] in ("finished", "error", "stopping"):
                import json
                yield f"event: done\ndata: {json.dumps({'status': j['status']})}\n\n"
                break

            time.sleep(0.5)

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
        },
    )
