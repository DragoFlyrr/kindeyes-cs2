"""Benchmark the GSI hot path: localhost TCP POST → snap → reply.
Fires N POSTs and reports client-observed round-trip. The log's
`snap_at` value is our in-process measurement and is authoritative.

Run the overlay first, then this. Second and later hits won't be
rising-edge (prev_flashed!=0) so we alternate flashed=1/0 posts.
"""
import socket
import time

HOST = "127.0.0.1"
PORT = 3000

POST_FLASH = (
    b"POST / HTTP/1.1\r\n"
    b"Host: 127.0.0.1:3000\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 124\r\n"
    b"\r\n"
    b'{"provider":{"name":"Counter-Strike: Global Offensive"},'
    b'"player":{"activity":"playing","state":{"flashed":255,"health":100}}}'
)

POST_CLEAR = (
    b"POST / HTTP/1.1\r\n"
    b"Host: 127.0.0.1:3000\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 122\r\n"
    b"\r\n"
    b'{"provider":{"name":"Counter-Strike: Global Offensive"},'
    b'"player":{"activity":"playing","state":{"flashed":0,"health":100}}}'
)

def one_shot(payload):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((HOST, PORT))
    t0 = time.perf_counter_ns()
    s.sendall(payload)
    # wait for response
    r = b""
    while b"\r\n\r\n" not in r:
        chunk = s.recv(4096)
        if not chunk:
            break
        r += chunk
    t1 = time.perf_counter_ns()
    s.close()
    return (t1 - t0) / 1e6  # ms

def main():
    # warmup
    for _ in range(5):
        one_shot(POST_CLEAR)
    # alternate flash/clear so each flash is a rising edge
    rtts = []
    for i in range(30):
        one_shot(POST_CLEAR)
        time.sleep(0.05)
        rtts.append(one_shot(POST_FLASH))
        time.sleep(0.05)
    rtts.sort()
    p50 = rtts[len(rtts)//2]
    p90 = rtts[int(len(rtts)*0.9)]
    p99 = rtts[int(len(rtts)*0.99)]
    print(f"n={len(rtts)} rising-edge POSTs, client-observed RTT:")
    print(f"  min  = {rtts[0]:.3f} ms")
    print(f"  p50  = {p50:.3f} ms")
    print(f"  p90  = {p90:.3f} ms")
    print(f"  p99  = {p99:.3f} ms")
    print(f"  max  = {rtts[-1]:.3f} ms")
    print(f"  mean = {sum(rtts)/len(rtts):.3f} ms")
    print()
    print("NOTE: RTT includes snap + body read + reply + close.")
    print("Check gsi_flashdim.log `snap_at=` values for authoritative in-process snap latency.")

if __name__ == "__main__":
    main()
