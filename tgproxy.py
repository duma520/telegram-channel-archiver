# -*- coding: utf-8 -*-
"""tgproxy.py —— MTProto 代理池工具（采集 / 去重 / 测通 / 选用）。

命令
  harvest   从频道和机器人抓链接（默认 4 个公开代理频道 + @mtpro_xyz_bot）
  add       直接粘贴链接/文本文件（支持 t.me/proxy|socks|webproxy、tg://、socks5://、http://）
  test      测连通性：TCP 预检 + Telethon 真实连一次 help.getConfig
  list      看池子（按状态过滤）
  stats     统计
  export    导出可用清单（可粘贴进 Telegram 的 t.me 链接）
  apply     把最优的一条写进 state/proxy.json，下载器下次重连就用它

用法示例
  python tgproxy.py harvest
  python tgproxy.py harvest --channels proxy_pub,mtpro3 --limit 200 --no-bot
  python tgproxy.py add "https://t.me/proxy?server=1.2.3.4&port=443&secret=dd..."
  python tgproxy.py add --file links.txt
  python tgproxy.py test --limit 60
  python tgproxy.py list --status alive
  python tgproxy.py apply
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
import proxyharvest
import proxylinks
import proxypool
import proxytest

API_ID, API_HASH = config.API_ID, config.API_HASH
SD = config.STATE_DIR
SESSION = config.SESSION if config.SESSION.endswith(".session") else config.SESSION + ".session"
PROXYF = os.path.join(SD, "proxy.json")
WORK_SESSION = os.path.join(proxypool.DB_DIR, "harvest.session")


def log(msg):
    print(time.strftime("%H:%M:%S") + " " + msg, flush=True)


def clone_session():
    """把正在用的会话复制一份给采集用（sqlite backup），避免和下载器抢锁。"""
    os.makedirs(proxypool.DB_DIR, exist_ok=True)
    if not os.path.exists(SESSION):
        raise SystemExit("找不到会话文件：%s（先跑 tg_login.bat）" % SESSION)
    if os.path.exists(WORK_SESSION):
        os.remove(WORK_SESSION)
    src = sqlite3.connect(SESSION)
    dst = sqlite3.connect(WORK_SESSION)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return WORK_SESSION


def state_proxy():
    """采集用的聊天线路：优先 state/proxy.json（tgproxy apply 写的），其次 .env 的 TG_PROXY。

    返回 (proxy 参数, connection 类或 None)——MTProxy 必须带连接类，否则连不上。
    """
    return config.proxy_and_conn()


def make_client():
    from telethon import TelegramClient
    px, conn = state_proxy()
    kw = {"connection": conn} if conn else {}
    return TelegramClient(clone_session(), API_ID, API_HASH, proxy=px,
                          timeout=30, request_retries=3, connection_retries=3, **kw)


# ------------------------------------------------------------------ 子命令
def load_channels(cli_arg):
    """频道清单：命令行 > state/mtproxy/channels.txt > 默认 4 个公开频道。

    channels.txt 里一行一个（可写 @xxx 或 t.me/xxx 或纯 username），# 开头为注释，
    这样用户想加自己的频道不用改代码。
    """
    if cli_arg:
        return [c.strip() for c in cli_arg.split(",") if c.strip()]
    f = os.path.join(proxypool.DB_DIR, "channels.txt")
    if os.path.exists(f):
        out = []
        for line in open(f, encoding="utf-8-sig"):
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
        if out:
            return out
    return list(proxyharvest.DEFAULT_CHANNELS)


def cmd_harvest(a):
    async def run():
        cl = make_client()
        await cl.connect()
        channels = load_channels(a.channels)
        total_new = total_known = total_bad = 0
        for ch in channels:
            since = int(proxypool.get_meta("last_id:" + ch, "0") or 0) if not a.full else 0
            try:
                pairs = await proxyharvest.harvest_channel(cl, ch, limit=a.limit, since_id=since)
            except Exception as e:  # noqa: BLE001
                log("频道 %s 抓取失败：%s: %s" % (ch, type(e).__name__, str(e)[:100]))
                continue
            n_items = sum(len(x[1]) for x in pairs)
            a1 = k1 = b1 = 0
            con = proxypool.connect()
            for mid, items in pairs:
                x, y, z = proxypool.upsert_many(items, ch, mid, con=con)
                a1 += x; k1 += y; b1 += z
            con.close()
            total_new += a1; total_known += k1; total_bad += b1
            if pairs:
                proxypool.set_meta("last_id:" + ch, max(x[0] for x in pairs))
            log("频道 %s%s：消息 %d 条含链接，条目 %d（新增 %d / 已有 %d / 不合法 %d）"
                % (ch, ("（增量，从 %d 起）" % since) if since else "", len(pairs), n_items, a1, k1, b1))
        if not a.no_bot:
            bots = [b.strip().lstrip("@") for b in (a.bot or ",".join(proxyharvest.DEFAULT_BOTS)).split(",") if b.strip()]
            for b in bots:
                try:
                    pairs = await proxyharvest.harvest_bot(cl, b, command=a.botcmd, wait=a.wait, rounds=a.rounds)
                except Exception as e:  # noqa: BLE001
                    log("机器人 @%s 抓取失败：%s: %s" % (b, type(e).__name__, str(e)[:100]))
                    continue
                a1 = k1 = b1 = 0
                con = proxypool.connect()
                for mid, items in pairs:
                    x, y, z = proxypool.upsert_many(items, "@" + b, mid, con=con)
                    a1 += x; k1 += y; b1 += z
                con.close()
                total_new += a1; total_known += k1; total_bad += b1
                log("机器人 @%s：%d 条回复带链接，新增 %d / 已有 %d / 不合法 %d" % (b, len(pairs), a1, k1, b1))
        await cl.disconnect()
        st = proxypool.stats()
        log("采集完成：新增 %d / 已有 %d / 不合法 %d；池子 %s" % (total_new, total_known, total_bad, fmt_stats(st)))

    asyncio.run(run())


def cmd_add(a):
    if a.file:
        text = open(a.file, encoding="utf-8", errors="ignore").read()
    else:
        text = " ".join(a.links)
    items = proxylinks.parse_text(text)
    if not items:
        log("没解析到任何代理链接")
        return
    con = proxypool.connect()
    x, y, z = proxypool.upsert_many(items, "user", 0, con=con)
    con.close()
    log("解析到 %d 条：新增 %d / 已有 %d / 不合法 %d" % (len(items), x, y, z))
    for e in items[:10]:
        log("  %s %s:%d %s %s" % (e["scheme"], e["host"], e["port"],
                                  (e["tag"] or "plain"), "OK" if not e.get("problem") else e["problem"]))


def cmd_test(a):
    rows = proxypool.due_for_check(cooldown_min=a.cooldown, limit=a.limit) if not a.all else proxypool.rows()
    if a.status:
        rows = [r for r in rows if r["status"] in a.status.split(",")]
    if not rows:
        log("没有待测条目（用 --all 强制全测，或调小 --cooldown）")
        return
    # 把 pool 行整理成 proxylinks 期望的结构
    entries = [dict(scheme=r["scheme"], host=r["host"], port=r["port"], secret=r["secret"],
                    tag=r["tag"], user=r["user"], pass_=r["pass"], key=r["key"],
                    problem=None) for r in rows]
    for e, r in zip(entries, rows):
        e["pass"] = r["pass"]
    log("待测 %d 条，并发 %d，先 TCP 预检再真连" % (len(entries), proxytest.CONC))
    t0 = time.monotonic()
    done = {"n": 0}

    def on_result(e, st, rtt, note):
        done["n"] += 1
        proxypool.mark(e["key"], st, rtt, note)
        if done["n"] % 10 == 0 or st == "alive":
            log("[%d/%d] %s %s:%d -> %s%s"
                % (done["n"], len(entries), e["scheme"], e["host"], e["port"],
                   proxypool.STATUS_ZH.get(st, st), (" %dms" % rtt) if rtt else ""))

    asyncio.run(proxytest.test_batch(entries, conc=a.conc, on_result=on_result))
    alive = [r for r in proxypool.rows(status="alive")]
    log("测完 %d 条，用时 %.1fs；现在池子里可用 %d 条" % (len(entries), time.monotonic() - t0, len(alive)))


def cmd_recheck(a):
    """把早期判成「不合法」的条目按新规则重解析（ee/dd + SNI 域名那种）。"""
    rows = proxypool.rows(status="invalid")
    con = proxypool.connect()
    fixed = still = 0
    for r in rows:
        e = proxylinks.parse_one(r["raw"])
        if not e or e.get("problem"):
            still += 1
            continue
        con.execute("DELETE FROM proxies WHERE key=?", (r["key"],))
        proxypool.upsert_many([e], r["source"], 0, con=con)
        fixed += 1
    con.commit()
    con.close()
    log("重解析 %d 条：修正 %d / 仍不合法 %d；%s" % (len(rows), fixed, still, fmt_stats(proxypool.stats())))


def fmt_stats(st):
    return "总 %d（可用 %d / 不通 %d / 未测 %d / 客户端专用 %d / 不合法 %d）" % (
        st.get("total", 0), st.get("alive", 0), st.get("dead", 0), st.get("new", 0),
        st.get("unsupported", 0), st.get("invalid", 0))


def cmd_stats(a):
    log(fmt_stats(proxypool.stats()))
    st = proxypool.stats()
    for src, c in sorted((st.get("sources") or {}).items(), key=lambda x: -x[1]):
        log("  来源 %s：%d" % (src, c))


def cmd_list(a):
    rows = proxypool.rows(status=a.status, limit=a.limit)
    if not rows:
        log("池子里没有符合条件的条目")
        return
    log("共 %d 条（%s）" % (len(rows), a.status or "全部"))
    for r in rows:
        log("%-11s %-22s %-6d %-5s %-6s %s"
            % (proxypool.STATUS_ZH.get(r["status"], r["status"]), r["host"], r["port"],
               r["scheme"], (r["tag"] or "plain"),
               ("%dms" % r["rtt_ms"]) if r["rtt_ms"] else (r["note"] or "")[:40]))


def cmd_export(a):
    rows = proxypool.rows(status=a.status or "alive", limit=a.limit,
                          order="COALESCE(rtt_ms,99999) ASC")
    out = a.out or os.path.join(proxypool.DB_DIR, "verified.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("# tgproxy 导出 %s  状态=%s  共 %d 条\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                          a.status or "alive", len(rows)))
        for r in rows:
            f.write(proxylinks.to_link(r) + "\n")
    log("已导出 %d 条到 %s" % (len(rows), out))


def cmd_apply(a):
    rows = proxypool.rows(status="alive", order="COALESCE(rtt_ms,99999) ASC")
    if not rows:
        log("没有可用条目，先跑 test")
        return
    r = rows[max(0, a.index - 1)] if a.index else rows[0]
    obj = {"type": "mtproxy" if r["scheme"] == "mtproxy" else ("socks5" if r["scheme"] == "socks" else r["scheme"]),
           "host": r["host"], "port": r["port"]}
    if r["scheme"] == "mtproxy":
        obj["secret"] = r["secret"]
    if r["user"]:
        obj["user"] = r["user"]
        obj["pass"] = r["pass"]
    tmp = PROXYF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PROXYF)
    log("已写入 %s：%s %s:%d（下载器会在下次重连时用上）" % (PROXYF, obj["type"], obj["host"], obj["port"]))


def main():
    p = argparse.ArgumentParser(description="MTProto 代理池工具")
    sub = p.add_subparsers(dest="cmd")

    h = sub.add_parser("harvest", help="从频道/机器人采集")
    h.add_argument("--channels", default="")
    h.add_argument("--bot", default="")
    h.add_argument("--command", dest="botcmd", default="/mtproto")
    h.add_argument("--limit", type=int, default=300)
    h.add_argument("--rounds", type=int, default=1)
    h.add_argument("--wait", type=int, default=30)
    h.add_argument("--no-bot", action="store_true")
    h.add_argument("--full", action="store_true", help="忽略增量游标，重扫历史")

    ad = sub.add_parser("add", help="粘贴链接")
    ad.add_argument("links", nargs="*")
    ad.add_argument("--file", default="")

    t = sub.add_parser("test", help="测连通性")
    t.add_argument("--limit", type=int, default=120)
    t.add_argument("--cooldown", type=int, default=30, help="失败条目多少分钟后可重测")
    t.add_argument("--conc", type=int, default=8)
    t.add_argument("--all", action="store_true")
    t.add_argument("--status", default="")

    sub.add_parser("recheck", help="按新规则重解析不合法条目")
    s = sub.add_parser("stats", help="统计")
    ls = sub.add_parser("list", help="列条目")
    ls.add_argument("--status", default="")
    ls.add_argument("--limit", type=int, default=40)

    e = sub.add_parser("export", help="导出可用清单")
    e.add_argument("--status", default="alive")
    e.add_argument("--out", default="")
    e.add_argument("--limit", type=int, default=0)

    ap = sub.add_parser("apply", help="写进 state/proxy.json")
    ap.add_argument("--index", type=int, default=1)

    a = p.parse_args()
    if not a.cmd:
        p.print_help()
        return
    {"harvest": cmd_harvest, "add": cmd_add, "test": cmd_test, "stats": cmd_stats, "recheck": cmd_recheck,
     "list": cmd_list, "export": cmd_export, "apply": cmd_apply}[a.cmd](a)


if __name__ == "__main__":
    main()
