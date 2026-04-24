"""Benchmark MagSetFullscreenColorEffect vs SetLayeredWindowAttributes.

MagSetFullscreenColorEffect applies a 5x5 color transform at the DWM
compositor level — a different injection point from layered windows.
We want to know if the API call itself is faster (floor ~1.65ms for
SetLayeredWindowAttributes on this machine).

Matrix convention (per Microsoft docs on MAGCOLOREFFECT):
    [r' g' b' a' 1] = [r g b a 1] * M
For uniform darkening by factor s:  M[0][0] = M[1][1] = M[2][2] = s,
M[3][3] = M[4][4] = 1, all else 0.

Effects the whole screen during the test — brief flashes are expected.
"""
import ctypes
from ctypes import wintypes, c_float
import time


class MAGCOLOREFFECT(ctypes.Structure):
    _fields_ = [("transform", c_float * 25)]  # row-major 5x5


try:
    mag = ctypes.windll.magnification
except OSError as e:
    print(f"magnification.dll not available: {e!r}")
    raise SystemExit(1)

mag.MagInitialize.restype = wintypes.BOOL
mag.MagUninitialize.restype = wintypes.BOOL
mag.MagSetFullscreenColorEffect.argtypes = (ctypes.POINTER(MAGCOLOREFFECT),)
mag.MagSetFullscreenColorEffect.restype = wintypes.BOOL


def make_effect(s):
    m = (c_float * 25)()
    m[0] = s       # r -> r * s
    m[6] = s       # g -> g * s
    m[12] = s      # b -> b * s
    m[18] = 1.0    # a -> a
    m[24] = 1.0    # const row
    return MAGCOLOREFFECT(transform=m)


def main():
    if not mag.MagInitialize():
        err = ctypes.get_last_error()
        print(f"MagInitialize failed, last_err={err}")
        return

    dark = make_effect(0.05)      # 95% darkening
    normal = make_effect(1.0)     # identity

    dark_p = ctypes.byref(dark)
    normal_p = ctypes.byref(normal)

    # warmup
    for _ in range(5):
        mag.MagSetFullscreenColorEffect(dark_p)
        mag.MagSetFullscreenColorEffect(normal_p)

    times_dark = []
    times_norm = []
    for _ in range(30):
        t0 = time.perf_counter_ns()
        ok1 = mag.MagSetFullscreenColorEffect(dark_p)
        t1 = time.perf_counter_ns()
        times_dark.append((t1 - t0) / 1e6)
        # restore immediately so screen isn't stuck dim if we crash
        t2 = time.perf_counter_ns()
        ok2 = mag.MagSetFullscreenColorEffect(normal_p)
        t3 = time.perf_counter_ns()
        times_norm.append((t3 - t2) / 1e6)
        if not (ok1 and ok2):
            print(f"MagSet returned 0 (dark_ok={ok1}, norm_ok={ok2}) - API call failed")
            break
        time.sleep(0.05)

    # ensure normal state before uninit
    mag.MagSetFullscreenColorEffect(normal_p)
    mag.MagUninitialize()

    def stats(xs, label):
        if not xs:
            print(f"{label}: no samples")
            return
        xs.sort()
        p50 = xs[len(xs)//2]
        p90 = xs[int(len(xs)*0.9)]
        p99 = xs[min(len(xs)-1, int(len(xs)*0.99))]
        print(f"{label}: n={len(xs)}")
        print(f"  min  = {xs[0]:.3f} ms")
        print(f"  p50  = {p50:.3f} ms")
        print(f"  p90  = {p90:.3f} ms")
        print(f"  p99  = {p99:.3f} ms")
        print(f"  max  = {xs[-1]:.3f} ms")
        print(f"  mean = {sum(xs)/len(xs):.3f} ms")

    print("=== MagSetFullscreenColorEffect(dark) ===")
    stats(times_dark, "MagSet dark")
    print()
    print("=== MagSetFullscreenColorEffect(normal) ===")
    stats(times_norm, "MagSet normal")
    print()
    print("Compare to SetLayeredWindowAttributes: p50 ~1.75ms, min ~1.65ms.")


if __name__ == "__main__":
    main()
