"""
gsi_flashdim.py - CS2 flashbang overlay driven by Game State Integration.

Uses Windows MagSetFullscreenColorEffect (DWM-level color transform).
Hot path: accept() -> scan -> MagSetFullscreenColorEffect inline, all on
one thread. No cross-thread event hop; accept loop is non-blocking and
interleaves fade ticks via tight polling.

Setup:
  1. Drop gamestate_integration_flashdim.cfg into CS2's cfg folder:
       <Steam>/steamapps/common/Counter-Strike Global Offensive/game/csgo/cfg/
  2. Run this script (run_gsi.bat).
  3. CS2 in fullscreen-windowed or borderless.

Hotkeys:
  F9  - toggle detection on/off
  F10 - hold for manual eyelids

Threads:
  main   -> run_unified_loop (owns MagInitialize AND accept loop + fade tick)
  hotkey -> hotkey_watcher (mutates state only; no Mag calls)

All MagSet calls MUST originate from the main thread (API thread-affinity).
"""
import configparser
import ctypes
from ctypes import wintypes, c_float
import json
import os
import socket
import sys
import threading
import time

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
        "HotkeyConfirm": "F8",
        "HotkeyReload": "F11",
        "TickMs": "1",
        "LatencyProbe": "0",
        "SelfTestOnStart": "1",
        "SynthesizeProfile": "1",
        "FullBlindMs": "1880",
        "FadeMs": "2990",
        "RetriggerMs": "250",
        "VisualDetect": "1",       # 1=enable DXGI Desktop Duplication primary trigger
        "VisualMinBright": "7",    # N-of-9 pixels must pass; tight to reject bright walls
        "VisualBrightMin": "230",  # per-channel floor; flashbangs hit 255, walls cap ~200
        "VisualMaxSpread": "20",   # max(R,G,B)-min(R,G,B); flashbangs are pure white
        "AudioPrime": "1",         # 1=enable WASAPI loopback audio priming
        "AudioPeakThresh": "0.45", # 0..1 sample peak needed to prime (was 0.90)
        "AudioRiseMin": "0.30",    # 0..1 peak rise over recent window (was 0.55)
        "AudioPrimeMs": "180",     # visual runs with loose gates this long after audio
        "AudioFire": "1",          # 1=audio prime fires MagSet directly (proactive)
        "AudioFireGateGsiMs": "5000", # require CS2 GSI POST within this window
        "AudioRingHz": "2200",     # target freq for flashbang tinnitus Goertzel (Hz)
        "AudioRingMin": "0.05",    # min normalized Goertzel magnitude (0 = disable)
        "AudioRingRatioMin": "0.35", # min mag/rms tonality ratio (0 = disable)
    }
}

VK = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}

user32 = ctypes.windll.user32


# --- Magnification API -----------------------------------------------------
class MAGCOLOREFFECT(ctypes.Structure):
    _fields_ = [("transform", c_float * 25)]

_mag = ctypes.windll.magnification
_mag.MagInitialize.restype = wintypes.BOOL
_mag.MagUninitialize.restype = wintypes.BOOL
_mag.MagSetFullscreenColorEffect.argtypes = (ctypes.POINTER(MAGCOLOREFFECT),)
_mag.MagSetFullscreenColorEffect.restype = wintypes.BOOL


def _make_effect(brightness):
    m = (c_float * 25)()
    m[0] = brightness
    m[6] = brightness
    m[12] = brightness
    m[18] = 1.0
    m[24] = 1.0
    return MAGCOLOREFFECT(transform=m)


_EFFECT_DARK = _make_effect(0.0)
_EFFECT_IDENTITY = _make_effect(1.0)
_EFFECT_FADE = _make_effect(1.0)
_dark_p = ctypes.byref(_EFFECT_DARK)
_identity_p = ctypes.byref(_EFFECT_IDENTITY)
_fade_p = ctypes.byref(_EFFECT_FADE)
_min_brightness = 0.05


# --- Native hot-path DLL ---------------------------------------------------
# flashdim_hot.dll wraps accept+recv+scan+MagSet in native code so the hot
# path has no Python interpreter overhead. Built from flashdim_hot.c via
# build_hot.bat. If the DLL is missing or fails to load, we fall back to
# the pure-Python hot path.
class _HotTick(ctypes.Structure):
    _fields_ = [
        ("had_connection", ctypes.c_int32),
        ("fired", ctypes.c_int32),
        ("flashed_value", ctypes.c_int32),
        ("bytes_read", ctypes.c_int32),
        ("t_gqcs_ns", ctypes.c_uint64),
        ("t_scan_ns", ctypes.c_uint64),
        ("t_magset_ns", ctypes.c_uint64),
        ("t_reply_ns", ctypes.c_uint64),
        ("t_total_ns", ctypes.c_uint64),
    ]

class _HotVisual(ctypes.Structure):
    _fields_ = [
        ("had_frame", ctypes.c_int32),
        ("fired", ctypes.c_int32),
        ("bright_count", ctypes.c_int32),
        ("frame_num", ctypes.c_int32),
        ("relaxed_count", ctypes.c_int32),
        ("prev_relaxed", ctypes.c_int32),
        ("trigger_kind", ctypes.c_int32),
        ("delta_count", ctypes.c_int32),
        ("audio_primed", ctypes.c_int32),
        ("max_luma", ctypes.c_int32),
        ("ramp_rise", ctypes.c_int32),
        ("t_acquire_ns", ctypes.c_uint64),
        ("t_copy_ns", ctypes.c_uint64),
        ("t_scan_ns", ctypes.c_uint64),
        ("t_magset_ns", ctypes.c_uint64),
        ("t_total_ns", ctypes.c_uint64),
    ]


class _HotAudio(ctypes.Structure):
    _fields_ = [
        ("had_samples", ctypes.c_int32),
        ("primed", ctypes.c_int32),
        ("frames_read", ctypes.c_int32),
        ("peak", ctypes.c_float),
        ("rise", ctypes.c_float),
        ("ring_mag", ctypes.c_float),
        ("ring_ratio", ctypes.c_float),
        ("t_drain_ns", ctypes.c_uint64),
        ("t_total_ns", ctypes.c_uint64),
    ]

