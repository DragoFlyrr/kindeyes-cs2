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
from ctypes import wintypes
import json
import os
import socket
import sys
import threading
import time
import tkinter as tk

# Encourage faster GIL handoff between the tk tick thread and the HTTP
# server thread. CPython default is 5ms; we want snap-fire as soon as the
# recv returns, so favor thread switch responsiveness over throughput.
try:
    sys.setswitchinterval(0.001)
except Exception:
    pass

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
        "DebugHud": "1",
        "HotkeyToggle": "F9",
        "HotkeyEyelids": "F10",
        "TickMs": "1",
        "LatencyProbe": "0",
        "SelfTestOnStart": "1",
        # Synthesized curve - CS2 GSI undersamples the flashed peak
        # (we routinely see a single flashed=1 at the tail of a real
        # full-blind flash). When any flashed>0 arrives we drive the
        # overlay from a timer using the CS wiki direct-hit profile.
        "SynthesizeProfile": "1",
        "FullBlindMs": "1880",
        "FadeMs": "2990",
        "RetriggerMs": "250",
    }
}

VK = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}

user32 = ctypes.windll.user32

# Pre-bind SetLayeredWindowAttributes with explicit argtypes. Without argtypes
# ctypes reinfers types on every call (Python int -> c_int, which would TRUNCATE
# a 64-bit HWND). With argtypes set, marshaling is faster and correct.
_SLWA = user32.SetLayeredWindowAttributes
_SLWA.argtypes = (wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD)
_SLWA.restype = wintypes.BOOL

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
        self.last_gsi_perf = 0.0   # perf_counter of last POST receive
        self.gsi_hits = 0
        self.enabled = True
        self.eyelids = False
        self.peak_flash_since = 0
        self.applied_perf = 0.0    # perf_counter when current flashed value was applied to overlay
        self.last_applied_flash = -1
        self.self_test_flash = 0   # non-zero during startup self-test
        # synthesized curve state: set when GSI reports first flashed>0;
        # driven from perf_counter in tick() so we don't depend on GSI
        # continuing to sample during the flash
        self.profile_start_perf = 0.0
        self.profile_active = False

state = State()

# Direct-to-Win32 fast path. Plain Python ints (not ctypes c_void_p / c_ubyte)
# so the hot scan path avoids `.value` attribute lookups before the snap.
# Updated once at startup by run_overlay().
_overlay_hwnd = 0
_alpha_byte = 0
LWA_ALPHA = 0x00000002

# Previous GSI flashed value, mirrored at module scope so _scan_and_snap can
# do a rising-edge check via module-dict lookup (~50ns) instead of
# `state.prev_gsi_flashed` attribute access (~100ns). Written only from the
# HTTP server thread; Python int assignment is GIL-atomic.
_prev_flashed = 0

# Diagnostic: last SetLayeredWindowAttributes call duration in ms. Written by
# _scan_and_snap on every snap, read by the flash-trigger log line.
_last_slwa_ms = 0.0
_last_scan_ms = 0.0

# one-shot body dump on first real flash trigger - captures raw bytes so we
# can see the exact JSON format CS2 is sending (debugs PRE-PARSE misses).
_dumped_first_flash_body = False


def fast_snap_to_max():
    """Non-hot-path fallback used by the POST-parse branch. Hot path inlines
    the Win32 call inside _scan_and_snap to skip one Python frame."""
    h = _overlay_hwnd
    if not h:
        return
    try:
        _SLWA(h, 0, _alpha_byte, LWA_ALPHA)
    except Exception:
        pass


