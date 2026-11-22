# -*- coding: utf-8 -*-
"""proxylinks.py —— 从任意文本里解析 Telegram 代理链接并规范化。

支持
  https://t.me/proxy?server=..&port=..&secret=..      MTProto 代理
  tg://proxy?server=..&port=..&secret=..              同上（客户端内部形式）
  https://t.me/socks?server=..&port=..&user=..&pass=.. SOCKS5 代理
  https://t.me/webproxy?server=..&port=..&secret=..   网页版 MTProto（WebSocket）
  socks5://user:pass@host:port / http://host:port     裸代理串

secret 的规范化（关键）
  dd 前缀 + 16 字节  → 加密封包 middle proxy（Telethon 可用）
  ee 前缀 + 16 字节  → fake-TLS（Telethon 不支持，只能客户端里用）
  无前缀 16 字节     → 明文 secret（Telethon 可用，随机 intermediate）
  base64/base64url   → 解出 16 字节后统一转 hex 存储
  长度不对           → invalid（很多频道发的是钓鱼/过期串）
"""
import base64
import binascii
import re
from urllib.parse import parse_qs, unquote, urlsplit

HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
URL_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:proxy|socks|webproxy)\?[^\s\"'<>\)\]]+"
    r"|tg://(?:proxy|socks)\?[^\s\"'<>\)\]]+"
    r"|(?:socks5|socks4|http|https)://[^\s\"'<>]+",
    re.I,
)
HTML_ENT = (("&amp;", "&"), ("&#38;", "&"), ("&quot;", '"'))


def _clean(text):
    s = text or ""
    for a, b in HTML_ENT:
        s = s.replace(a, b)
    return s


def _b64_to_bytes(s):
    t = s.strip().replace("-", "+").replace("_", "/")
    t += "=" * (-len(t) % 4)
    try:
        return base64.b64decode(t, validate=False)
    except (binascii.Error, ValueError):
        return None


def _hex_to_domain(hx):
    """fake-TLS 的 secret 后面可以拼一段 hex 域名（SNI），尽量还原出来。"""
    try:
        b = bytes.fromhex(hx[:64])
        s = b.decode("ascii", "ignore").strip("\x00 .")
        if s and all(32 <= ord(ch) < 127 for ch in s) and "." in s:
            return s
    except (ValueError, binascii.Error):
        pass
    return ""


def classify_secret(raw):
    """返回 (tag, hex16, problem)。tag 取 '' / 'dd' / 'ee'。

    ee/dd 后面可能再跟一段 hex（SNI 域名），这是 fake-TLS 的真实格式，不能判成不合法。
    """
    s = (raw or "").strip().strip('"').strip("'")
    if not s:
        return None, None, "empty"
    low = s.lower()
    # dd/ee + 至少 32 hex（后面可再跟 SNI 的 hex）
    if len(low) >= 34 and low[:2] in ("dd", "ee") and HEX_RE.match(low[2:]):
        return low[:2], low[2:34], None
    # 32 位纯 hex
    if len(low) == 32 and HEX_RE.match(low):
        return "", low, None
    # base64：22/24 位左右，解出 16 或 17 字节
    if not HEX_RE.match(low) or len(low) > 34:
        b = _b64_to_bytes(s)
        if b:
            if len(b) == 16:
                return "", b.hex(), None
            if len(b) == 17 and b[0] in (0xDD, 0xEE):
                return ("dd" if b[0] == 0xDD else "ee"), b[1:].hex(), None
    return None, None, "bad_secret_len=%d" % len(s)


def _entry(scheme, host, port, secret="", user="", pw="", raw="", problem=None):
    host = (host or "").strip().strip(".").lower()
    try:
        port = int(str(port).strip())
    except (TypeError, ValueError):
        return None
    if not host or not (0 < port < 65536):
        return None
    tag, hex16, prob = (None, None, problem)
    if scheme in ("mtproxy", "webproxy"):
        tag, hex16, prob = classify_secret(secret)
        if prob:
            return {"scheme": scheme, "host": host, "port": port, "secret": (secret or "")[:64],
                    "tag": None, "user": "", "pass": "", "raw": raw, "problem": prob,
                    "key": "bad|%s|%s|%d" % (scheme, host, port)}
    sni = ""
    if scheme in ("mtproxy", "webproxy") and (secret or "").strip().lower().startswith(("ee", "dd")) \
            and len((secret or "").strip()) > 34:
        sni = _hex_to_domain((secret or "").strip().lower()[34:])
    return {
        "scheme": scheme, "host": host, "port": port,
        "secret": (tag or "") + (hex16 or ""), "tag": tag or "", "sni": sni,
        "user": user or "", "pass": pw or "", "raw": raw, "problem": prob,
        "key": "%s|%s|%d|%s" % (scheme, host, port, ((tag or "") + (hex16 or "")) or user),
    }