_hot = None
_hot_load_err = None
_hot_ring_ready = False
try:
    _hot_dll_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "flashdim_hot.dll")
    _hot = ctypes.CDLL(_hot_dll_path)
    _hot.hot_init.argtypes = (ctypes.c_float, ctypes.c_uint16)
    _hot.hot_init.restype = ctypes.c_int
    _hot.hot_apply_fade.argtypes = (ctypes.c_float,)
    _hot.hot_apply_fade.restype = ctypes.c_int
    _hot.hot_apply_dark.argtypes = ()
    _hot.hot_apply_dark.restype = ctypes.c_int
    _hot.hot_apply_identity.argtypes = ()
    _hot.hot_apply_identity.restype = ctypes.c_int
    _hot.hot_tick.argtypes = (ctypes.c_int32, ctypes.c_int32, ctypes.POINTER(_HotTick))
    _hot.hot_tick.restype = ctypes.c_int
    _hot.hot_visual_init.argtypes = (ctypes.c_int32, ctypes.c_int32, ctypes.c_int32)
    _hot.hot_visual_init.restype = ctypes.c_int
    _hot.hot_visual_tick.argtypes = (ctypes.c_int32, ctypes.POINTER(_HotVisual))
    _hot.hot_visual_tick.restype = ctypes.c_int
    _hot.hot_visual_get_info.argtypes = (
        ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_int32),
    )
    _hot.hot_visual_get_info.restype = ctypes.c_int
    _hot.hot_audio_init.argtypes = (ctypes.c_float, ctypes.c_float, ctypes.c_int32)
    _hot.hot_audio_init.restype = ctypes.c_int
    _hot.hot_audio_tick.argtypes = (ctypes.POINTER(_HotAudio),)
    _hot.hot_audio_tick.restype = ctypes.c_int
    _hot.hot_audio_get_info.argtypes = (
        ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32),
    )
    _hot.hot_audio_get_info.restype = ctypes.c_int
    try:
        _hot.hot_audio_configure_ring.argtypes = (ctypes.c_float, ctypes.c_float, ctypes.c_float)
        _hot.hot_audio_configure_ring.restype = ctypes.c_int
        _hot_ring_ready = True
    except AttributeError:
        _hot_ring_ready = False
    try:
        _hot.hot_audio_get_device_name.argtypes = (ctypes.c_wchar_p, ctypes.c_int32)
        _hot.hot_audio_get_device_name.restype = ctypes.c_int
        _hot_devname_ready = True
    except AttributeError:
        _hot_devname_ready = False
except OSError as e:
    _hot = None
    _hot_load_err = repr(e)

# Optional gamma exports. Older builds of flashdim_hot.dll won't have these
# symbols, so we bind them separately and feature-detect at runtime. Gamma
# ramp is a scanout-path darkening layer that fires ~1 vsync ahead of MagSet.
_hot_gamma_ready = False
if _hot is not None:
    try:
        _hot.hot_gamma_init.argtypes = (ctypes.c_float,)
        _hot.hot_gamma_init.restype = ctypes.c_int
        _hot.hot_gamma_apply_dark.argtypes = ()
        _hot.hot_gamma_apply_dark.restype = ctypes.c_int
        _hot.hot_gamma_apply_fade.argtypes = (ctypes.c_float,)
        _hot.hot_gamma_apply_fade.restype = ctypes.c_int
        _hot.hot_gamma_apply_identity.argtypes = ()
        _hot.hot_gamma_apply_identity.restype = ctypes.c_int
        _hot_gamma_ready = True
    except AttributeError:
        _hot_gamma_ready = False


# --- logging ---------------------------------------------------------------
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
        self.last_gsi_perf = 0.0
        self.gsi_hits = 0
        self.enabled = True
        self.eyelids = False
        self.peak_flash_since = 0
        self.self_test_flash = 0

state = State()


# --- hot-path globals ------------------------------------------------------
# Single-threaded: accept/scan/magset and fade tick all run on main. Hotkey
# thread only mutates state.* under state.lock.
_prev_flashed = 0
_mag_profile_start = 0.0
_mag_profile_active = False
_mag_profile_audio_only = False
_profile_saw_gsi = False
g_audio_ring_min = 0.0
g_audio_ring_ratio = 0.0
_last_applied = 1.0
_last_scan_us = 0.0
_last_magset_us = 0.0
_dumped_first_flash_body = False
# perf_counter timestamp after which we should revert gamma to identity
# (0.0 = no pending revert). Set on fire, cleared after revert fires.
_gamma_revert_deadline = 0.0