_HTTP_200_EMPTY = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def _scan_and_snap(buf):
    """Scan buffer for `"flashed":<nonzero>` and fire the Win32 snap inline.
    Whitespace-robust (`"flashed": 1` or `"flashed":1`), scans from any offset
    (pre-header-parse friendly). Returns perf_counter() on snap, else 0.0.
    Side effect: writes `_last_scan_ms` (scan cost) and `_last_slwa_ms`
    (Win32 call cost) for diagnostic logging on the next trigger line.
    """
    t_scan_start = time.perf_counter()
    idx = buf.find(b'"flashed":')
    if idx < 0:
        return 0.0
    vs = idx + 10
    n = len(buf)
    # skip whitespace after colon (CS2 pretty-prints: `"flashed": 1`)
    while vs < n and buf[vs] in (32, 9, 10, 13):
        vs += 1
    # rising edge only: digit 1-9 AND we weren't already flashed
    if vs < n and 49 <= buf[vs] <= 57 and _prev_flashed == 0:
        global _last_slwa_ms, _last_scan_ms
        _last_scan_ms = (time.perf_counter() - t_scan_start) * 1000.0
        h = _overlay_hwnd
        if h:
            t_w0 = time.perf_counter()
            _SLWA(h, 0, _alpha_byte, LWA_ALPHA)
            t_w1 = time.perf_counter()
            _last_slwa_ms = (t_w1 - t_w0) * 1000.0
            return t_w1
        return time.perf_counter()
    return 0.0


