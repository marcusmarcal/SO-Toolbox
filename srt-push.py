#!/usr/bin/env python3
# ============================================================
# HTML (NGINX) -> Xvfb -> Chromium -> FFmpeg -> SRT PUSH
# ORACLE LINUX 9.7 - PRODUCTION STREAMING (FINAL VERSION)
# ============================================================
#
# REQUIRED PACKAGES:
#
# sudo dnf install -y \
#   chromium \
#   xorg-x11-server-Xvfb \
#   xorg-x11-server-utils \
#   ffmpeg \
#   psmisc
#
# ============================================================
# FEATURES:
# - Chromium OL9 wrapper support
# - SSL bypass (self-signed HTTPS OK)
# - Cursor hidden via xsetroot
# - Xvfb virtual display
# - FFmpeg x11grab -> SRT
# - watchdog auto-restart
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

HTML_URL = "https://10.11.203.239/id3as-DC-Monitor.html"

SRT_URL = "srt://10.11.203.2:3292?mode=caller&passphrase=rQ6zgFnfz1WgmJ0AgzI4Zs7Own54K0dU&latency=1000"

DISPLAY = ":99"

WIDTH = 1280
HEIGHT = 720
FPS = 30

VIDEO_BITRATE = "3000k"
VIDEO_CODEC = "libx264"

XVFB_PATH = "/usr/bin/Xvfb"
FFMPEG_PATH = "/usr/bin/ffmpeg"

# Auto-detect Chromium (Oracle Linux RPM style)
CHROMIUM_PATH = (
    shutil.which("chromium-browser")
    or shutil.which("chromium")
    or "/usr/bin/chromium-browser"
)

processes = []

# ============================================================
# CHROMIUM FLAGS (OL9 SAFE + STREAMING OPTIMIZED)
# ============================================================

CHROMIUM_FLAGS = [
    "--kiosk",
    "--start-fullscreen",

    # ROOT FIX
    "--no-sandbox",
    "--disable-setuid-sandbox",

    # SSL BYPASS
    "--ignore-certificate-errors",
    "--ignore-ssl-errors",
    "--allow-insecure-localhost",

    # PERFORMANCE
    "--disable-gpu",
    "--disable-infobars",
    "--autoplay-policy=no-user-gesture-required",
    "--noerrdialogs",
    "--disable-session-crashed-bubble",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--disable-notifications",
    "--disable-extensions",

    # STREAM STABILITY
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
    print("[INFO] Cleaning old processes...")
    subprocess.run(["pkill", "-9", "Xvfb"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "chromium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "chromium-browser"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "ffmpeg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
# CURSOR HIDE (X11 METHOD - NO UNCLUTTER)
# ============================================================

def hide_cursor():
    print("[INFO] Hiding cursor (xsetroot)...")

    subprocess.Popen([
        "xsetroot",
        "-cursor_name",
        "none"
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
# WATCHDOG
# ============================================================

def restart_all():
    print("[WATCHDOG] Restarting pipeline...")
    cleanup()
    main()

# ============================================================
# CLEANUP
# ============================================================

def cleanup(sig=None, frame=None):
    print("\n[INFO] Shutting down...")

    for p in processes:
        try:
            p.kill()
        except:
            pass

    kill_existing()
    sys.exit(0)

# ============================================================
# MAIN
# ============================================================

def main():
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    kill_existing()

    start_xvfb()
    hide_cursor()
    start_chromium()
    start_ffmpeg()

    print("\n[OK] STREAMING ACTIVE (ORACLE LINUX 9.7)")
    print("[INFO] Ctrl+C to stop\n")

    while True:
        time.sleep(5)

        for p in processes:
            if p.poll() is not None:
                print("[ERROR] Process crashed — restarting")
                restart_all()


if __name__ == "__main__":
    main()