_HTTP_200_EMPTY = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def finish_gsi_conn(conn, prebuf, prebuf_n, t_start, snap_perf,
                    t_first_chunk, pre_recv_t, scan_us, magset_us):
    """Post-snap HTTP handling. Runs AFTER the MagSet already fired (or was
    skipped), so nothing here is on the critical snap path. Takes the
    pre-read first chunk as bytes so we don't re-read it."""
    try:
        # hot path left conn in non-blocking mode (inherited from listen).
        # drop back to blocking for the rest of the body + reply.
        conn.setblocking(True)
        conn.settimeout(2.0)
        buf = bytes(prebuf[:prebuf_n])
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(8192)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 65536:
                return
        header_end = buf.index(b"\r\n\r\n") + 4
        headers_bytes = buf[:header_end]
        body = buf[header_end:]

        if headers_bytes[:4] == b"GET ":
            with state.lock:
                msg = (f"gsi_flashdim alive (mag-inline); hits={state.gsi_hits} "
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

        t_in = time.perf_counter()

        try:
            conn.sendall(_HTTP_200_EMPTY)
        except Exception:
            pass

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
        global _prev_flashed, _mag_profile_start, _mag_profile_active, _mag_profile_audio_only, _last_applied, _profile_saw_gsi
        prev = _prev_flashed
        triggered = False
        with state.lock:
            state.flashed = flashed
            state.last_gsi_ts = time.time()
            state.last_gsi_perf = t_in
            state.gsi_hits += 1
            if flashed > state.peak_flash_since:
                state.peak_flash_since = flashed
            hit_no = state.gsi_hits
            if flashed > 0 and prev == 0:
                triggered = True
        _prev_flashed = flashed
        if triggered:
            global _dumped_first_flash_body
            if not _dumped_first_flash_body:
                _dumped_first_flash_body = True
                log(f"first-flash body dump: {body[:600]!r}")
            pre_recv_ms = (pre_recv_t - t_start) * 1000.0 if pre_recv_t else 0.0
            recv_wait_ms = (t_first_chunk - pre_recv_t) * 1000.0 if t_first_chunk and pre_recv_t else 0.0
            if not snap_perf:
                # fallback: pre-scan missed (flashed field after first chunk?)
                t_pre = time.perf_counter()
                _mag.MagSetFullscreenColorEffect(_dark_p)
                t_post = time.perf_counter()
                _mag_profile_start = t_post
                _mag_profile_active = True
                _mag_profile_audio_only = False
                _profile_saw_gsi = False
                _last_applied = _min_brightness
                log(f"flash trigger: GSI flashed={flashed} -> snap POST-PARSE "
                    f"(lag_from_conn_start={(t_post - t_start)*1000:.3f}ms, "
                    f"pre_recv={pre_recv_ms:.3f}ms recv_wait={recv_wait_ms:.3f}ms, "
                    f"magset={(t_post - t_pre)*1_000_000:.1f}us)")
            else:
                log(f"flash trigger: GSI flashed={flashed} -> snap INLINE "
                    f"(snap_at={(snap_perf - t_start)*1000:.3f}ms, "
                    f"pre_recv={pre_recv_ms:.3f}ms recv_wait={recv_wait_ms:.3f}ms, "
                    f"scan={scan_us:.1f}us magset={magset_us:.1f}us, "
                    f"body_done={(t_in - t_start)*1000:.3f}ms)")
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
        log(f"finish_gsi_conn error: {e!r}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _run_confirm_pulse():
    """Short visual 'I'm alive' pulse — two gentle dims so the user
    can see the overlay is active without it being jarring."""
    log("confirm: overlay alive-pulse")
    for v in [120, 0, 120, 0]:
        with state.lock:
            state.self_test_flash = v
        time.sleep(0.18)
    with state.lock:
        state.self_test_flash = 0
    log("confirm: pulse complete")


def hotkey_watcher(settings):
    tog_vk = VK.get(settings.get("HotkeyToggle", "F9").upper(), VK["F9"])
    eye_vk = VK.get(settings.get("HotkeyEyelids", "F10").upper(), VK["F10"])
    cfm_vk = VK.get(settings.get("HotkeyConfirm", "F8").upper(), VK["F8"])
    rld_vk = VK.get(settings.get("HotkeyReload", "F11").upper(), VK["F11"])
    prev_tog = False
    prev_cfm = False
    prev_rld = False
    while True:
        tog_now = bool(user32.GetAsyncKeyState(tog_vk) & 0x8000)
        if tog_now and not prev_tog:
            with state.lock:
                state.enabled = not state.enabled
                now_enabled = state.enabled
            log(f"toggle -> enabled={now_enabled}")
        prev_tog = tog_now

        cfm_now = bool(user32.GetAsyncKeyState(cfm_vk) & 0x8000)
        if cfm_now and not prev_cfm:
            threading.Thread(target=_run_confirm_pulse, daemon=True).start()
        prev_cfm = cfm_now

        rld_now = bool(user32.GetAsyncKeyState(rld_vk) & 0x8000)
        if rld_now and not prev_rld:
            log("reload hotkey pressed -> exiting (run_gsi.bat will restart)")
            # brief pulse so user sees acknowledgement
            threading.Thread(target=_run_confirm_pulse, daemon=True).start()
            time.sleep(0.9)
            os._exit(7)  # bat file restarts on exit code 7
        prev_rld = rld_now

        eye_now = bool(user32.GetAsyncKeyState(eye_vk) & 0x8000)
        with state.lock:
            state.eyelids = eye_now
        time.sleep(0.02)


def boost_priority():
    try:
        winmm = ctypes.windll.winmm
        winmm.timeBeginPeriod(1)
    except Exception:
        pass
    try:
        kernel32 = ctypes.windll.kernel32
        HIGH_PRIORITY_CLASS = 0x00000080
        handle = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(handle, HIGH_PRIORITY_CLASS)
        THREAD_PRIORITY_TIME_CRITICAL = 15
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(),
                                   THREAD_PRIORITY_TIME_CRITICAL)
    except Exception:
        pass


def prewarm_mag():
    """Run one dark -> identity cycle after self-test so the DWM compositor
    has the matrix path compiled/cached. First real flash should hit the
    warm path instead of eating a ~100us cold cost."""
    try:
        _mag.MagSetFullscreenColorEffect(_dark_p)
        _mag.MagSetFullscreenColorEffect(_identity_p)
        _mag.MagSetFullscreenColorEffect(_dark_p)
        _mag.MagSetFullscreenColorEffect(_identity_p)
        log("mag prewarm: dark/identity cycle x2 done")
    except Exception as e:
        log(f"mag prewarm failed: {e!r}")


def run_unified_loop(settings, host, port):
    """Single-thread loop: owns Mag, handles accept, runs fade tick.
    Busy-polls the listening socket (no event, no wake hop). Fade ticks
    interleave when no conn is pending."""
    boost_priority()

    gamma = float(settings.get("GammaCurve", "0.65"))
    max_alpha = float(settings.get("MaxAlpha", "0.95"))
    tick_ms = max(1, int(settings.get("TickMs", "1")))
    self_test = settings.get("SelfTestOnStart", "1").strip().lower() not in ("0", "false", "no", "")
    synth_profile = settings.get("SynthesizeProfile", "1").strip().lower() not in ("0", "false", "no", "")
    full_blind_s = float(settings.get("FullBlindMs", "1880")) / 1000.0
    fade_s = float(settings.get("FadeMs", "2990")) / 1000.0
    audio_blind_s = float(settings.get("AudioProactiveBlindMs", "1880")) / 1000.0
    audio_fade_s  = float(settings.get("AudioProactiveFadeMs",  "2990")) / 1000.0

    ctypes.set_last_error(0)
    if not _mag.MagInitialize():
        err = ctypes.get_last_error()
        log(f"FATAL: MagInitialize failed (err={err}).")
        return
    log("MagInitialize OK")

    global _min_brightness, _mag_profile_start, _mag_profile_active, _mag_profile_audio_only
    global _last_applied, _prev_flashed, _last_scan_us, _last_magset_us
    global _gamma_revert_deadline, _profile_saw_gsi
    _min_brightness = max(0.0, min(1.0, 1.0 - max_alpha))
    _EFFECT_DARK.transform[0] = _min_brightness
    _EFFECT_DARK.transform[6] = _min_brightness
    _EFFECT_DARK.transform[12] = _min_brightness

    hot_ready = False
    if _hot is not None:
        # Native side owns the listen socket + IOCP under this path.
        rc = _hot.hot_init(ctypes.c_float(_min_brightness),
                           ctypes.c_uint16(port))
        if rc == 0:
            hot_ready = True
            log(f"native hot path loaded (flashdim_hot.dll), IOCP+AcceptEx "
                f"bound 127.0.0.1:{port}")
        else:
            log(f"flashdim_hot.dll hot_init rc={rc}, falling back to Python")
    elif _hot_load_err:
        log(f"flashdim_hot.dll not loaded ({_hot_load_err}); using Python path")

    # Gamma ramp init. Scanout-path darkening lands ~1 vsync before MagSet
    # because it bypasses the DWM compositor. Win10 clamps the usable LUT
    # range unless HKLM\...\ICM\GdiICMGammaRange is 0x100, but even the
    # clamped range shaves a frame off visible flash.
    gamma_ready = False
    if hot_ready and _hot_gamma_ready:
        rc_g = _hot.hot_gamma_init(ctypes.c_float(_min_brightness))
        if rc_g == 0:
            gamma_ready = True
            log(f"gamma ramp path loaded (scanout-layer dim, "
                f"min_bright={_min_brightness:.3f})")
        else:
            log(f"hot_gamma_init rc={rc_g}, gamma layer disabled")
    elif hot_ready and not _hot_gamma_ready:
        log("flashdim_hot.dll lacks hot_gamma_* exports; rebuild to enable "
            "scanout-layer dim")

    # DXGI Desktop Duplication primary trigger (native). Fires MagSet ahead
    # of GSI because CS2's GSI pipeline coalesces and throttles, while DXGI
    # sees the bright frame ~1 monitor refresh after the GPU renders it.
    visual_enabled = settings.get("VisualDetect", "1").strip().lower() not in ("0", "false", "no", "")
    visual_ready = False
    if hot_ready and visual_enabled:
        v_min   = int(settings.get("VisualMinBright", "5"))
        v_bmin  = int(settings.get("VisualBrightMin", "150"))
        v_spread = int(settings.get("VisualMaxSpread", "25"))
        rc_v = _hot.hot_visual_init(ctypes.c_int32(v_min),
                                    ctypes.c_int32(v_bmin),
                                    ctypes.c_int32(v_spread))
        if rc_v == 0:
            w = ctypes.c_int32(0); h = ctypes.c_int32(0)
            mn = ctypes.c_int32(0); bmin = ctypes.c_int32(0); sp = ctypes.c_int32(0)
            _hot.hot_visual_get_info(ctypes.byref(w), ctypes.byref(h),
                                     ctypes.byref(mn), ctypes.byref(bmin),
                                     ctypes.byref(sp))
            visual_ready = True
            log(f"DXGI visual path loaded: {w.value}x{h.value} "
                f"min_bright={mn.value}/9 bright_min={bmin.value} "
                f"max_spread={sp.value}")
        else:
            log(f"hot_visual_init rc={rc_v}, GSI-only path")

    # WASAPI audio priming. Does NOT fire MagSet on its own -- only
    # lowers the visual scan threshold for ~180ms after hearing a
    # loud transient. Flashes heard through walls that aren't on
    # your screen won't dim (visual still has to see bright pixels).
    audio_enabled = settings.get("AudioPrime", "1").strip().lower() not in ("0", "false", "no", "")
    audio_fire_enabled = settings.get("AudioFire", "1").strip().lower() not in ("0", "false", "no", "")
    audio_fire_gate_ms = float(settings.get("AudioFireGateGsiMs", "5000"))
    audio_fire_gate_s = audio_fire_gate_ms / 1000.0
    audio_ready = False
    g_audio_peak = 0.0
    g_audio_rise = 0.0
    if hot_ready and audio_enabled:
        a_peak = float(settings.get("AudioPeakThresh", "0.45"))
        a_rise = float(settings.get("AudioRiseMin", "0.30"))
        a_prime = int(settings.get("AudioPrimeMs", "180"))
        g_audio_peak = a_peak
        g_audio_rise = a_rise
        rc_a = _hot.hot_audio_init(ctypes.c_float(a_peak),
                                   ctypes.c_float(a_rise),
                                   ctypes.c_int32(a_prime))
        if rc_a == 0:
            sr = ctypes.c_int32(0); ch = ctypes.c_int32(0)
            bits = ctypes.c_int32(0); isf = ctypes.c_int32(0)
            _hot.hot_audio_get_info(ctypes.byref(sr), ctypes.byref(ch),
                                    ctypes.byref(bits), ctypes.byref(isf))
            audio_ready = True
            log(f"WASAPI loopback audio priming loaded: "
                f"{sr.value}Hz/{ch.value}ch/{bits.value}bit "
                f"float={isf.value} peak>={a_peak} rise>={a_rise} "
                f"prime_ms={a_prime}")
            if _hot_devname_ready:
                _dev_buf = ctypes.create_unicode_buffer(256)
                _dev_rc = _hot.hot_audio_get_device_name(_dev_buf, 256)
                log(f"WASAPI bound device: '{_dev_buf.value}' rc={_dev_rc} "
                    f"(default render endpoint)")
            if _hot_ring_ready:
                a_ring_hz = float(settings.get("AudioRingHz", "2200"))
                a_ring_min = float(settings.get("AudioRingMin", "0.05"))
                a_ring_ratio = float(settings.get("AudioRingRatioMin", "0.35"))
                global g_audio_ring_min, g_audio_ring_ratio
                g_audio_ring_min = a_ring_min
                g_audio_ring_ratio = a_ring_ratio
                _hot.hot_audio_configure_ring(ctypes.c_float(a_ring_hz),
                                              ctypes.c_float(a_ring_min),
                                              ctypes.c_float(a_ring_ratio))
                log(f"flashbang tinnitus detector: "
                    f"target={a_ring_hz:.0f}Hz ring_mag>={a_ring_min} "
                    f"ring_ratio>={a_ring_ratio}")
        else:
            log(f"hot_audio_init rc={rc_a}, no audio priming")

    srv = None
    if not hot_ready:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((host, port))
            srv.listen(16)
            srv.setblocking(False)  # busy-poll; accept raises BlockingIOError when idle
            log(f"GSI listener bound {host}:{port} (non-blocking, busy-poll)")
        except OSError as e:
            log(f"bind failed ({host}:{port}): {e!r}")
            try:
                _mag.MagUninitialize()
            except Exception:
                pass
            return

    log(f"unified loop ready: min_brightness={_min_brightness:.3f} "
        f"(max_alpha={max_alpha}) gamma={gamma} tick_ms={tick_ms} "
        f"synth_profile={synth_profile} full_blind_s={full_blind_s} fade_s={fade_s}")

    # self-test to prove the overlay draws
    if self_test:
        def run_self_test():
            log("self-test: ramping mag brightness")
            for v in [30, 80, 150, 220, 255]:
                with state.lock:
                    state.self_test_flash = v
                time.sleep(0.12)
            time.sleep(0.4)
            for v in [220, 150, 80, 30, 0]:
                with state.lock:
                    state.self_test_flash = v
                time.sleep(0.08)
            with state.lock:
                state.self_test_flash = 0
            log("self-test complete")
            prewarm_mag()
        threading.Thread(target=run_self_test, daemon=True).start()
    else:
        # prewarm synchronously when self-test is off
        prewarm_mag()

    tick_s = tick_ms / 1000.0
    last_tick_perf = 0.0
    last_logged_flash = 0

    # Hot-path local binds. Every attribute lookup skipped here saves ~100ns,
    # and at 40us budget that adds up. Also bypasses `self` and dict lookups.
    mag_set = _mag.MagSetFullscreenColorEffect
    dark_p = _dark_p
    fade_effect = _EFFECT_FADE
    fade_p = _fade_p
    perf_counter = time.perf_counter
    srv_accept = srv.accept if srv is not None else None

    # Pre-allocated recv buffer. CS2 GSI message is ~400 bytes; 4KB is ample.
    # recv_into(memoryview) writes without allocating a new bytes object per
    # connection -- saves ~2-5us of Python alloc/GC churn.
    rbuf = bytearray(4096)
    rmv = memoryview(rbuf)
    FIND = rbuf.find
    find_flashed = b'"flashed":'

    # Native hot-path: C owns the socket and handles the full POST/reply
    # cycle. Python only receives the parsed flashed value + timings for
    # state bookkeeping.
    if hot_ready:
        _hot_tick_result = _HotTick()
        _hot_tick_p = ctypes.byref(_hot_tick_result)
        _hot_tick = _hot.hot_tick
        _hot_fade = _hot.hot_apply_fade
    _hot_visual_result = _HotVisual() if visual_ready else None
    _hot_visual_p = ctypes.byref(_hot_visual_result) if visual_ready else None
    _hot_visual_tick = _hot.hot_visual_tick if visual_ready else None
    _visual_frames_seen = 0
    _visual_last_bright = 0

    _hot_audio_result = _HotAudio() if audio_ready else None
    _hot_audio_p = ctypes.byref(_hot_audio_result) if audio_ready else None
    _hot_audio_tick = _hot.hot_audio_tick if audio_ready else None
    _audio_primes_total = 0
    _audio_last_prime_log = 0.0
    _audio_peak_window_max = 0.0
    _audio_rise_window_max = 0.0
    _audio_ring_mag_max = 0.0
    _audio_ring_ratio_max = 0.0
    _audio_frames_window = 0
    _audio_ticks_window = 0
    _audio_primes_window = 0
    _audio_peakrise_ok_window = 0
    _audio_all4_ok_window = 0
    _audio_ringratio_ok_window = 0
    _audio_window_start = 0.0
    _audio_stats_interval = 3.0  # seconds between heartbeat logs
    # Ring buffer of recent DXGI tick summaries so that on fire we can
    # dump what the pre-flash frames looked like (catches whether the
    # flash ramps or saturates instantly).
    _visual_ring = [None] * 10
    _visual_ring_idx = 0

    try:
        while True:
            conn = None
            already_dim = 1 if _mag_profile_active else 0
            if hot_ready:
                rc = _hot_tick(_prev_flashed, already_dim, _hot_tick_p)
                if rc < 0:
                    log(f"hot_tick rc={rc} (wsa err)")
                elif _hot_tick_result.had_connection:
                    r = _hot_tick_result
                    fired = bool(r.fired)
                    flashed_val = int(r.flashed_value)
                    gqcs_us = r.t_gqcs_ns / 1000.0
                    scan_us = r.t_scan_ns / 1000.0
                    magset_us = r.t_magset_ns / 1000.0
                    reply_us = r.t_reply_ns / 1000.0
                    total_us = r.t_total_ns / 1000.0

                    now = time.time()
                    with state.lock:
                        state.flashed = flashed_val
                        state.last_gsi_ts = now
                        state.last_gsi_perf = perf_counter()
                        state.gsi_hits += 1
                        if flashed_val > state.peak_flash_since:
                            state.peak_flash_since = flashed_val
                        hit_no = state.gsi_hits

                    if fired:
                        _mag_profile_start = perf_counter() - (r.t_reply_ns / 1_000_000_000.0)
                        _mag_profile_active = True
                        _mag_profile_audio_only = False
                        _profile_saw_gsi = False
                        _last_applied = _min_brightness
                        _last_scan_us = scan_us
                        _last_magset_us = magset_us
                        # C-side already applied gamma dark before MagSet.
                        # Schedule revert to identity after 2 vsyncs so the
                        # fade loop doesn't double-dim through gamma.
                        if gamma_ready:
                            _gamma_revert_deadline = perf_counter() + 0.034
                        log(f"flash trigger: GSI flashed={flashed_val} -> "
                            f"native tick (gqcs={gqcs_us:.1f}us scan={scan_us:.1f}us "
                            f"magset={magset_us:.1f}us reply={reply_us:.1f}us "
                            f"total={total_us:.1f}us)")
                    elif flashed_val > 0 and _prev_flashed == 0 and already_dim:
                        log(f"GSI flashed={flashed_val} suppressed "
                            f"(already dimmed, likely from visual trigger)")
                    _prev_flashed = flashed_val
                    if hit_no <= 5:
                        log(f"GSI hit #{hit_no} (native): flashed={flashed_val} "
                            f"bytes={r.bytes_read} total={total_us:.1f}us")
                    elif hit_no % 50 == 0:
                        log(f"GSI hit #{hit_no} (native): flashed={flashed_val} (heartbeat)")

            # WASAPI loopback audio tick. Runs every loop iteration to
            # keep the ring buffer drained and detect transients ASAP.
            # Only sets the "primed" flag read by the visual scan; does
            # NOT fire MagSet on its own.
            if audio_ready:
                rc_a = _hot_audio_tick(_hot_audio_p)
                # Accumulate audio stats for periodic heartbeat so we can
                # see WHY priming fails when it does (peak too low? rise
                # too low? no frames at all?).
                if rc_a == 0:
                    _audio_ticks_window += 1
                    _apk = float(_hot_audio_result.peak)
                    _ari = float(_hot_audio_result.rise)
                    _arm = float(_hot_audio_result.ring_mag)
                    _arr = float(_hot_audio_result.ring_ratio)
                    _afr = int(_hot_audio_result.frames_read)
                    if _apk > _audio_peak_window_max:
                        _audio_peak_window_max = _apk
                    if _ari > _audio_rise_window_max:
                        _audio_rise_window_max = _ari
                    if _arm > _audio_ring_mag_max:
                        _audio_ring_mag_max = _arm
                    if _arr > _audio_ring_ratio_max:
                        _audio_ring_ratio_max = _arr
                    if int(_hot_audio_result.primed):
                        _audio_primes_window += 1
                    _pr_ok = (_apk >= g_audio_peak and _ari >= g_audio_rise)
                    _rr_ok = (_arm >= g_audio_ring_min and _arr >= g_audio_ring_ratio)
                    if _pr_ok:
                        _audio_peakrise_ok_window += 1
                    if _rr_ok:
                        _audio_ringratio_ok_window += 1
                    if _pr_ok and _rr_ok:
                        _audio_all4_ok_window += 1
                    _audio_frames_window += _afr
                    _now_heartbeat = perf_counter()
                    if _audio_window_start == 0.0:
                        _audio_window_start = _now_heartbeat
                    elif _now_heartbeat - _audio_window_start >= _audio_stats_interval:
                        log(f"AUDIO heartbeat: "
                            f"max_peak={_audio_peak_window_max:.3f} "
                            f"max_rise={_audio_rise_window_max:.3f} "
                            f"max_ring={_audio_ring_mag_max:.4f} "
                            f"max_ratio={_audio_ring_ratio_max:.3f} "
                            f"frames={_audio_frames_window} "
                            f"ticks={_audio_ticks_window} "
                            f"peakrise_ok={_audio_peakrise_ok_window} "
                            f"ringratio_ok={_audio_ringratio_ok_window} "
                            f"all4_ok={_audio_all4_ok_window} "
                            f"primes={_audio_primes_window} "
                            f"mag_active={_mag_profile_active} "
                            f"over {_now_heartbeat - _audio_window_start:.1f}s "
                            f"(thresholds: peak>={g_audio_peak:.2f} rise>={g_audio_rise:.2f})")
                        _audio_peak_window_max = 0.0
                        _audio_rise_window_max = 0.0
                        _audio_ring_mag_max = 0.0
                        _audio_ring_ratio_max = 0.0
                        _audio_primes_window = 0
                        _audio_peakrise_ok_window = 0
                        _audio_ringratio_ok_window = 0
                        _audio_all4_ok_window = 0
                        _audio_frames_window = 0
                        _audio_ticks_window = 0
                        _audio_window_start = _now_heartbeat
                # Audio-fires-MagSet (proactive): when the C-side primes
                # (peak+rise gate crossed), fire dim immediately ahead of
                # the visual flash. Gated on recent GSI POST so random
                # browser/music audio can't trigger it when CS2 isn't running.
                if (audio_fire_enabled and rc_a == 0
                        and _hot_audio_result.primed
                        and not _mag_profile_active
                        and (perf_counter() - _audio_last_prime_log) >= 0.8):
                    with state.lock:
                        _gsi_age = perf_counter() - state.last_gsi_perf
                    # Tone gate (ring_ratio >= 0.18) already proves CS2-flashbang
                    # specificity. Don't also require fresh GSI — CS2's GSI
                    # integration can drop out across sessions while audio keeps
                    # flowing, which would silently disable the whole feature.
                    if True:
                        # Fire dark: gamma first (scanout path), then MagSet.
                        if gamma_ready:
                            _hot.hot_gamma_apply_dark()
                            _gamma_revert_deadline = perf_counter() + 0.034
                        mag_set(dark_p)
                        _mag_profile_start = perf_counter()
                        _mag_profile_active = True
                        _mag_profile_audio_only = True
                        _profile_saw_gsi = False
                        _last_applied = _min_brightness
                        _audio_primes_total += 1
                        _audio_last_prime_log = perf_counter()
                        with state.lock:
                            state.flashed = 255
                            state.last_gsi_ts = time.time()
                            if state.peak_flash_since < 255:
                                state.peak_flash_since = 255
                        log(f"flash trigger: AUDIO PROACTIVE "
                            f"peak={_hot_audio_result.peak:.3f} "
                            f"rise={_hot_audio_result.rise:.3f} "
                            f"ring={_hot_audio_result.ring_mag:.4f} "
                            f"ratio={_hot_audio_result.ring_ratio:.3f} "
                            f"gsi_age={_gsi_age*1000:.0f}ms")

            # DXGI Desktop Duplication: fires MagSet ~15-70ms ahead of GSI.
            # Interleaves with hot_tick polling; new frame every ~monitor-refresh.
            if visual_ready:
                rc_v = _hot_visual_tick(already_dim, _hot_visual_p)
                if rc_v == -2:
                    log("DXGI access lost -- duplication recreated")
                elif rc_v < 0 and rc_v != -1:
                    log(f"hot_visual_tick rc={rc_v}")
                elif _hot_visual_result.had_frame:
                    v = _hot_visual_result
                    _visual_frames_seen += 1
                    _visual_last_bright = int(v.bright_count)
                    # Record into ring unless we just fired (we'll dump
                    # the ring and keep it intact for post-fire context).
                    if not v.fired:
                        _visual_ring[_visual_ring_idx] = (
                            perf_counter(), int(v.bright_count),
                            int(v.relaxed_count), int(v.delta_count),
                            int(v.max_luma), int(v.audio_primed),
                            int(v.prev_relaxed))
                        _visual_ring_idx = (_visual_ring_idx + 1) % len(_visual_ring)
                    if v.fired:
                        acq_us = v.t_acquire_ns / 1000.0
                        cpy_us = v.t_copy_ns / 1000.0
                        scn_us = v.t_scan_ns / 1000.0
                        mag_us = v.t_magset_ns / 1000.0
                        tot_us = v.t_total_ns / 1000.0
                        _mag_profile_start = perf_counter()
                        _mag_profile_active = True
                        _mag_profile_audio_only = False
                        _profile_saw_gsi = False
                        _last_applied = _min_brightness
                        _last_scan_us = scn_us
                        _last_magset_us = mag_us
                        # C-side already applied gamma dark before MagSet.
                        # Schedule revert to identity after 2 vsyncs so the
                        # fade loop doesn't double-dim through gamma.
                        if gamma_ready:
                            _gamma_revert_deadline = perf_counter() + 0.034
                        now = time.time()
                        with state.lock:
                            state.flashed = 255
                            state.last_gsi_ts = now
                            state.last_gsi_perf = perf_counter()
                            if state.peak_flash_since < 255:
                                state.peak_flash_since = 255
                        _TK = {1: "PEAK", 2: "RISE", 3: "DELTA", 4: "PRIMED", 5: "RAMP"}
                        kind = _TK.get(v.trigger_kind, f"?{v.trigger_kind}")
                        log(f"flash trigger: VISUAL {kind} "
                            f"bright={v.bright_count}/9 relaxed={v.relaxed_count}/9 "
                            f"delta={v.delta_count}/9 prev_rel={v.prev_relaxed} "
                            f"max_lum={v.max_luma}/765 ramp_rise={v.ramp_rise} "
                            f"audio_primed={v.audio_primed} "
                            f"-> dxgi tick (acquire={acq_us:.1f}us copy={cpy_us:.1f}us "
                            f"scan={scn_us:.1f}us magset={mag_us:.1f}us "
                            f"total={tot_us:.1f}us frame#{v.frame_num})")
                        # Dump the last 10 pre-fire DXGI ticks so we can see
                        # whether the flash ramped or saturated instantly.
                        now_pc = perf_counter()
                        hist = []
                        for i in range(len(_visual_ring)):
                            slot = _visual_ring[(_visual_ring_idx + i) % len(_visual_ring)]
                            if slot is None:
                                continue
                            t_p, b, r, d, ml, ap, pr = slot
                            hist.append(f"t-{(now_pc-t_p)*1000:6.1f}ms "
                                        f"b={b} r={r} d={d} ml={ml} pr={pr} ap={ap}")
                        log("  pre-flash ring (oldest first): " + " | ".join(hist))
                    elif _visual_frames_seen <= 3:
                        log(f"DXGI frame #{_visual_frames_seen}: "
                            f"bright={v.bright_count}/9 relaxed={v.relaxed_count}/9 "
                            f"delta={v.delta_count}/9 max_lum={v.max_luma} "
                            f"acquire={v.t_acquire_ns/1000:.1f}us "
                            f"copy={v.t_copy_ns/1000:.1f}us "
                            f"scan={v.t_scan_ns/1000:.1f}us "
                            f"total={v.t_total_ns/1000:.1f}us")
            else:
                # Python fallback path
                try:
                    conn, _ = srv_accept()
                except BlockingIOError:
                    conn = None
                except OSError as e:
                    log(f"accept error: {e!r}")
                    conn = None

                if conn is not None:
                    t_conn_start = perf_counter()
                    n = 0
                    pre_recv_t = perf_counter()
                    while True:
                        try:
                            n = conn.recv_into(rmv)
                            break
                        except BlockingIOError:
                            pass
                    t_first_chunk = perf_counter()

                    snap_perf = 0.0
                    scan_us = 0.0
                    magset_us = 0.0
                    if n > 0:
                        t_scan_start = perf_counter()
                        idx = FIND(find_flashed, 0, n)
                        if idx >= 0:
                            vs = idx + 10
                            while vs < n and rbuf[vs] in (32, 9, 10, 13):
                                vs += 1
                            if vs < n and 49 <= rbuf[vs] <= 57 and _prev_flashed == 0:
                                t_pre = perf_counter()
                                mag_set(dark_p)
                                t_post = perf_counter()
                                scan_us = (t_pre - t_scan_start) * 1_000_000.0
                                magset_us = (t_post - t_pre) * 1_000_000.0
                                snap_perf = t_post
                                _mag_profile_start = t_post
                                _mag_profile_active = True
                                _mag_profile_audio_only = False
                                _profile_saw_gsi = False
                                _last_applied = _min_brightness
                                _last_scan_us = scan_us
                                _last_magset_us = magset_us

                        try:
                            finish_gsi_conn(conn, rbuf, n, t_conn_start,
                                            snap_perf, t_first_chunk, pre_recv_t,
                                            scan_us, magset_us)
                        except Exception as e:
                            log(f"finish error: {e!r}")
                    else:
                        try:
                            conn.close()
                        except Exception:
                            pass

            # fade tick: only fire when the tick interval has elapsed, so
            # we don't flood DWM with redundant MagSets while busy-polling
            t_tick = time.perf_counter()
            if (t_tick - last_tick_perf) < tick_s:
                continue
            last_tick_perf = t_tick

            with state.lock:
                enabled = state.enabled
                eyelids = state.eyelids
                gsi_flashed = state.flashed
                last_ts = state.last_gsi_ts
                selftest_val = state.self_test_flash
                hits = state.gsi_hits

            age = (time.time() - last_ts) if last_ts else 0.0
            effective_flash = 0 if age > 2.0 else gsi_flashed

            # Synth profile guarantees visibility for accessibility (real CS2
            # often reports `flashed=1` at trigger time, which by itself is
            # nearly invisible). GSI is used to END the profile early for
            # wide-angle flashes that resolve in well under the synth total.
            synth_flash = 0
            gsi_fresh = (age < 0.5 and gsi_flashed > 0)
            if gsi_fresh and _mag_profile_active:
                _profile_saw_gsi = True

            if synth_profile and _mag_profile_active:
                elapsed = t_tick - _mag_profile_start
                # Audio-only fires use a short bridge; if GSI/visual later
                # confirms it's a real flash, they extend via full profile.
                blind = audio_blind_s if _mag_profile_audio_only else full_blind_s
                fade  = audio_fade_s  if _mag_profile_audio_only else fade_s
                if elapsed < blind:
                    synth_flash = 255
                elif elapsed < blind + fade:
                    synth_flash = int(255 * (1.0 - (elapsed - blind) / fade))
                else:
                    synth_flash = 0
                    _mag_profile_active = False
                    _mag_profile_audio_only = False
                    _profile_saw_gsi = False

            # Angle-correct shortening: once GSI has confirmed the flash
            # and then reports 0 for the CS2-configured grace window, the
            # flash is over — kill the synth early. 0.3s grace survives
            # a single dropped GSI packet while still reliably catching
            # the 0.95s wide-angle decay.
            if _mag_profile_active and _profile_saw_gsi:
                elapsed = t_tick - _mag_profile_start
                if effective_flash <= 0 and elapsed > 0.3:
                    _mag_profile_active = False
                    _mag_profile_audio_only = False
                    _profile_saw_gsi = False
                    synth_flash = 0

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

            brightness = 1.0 - alpha

            if abs(brightness - _last_applied) > 0.003:
                if hot_ready:
                    _hot_fade(ctypes.c_float(brightness))
                else:
                    _EFFECT_FADE.transform[0] = brightness
                    _EFFECT_FADE.transform[6] = brightness
                    _EFFECT_FADE.transform[12] = brightness
                    _mag.MagSetFullscreenColorEffect(_fade_p)
                _last_applied = brightness

            # After the flash onset, once MagSet has had time to land (~2
            # vsyncs), revert gamma to identity so the fade curve isn't
            # double-applied (gamma * MagSet would square the brightness).
            # Gamma's job is only to win the first frame.
            if gamma_ready and _gamma_revert_deadline > 0.0 and \
               t_tick >= _gamma_revert_deadline:
                _hot.hot_gamma_apply_identity()
                _gamma_revert_deadline = 0.0

            if (drive_flash >= 8 and abs(drive_flash - last_logged_flash) >= 16) or \
               (drive_flash == 0 and last_logged_flash >= 8):
                log(f"drive={drive_flash} brightness={brightness:.3f} "
                    f"enabled={enabled} eyelids={eyelids} hits={hits}")
                last_logged_flash = drive_flash

    except KeyboardInterrupt:
        log("KeyboardInterrupt in unified loop")
    finally:
        # Restore gamma FIRST (scanout path) so the monitor stops dimming
        # immediately even if MagSet cleanup hangs.
        if gamma_ready:
            try:
                _hot.hot_gamma_apply_identity()
            except Exception:
                pass
        try:
            if hot_ready:
                _hot.hot_apply_identity()
            else:
                _mag.MagSetFullscreenColorEffect(_identity_p)
        except Exception:
            pass
        try:
            _mag.MagUninitialize()
        except Exception:
            pass
        log("mag cleanup complete")


def check_single_instance(port):
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
    log("=== startup (mag-inline busy-poll) ===")
    log(f"script_dir={SCRIPT_DIR}")
    settings = load_settings()
    host = settings.get("ListenHost", "127.0.0.1")
    port = int(settings.get("ListenPort", "3000"))

    if not check_single_instance(port):
        return

    t_hot = threading.Thread(target=hotkey_watcher, args=(settings,), daemon=True)
    t_hot.start()

    run_unified_loop(settings, host, port)


if __name__ == "__main__":
    main()