def parse_one(url):
    """解析单条链接；不能识别返回 None。"""
    u = _clean(url).strip().rstrip(".,;，。；")
    low = u.lower()
    if low.startswith("tg://"):
        u = "https://t.me/" + u[5:]
    elif low.startswith("t.me/") or low.startswith("telegram.me/"):
        u = "https://" + u
    if "/proxy?" in u.lower() or "/webproxy?" in u.lower() or "/socks?" in u.lower():
        scheme = "mtproxy" if "/proxy?" in u.lower() else ("webproxy" if "/webproxy?" in u.lower() else "socks")
        q = parse_qs(urlsplit(u).query, keep_blank_values=True)
        g = lambda k: unquote((q.get(k) or [""])[0])
        if scheme == "socks":
            return _entry("socks", g("server"), g("port"), user=g("user"), pw=g("pass"), raw=u)
        return _entry(scheme, g("server"), g("port"), secret=g("secret"), raw=u)
    if "://" in u:
        sp = urlsplit(u)
        sch = sp.scheme.lower()
        if sch in ("socks5", "socks4", "socks"):
            return _entry("socks", sp.hostname, sp.port, user=unquote(sp.username or ""),
                          pw=unquote(sp.password or ""), raw=u)
        if sch in ("http", "https"):
            return _entry("http", sp.hostname, sp.port, user=unquote(sp.username or ""),
                          pw=unquote(sp.password or ""), raw=u)
    return None


def parse_text(text):
    """解析整段文本（消息/文档），返回去重后的条目列表。"""
    out, seen = [], set()
    for m in URL_RE.findall(_clean(text)):
        e = parse_one(m)
        if not e or e["key"] in seen:
            continue
        seen.add(e["key"])
        out.append(e)
    return out


def to_link(e):
    """生成 t.me 链接（可直接粘贴进 Telegram 客户端）。"""
    if e["scheme"] == "socks":
        s = "https://t.me/socks?server=%s&port=%d" % (e["host"], e["port"])
        if e.get("user"):
            s += "&user=%s" % e["user"]
            if e.get("pass"):
                s += "&pass=%s" % e["pass"]
        return s
    sch = "webproxy" if e["scheme"] == "webproxy" else "proxy"
    return "https://t.me/%s?server=%s&port=%d&secret=%s" % (sch, e["host"], e["port"], e.get("secret", ""))


def telethon_proxy(e):
    """转成 Telethon 的连接参数。

    返回 (proxy, connection, why) 三态：
      * socks / http  → (元组, None, None)，connection 用默认的
      * MTProxy(dd 或裸 secret) → ((host,port,secret), ConnectionTcpMTProxyRandomizedIntermediate, None)
      * 不支持 → (None, None, 原因)

    为什么 MTProxy 不能只给 proxy=
      telethon 的 _parse_proxy 只认 socks5 / socks4 / http，传 "mtproxy" 会直接抛
      ValueError: Unknown proxy protocol type: mtproxy。MTProxy 走的是另一套连接类，
      必须 connection=ConnectionTcpMTProxy* 且 proxy=(host, port, secret) 三元组，
      否则 secret 根本没机会被用上。
    """
    if e.get("problem"):
        return None, None, e["problem"]
    if e["scheme"] == "socks":
        if e.get("user"):
            return ("socks5", e["host"], e["port"], True, e["user"], e.get("pass") or ""), None, None
        return ("socks5", e["host"], e["port"]), None, None
    if e["scheme"] == "webproxy":
        return None, None, "webproxy(WebSocket) Telethon 不支持"
    if e.get("tag") == "ee":
        return None, None, "fake-TLS(ee) Telethon 不支持"
    secret = (e.get("secret") or "").strip()
    if not secret:
        return None, None, "缺少 secret"
    from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
    # 只保留 16 字节 secret（dd 前缀可以留着，normalize_secret 会剥掉并校验 dd）
    return ((e["host"], int(e["port"]), secret),
            ConnectionTcpMTProxyRandomizedIntermediate, None)