def handle_gsi_conn(conn):
    t_start = time.perf_counter()
    # Instrumentation points for latency breakdown:
    #   t_start -> t_pre_recv : setsockopt/settimeout + GIL wait after accept
    #   t_pre_recv -> t_first_chunk : kernel recv wait (time CS2 took to actually send)
    #   t_first_chunk -> pre_snap_perf : scan + inlined Win32 call
    t_pre_recv = 0.0
    t_first_chunk = 0.0
    try:
        conn.settimeout(2.0)
        # NOTE: TCP_NODELAY removed. It only disables Nagle on the OUTGOING
        # side (our 200 OK reply), which fires AFTER the snap. Doesn't affect
        # dark-time. Saves ~2-5us syscall every POST.
        # slurp until we have headers AND we can resolve Content-Length. Scan
        # for the flashed pattern on EVERY chunk so snap fires the moment we
        # have the bytes - not after header parsing.
        buf = b""
        pre_snap_perf = 0.0
        first = True
        while b"\r\n\r\n" not in buf:
            if first:
                t_pre_recv = time.perf_counter()
            chunk = conn.recv(8192)
            if first:
                t_first_chunk = time.perf_counter()
                first = False
            if not chunk:
                return
            buf += chunk
            if not pre_snap_perf:
                pre_snap_perf = _scan_and_snap(buf)
            if len(buf) > 65536:
                return
        header_end = buf.index(b"\r\n\r\n") + 4
        headers_bytes = buf[:header_end]
        body = buf[header_end:]

        # GET -> status page
        if headers_bytes[:4] == b"GET ":
            with state.lock:
                msg = (f"gsi_flashdim alive; hits={state.gsi_hits} "
                       f"last_flashed={state.flashed} "
                       f"enabled={state.enabled} eyelids={state.eyelids}\n").encode()
            reply = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                     b"Content-Length: " + str(len(msg)).encode() +
                     b"\r\nConnection: close\r\n\r\n" + msg)
            try:
                conn.sendall(reply)
            except Exception:
                pass
            return

        # POST: resolve Content-Length without allocating a list
        cl = 0
        lc = headers_bytes.lower()
        cl_idx = lc.find(b"\r\ncontent-length:")
        if cl_idx >= 0:
            val_start = cl_idx + 17
            val_end = headers_bytes.find(b"\r\n", val_start)
            if val_end > 0:
                try:
                    cl = int(headers_bytes[val_start:val_end].strip())
                except ValueError:
                    cl = 0
        while len(body) < cl:
            chunk = conn.recv(8192)
            if not chunk:
                break
            body += chunk
            # body may have grown past where the pattern lives - rescan
            if not pre_snap_perf:
                pre_snap_perf = _scan_and_snap(body)

        t_in = time.perf_counter()

        # reply IMMEDIATELY so CS2 can move on (this is AFTER the snap; only
        # unblocks CS2 sooner, doesn't affect our dark-time)
        try:
            conn.sendall(_HTTP_200_EMPTY)
        except Exception:
            pass

        # full parse for state update + logging
        triggered = False
        flashed = 0
        hit_no = 0
        try:
            data = json.loads(body) if body else {}
        except Exception as e:
            log(f"GSI parse error: {e!r} body[:200]={body[:200]!r}")
            return
        flashed_raw = data.get("player", {}).get("state", {}).get("flashed", 0)
        try:
            flashed = int(flashed_raw)
        except (TypeError, ValueError):
            flashed = 0
        if flashed < 0:
            flashed = 0
        elif flashed > 255:
            flashed = 255
        global _prev_flashed
        prev = _prev_flashed
        with state.lock:
            state.flashed = flashed
            state.last_gsi_ts = time.time()
            state.last_gsi_perf = t_in
            state.gsi_hits += 1
            if flashed > state.peak_flash_since:
                state.peak_flash_since = flashed
            hit_no = state.gsi_hits
            if flashed > 0 and (prev == 0 or not state.profile_active):
                state.profile_active = True
                state.profile_start_perf = t_in
                triggered = True
        # GIL-atomic int assignment; mirror of prev_gsi for the hot scan path
        _prev_flashed = flashed
        if triggered:
            global _dumped_first_flash_body
            if not _dumped_first_flash_body:
                _dumped_first_flash_body = True
                # one-shot dump to diagnose pre-parse misses
                log(f"first-flash body dump: {body[:600]!r}")
            # breakdown in ms: how long we waited to START recv (GIL + syscalls),
            # how long the first recv blocked (CS2/kernel send delay), and
            # end-to-end from conn-accept to snap. scan_ms + slwa_ms isolate
            # the Python/Win32 cost after recv returned.
            pre_recv_ms = (t_pre_recv - t_start) * 1000.0 if t_pre_recv else 0.0
            recv_wait_ms = (t_first_chunk - t_pre_recv) * 1000.0 if t_first_chunk and t_pre_recv else 0.0
            if not pre_snap_perf:
                fast_snap_to_max()
                post_snap_perf = time.perf_counter()
                log(f"flash trigger: GSI flashed={flashed} -> snap POST-PARSE "
                    f"(lag_from_conn_start={(post_snap_perf - t_start)*1000:.2f}ms, "
                    f"pre_recv={pre_recv_ms:.2f}ms recv_wait={recv_wait_ms:.2f}ms)")
            else:
                log(f"flash trigger: GSI flashed={flashed} -> snap PRE-PARSE "
                    f"(snap_at={(pre_snap_perf - t_start)*1000:.2f}ms, "
                    f"pre_recv={pre_recv_ms:.2f}ms recv_wait={recv_wait_ms:.2f}ms, "
                    f"scan={_last_scan_ms:.3f}ms slwa={_last_slwa_ms:.3f}ms, "
                    f"body_done={(t_in - t_start)*1000:.2f}ms)")
        if hit_no <= 5:
            provider = data.get("provider", {}).get("name", "?")
            activity = data.get("player", {}).get("activity", "?")
            player_state_keys = list(data.get("player", {}).get("state", {}).keys())
            top_keys = list(data.keys())
            log(f"GSI hit #{hit_no}: provider={provider!r} activity={activity!r} "
                f"flashed={flashed} top_keys={top_keys} state_fields={player_state_keys}")
        elif flashed > 0:
            activity = data.get("player", {}).get("activity", "?")
            log(f"GSI hit #{hit_no}: activity={activity} flashed={flashed} (flash event)")
        elif hit_no % 50 == 0:
            log(f"GSI hit #{hit_no}: flashed={flashed} (heartbeat)")
    except Exception as e:
        log(f"handle_gsi_conn error: {e!r}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_http_server(host, port):
    # Boost this thread to TIME_CRITICAL within the HIGH priority class the
    # process already runs under. Reduces scheduler-induced jitter between
    # `accept()` returning and our Python code getting scheduled.
    try:
        kernel32 = ctypes.windll.kernel32
        THREAD_PRIORITY_TIME_CRITICAL = 15
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(),
                                   THREAD_PRIORITY_TIME_CRITICAL)
    except Exception:
        pass
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(16)
        log(f"GSI listener bound {host}:{port} (raw, inline-dispatch, tc-prio)")
    except OSError as e:
        log(f"bind failed ({host}:{port}): {e!r}. another instance running?")
        return
    except Exception as e:
        log(f"HTTP server error: {e!r}")
        return
    while True:
        try:
            conn, _ = srv.accept()
        except Exception as e:
            log(f"accept error: {e!r}")
            continue
        # Inline dispatch: no per-conn thread. Saves ~50-200us of
        # threading.Thread(...).start() + context-switch on every POST.
        # GSI rate is ~10/sec and each handler runs in <5ms, so queuing in
        # the listen backlog (size 16) is a non-issue.
        try:
            handle_gsi_conn(conn)
        except Exception as e:
            log(f"handler error: {e!r}")


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


