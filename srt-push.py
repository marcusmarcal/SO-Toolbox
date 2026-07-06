#!/usr/bin/env python3

import os
import re
import signal
import subprocess
import sys
import time
import shutil
import json
import threading
from datetime import datetime, timezone

# ============================================================
# CONFIG
# ============================================================
# Runtime configuration is loaded from CONFIG_FILE (written by the
# SO-Toolbox web UI). Any key missing from the file falls back to
# DEFAULT_CONFIG below. Config changes only take effect on the next
# process start (the web UI restarts the service via systemctl after
# saving).

STORE_DIR = "/opt/web/store"
CONFIG_FILE = os.path.join(STORE_DIR, "srt-push-config.json")
STATS_FILE = os.path.join(STORE_DIR, "srt-push-stats.json")
PREVIEW_FILE = os.path.join(STORE_DIR, "srt-push-preview.jpg")
PREVIEW_TMP_FILE = os.path.join(STORE_DIR, "srt-push-preview.tmp.jpg")
LOG_FILE = "/var/log/srt-push.log"

DEFAULT_CONFIG = {
    "html_url": "https://127.0.0.1/id3as-DC-Monitor.html?view=nodes&dc=ix&inuse=1&sort=nW&dir=-1",
    "srt_host": "10.11.203.1",
    "srt_port": 3292,
    "srt_mode": "caller",
    "srt_latency": 1000,
    "srt_passphrase": "rQ6zgFnfz1WgmJ0AgzI4Zs7Own54K0dU",
    "width": 1920,
    "height": 1080,
    "fps": 5,
    "video_bitrate_kbps": 500,
}


