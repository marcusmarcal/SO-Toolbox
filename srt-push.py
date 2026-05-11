#!/usr/bin/env python3
# ============================================================
# HTML (NGINX) -> Xvfb -> Chromium -> FFmpeg -> SRT PUSH
# ORACLE LINUX 9.7 - PRODUCTION AUTO-HEALING VERSION
# ============================================================

import os
import signal
import subprocess
import sys
import time
import shutil

# ============================================================
# CONFIGURATION
# ============================================================

HTML_URL = "https://127.0.0.1/dashboard.html"  # pode ser http também
SRT_URL = "srt://127.0.0.1:9000?mode=caller"

DISPLAY = ":99"

WIDTH = 1280
HEIGHT = 720
FPS = 30

VIDEO_BITRATE = "3000k"
VIDEO_CODEC = "libx264"

XVFB_PATH = "/usr/bin/Xvfb"
FFMPEG_PATH = "/usr/bin/ffmpeg"

# auto detect chromium (OL9 compatible)
CHROMIUM_PATH = (
    shutil.which("chromium-browser")
    or shutil.which("chromium")
    or "/usr/bin/chromium-browser"
)

processes = []

# ============================================================
# CHROMIUM FLAGS (FIXED FOR ROOT + SSL + STREAMING)
# ============================================================

CHROMIUM_FLAGS = [
    "--kiosk",
    "--start-fullscreen",

    # ROOT FIX
    "--no-sandbox",
    "--disable-setuid-sandbox",

    # SSL BYPASS (IMPORTANT)
    "--ignore-certificate-errors",
    "--ignore-ssl-errors",
    "--allow-insecure-localhost",

    # PERFORMANCE / STABILITY
    "--disable-gpu",
    "--disable-infobars",
    "--autoplay-policy=no-user-gesture-required",
    "--noerrdialogs",
    "--disable-session-crashed-bubble",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--disable-notifications",
    "--disable-extensions",

    # streaming stability
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-frame-rate-limit",
]

# ============================================================
# HELPERS
# ============================================================

def run(cmd, env=None):
    print("\n[RUN]", " ".join(cmd))
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env
    )


def kill_existing():
    print("[INFO] Killing old processes...")
    subprocess.run(["pkill", "-9", "Xvfb"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "chromium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "chromium-browser"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "ffmpeg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "unclutter"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ============================================================
# Xvfb
# ============================================================

def start_xvfb():
    print("[INFO] Starting Xvfb...")

    proc = run([
        XVFB_PATH,
        DISPLAY,
        "-screen",
        "0",
        f"{WIDTH}x{HEIGHT}x24"
    ])

    processes.append(proc)
    time.sleep(2)

# ============================================================
# HIDE CURSOR (IMPORTANT UX FIX)
# ============================================================

def hide_cursor():
    print("[INFO] Hiding cursor...")

    subprocess.Popen([
        "unclutter",
        "-idle", "0.1",
        "-root"
    ])

# ============================================================
# CHROMIUM
# ============================================================

def start_chromium():
    print("[INFO] Starting Chromium...")

    if not CHROMIUM_PATH:
        print("[ERROR] Chromium not found")
        sys.exit(1)

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY

    cmd = [
        CHROMIUM_PATH,
        f"--window-size={WIDTH},{HEIGHT}",
    ] + CHROMIUM_FLAGS + [HTML_URL]

    proc = run(cmd, env=env)

    processes.append(proc)
    time.sleep(6)

# ============================================================
# FFMPEG
# ============================================================

def build_ffmpeg():
    return [
        FFMPEG_PATH,

        "-thread_queue_size", "256",

        "-f", "x11grab",
        "-video_size", f"{WIDTH}x{HEIGHT}",
        "-framerate", str(FPS),
        "-i", DISPLAY,

        "-c:v", VIDEO_CODEC,
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",

        "-g", str(FPS * 2),

        "-b:v", VIDEO_BITRATE,
        "-maxrate", VIDEO_BITRATE,
        "-bufsize", "6M",

        "-f", "mpegts",
        SRT_URL
    ]


def start_ffmpeg():
    print("[INFO] Starting FFmpeg SRT push...")

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY

    proc = run(build_ffmpeg(), env=env)

    processes.append(proc)

# ============================================================
# WATCHDOG (BASIC AUTO-RECOVERY)
# ============================================================

def restart_all():
    print("[WATCHDOG] Restarting pipeline...")
    cleanup()
    main()

# ============================================================
# CLEANUP
# ============================================================

def cleanup(sig=None, frame=None):
    print("\n[INFO] Cleaning up...")

    for p in processes:
        try:
            p.kill()
        except:
            pass

    kill_existing()
    sys.exit(0)

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    kill_existing()

    start_xvfb()
    hide_cursor()
    start_chromium()
    start_ffmpeg()

    print("\n[OK] SRT STREAMING RUNNING (ORACLE LINUX 9)")
    print("[INFO] Press Ctrl+C to stop\n")

    while True:
        time.sleep(5)

        for p in processes:
            if p.poll() is not None:
                print("[ERROR] Process crashed — auto restart")
                restart_all()


if __name__ == "__main__":
    main()