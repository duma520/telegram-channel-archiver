# -*- coding: utf-8 -*-
"""speedprobe.py —— 用指定代理真下一小段，量真实吞吐（MB/s）。

用法
  python speedprobe.py --key "socks|1.2.3.4|1080|用户名"
  python speedprobe.py --top 3            # 池子里 rtt 最小的 3 条依次测
  python speedprobe.py --bytes 6         # 每次只取 6MB

为什么不只看 help.getConfig 的 RTT：
  RTT 小不代表能跑量（限速/拥塞/丢包都会让 RTT 好看、吞吐难看）。
  这里真的取 4MB，得到的是「这个代理能给我多少」。
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import proxylinks
import proxypool

API_ID, API_HASH = config.API_ID, config.API_HASH
SD = config.STATE_DIR
SESSION = config.SESSION if config.SESSION.endswith(".session") else config.SESSION + ".session"
WORK = os.path.join(proxypool.DB_DIR, "speedprobe.session")
INDEX = os.path.join(SD, "channel_index.json")
PROXYF = os.path.join(SD, "proxy.json")


def clone_session():
    os.makedirs(proxypool.DB_DIR, exist_ok=True)
    if os.path.exists(WORK):
        os.remove(WORK)
    s = sqlite3.connect(SESSION)
    d = sqlite3.connect(WORK)
    with d:
        s.backup(d)
    d.close()
    s.close()
    return WORK


def pick_file(min_mb=3, max_mb=40):
    idx = json.load(open(INDEX, encoding="utf-8"))
    items = idx.get("items") or []
    cand = [x for x in items if min_mb * 1048576 <= (x.get("size") or 0) <= max_mb * 1048576]
    if not cand:
        cand = [x for x in items if (x.get("size") or 0) > 0]
    cand.sort(key=lambda x: x.get("size") or 0)
    return idx, cand[0]


async def measure(proxy_arg, label, nbytes, conn=None):
    from telethon import TelegramClient
    idx, it = pick_file()
    kw = {"connection": conn} if conn else {}
    cl = TelegramClient(clone_session(), API_ID, API_HASH, proxy=proxy_arg,
                        timeout=25, request_retries=0, connection_retries=0,
                        auto_reconnect=False, **kw)
    try:
        await asyncio.wait_for(cl.connect(), 25)
        ent = await cl.get_entity(idx.get("channel_username") or idx.get("channel"))
        msg = await cl.get_messages(ent, ids=it["msg_id"])
        if msg is None or msg.document is None:
            return None, "拿不到消息"
        t0 = time.monotonic()
        got = 0
        async for chunk in cl.iter_download(msg.document, offset=0, limit=nbytes or None, request_size=512 * 1024):
            got += len(chunk)
            if nbytes and got >= nbytes:
                break
        dt = max(1e-6, time.monotonic() - t0)
        return got / dt / 1048576.0, "%.1fMB / %.1fs" % (got / 1048576.0, dt)
    finally:
        try:
            await cl.disconnect()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--bytes", type=int, default=4)
    ap.add_argument("--status", default="alive")
    a = ap.parse_args()
    nbytes = a.bytes * 1024 * 1024

    if a.key:
        cands = [r for r in proxypool.rows(status=a.status) if r["key"] == a.key]
    elif a.top:
        cands = proxypool.rows(status=a.status, limit=a.top, order="COALESCE(rtt_ms,99999) ASC")
    else:
        cands = proxypool.rows(status=a.status, limit=5, order="COALESCE(rtt_ms,99999) ASC")
    if not cands:
        print("池子里没有可用条目")
        return

    for r in cands:
        e = dict(scheme=r["scheme"], host=r["host"], port=r["port"], secret=r["secret"],
                 tag=r["tag"], user=r["user"], pw=r["pass"], problem=None)
        e["pass"] = r["pass"]
        p, conn, why = proxylinks.telethon_proxy(e)
        label = "%s %s:%d" % (r["scheme"], r["host"], r["port"])
        if p is None:
            print("%-34s 跳过：%s" % (label, why))
            continue
        try:
            mbps, note = asyncio.run(asyncio.wait_for(measure(p, label, nbytes, conn), 100))
        except asyncio.TimeoutError:
            print("%-34s 失败：整体超时 100s" % label, flush=True)
            continue
        except Exception as ex:  # noqa: BLE001
            print("%-34s 失败：%s: %s" % (label, type(ex).__name__, str(ex)[:90]), flush=True)
            continue
        if mbps is None:
            print("%-34s 失败：%s" % (label, note))
            continue
        print("%-34s %.3f MB/s  （%s）" % (label, mbps, note))
        if mbps > 0:
            proxypool.mark(r["key"], "alive", r["rtt_ms"],
                           "实测 %.2f MB/s" % mbps, speed_bps=int(mbps * 1048576))


if __name__ == "__main__":
    main()
