# -*- coding: utf-8 -*-
"""proxy_rotate.py —— 代理候选表 + 下载器失败自动轮换。

分工
  * 看板 monitor.py：收 t.me/proxy|socks|webproxy 链接、测活、把结果写进
    state/proxy_candidates.json，并把选中那条写进 state/proxy.json。
  * 下载器 tgarchiver.py：连不上时按这张表依次换下一条，坏掉的标 dead。

为什么单独一个模块
  state/proxy.json 是「当前生效」的唯一真相，候选表是「还能试哪些」。
  两个进程（看板 + 下载器）都要读它，逻辑必须只有一份，避免各算各的。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import proxylinks

SD = config.STATE_DIR
CANDF = os.path.join(SD, "proxy_candidates.json")
CURF = os.path.join(SD, "proxy.json")

# 从好到差：轮换时优先挑 alive，其次还没测过的
ORDER = ("alive", "untested")

STATUS_ZH = {
    "alive": "可用",
    "dead": "不通",
    "untested": "未测",
    "unsupported": "客户端不支持",
}


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    """原子写。杀软/网盘同步偶尔占用文件，重试几次。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    except Exception:
        return False
    for _ in range(6):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(0.12)
    try:
        os.remove(tmp)
    except Exception:
        pass
    return False


# ------------------------------------------------------------------ 候选表
def candidates():
    d = _load(CANDF, {})
    return d.get("items") or []


def save_candidates(items):
    return _save(CANDF, {"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "items": items})


def entry_to_obj(e):
    """proxylinks 条目 -> state/proxy.json 的对象（config._proxy_from_state 认这个形状）。"""
    obj = {"type": e["scheme"], "host": e["host"], "port": int(e["port"])}
    if e["scheme"] == "socks":
        obj["type"] = "socks5"
    if e.get("secret"):
        obj["secret"] = e["secret"]
    if e.get("user"):
        obj["user"] = e["user"]
        obj["pass"] = e.get("pass") or ""
    return obj


def add_text(text, source="看板"):
    """解析一段文本并追加进候选表。

    返回 (新增数, 重复数, 不支持清单)。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    items = candidates()
    have = set(it["key"] for it in items)
    added, dup, unsup = 0, 0, []
    for e in proxylinks.parse_text(text or ""):
        if e["key"] in have:
            dup += 1
            continue
        support, _conn, why = proxylinks.telethon_proxy(e)
        items.append({
            "key": e["key"], "scheme": e["scheme"], "host": e["host"], "port": int(e["port"]),
            "secret": e.get("secret") or "", "user": e.get("user") or "", "pass": e.get("pass") or "",
            "link": proxylinks.to_link(e), "source": source, "added": now,
            "status": "untested" if support is not None else "unsupported",
            "note": "" if support is not None else (why or ""),
            "rtt_ms": None, "checked": "",
        })
        have.add(e["key"])
        added += 1
        if support is None:
            unsup.append("%s:%d %s" % (e["host"], e["port"], why))
    if added:
        save_candidates(items)
    return added, dup, unsup


def set_status(key, status, note="", rtt_ms=None):
    items = candidates()
    for it in items:
        if it["key"] == key:
            it["status"] = status
            it["note"] = (note or "")[:200]
            if rtt_ms is not None:
                it["rtt_ms"] = int(rtt_ms)
            it["checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_candidates(items)
            return True
    return False


def remove(key):
    items = [it for it in candidates() if it["key"] != key]
    return save_candidates(items)


# ------------------------------------------------------------------ 当前生效
def current():
    return _load(CURF, {})


def apply(key, note=""):
    """把候选表里某条写成 state/proxy.json。返回 (成功?, 写入的对象)。"""
    for it in candidates():
        if it["key"] != key:
            continue
        obj = entry_to_obj(it)
        obj["note"] = note or ("看板选用 %s" % time.strftime("%Y-%m-%d %H:%M"))
        return _save(CURF, obj), obj
    return False, None


def rotate(avoid=()):
    """挑下一条可用的写进 proxy.json；没有可用的返回 (None, None)。

    avoid 里的 key 本轮跳过（调用方用来避免原地打转）。全部候选都被 avoid
    时自动清空 avoid 再挑一轮 —— 否则一旦在一轮里把所有线路都试过一遍，
    后续就永远挑不到东西（早期版本的缺陷：没有这条重置，会原地卡死）。
    """
    for skip in (set(avoid), set()):
        items = candidates()
        for want in ORDER:
            for it in items:
                if it["key"] in skip or it["status"] != want:
                    continue
                ok, obj = apply(it["key"], note="自动轮换 %s" % time.strftime("%H:%M:%S"))
                if ok:
                    return it, obj
    return None, None


def label_of(obj):
    """和 tgarchiver.proxy_label() 口径一致的短标签。"""
    if not obj:
        return "-"
    t = obj.get("type") or "?"
    return "%s %s:%s" % (t, obj.get("host"), obj.get("port"))
