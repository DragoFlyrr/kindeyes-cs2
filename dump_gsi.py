"""Temp: accept POSTs to :3000, log body, exit after first real flash-looking hit."""
import socket, time, sys, re

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', 3000))
s.listen(8)
print("listening on :3000 — waiting for CS2 POSTs")
sys.stdout.flush()

t_start = time.time()
n = 0
nonzero_logged = 0

while True:
    c, _ = s.accept()
    c.settimeout(1.0)
    buf = b''
    try:
        while b'\r\n\r\n' not in buf:
            r = c.recv(4096)
            if not r: break
            buf += r
        # grab a bit more body if needed
        try:
            while True:
                r = c.recv(4096)
                if not r: break
                buf += r
                if len(buf) > 16384: break
        except socket.timeout:
            pass
    except Exception as e:
        pass
    c.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
    c.close()
    n += 1
    body_start = buf.find(b'\r\n\r\n')
    body = buf[body_start+4:] if body_start > 0 else buf
    # scan for "flash" occurrences
    flash_hits = [m.start() for m in re.finditer(rb'flash', body, re.IGNORECASE)]
    elapsed = time.time() - t_start
    # log ALL hits, snippet around flash keys
    snippets = []
    for h in flash_hits:
        snippets.append(body[max(0,h-5):h+50].decode('utf-8', errors='replace'))
    print(f"[{elapsed:.1f}s] #{n} len={len(body)} flash_keys={len(flash_hits)} snippets={snippets}")
    sys.stdout.flush()
    # if we found a flash key with a nonzero digit, log full body once and keep going
    for h in flash_hits:
        chunk = body[h:h+60]
        m = re.search(rb'"flashed"\s*:\s*([0-9]+)', chunk)
        if m and int(m.group(1)) > 0 and nonzero_logged < 3:
            print("=== NONZERO FLASH BODY ===")
            print(body.decode('utf-8', errors='replace')[:3000])
            print("=== END ===")
            sys.stdout.flush()
            nonzero_logged += 1
    if elapsed > 90:
        print("timeout, exiting")
        break

s.close()