def load_config() -> dict:
    """Load configuration from CONFIG_FILE, falling back to defaults for missing keys."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r") as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        print(f"[CONFIG] Using defaults ({e})")
    return cfg


os.makedirs(STORE_DIR, exist_ok=True)
CONFIG = load_config()

HTML_URL = CONFIG["html_url"]

DISPLAY = ":99"
WIDTH = int(CONFIG["width"])
HEIGHT = int(CONFIG["height"])
FPS = int(CONFIG["fps"])

VIDEO_BITRATE_KBPS = int(CONFIG["video_bitrate_kbps"])
VIDEO_BITRATE = f"{VIDEO_BITRATE_KBPS}k"
VIDEO_BUFSIZE = f"{VIDEO_BITRATE_KBPS * 2}k"
GOP_SIZE = FPS * 5  # 1 IDR frame every 5 seconds

XVFB_PATH = "/usr/bin/Xvfb"
FFMPEG_PATH = "/usr/bin/ffmpeg"

CHROMIUM_PATH = (
    shutil.which("chromium-browser")
    or shutil.which("chromium")
    or "/usr/bin/chromium-browser"
)


def _build_srt_url() -> str:
    return (
        f"srt://{CONFIG['srt_host']}:{CONFIG['srt_port']}"
        f"?mode={CONFIG['srt_mode']}&latency={CONFIG['srt_latency']}"
        f"&passphrase={CONFIG['srt_passphrase']}"
    )


SRT_URL = _build_srt_url()

processes = []

# ============================================================
# LIVE STATS (consumed by the SO-Toolbox web UI)
# ============================================================

_FFMPEG_RE = re.compile(
    r"frame=\s*(\d+).*?fps=\s*([\d.]+).*?size=\s*([\d.]+\w+).*?"
    r"time=([\d:.]+).*?bitrate=\s*([\d.]+\w+/s).*?speed=\s*([\d.]+)x",
    re.S,
)

_stats_lock = threading.Lock()
_stats = {
    "service_status": "starting",
    "ffmpeg_pid": None,
    "started_at": None,
    "updated_at": None,
    "frame": None,
    "fps": None,
    "size": None,
    "time": None,
    "bitrate": None,
    "speed": None,
    "last_error": None,
}


def _write_stats() -> None:
    """Atomically write the current stats snapshot to STATS_FILE."""
    tmp_path = STATS_FILE + ".tmp"
    with _stats_lock:
        snapshot = dict(_stats)
    try:
        with open(tmp_path, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp_path, STATS_FILE)
    except OSError as e:
        print(f"[STATS] Failed to write stats file: {e}")


def _update_stats(**kwargs) -> None:
    with _stats_lock:
        _stats.update(kwargs)
        _stats["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_stats()


# ============================================================
# HELPERS
# ============================================================

def run(cmd, env=None):
    print("[RUN]", " ".join(cmd))
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=open(LOG_FILE, "a"),
        text=True,
        env=env
    )


def kill_existing():
    print("[INFO] cleaning old processes...")
    for p in ["Xvfb", "chromium", "chromium-browser", "ffmpeg"]:
        subprocess.run(["pkill", "-9", p],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

# ============================================================
# Xvfb
# ============================================================

def start_xvfb():
    print("[INFO] starting Xvfb...")
    p = run([
        XVFB_PATH,
        DISPLAY,
        "-screen", "0", f"{WIDTH}x{HEIGHT}x24",
        "-nocursor"
    ])
    processes.append(p)
    time.sleep(2)

# ============================================================
# CHROMIUM
# ============================================================

def start_chromium():
    print("[INFO] starting chromium...")
    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY

    p = run([
        CHROMIUM_PATH,
        "--incognito",
        "--window-position=0,0",
        "--window-size=1920,1080",
        "--kiosk",
        "--start-fullscreen",
        "--disable-infobars",
        "--noerrdialogs",
        "--disable-session-crashed-bubble",
        "--disable-features=TranslateUI",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-background-networking",
        "--disable-extensions",
        "--autoplay-policy=no-user-gesture-required",
        "--ignore-certificate-errors",
        "--allow-insecure-localhost",
        "--touch-events=enabled",
        "--disable-gpu-vsync",
        "--disable-smooth-scrolling",
        "--disable-low-end-device-mode",
        "--blink-settings=imagesEnabled=true",
        "--unsafely-treat-insecure-origin-as-secure=https://127.0.0.1",
        HTML_URL
    ], env=env)

    processes.append(p)
    time.sleep(10)

# ============================================================
# PREVIEW CAPTURE (single overwritten file, no history)
# ============================================================

def _capture_preview_loop(interval: int = 5) -> None:
    """Periodically grab a single frame from the virtual display for the web preview."""
    while True:
        try:
            subprocess.run(
                [
                    FFMPEG_PATH, "-y",
                    "-f", "x11grab",
                    "-video_size", f"{WIDTH}x{HEIGHT}",
                    "-i", f"{DISPLAY}+0,0",
                    "-frames:v", "1",
                    PREVIEW_TMP_FILE,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            os.replace(PREVIEW_TMP_FILE, PREVIEW_FILE)
        except (subprocess.SubprocessError, OSError) as e:
            print(f"[PREVIEW] Capture failed: {e}")
        time.sleep(interval)

# ============================================================
# FFMPEG (SRT PUSH)
# ============================================================

def _read_ffmpeg_stderr(proc: subprocess.Popen) -> None:
    """Read ffmpeg stderr, log raw lines with timestamp every minute, and parse progress."""
    log_f = open(LOG_FILE, "a")
    buf = ""
    
    last_log_time = 0.0  
    interval = 60.0  

    try:
        for chunk in iter(lambda: proc.stderr.read(256), ""):
            buf += chunk
            while "\r" in buf or "\n" in buf:
                sep = "\r" if "\r" in buf else "\n"
                line, buf = buf.split(sep, 1)
                
                if line.strip():
                    current_time = time.time()
                    if current_time - last_log_time >= interval:
                        # 1. Gera o timestamp formatado (Ex: [2026-07-06 15:45:01])
                        timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
                        
                        # 2. Grava o timestamp concatenado com a linha do FFmpeg
                        log_f.write(timestamp + line + "\n")
                        log_f.flush()
                        
                        last_log_time = current_time

                m = _FFMPEG_RE.search(line)
                if m:
                    _update_stats(
                        service_status="running",
                        frame=int(m.group(1)),
                        fps=float(m.group(2)),
                        size=m.group(3),
                        time=m.group(4),
                        bitrate=m.group(5),
                        speed=m.group(6),
                    )
    finally:
        log_f.close()


def start_ffmpeg():
    print("[INFO] starting ffmpeg...")

    cmd = [
        FFMPEG_PATH,
        "-f", "x11grab",
        "-draw_mouse", "0",
        "-video_size", f"{WIDTH}x{HEIGHT}",
        "-framerate", str(FPS),
        "-i", f"{DISPLAY}+0,0",

        "-vf", "format=yuv420p",

        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",

        # --- Strict CBR ---
        "-b:v", VIDEO_BITRATE,
        "-maxrate", VIDEO_BITRATE,
        "-minrate", VIDEO_BITRATE,
        "-bufsize", VIDEO_BUFSIZE,
        "-nal-hrd", "cbr",

        "-g", str(GOP_SIZE),
        "-threads", "2",

        "-f", "mpegts",
        SRT_URL
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    _update_stats(
        service_status="running",
        ffmpeg_pid=proc.pid,
        started_at=datetime.now(timezone.utc).isoformat(),
        last_error=None,
    )

    threading.Thread(target=_read_ffmpeg_stderr, args=(proc,), daemon=True).start()
    return proc

# ============================================================
# WATCHDOG (NEVER STOPS)
# ============================================================

def watchdog_loop():
    ffmpeg_proc = start_ffmpeg()

    while True:
        time.sleep(3)

        # Xvfb died -> full restart
        if processes[0].poll() is not None:
            print("[WATCHDOG] Xvfb died -> full restart...")
            _update_stats(service_status="starting", ffmpeg_pid=None)
            kill_existing()
            time.sleep(1)
            start_xvfb()
            start_chromium()
            ffmpeg_proc = start_ffmpeg()
            continue

        # Chromium died -> restart chromium + ffmpeg
        if processes[1].poll() is not None:
            print("[WATCHDOG] Chromium died -> restarting chromium + ffmpeg...")
            _update_stats(service_status="starting", ffmpeg_pid=None)
            try:
                ffmpeg_proc.kill()
            except Exception:
                pass
            subprocess.run(["pkill", "-9", "chromium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "chromium-browser"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.pop()  # remove chromium from the list
            time.sleep(2)
            start_chromium()
            ffmpeg_proc = start_ffmpeg()
            continue

        # ffmpeg died
        if ffmpeg_proc.poll() is not None:
            print("[WATCHDOG] FFmpeg died -> restarting...")
            _update_stats(service_status="error", ffmpeg_pid=None, last_error="ffmpeg process exited unexpectedly")
            time.sleep(2)
            ffmpeg_proc = start_ffmpeg()

# ============================================================
# CLEANUP
# ============================================================

def cleanup(sig=None, frame=None):
    print("\n[INFO] shutting down...")
    _update_stats(service_status="stopped", ffmpeg_pid=None)
    for p in processes:
        try:
            p.kill()
        except Exception:
            pass
    sys.exit(0)

# ============================================================
# MAIN
# ============================================================

def main():
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    kill_existing()
    start_xvfb()
    start_chromium()

    threading.Thread(target=_capture_preview_loop, daemon=True).start()

    # infinite loop - never lets the SRT push stop
    watchdog_loop()


if __name__ == "__main__":
    main()
