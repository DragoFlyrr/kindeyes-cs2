"""Dump full GSI body to stdout, exit after 3 bodies or 90s."""
import socket, time, sys

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', 3000))
s.listen(8)
print("listening on :3000 — waiting for CS2 POSTs")
sys.stdout.flush()

t_start = time.time()
n = 0
while True:
    c, _ = s.accept()
    c.settimeout(1.0)
    buf = b''
    try:
        while b'\r\n\r\n' not in buf:
            r = c.recv(4096)
            if not r: break
            buf += r
        try:
            while True:
                r = c.recv(4096)
                if not r: break
                buf += r
                if len(buf) > 16384: break
        except socket.timeout:
            pass
    except Exception:
        pass
    c.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
    c.close()
    n += 1
    body_start = buf.find(b'\r\n\r\n')
    body = buf[body_start+4:] if body_start > 0 else buf
    elapsed = time.time() - t_start
    print(f"=== BODY #{n} elapsed={elapsed:.1f}s len={len(body)} ===")
    print(body.decode('utf-8', errors='replace'))
    print("=== END ===")
    sys.stdout.flush()
    if n >= 3 or elapsed > 90:
        break
s.close()
