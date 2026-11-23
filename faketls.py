# -*- coding: utf-8 -*-
"""fake-TLS(ee) 客户端隧道。

背景：Telegram 的 MTProto Proxy 有三种 secret 形态——dd / plain16 / ee。
前两种是裸 TCP 上的 obfuscated2，Telethon 原生支持；ee 是「先握手一次假
TLS，然后把 obfuscated2 的字节包进 TLS ApplicationData 记录里」，Telethon
的 tcpmtproxy.py 没有这一层，所以 ee 节点它连不上。

本模块补的就是这一层，做法是本地起一个透明中转：

    Telethon --(裸 TCP, obfuscated2)--> 本机中转 --(fake-TLS 记录)--> ee 节点

中转只做两件事：把上游/下游的字节包成 / 剥掉 TLS 记录头，其余原样透传。
obfuscated2 仍然是端到端在 Telethon 与真正的 MTProxy 之间完成的，中转不碰
它的密钥，所以既不需要改 Telethon，也不需要知道 MTProto 的任何内容。

协议细节按 mtg（9seconds/mtg）的 Go 实现：
  - ClientHello 的 random（32 字节）里塞签名：
      整帧 random 置零 -> HMAC-SHA256(secret16, 整帧) -> 异或进 random
      前 28 字节必须全零，第 28~32 字节放小端时间戳
  - 上游回 ServerHello + ChangeCipherSpec + 恰好一条 ApplicationData，
    并在回包第 11~43 字节放 HMAC-SHA256(secret16, ClientHello.random + 整段回包)
  - 之后双向都只是 ApplicationData 记录（不做真加密）
"""

import base64
import binascii
import hashlib
import hmac
import os
import socket
import struct
import threading
import time

TLS_VERSION = b"\x03\x03"
TYPE_HANDSHAKE = 0x16
TYPE_CHANGE_CIPHER_SPEC = 0x14
TYPE_APPLICATION_DATA = 0x17
MAX_RECORD = 16384
RANDOM_OFFSET = 11          # record(5) + handshake_type(1) + handshake_len(3) + version(2)
DEFAULT_SNI = "www.cloudflare.com"
HANDSHAKE_TIMEOUT = 10.0


# ---------------------------------------------------------------- secret 解析

def secret_bytes(raw):
    """把各种写法的 secret 归一成 16 字节。

    支持：ee + 32 hex + hex(域名)、dd + 32 hex、纯 32 hex、base64。
    """
    s = (raw or "").strip()
    low = s.lower()
    if low.startswith("ee") or low.startswith("dd"):
        s = s[2:]
    if len(s) < 32:
        raise ValueError("secret 太短：%r" % (raw,))
    head = s[:32]
    try:
        return bytes.fromhex(head)
    except ValueError:
        pad = "=" * (-len(s) % 4)
        return base64.b64decode(s + pad)[:16]


def secret_sni(raw):
    """ee secret 里如果拼了域名（hex），取出来当 SNI；没有就返回空串。"""
    s = (raw or "").strip()
    low = s.lower()
    if not low.startswith("ee"):
        return ""
    tail = low[34:]
    if len(tail) < 2 or len(tail) % 2:
        return ""
    try:
        dom = binascii.unhexlify(tail).decode("ascii")
    except Exception:
        return ""
    return dom if dom and all(32 < ord(c) < 127 for c in dom) else ""


# ---------------------------------------------------------------- 报文构造

def _sni_extension(sni):
    name = sni.encode("ascii")
    # ext: type(2) len(2) | list_len(2) name_type(1) name_len(2) name
    list_len = 1 + 2 + len(name)
    body = struct.pack(">H", list_len) + b"\x00" + struct.pack(">H", len(name)) + name
    return struct.pack(">HH", 0x0000, len(body)) + body


def build_client_hello(sec, sni, ts=None):
    """构造带 mtg 签名的 ClientHello，返回完整字节。"""
    ts = int(time.time()) if ts is None else int(ts)
    sid = os.urandom(32)
    ciphers = (0xC02B, 0xC02F, 0xC02C, 0xC030, 0xCCA9, 0xCCA8,
               0xC013, 0xC014, 0x009C, 0x009D, 0x002F, 0x0035)
    cs = b"".join(struct.pack(">H", c) for c in ciphers)
    ext = _sni_extension(sni)
    body = (TLS_VERSION + bytes(32) + bytes([len(sid)]) + sid +
            struct.pack(">H", len(cs)) + cs + b"\x01\x00" +
            struct.pack(">H", len(ext)) + ext)
    handshake = b"\x01" + struct.pack(">I", len(body))[1:] + body
    record = TLS_VERSION.join((b"\x16", b"")) if False else (
        bytes([TYPE_HANDSHAKE]) + b"\x03\x01" + struct.pack(">H", len(handshake)) + handshake)
    zeroed = record[:RANDOM_OFFSET] + bytes(32) + record[RANDOM_OFFSET + 32:]
    digest = hmac.new(sec, zeroed, hashlib.sha256).digest()
    random = bytearray(digest)
    random[28:32] = struct.pack("<I", ts ^ struct.unpack("<I", digest[28:32])[0])
    return record[:RANDOM_OFFSET] + bytes(random) + record[RANDOM_OFFSET + 32:]


