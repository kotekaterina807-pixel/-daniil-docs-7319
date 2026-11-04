"""Локальный HTTP-прокси поверх SOCKS-прокси программы.
Claude Code умеет ходить только через HTTP(S)-прокси, а в программе часто указан SOCKS (порт VPN-приложения).
Мост принимает CONNECT на 127.0.0.1 и пересылает соединение через SOCKS. Работает, пока открыта программа."""
import select
import socket
import socketserver
import threading
from urllib.parse import urlparse

_lock = threading.Lock()
_bridge = {"key": None, "port": None, "srv": None}


def _recvn(s, n):
    b = b""
    while len(b) < n:
        d = s.recv(n - len(b))
        if not d:
            raise OSError("SOCKS: соединение закрыто")
        b += d
    return b


def _socks_conn(proxy, host, port):
    """Соединение через SOCKS5 (имя сайта резолвит прокси; логин/пароль поддерживаются)."""
    import struct
    u = urlparse(proxy)
    s = socket.create_connection((u.hostname, u.port or 1080), timeout=30)
    user, pwd = u.username or "", u.password or ""
    s.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
    ver, method = _recvn(s, 2)
    if method == 2:
        from urllib.parse import unquote
        ub, pb = unquote(user).encode(), unquote(pwd).encode()
        s.sendall(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
        if _recvn(s, 2)[1] != 0:
            raise OSError("SOCKS: неверный логин или пароль прокси")
    elif method != 0:
        raise OSError("SOCKS: прокси не принял способ входа")
    hb = host.encode("idna")
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port))
    rep = _recvn(s, 4)
    if rep[1] != 0:
        raise OSError(f"SOCKS: ошибка {rep[1]}")
    atyp = rep[3]
    _recvn(s, 4 if atyp == 1 else 16 if atyp == 4 else _recvn(s, 1)[0])
    _recvn(s, 2)
    s.settimeout(None)
    return s


def _pipe(a, b):
    socks_ = [a, b]
    try:
        while True:
            r, _, x = select.select(socks_, [], socks_, 300)
            if x or not r:
                return
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


def _handler(proxy):
    class H(socketserver.BaseRequestHandler):
        def handle(self):
            c = self.request
            c.settimeout(30)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                chunk = c.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            line = head.split(b"\r\n")[0].decode("latin-1", "replace").split()
            if len(line) < 2 or line[0].upper() != "CONNECT":
                c.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                return
            host, _, port = line[1].rpartition(":")
            try:
                up = _socks_conn(proxy, host.strip("[]"), int(port or 443))
            except Exception:
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                up.sendall(rest)
            c.settimeout(None)
            try:
                _pipe(c, up)
            finally:
                up.close()
    return H


class _Srv(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def http_proxy_for(proxy):
    """Для SOCKS-прокси возвращает адрес локального HTTP-моста (запускает его при необходимости).
    Для HTTP(S)-прокси — его самого. Пусто — пусто."""
    proxy = (proxy or "").strip()
    if not proxy or not proxy.lower().startswith("socks"):
        return proxy
    with _lock:
        if _bridge["key"] == proxy and _bridge["srv"]:
            return f"http://127.0.0.1:{_bridge['port']}"
        if _bridge["srv"]:
            try:
                _bridge["srv"].shutdown()
                _bridge["srv"].server_close()
            except Exception:
                pass
        srv = _Srv(("127.0.0.1", 0), _handler(proxy))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _bridge.update(key=proxy, port=srv.server_address[1], srv=srv)
        return f"http://127.0.0.1:{_bridge['port']}"
