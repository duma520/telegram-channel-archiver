# -*- coding: utf-8 -*-
"""proxytest.py —— 代理可用性测试。

两级
  1) TCP 预检：直连目标 host:port（3~5 秒超时）。不通的直接判 dead，省掉后面的耗时测试。
  2) 真实连通：用 Telethon 以「未登录会话」连上该代理并调 help.getConfig（不需要账号、不产生登录态）。
     成功即证明这个代理此刻真的能把 MTProto 送到 Telegram，才算 alive。

为什么两级
  * 只测 TCP 会把「端口开但代理已死/secret 错」的算成可用，实际连不上；
  * 每条都上 Telethon 又太慢，先 TCP 过滤能把耗时压到 1/3。

能力边界（实测 telethon 1.45 源码 tcpmtproxy.py 确认）
  * dd 前缀（padded）与无前缀 16 字节 secret → 支持
  * ee 前缀（fake-TLS）→ **不支持**（没有 TLS 层），只能标 unsupported，需在 Telegram 客户端里验证
  * webproxy（WebSocket）→ 不支持
  * socks5（含账号密码）→ 支持，且往往比 MTProxy 快
"""
import asyncio
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import proxylinks

API_ID, API_HASH = config.API_ID, config.API_HASH
CONC = int(os.environ.get("TEST_CONC", "8"))
TCP_TIMEOUT = float(os.environ.get("TEST_TCP_TIMEOUT", "5"))
MT_TIMEOUT = float(os.environ.get("TEST_MT_TIMEOUT", "18"))


def tcp_probe(host, port, timeout=TCP_TIMEOUT):
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, int((time.monotonic() - t0) * 1000), None
    except socket.gaierror as e:
        return False, None, "dns: %s" % e
    except socket.timeout:
        return False, None, "tcp timeout %.0fs" % timeout
    except OSError as e:
        return False, None, "tcp %s" % (e.strerror or type(e).__name__)


async def _mt_call(proxy_arg, timeout=MT_TIMEOUT):
    from telethon import TelegramClient
    from telethon.tl import functions
    t0 = time.monotonic()
    cl = TelegramClient(None, API_ID, API_HASH, proxy=proxy_arg,
                        timeout=10, connection_retries=0, request_retries=1)
    try:
        await asyncio.wait_for(cl.connect(), timeout)
        await asyncio.wait_for(cl(functions.help.GetConfigRequest()), timeout)
        return True, int((time.monotonic() - t0) * 1000), None
    except asyncio.TimeoutError:
        return False, None, "mtproto timeout %.0fs" % timeout
    except Exception as e:  # noqa: BLE001 - 需要把任意底层错误记下来
        return False, None, "%s: %s" % (type(e).__name__, str(e)[:110])
    finally:
        try:
            await cl.disconnect()
        except Exception:
            pass


async def test_entry(e, sem):
    """测试单条，返回 (status, rtt_ms, note)。"""
    async with sem:
        ok, rtt, err = await asyncio.to_thread(tcp_probe, e["host"], e["port"])
        if not ok:
            return "dead", None, err
        p, why = proxylinks.telethon_proxy(e)
        if p is None:
            return "unsupported", rtt, "tcp 通 · %s" % why
        ok2, rtt2, err2 = await _mt_call(p)
        if ok2:
            return "alive", rtt2 or rtt, "ok"
        return "dead", None, err2


async def test_batch(entries, conc=CONC, on_result=None):
    sem = asyncio.Semaphore(conc)
    async def one(e):
        st, rtt, note = await test_entry(e, sem)
        if on_result:
            on_result(e, st, rtt, note)
        return e["key"], st, rtt, note
    return await asyncio.gather(*[one(e) for e in entries])
