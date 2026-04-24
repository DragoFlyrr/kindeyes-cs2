"""Synthetic GSI feeder for testing angle-correct dim decay.

CS2 encodes angle indirectly: head-on flashes start `flashed` at 255 and
decay over ~4.87s; wide-angle flashes start lower and decay in well under
a second. This script POSTs fake GSI payloads to localhost:3000 with
precomputed decay curves so you can watch the overlay match the angle
without needing a match.

Usage:
    python test_angles.py             # runs all 4 angle buckets, 2s gap
    python test_angles.py 0 53        # only the 0-53 bucket

The buckets come from the CS2 angle table:
    0-53°    full_blind=1.88s   total=4.87s   peak=255
    53-72°   full_blind=0.45s   total=3.40s   peak=180
    72-101°  full_blind=0.08s   total=1.95s   peak=90
    101-180° full_blind=0.08s   total=0.95s   peak=40
"""
import json
import socket
import sys
import time

HOST = "127.0.0.1"
PORT = 3000

BUCKETS = [
    ("0-53",    255, 1.88, 4.87),
    ("53-72",   180, 0.45, 3.40),
    ("72-101",   90, 0.08, 1.95),
    ("101-180",  40, 0.08, 0.95),
]


def post_gsi(flashed_val):
    body = json.dumps({
        "player": {"state": {"flashed": int(flashed_val)}},
        "provider": {"name": "test_angles", "timestamp": int(time.time())},
    }).encode()
    req = (
        b"POST / HTTP/1.1\r\n"
        b"Host: 127.0.0.1:3000\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    try:
        s = socket.create_connection((HOST, PORT), timeout=0.5)
        s.sendall(req)
        try:
            s.recv(256)
        except Exception:
            pass
        s.close()
    except Exception as e:
        print(f"  post failed: {e}")


def run_bucket(label, peak, blind_s, total_s):
    print(f"\n=== {label}° angle: peak={peak} blind={blind_s}s total={total_s}s ===")
    fade_s = total_s - blind_s
    start = time.perf_counter()
    # 50 Hz feed while the flash is active
    dt = 0.02
    while True:
        t = time.perf_counter() - start
        if t < blind_s:
            val = peak
        elif t < total_s:
            # Linear decay from peak to 0 across the fade window.
            frac = (t - blind_s) / fade_s if fade_s > 0 else 1.0
            val = int(peak * max(0.0, 1.0 - frac))
        else:
            val = 0
        post_gsi(val)
        if val == 0 and t >= total_s:
            break
        time.sleep(dt)
    # Final zero so overlay clears
    post_gsi(0)
    print(f"  done @ {time.perf_counter() - start:.2f}s")


def main():
    if len(sys.argv) > 1:
        wanted = " ".join(sys.argv[1:])
        buckets = [b for b in BUCKETS if b[0] == wanted or wanted in b[0]]
        if not buckets:
            print(f"no bucket matching {wanted!r}; valid: {[b[0] for b in BUCKETS]}")
            return
    else:
        buckets = BUCKETS

    for b in buckets:
        run_bucket(*b)
        time.sleep(2.0)

    print("\nall buckets complete")


if __name__ == "__main__":
    main()
