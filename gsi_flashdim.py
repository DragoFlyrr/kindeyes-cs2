"""
gsi_flashdim.py - CS2 flashbang overlay driven by Game State Integration.

Replaces pixel-based detection with Valve's official GSI push API. CS2 reports
player.state.flashed (0-255) on every state change; we map that directly to
overlay alpha. The game's engine already weights the value by angle-to-flash
and distance, so we get continuous proportional darkening instead of the
4-bucket approximation the old AHK tool used.

Setup:
  1. Drop gamestate_integration_flashdim.cfg into CS2's cfg folder:
       <Steam>/steamapps/common/Counter-Strike Global Offensive/game/csgo/cfg/
  2. Run this script (run_gsi.bat). Can start before or after CS2.
  3. Run CS2 in fullscreen-windowed or borderless (overlay does not show
     through exclusive fullscreen - standard limitation of all GDI overlays).

Hotkeys:
  F9  - toggle detection on/off
  F10 - hold for manual eyelids (darken while key is held)
"""
import configparser
import ctypes
import json
import os
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, HTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "gsi_settings.ini")
LOG_PATH = os.path.join(SCRIPT_DIR, "gsi_flashdim.log")

DEFAULT_SETTINGS = {
    "Settings": {
        "ListenHost": "127.0.0.1",
        "ListenPort": "3000",
        "GammaCurve": "0.65",
        "MaxAlpha": "0.95",
        "OverlayColor": "#000000",
        "DebugHud": "0",
        "HotkeyToggle": "F9",
        "HotkeyEyelids": "F10",
    }
}

VK = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}

user32 = ctypes.windll.user32

_log_lock = threading.Lock()

def log(msg):
    now = time.time()
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int(now*1000)%1000:03d}"
    line = f"{ts} | {msg}\n"
    with _log_lock:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass
    try:
        print(line, end="", flush=True)
    except Exception:
        pass


def load_settings():
    cp = configparser.ConfigParser()
    if os.path.exists(SETTINGS_PATH):
        cp.read(SETTINGS_PATH, encoding="utf-8")
    changed = False
    for sect, kvs in DEFAULT_SETTINGS.items():
        if sect not in cp:
            cp[sect] = {}
            changed = True
        for k, v in kvs.items():
            if k not in cp[sect]:
                cp[sect][k] = v
                changed = True
    if changed:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            cp.write(f)
    return cp["Settings"]


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.flashed = 0
        self.last_gsi_ts = 0.0
        self.gsi_hits = 0
        self.enabled = True
        self.eyelids = False
        self.peak_flash_since = 0

state = State()


class GSIHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            data = json.loads(body) if body else {}
            flashed_raw = data.get("player", {}).get("state", {}).get("flashed", 0)
            flashed = int(flashed_raw)
            if flashed < 0:
                flashed = 0
            elif flashed > 255:
                flashed = 255
            with state.lock:
                state.flashed = flashed
                state.last_gsi_ts = time.time()
                state.gsi_hits += 1
                if flashed > state.peak_flash_since:
                    state.peak_flash_since = flashed
                if state.gsi_hits == 1:
                    log(f"first GSI payload received; flashed={flashed}")
        except Exception as e:
            log(f"GSI parse error: {e!r}")
        try:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass

    def log_message(self, *args, **kwargs):
        return


def run_http_server(host, port):
    try:
        srv = HTTPServer((host, port), GSIHandler)
        log(f"GSI listener bound {host}:{port}")
        srv.serve_forever()
    except OSError as e:
        log(f"bind failed ({host}:{port}): {e!r}. another instance running?")
    except Exception as e:
        log(f"HTTP server error: {e!r}")


def hotkey_watcher(settings):
    tog_vk = VK.get(settings.get("HotkeyToggle", "F9").upper(), VK["F9"])
    eye_vk = VK.get(settings.get("HotkeyEyelids", "F10").upper(), VK["F10"])
    prev_tog = False
    while True:
        tog_now = bool(user32.GetAsyncKeyState(tog_vk) & 0x8000)
        if tog_now and not prev_tog:
            with state.lock:
                state.enabled = not state.enabled
                now_enabled = state.enabled
            log(f"toggle -> enabled={now_enabled}")
        prev_tog = tog_now
        eye_now = bool(user32.GetAsyncKeyState(eye_vk) & 0x8000)
        with state.lock:
            state.eyelids = eye_now
        time.sleep(0.02)


def make_click_through(root):
    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id())
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor-v2"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


def run_overlay(settings):
    dpi_mode = set_dpi_aware()

    gamma = float(settings.get("GammaCurve", "0.65"))
    max_alpha = float(settings.get("MaxAlpha", "0.95"))
    color = settings.get("OverlayColor", "#000000")
    debug_hud = settings.get("DebugHud", "0").strip().lower() not in ("0", "false", "no", "")

    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    root.configure(bg=color)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.0)
    root.deiconify()
    make_click_through(root)

    hud_label = None
    if debug_hud:
        hud_label = tk.Label(root, text="", fg="#00ff80", bg=color,
                             font=("Consolas", 14))
        hud_label.place(x=40, y=40)

    log(f"overlay ready vscreen={vw}x{vh}@{vx},{vy} dpi={dpi_mode} "
        f"gamma={gamma} max_alpha={max_alpha} debug_hud={debug_hud}")

    last_logged_flash = 0

    def tick():
        nonlocal last_logged_flash
        with state.lock:
            enabled = state.enabled
            eyelids = state.eyelids
            flashed = state.flashed
            last_ts = state.last_gsi_ts
            hits = state.gsi_hits

        age = (time.time() - last_ts) if last_ts else 0.0
        if age > 2.0:
            effective_flash = 0
        else:
            effective_flash = flashed

        if eyelids:
            alpha = max_alpha
        elif enabled:
            normalized = effective_flash / 255.0
            if normalized < 0:
                normalized = 0.0
            elif normalized > 1:
                normalized = 1.0
            alpha = max_alpha * (normalized ** gamma)
        else:
            alpha = 0.0

        try:
            root.attributes("-alpha", alpha)
        except tk.TclError:
            return

        if (flashed >= 8 and abs(flashed - last_logged_flash) >= 16) or \
           (flashed == 0 and last_logged_flash >= 8):
            log(f"flashed={flashed} alpha={alpha:.3f} enabled={enabled} eyelids={eyelids}")
            last_logged_flash = flashed

        if hud_label is not None:
            hud_label.configure(
                text=(f"GSI hits={hits} age={age:.1f}s  "
                      f"flashed={flashed}  alpha={alpha:.2f}  "
                      f"enabled={enabled} eyelids={eyelids}")
            )

        root.after(8, tick)

    tick()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass


def main():
    log("=== startup ===")
    log(f"script_dir={SCRIPT_DIR}")
    settings = load_settings()
    host = settings.get("ListenHost", "127.0.0.1")
    port = int(settings.get("ListenPort", "3000"))

    t_http = threading.Thread(target=run_http_server, args=(host, port), daemon=True)
    t_http.start()

    t_hot = threading.Thread(target=hotkey_watcher, args=(settings,), daemon=True)
    t_hot.start()

    run_overlay(settings)


if __name__ == "__main__":
    main()
