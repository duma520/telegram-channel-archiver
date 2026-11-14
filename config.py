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


def proxy_tuple():
    """把 TG_PROXY 解析成 Telethon 的 proxy 参数；留空返回 None（直连）。"""
    url = PROXY_URL
    if not url:
        return None
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
        return (scheme, host, port, True, up.unquote(p.username), up.unquote(p.password or ""))
    return (scheme, host, port, True)


def proxy_label():
    return PROXY_URL or "直连"