def client_random(hello):
    return hello[RANDOM_OFFSET:RANDOM_OFFSET + 32]


def verify_server_hello(sec, hello, packet):
    """按 mtg 的 server_side 契约校验上游回的 ServerHello 段。

    真 MTProxy 会把 HMAC 放在 packet[RANDOM_OFFSET:RANDOM_OFFSET+32]；
    普通 HTTPS 站点这里放的是真 random，所以一定验不过——这正是用来
    区分「真 ee 节点」和「被当成代理链接的普通网站」的判据。
    """
    if len(packet) < RANDOM_OFFSET + 32:
        return False
    got = packet[RANDOM_OFFSET:RANDOM_OFFSET + 32]
    zeroed = bytearray(packet)
    zeroed[RANDOM_OFFSET:RANDOM_OFFSET + 32] = bytes(32)
    want = hmac.new(sec, client_random(hello) + bytes(zeroed), hashlib.sha256).digest()
    return hmac.compare_digest(got, want)


def wrap_record(payload):
    """把一段裸字节包成一条或多条 ApplicationData 记录。"""
    out = []
    for i in range(0, len(payload), MAX_RECORD):
        chunk = payload[i:i + MAX_RECORD]
        out.append(bytes([TYPE_APPLICATION_DATA]) + TLS_VERSION +
                   struct.pack(">H", len(chunk)) + chunk)
    return b"".join(out)


# ---------------------------------------------------------------- socket 工具

def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("连接被对端关闭（已读 %d/%d 字节）" % (len(buf), n))
        buf += chunk
    return buf


def read_record(sock):
    """读一条完整 TLS 记录，返回 (类型, 负载)。"""
    head = _read_exact(sock, 5)
    length = struct.unpack(">H", head[3:5])[0]
    if length > MAX_RECORD + 1024:
        raise ValueError("记录过长：%d" % length)
    return head[0], _read_exact(sock, length)


def handshake(sock, sec, sni, timeout=HANDSHAKE_TIMEOUT):
    """在已连通的 socket 上完成 fake-TLS 握手，成功后 socket 处于可透传状态。

    返回 True 表示握手成功且 HMAC 通过；返回 False 表示对端不是真 ee 节点。
    """
    sock.settimeout(timeout)
    hello = build_client_hello(sec, sni)
    sock.sendall(hello)

    packet = b""
    need = 3           # ServerHello + CCS + 1 条 ApplicationData
    saw_ccs = False
    while need:
        typ, payload = read_record(sock)
        packet += bytes([typ]) + TLS_VERSION + struct.pack(">H", len(payload)) + payload
        if typ == TYPE_HANDSHAKE:
            need -= 1
        elif typ == TYPE_CHANGE_CIPHER_SPEC and not saw_ccs:
            saw_ccs = True
            need -= 1
        elif typ == TYPE_APPLICATION_DATA:
            need -= 1
        else:
            raise ValueError("握手阶段收到意外记录类型 0x%02x" % typ)
    ok = verify_server_hello(sec, hello, packet)
    sock.settimeout(None)
    return ok


# ---------------------------------------------------------------- 双向泵

def _pump_upstream_to_client(upstream, client, stop):
    """上游 TLS 记录 → 剥头 → 写给 Telethon。"""
    try:
        while not stop.is_set():
            typ, payload = read_record(upstream)
            if typ != TYPE_APPLICATION_DATA:
                continue
            if payload:
                client.sendall(payload)
    except Exception:
        pass
    finally:
        stop.set()
        for s in (upstream, client):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


def _pump_client_to_upstream(client, upstream, stop):
    """Telethon 裸字节 → 包成 ApplicationData 记录 → 写给上游。"""
    try:
        while not stop.is_set():
            data = client.recv(MAX_RECORD)
            if not data:
                break
            upstream.sendall(wrap_record(data))
    except Exception:
        pass
    finally:
        stop.set()
        for s in (upstream, client):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


# ---------------------------------------------------------------- 隧道服务

class Tunnel(object):
    """一个 ee 上游对应的本地中转。Telethon 连 127.0.0.1:<port> 即可。"""

    def __init__(self, host, port, secret, sni=None):
        self.host = host
        self.port = int(port)
        self.secret = secret
        self.sec = secret_bytes(secret)
        self.sni = sni or secret_sni(secret) or DEFAULT_SNI
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.local_port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = None
        self.handshaked = 0
        self.rejected = 0
        self.last_error = ""

    @property
    def label(self):
        return "ee %s:%s (本地 %d)" % (self.host, self.port, self.local_port)

    def start(self):
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        while not self._stop.is_set():
            try:
                client, _ = self.sock.accept()
            except Exception:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        upstream = None
        try:
            upstream = socket.create_connection((self.host, self.port), timeout=HANDSHAKE_TIMEOUT)
            if not handshake(upstream, self.sec, self.sni):
                self.rejected += 1
                self.last_error = "上游回的不是真 ee 节点（ServerHello 的 HMAC 验不过）"
                raise ValueError(self.last_error)
            self.handshaked += 1
            stop = threading.Event()
            t = threading.Thread(target=_pump_upstream_to_client,
                                 args=(upstream, client, stop), daemon=True)
            t.start()
            _pump_client_to_upstream(client, upstream, stop)
            t.join(timeout=5)
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            for s in (client, upstream):
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass


