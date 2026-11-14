# -*- coding: utf-8 -*-
"""proxyharvest.py —— 从频道和机器人处收集 MTProto 代理链接。

三条来源
  1) 频道历史消息（含消息里的按钮 url、文本链接实体、消息原文）
  2) 机器人：发 /mtproto 这类命令，等它的回复（回复里通常是一批链接）
  3) 用户直接粘贴的文本（tgproxy add）

同一批链接反复抓到时靠 proxypool 的 key 去重，seen_count 只加不重测，
所以「避免多次获取相同的连接」这条是落在池子里的。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proxylinks

# 默认监控的公开代理频道（可按需增删；只放公开频道，不放私有群）
DEFAULT_CHANNELS = [
    "MTProtoMianFeiTG",
    "TGMTProtoFree",
    "mtpro3",
    "proxy_pub",
]
DEFAULT_BOTS = ["mtpro_xyz_bot"]


def _msg_text_with_links(m):
    """把消息正文、按钮 url、文本链接实体拼成一段可解析的文本。"""
    parts = [m.message or ""]
    ent = getattr(m, "entities", None) or []
    for x in ent:
        u = getattr(x, "url", None)
        if u:
            parts.append(u)
    rm = getattr(m, "reply_markup", None)
    for row in getattr(rm, "rows", None) or []:
        for b in row.buttons:
            u = getattr(b, "url", None)
            if u:
                parts.append(u)
    return "\n".join(p for p in parts if p)


async def harvest_channel(client, channel, limit=300, on_page=None, since_id=0):
    """抓一个频道的消息，返回 [(msg_id, items)]，items 已去重。

    since_id>0 时只抓比它更新的消息（增量），避免每次重扫历史、重复解析同一批链接。
    """
    ent = await client.get_entity(channel)
    out, buf = [], []
    kw = {"limit": limit}
    if since_id:
        kw["min_id"] = int(since_id)
    async for m in client.iter_messages(ent, **kw):
        items = proxylinks.parse_text(_msg_text_with_links(m))
        if items:
            out.append((m.id, items))
            buf.extend(items)
        if on_page and len(out) and len(out) % 20 == 0:
            on_page(channel, len(out), len(buf))
    return out


async def harvest_bot(client, username, command="/mtproto", wait=30, rounds=1, gap=5):
    """给机器人发命令并收集回复里的链接。返回 [(msg_id, items)]。"""
    ent = await client.get_entity(username)
    collected = []
    for r in range(max(1, rounds)):
        last = 0
        try:
            hist = await client.get_messages(ent, limit=1)
            last = hist[0].id if hist else 0
        except Exception:
            last = 0
        await client.send_message(ent, command)
        deadline = asyncio.get_event_loop().time() + wait
        got = 0
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2.5)
            try:
                msgs = await client.get_messages(ent, limit=10, min_id=last)
            except Exception:
                continue
            fresh = [m for m in msgs if not getattr(m, "out", False)]
            for m in fresh:
                items = proxylinks.parse_text(_msg_text_with_links(m))
                if items:
                    collected.append((m.id, items))
                    got += len(items)
                    last = max(last, m.id)
            if got:
                break
        if r + 1 < rounds:
            await asyncio.sleep(gap)
    return collected


def harvest_text(text):
    """用户粘贴的文本（可含多行/多个链接）。"""
    return proxylinks.parse_text(text)
