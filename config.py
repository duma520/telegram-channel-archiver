# -*- coding: utf-8 -*-
"""全局配置：所有敏感项都从环境变量或同目录 .env 读取，代码里不硬编码任何凭据。

.env 示例见 .env.example（该文件不会被提交）。

  TG_API_ID       https://my.telegram.org 申请到的 api_id（整数）
  TG_API_HASH     https://my.telegram.org 申请到的 api_hash
  TG_PROXY        连接 Telegram 用的代理，留空 = 直连。支持：
                    socks5://127.0.0.1:1080
                    socks5://user:pass@127.0.0.1:1080
                    http://127.0.0.1:8080
  TG_STATE_DIR    状态目录（session / 索引 / 进度 / 日志），默认脚本目录下 state
  TG_PORT_LOGIN   登录页端口，默认 47050
  TG_PORT_MONITOR 速度看板端口，默认 47060
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env(path):
    """极简 .env 读取：KEY=VALUE，# 开头为注释，已存在的环境变量优先。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass


_load_env(os.path.join(HERE, ".env"))


def _int(name, default=0):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


API_ID = _int("TG_API_ID", 0)
API_HASH = (os.environ.get("TG_API_HASH") or "").strip()
PROXY_URL = (os.environ.get("TG_PROXY") or "").strip()

STATE_DIR = os.path.abspath(os.environ.get("TG_STATE_DIR") or os.path.join(HERE, "state"))
SESSION = os.path.join(STATE_DIR, "tg_session")

PORT_LOGIN = _int("TG_PORT_LOGIN", 47050)
PORT_MONITOR = _int("TG_PORT_MONITOR", 47060)

# 文件名长度上限（Windows 单段 255 字节，留足余量）
MAX_NAME_LEN = 150
# 多值拼接符（如多个演员名）
NAME_SEP = "、"


def require_credentials():
    if not API_ID or not API_HASH:
        raise SystemExit(
            "缺少 TG_API_ID / TG_API_HASH。\n"
            "1) 打开 https://my.telegram.org 用手机号登录\n"
            "2) API development tools 建一个应用，抄下 api_id 与 api_hash\n"
            "3) 复制 .env.example 为 .env，填进去（.env 不会被提交）")


def _proxy_from_state():
    """tgproxy.py apply 写的 state/proxy.json 优先（换线路不必改 .env）。"""
    import json
    p = os.path.join(STATE_DIR, "proxy.json")
    try:
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None
    host = (c.get("host") or "").strip()
    try:
        port = int(c.get("port") or 0)
    except (TypeError, ValueError):
        return None
    if not host or not (0 < port < 65536):
        return None
    typ = (c.get("type") or "socks5").lower()
    if typ in ("mtproxy", "mtproto"):
        secret = (c.get("secret") or "").strip()
        if not secret:
            return None
        from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
        # MTProxy 必须配专用连接类；把 "mtproxy" 当协议名塞进 proxy 会被
        # Telethon 的 _parse_proxy 直接拒绝（ValueError: Unknown proxy protocol type）
        if secret.lower().startswith("ee"):
            # fake-TLS（v1.1.0 起）：secret 以 ee 开头表示服务端要 TLS 记录层。
            # Telethon 的 tcpmtproxy 不做 TLS，只能在本地起一个中转补齐握手，
            # 对 Telethon 而言上游就是 127.0.0.1:<本地端口> 的普通 MTProxy。
            import faketls
            tun = faketls.ensure_tunnel(host, port, secret)
            print("fake-TLS 隧道就绪 → %s" % tun.label, flush=True)
            return (("127.0.0.1", tun.local_port, secret),
                    ConnectionTcpMTProxyRandomizedIntermediate)
        return ((host, port, secret), ConnectionTcpMTProxyRandomizedIntermediate)
    if typ == "http":
        if c.get("user"):
            return (("http", host, port, True, c["user"], c.get("pass") or ""), None)
        return (("http", host, port), None)
    if c.get("user"):
        return (("socks5", host, port, True, c["user"], c.get("pass") or ""), None)
    return (("socks5", host, port), None)


def proxy_and_conn():
    """返回 (proxy 参数, connection 类或 None)。

    MTProxy 与 socks/http 走的是两套连接栈：
      * socks5 / http → proxy=元组，connection 留空用默认的 ConnectionTcpFull
      * MTProxy       → connection=ConnectionTcpMTProxyRandomizedIntermediate
                        + proxy=(host, port, secret) 三元组
    只传 proxy 不传 connection 的旧写法会让 MTProxy 永远连不上。
    """
    from_state = _proxy_from_state()
    if from_state is not None:
        return from_state
    url = PROXY_URL
    if not url:
        return None, None
    if "://" not in url:
        url = "socks5://" + url
    import urllib.parse as up
    p = up.urlparse(url)
    scheme = (p.scheme or "socks5").lower()
    if scheme in ("socks5", "socks5h", "socks"):
        scheme = "socks5"
    elif scheme in ("http", "https"):
        scheme = "http"
    else:
        raise SystemExit("TG_PROXY 只支持 socks5:// 或 http:// 开头的地址：%s" % PROXY_URL)
    host = p.hostname or "127.0.0.1"
    port = p.port or (1080 if scheme == "socks5" else 8080)
    if p.username:
        return (scheme, host, port, True, up.unquote(p.username), up.unquote(p.password or "")), None
    return (scheme, host, port), None  # 无账号密码用 3 元组，Telethon 不接受 4 元组


def proxy_tuple():
    """只要 proxy 参数的老入口（不涉及 MTProxy 连接类时可以继续用）。"""
    return proxy_and_conn()[0]


def proxy_label():
    import json
    try:
        with open(os.path.join(STATE_DIR, "proxy.json"), encoding="utf-8") as f:
            c = json.load(f)
        if c.get("host"):
            return "%s %s:%s" % ((c.get("type") or "socks5"), c.get("host"), c.get("port"))
    except (OSError, ValueError):
        pass
    return PROXY_URL or "直连"