_TUNNELS = {}
_TUNNEL_LOCK = threading.Lock()


def ensure_tunnel(host, port, secret, sni=None):
    """按 (host, port) 复用隧道，返回 Tunnel。同一上游只开一个。"""
    key = (host, int(port))
    with _TUNNEL_LOCK:
        t = _TUNNELS.get(key)
        if t is None or t._stop.is_set():
            t = Tunnel(host, port, secret, sni).start()
            _TUNNELS[key] = t
        return t


def close_all():
    with _TUNNEL_LOCK:
        for t in list(_TUNNELS.values()):
            t.close()
        _TUNNELS.clear()


# ---------------------------------------------------------------- 自测用的「假上游」

class MockUpstream(object):
    """按 mtg 的 server_side 逻辑实现一个本地 ee 节点，只用于自测。

    它不连 Telegram：握手完成后把收到的字节原样回送（echo），
    用来验证「包记录 / 剥记录 / HMAC 双向」这三件事。
    """

    def __init__(self, secret, echo=True):
        self.sec = secret_bytes(secret)
        self.echo = echo
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except Exception:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            typ, payload = read_record(conn)
            if typ != TYPE_HANDSHAKE:
                return
            hello = bytes([typ]) + TLS_VERSION + struct.pack(">H", len(payload)) + payload
            server_hello = self._make_server_hello(hello)
            ccs = bytes([TYPE_CHANGE_CIPHER_SPEC]) + TLS_VERSION + struct.pack(">H", 1) + b"\x01"
            noise = os.urandom(2800)
            appdata = bytes([TYPE_APPLICATION_DATA]) + TLS_VERSION + struct.pack(">H", len(noise)) + noise
            packet = server_hello + ccs + appdata
            digest = hmac.new(self.sec, client_random(hello) + packet, hashlib.sha256).digest()
            packet = (packet[:RANDOM_OFFSET] + digest + packet[RANDOM_OFFSET + 32:])
            conn.sendall(packet)
            if not self.echo:
                return
            while True:
                typ, payload = read_record(conn)
                if typ != TYPE_APPLICATION_DATA:
                    continue
                if payload:
                    conn.sendall(wrap_record(payload))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _make_server_hello(self, hello):
        sid_len = hello[RANDOM_OFFSET + 32]
        sid = hello[RANDOM_OFFSET + 33:RANDOM_OFFSET + 33 + sid_len]
        cs_off = RANDOM_OFFSET + 33 + sid_len
        cipher = hello[cs_off:cs_off + 2]
        suffix = (b"\x00\x00\x2e" + b"\x00\x2b\x00\x02\x03\x04" +
                  b"\x00\x33\x00\x24\x00\x1d\x00\x20" + os.urandom(32))
        body = (TLS_VERSION + bytes(32) + bytes([len(sid)]) + sid + cipher + suffix)
        handshake = b"\x02" + struct.pack(">I", len(body))[1:] + body
        return bytes([TYPE_HANDSHAKE]) + TLS_VERSION + struct.pack(">H", len(handshake)) + handshake

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass


def selftest(verbose=True):
    """不依赖外网的自检：假上游 echo + 本模块隧道，验证字节双向无损。"""
    secret = "ee" + "00" * 15 + "11" + binascii.hexlify(b"www.cloudflare.com").decode()
    up = MockUpstream(secret)
    tun = Tunnel("127.0.0.1", up.port, secret).start()
    results = []
    try:
        s = socket.create_connection(("127.0.0.1", tun.local_port), timeout=5)
        payloads = [b"hello-ee" * 100, os.urandom(40000)]
        for p in payloads:
            s.sendall(p)
            got = b""
            while len(got) < len(p):
                chunk = s.recv(len(p) - len(got))
                if not chunk:
                    break
                got += chunk
            results.append((len(p), len(got), got == p))
        s.close()
        time.sleep(0.2)
        ok = all(r[2] for r in results) and tun.handshaked >= 1
        if verbose:
            print("selftest: 握手成功 %d 次、被拒 %d 次" % (tun.handshaked, tun.rejected))
            for size, got, same in results:
                print("  发送 %6d 字节 -> 收回 %6d 字节，内容一致=%s" % (size, got, same))
            print("  结论：%s" % ("PASS" if ok else "FAIL" + " / " + tun.last_error))
        return ok
    finally:
        tun.close()
        up.close()


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        raise SystemExit(0 if selftest() else 1)
    print("用法：python faketls.py --selftest")