def boost_priority():
    try:
        winmm = ctypes.windll.winmm
        winmm.timeBeginPeriod(1)
    except Exception:
        pass
    try:
        kernel32 = ctypes.windll.kernel32
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        HIGH_PRIORITY_CLASS = 0x00000080
        handle = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(handle, HIGH_PRIORITY_CLASS)
    except Exception:
        pass


def run_overlay(settings):
    dpi_mode = set_dpi_aware()
    boost_priority()

    gamma = float(settings.get("GammaCurve", "0.65"))
    max_alpha = float(settings.get("MaxAlpha", "0.95"))
    color = settings.get("OverlayColor", "#000000")
    debug_hud = settings.get("DebugHud", "0").strip().lower() not in ("0", "false", "no", "")
    tick_ms = max(1, int(settings.get("TickMs", "4")))
    latency_probe = settings.get("LatencyProbe", "0").strip().lower() not in ("0", "false", "no", "")
    self_test = settings.get("SelfTestOnStart", "1").strip().lower() not in ("0", "false", "no", "")
    synth_profile = settings.get("SynthesizeProfile", "1").strip().lower() not in ("0", "false", "no", "")
    full_blind_s = float(settings.get("FullBlindMs", "1880")) / 1000.0
    fade_s = float(settings.get("FadeMs", "2990")) / 1000.0

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

    # expose HWND + max-alpha byte as plain ints for the hot Win32 snap path.
    # Plain globals skip the .value indirection a c_void_p / c_ubyte would add.
    global _overlay_hwnd, _alpha_byte
    _overlay_hwnd = user32.GetParent(root.winfo_id())
    _alpha_byte = int(round(max_alpha * 255))
    log(f"fast path wired: hwnd={_overlay_hwnd} alpha_byte={_alpha_byte}")

    hud_label = None
    if debug_hud:
        hud_label = tk.Label(root, text="", fg="#00ff80", bg=color,
                             font=("Consolas", 14))
        hud_label.place(x=40, y=40)

    log(f"overlay ready vscreen={vw}x{vh}@{vx},{vy} dpi={dpi_mode} "
        f"gamma={gamma} max_alpha={max_alpha} debug_hud={debug_hud} tick_ms={tick_ms} "
        f"latency_probe={latency_probe} synth_profile={synth_profile} "
        f"full_blind_s={full_blind_s} fade_s={fade_s}")

    last_logged_flash = 0
    last_applied_alpha = -1.0
    last_hud_update = 0.0

    def tick():
        nonlocal last_logged_flash, last_applied_alpha, last_hud_update
        t_tick = time.perf_counter()
        # single lock acquisition per tick - read everything we need, write
        # once at the end. cuts ~2 locks/tick vs the 3-acquire version.
        with state.lock:
            enabled = state.enabled
            eyelids = state.eyelids
            flashed = state.flashed
            last_ts = state.last_gsi_ts
            last_perf = state.last_gsi_perf
            hits = state.gsi_hits
            prev_applied = state.last_applied_flash
            prev_applied_perf = state.applied_perf
            selftest_val = state.self_test_flash
            profile_active = state.profile_active
            profile_start = state.profile_start_perf

        age = (time.time() - last_ts) if last_ts else 0.0
        effective_flash = 0 if age > 2.0 else flashed

        # synthesized flash value driven from the flash trigger timestamp:
        # 255 held for full_blind_s, then linear fade to 0 over fade_s
        synth_flash = 0
        profile_just_ended = False
        if synth_profile and profile_active:
            elapsed = t_tick - profile_start
            if elapsed < full_blind_s:
                synth_flash = 255
            elif elapsed < full_blind_s + fade_s:
                synth_flash = int(255 * (1.0 - (elapsed - full_blind_s) / fade_s))
            else:
                synth_flash = 0
                profile_just_ended = True

        # effective_flash = whichever is larger: what GSI told us, or our
        # synthesized curve. Guarantees we never under-darken but will
        # track a genuinely-sampled late-spike if one arrives.
        drive_flash = max(effective_flash, synth_flash)

        if eyelids:
            alpha = max_alpha
        elif selftest_val > 0:
            normalized = selftest_val / 255.0
            alpha = max_alpha * (normalized ** gamma)
        elif enabled:
            normalized = drive_flash / 255.0
            if normalized < 0:
                normalized = 0.0
            elif normalized > 1:
                normalized = 1.0
            alpha = max_alpha * (normalized ** gamma)
        else:
            alpha = 0.0

        # skip tkinter call when alpha is unchanged - tk.attributes() round-trips
        # through Tcl and schedules a composite; avoiding no-op calls cuts CPU
        # and, more importantly, avoids contending with DWM when we need the
        # next real update to land fast.
        if alpha != last_applied_alpha:
            try:
                root.attributes("-alpha", alpha)
            except tk.TclError:
                return
            last_applied_alpha = alpha

        # latency probe: log the delay from GSI-receive to overlay-apply
        # only when a NEW value arrives (prev_applied != flashed)
        if latency_probe and flashed != prev_applied and last_perf > 0:
            lag_ms = (t_tick - last_perf) * 1000.0
            gap_ms = (t_tick - prev_applied_perf) * 1000.0 if prev_applied_perf else 0.0
            log(f"probe flashed={prev_applied}->{flashed} alpha={alpha:.3f} "
                f"recv_to_apply={lag_ms:.1f}ms since_last_apply={gap_ms:.1f}ms")

        # single write-back of state
        if profile_just_ended or flashed != prev_applied or t_tick != prev_applied_perf:
            with state.lock:
                state.last_applied_flash = flashed
                state.applied_perf = t_tick
                if profile_just_ended:
                    state.profile_active = False

        if (flashed >= 8 and abs(flashed - last_logged_flash) >= 16) or \
           (flashed == 0 and last_logged_flash >= 8):
            log(f"flashed={flashed} alpha={alpha:.3f} enabled={enabled} eyelids={eyelids}")
            last_logged_flash = flashed

        # throttle HUD updates to ~30Hz; .configure() triggers a re-layout
        # and we don't need a million fps in the debug display.
        if hud_label is not None and (t_tick - last_hud_update) >= 0.033:
            hud_label.configure(
                text=(f"GSI hits={hits} age={age:.1f}s  "
                      f"flashed={flashed}  synth={synth_flash}  "
                      f"drive={drive_flash}  alpha={alpha:.2f}  "
                      f"profile={profile_active} "
                      f"enabled={enabled} eyelids={eyelids}")
            )
            last_hud_update = t_tick

        root.after(tick_ms, tick)

    if self_test:
        def run_self_test():
            log("self-test: ramping overlay up/down to prove it draws")
            steps_up = [30, 80, 150, 220, 255]
            steps_down = [220, 150, 80, 30, 0]
            for v in steps_up:
                with state.lock:
                    state.self_test_flash = v
                time.sleep(0.12)
            time.sleep(0.4)
            for v in steps_down:
                with state.lock:
                    state.self_test_flash = v
                time.sleep(0.08)
            with state.lock:
                state.self_test_flash = 0
            log("self-test complete")
        threading.Thread(target=run_self_test, daemon=True).start()

    tick()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass


def check_single_instance(port):
    """Exit early if another instance is already bound to our port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        result = s.connect_ex(("127.0.0.1", port))
        if result == 0:
            log(f"port {port} already in use -- another instance running. exiting.")
            s.close()
            return False
    except Exception:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass
    return True


def main():
    log("=== startup ===")
    log(f"script_dir={SCRIPT_DIR}")
    settings = load_settings()
    host = settings.get("ListenHost", "127.0.0.1")
    port = int(settings.get("ListenPort", "3000"))

    if not check_single_instance(port):
        return

    t_http = threading.Thread(target=run_http_server, args=(host, port), daemon=True)
    t_http.start()

    t_hot = threading.Thread(target=hotkey_watcher, args=(settings,), daemon=True)
    t_hot.start()

    run_overlay(settings)


if __name__ == "__main__":
    main()
