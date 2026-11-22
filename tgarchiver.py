# -*- coding: utf-8 -*-
"""tgarchiver.py —— Telegram 频道批量归档器（命令行工具）。

特性
  * 单进程永续运行：代理线路抖动/切换导致断连时自动重连，分片级断点续传
  * 任务化：一个任务 = 一个 job JSON（频道 / 输出目录 / 筛选 / 命名规则）
  * 三种筛选：caption（只下带文案的）/ keyword（文案含关键词）/ all（全部视频）
  * 三种命名：caption（《标题》（年份））/ orig（原始文件名）/ msgid（频道_消息号）
  * 重名自动在扩展名前加 _YYYYMMDD_HHMMSS，并把最终名字记进进度文件，重启后不变
  * 每 5 秒写速度遥测 JSON，供 monitor.py 看板显示
  * 加密后端自检：必须装 cryptg（C 实现的 AES-IGE）。缺了会退回 pyaes 纯 Python 实现，
    把 asyncio 事件循环占满，实测 8 并发从 7.3 MB/s 掉到 0.065 MB/s（慢 112 倍），
    而且加并发、换代理、换协议都无效 —— 所以开机就叫，不静默降级。

用法
  python tgarchiver.py new --name demo --channel https://t.me/somechannel --out "D:\\dl"
  python tgarchiver.py index demo        # 扫描频道，生成消息索引
  python tgarchiver.py run demo          # 开始/继续下载（可随时 Ctrl+C，重跑即续）
  python tgarchiver.py list              # 列出所有任务
  python tgarchiver.py probe somechannel # 只看频道信息（标题/消息数）

常用参数（new 时指定，写进 job 文件，可事后手改）
  --selector caption|keyword|all  默认 caption
  --keywords 词1,词2              selector=keyword 时生效
  --min-mb 5 --max-mb 200         体积过滤（0 表示不限）
  --name-mode caption|orig|msgid  默认 orig
  --workers 8                     并发分片数（实测饱和点，1~16 之间调；前提是装了 cryptg）
  --limit 20                      run 时只处理前 N 个文件（试跑用）
"""
import argparse, asyncio, datetime, json, math, os, re, sys, time, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, naming
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename

SD = config.STATE_DIR
API_ID, API_HASH = config.API_ID, config.API_HASH
SESSION = os.path.join(SD, "tg_session.session")
JOBD = os.path.join(SD, "jobs")
PROXYF = os.path.join(SD, "proxy.json")
os.makedirs(JOBD, exist_ok=True)


# ---------------------------------------------------------------- 加密后端自检
# MTProto 每个数据块都要 AES-IGE 解密。装了 cryptg 走 C 实现；没有就退回 pyaes
# 的纯 Python 实现，把 asyncio 单线程事件循环占满 —— 实测 8 并发从 7.3 MB/s
# 掉到 0.065 MB/s（慢 112 倍），且加并发、换代理、换协议全部无效。
def _check_crypto():
    try:
        from telethon.crypto import aes as _aes
        if getattr(_aes, "cryptg", None) is not None:
            try:
                from importlib.metadata import version as _ver
                _v = _ver("cryptg")
            except Exception:
                _v = "已启用"
            return True, "cryptg %s" % _v
        return False, "pyaes（纯 Python）"
    except Exception as ex:
        return False, "检测失败 %s" % type(ex).__name__


CRYPTO_OK, CRYPTO_DESC = _check_crypto()

if not CRYPTO_OK:
    sys.stderr.write(
        "\n" + "=" * 72 + "\n"
        "【警告】加密后端是 %s，未启用 cryptg。\n"
        "  MTProto 解密会跑纯 Python AES-IGE，下载速度将掉到 1/100 左右，\n"
        "  而且提高并发、更换代理、更换传输协议都不会有任何改善。\n"
        "  修复： python -m pip install cryptg -i https://pypi.org/simple\n"
        "=" * 72 + "\n\n" % CRYPTO_DESC)
    sys.stderr.flush()


# ---------------------------------------------------------------- 基础工具
def jload(p, d=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return d if d is not None else {}


def jsave(p, o):
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(o, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def proxy_and_conn():
    """返回 (proxy 参数, connection 类或 None)。

    代理优先级：state/proxy.json > .env 的 TG_PROXY > 直连。
    MTProxy 需要专用连接类，所以这里和 proxy() 分开：只拿 proxy 参数用 proxy()。
    """
    return config.proxy_and_conn()


def proxy():
    """只要 proxy 参数时用它（socks5 / http / 直连都适用）。"""
    return config.proxy_tuple()


def proxy_label():
    return config.proxy_label()


def norm_channel(s):
    s = (s or "").strip()
    s = re.sub(r"^https?://t\.me/", "", s)
    s = re.sub(r"^t\.me/", "", s)
    s = s.split("?")[0].strip("/")
    if s.startswith("+"):
        return s
    return s


def job_path(name):
    if os.path.isfile(name):
        return os.path.abspath(name)
    p = os.path.join(JOBD, name if name.endswith(".json") else name + ".json")
    return p


def load_job(name):
    p = job_path(name)
    j = jload(p, None)
    if not j:
        raise SystemExit("找不到任务文件: %s\n先跑: python tgarchiver.py list" % p)
    j["_file"] = p
    j.setdefault("name", os.path.splitext(os.path.basename(p))[0])
    j.setdefault("selector", "caption")
    j.setdefault("keywords", [])
    j.setdefault("min_mb", 0)
    j.setdefault("max_mb", 0)
    j.setdefault("name_mode", "orig")
    j.setdefault("workers", 8)      # 实测饱和点：8 流 × 512KB 约 6.5~7.3 MB/s
    j.setdefault("seg_mb", 2)
    j.setdefault("chunk_kb", 512)   # 128KB 块太小，重试与计量的粒度反而不划算
    j.setdefault("ext", ".mp4")
    j.setdefault("index_file", "%s_index.json" % j["name"])
    j.setdefault("progress_file", "%s_progress.json" % j["name"])
    j.setdefault("log_file", "%s.log" % j["name"])
    j.setdefault("speed_file", "%s_speed.json" % j["name"])
    j.setdefault("parts_dir_name", "__parts")
    for k in ("index_file", "progress_file", "log_file", "speed_file"):
        if not os.path.isabs(j[k]):
            j[k] = os.path.join(SD, j[k])
    if not j.get("out_dir"):
        raise SystemExit("任务缺少 out_dir: %s" % p)
    return j


def log(j, *a):
    s = "%s %s" % (datetime.datetime.now().strftime("%H:%M:%S"), " ".join(str(x) for x in a))
    print(s, flush=True)
    try:
        with open(j["log_file"], "a", encoding="utf-8") as f:
            f.write(s + "\n")
    except Exception:
        pass


_client = None


async def get_client(j, force=False):
    global _client
    if _client is not None and not force:
        try:
            if _client.is_connected():
                return _client
        except Exception:
            pass
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
    _client = None
    fails = 0
    tried = []
    for attempt in range(9999):
        try:
            px, conn = proxy_and_conn()
            kw = {"connection": conn} if conn else {}
            c = TelegramClient(SESSION, API_ID, API_HASH, connection_retries=3, retry_delay=2,
                               timeout=30, request_retries=3, auto_reconnect=True, proxy=px, **kw)
            await asyncio.wait_for(c.connect(), timeout=25)
            if await c.is_user_authorized():
                _client = c
                fails = 0
                log(j, "CONNECT ok proxy=%s" % proxy_label())
                return c
            await c.disconnect()
        except Exception as ex:
            fails += 1
            if attempt % 3 == 0:
                log(j, "CONNECT retry %s proxy=%s" % (type(ex).__name__, proxy_label()))
        # 连不上就换线路。阈值取 2 而不是 3：单次退避最长 20 秒，3 次失败会让
        # 换线迟滞一分钟以上，日志表现成「一直 CONNECT retry 不动」。
        if fails >= 2:
            tried.append(_cur_key())
            fails = 0
            try:
                import proxy_rotate
                it, obj = proxy_rotate.rotate(avoid=tried)
            except Exception:
                it, obj = None, None
            if it:
                log(j, "ROTATE → %s (%s)" % (proxy_rotate.label_of(obj), it.get("status")))
            else:
                tried = []
                log(j, "ROTATE 候选表里没有可用的了，继续用当前线路重试")
        await asyncio.sleep(min(20, 2 + attempt * 2))
    return None


def _cur_key():
    """当前 state/proxy.json 对应的候选表 key（轮换时用来避开刚失败的那条）。"""
    try:
        import proxy_rotate
        cur = proxy_rotate.current()
        if not cur:
            return ""
        for it in proxy_rotate.candidates():
            if (it["host"] == cur.get("host")
                    and int(it["port"]) == int(cur.get("port") or 0)
                    and (it.get("user") or "") == (cur.get("user") or "")):
                return it["key"]
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 命名
def caption_of(msg):
    txt = (msg.message or "").strip()
    if not txt:
        return ""
    return txt


def orig_name(msg):
    if msg.document is not None:
        for a in msg.document.attributes:
            if isinstance(a, DocumentAttributeFilename) and a.file_name:
                return a.file_name
    return ""


def build_name(j, it):
    mode = j["name_mode"]
    ext = j["ext"]
    fallback = "%s_%s" % (norm_channel(j["channel"]), it.get("msg_id"))
    caption_name = naming.build_name(it.get("title"), it.get("year"))
    if mode == "msgid":
        base = fallback
    elif mode == "orig":
        nm = (it.get("name") or "").strip()
        base = os.path.splitext(nm)[0] if nm else ""
        base = base or caption_name or fallback
    else:
        base = caption_name or (it.get("name") or "").strip() or fallback
    base = naming.sanitize(base, config.MAX_NAME_LEN)
    if base.lower().endswith(ext.lower()):
        base = base[: -len(ext)]
    return base + ext


def unique_name(out_dir, base, names, key):
    """重名时加时间戳；同一 msg 的已定名字记进 names，重启后保持稳定。"""
    fixed = names.get(key)
    if fixed:
        return fixed
    cand = base
    n = 0
    while os.path.exists(os.path.join(out_dir, cand)):
        n += 1
        stem, ext = os.path.splitext(base)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        cand = "%s_%s%s" % (stem, stamp, ext) if n == 1 else "%s_%s_%d%s" % (stem, stamp, n, ext)
    names[key] = cand
    return cand


# ---------------------------------------------------------------- 索引
def passes(j, it):
    sel = j["selector"]
    if sel == "all":
        ok = True
    elif sel == "keyword":
        cap = (it.get("caption") or "")
        kws = [k for k in j["keywords"] if k]
        ok = any(k in cap for k in kws) if kws else True
    else:
        ok = bool(it.get("title") or it.get("caption"))
    if not ok:
        return False
    mb = (it.get("size") or 0) / 1048576.0
    if j["min_mb"] and mb < j["min_mb"]:
        return False
    if j["max_mb"] and mb > j["max_mb"]:
        return False
    return True


async def cmd_index(name):
    j = load_job(name)
    cl = await get_client(j)
    ent = await cl.get_entity(norm_channel(j["channel"]))
    log(j, "INDEX start channel=%s id=%s" % (norm_channel(j["channel"]), ent.id))
    items, scanned, tagged, total = [], 0, 0, 0
    async for m in cl.iter_messages(ent):
        scanned += 1
        if scanned % 300 == 0:
            log(j, "  scanned %d items %d" % (scanned, len(items)))
        d = m.document
        if d is None:
            continue
        if not (getattr(d, "mime_type", "") or "").startswith("video"):
            continue
        size = int(d.size or 0)
        if size <= 0:
            continue
        cap = caption_of(m)
        nm = orig_name(m)
        meta = naming.parse_caption(cap)
        it = {"msg_id": m.id, "date": m.date.isoformat() if m.date else None,
              "size": size, "mime": d.mime_type, "name": nm or "",
              "caption": cap, "title": meta["title"], "year": meta["year"],
              "headline": meta["headline"],
              "doc_id": d.id, "access_hash": d.access_hash,
              "file_reference": d.file_reference.hex() if d.file_reference else ""}
        if meta["title"]:
            tagged += 1
        total += size
        if passes(j, it):
            items.append(it)
    idx = {"channel": ent.id, "channel_username": norm_channel(j["channel"]),
           "scanned": scanned, "tagged": tagged, "items": items,
           "bytes": sum(x["size"] for x in items),
           "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    jsave(j["index_file"], idx)
    log(j, "INDEX done scanned=%d titled=%d selected=%d %.1fGB -> %s"
        % (scanned, tagged, len(items), idx["bytes"] / 1073741824.0, j["index_file"]))
    return idx


# ---------------------------------------------------------------- 下载
class Meter(object):
    def __init__(self):
        self.bytes = 0
        self.done = 0
        self.total = 0
        self.cur = ""
        self.fail = 0
        self.cum = 0

    def add(self, n):
        self.bytes += n
        self.cum += n

    def session_mb(self):
        return self.cum / 1048576.0


async def meter_task(j, m):
    while True:
        await asyncio.sleep(5)
        jsave(j["speed_file"],
              {"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "bps": int(m.bytes / 5.0),
               "mbps": round(m.bytes / 1048576.0 / 5.0, 3),
               "session_mb": round(m.session_mb(), 1),
               "files_done": m.done, "files_total": m.total,
               "current": m.cur, "fail": m.fail,
               "job": j["name"],
               "proxy": proxy_label(),
               "crypto": CRYPTO_DESC})
        m.bytes = 0


def _session_mb_factory():
    box = {"b": 0}

    def add(n):
        box["b"] += n
    return box, add


async def fetch_seg(j, client, msg, off, ln, meter, tries=5):
    chunk = j["chunk_kb"] * 1024
    for t in range(tries):
        try:
            buf = bytearray()

            async def _pump():
                async for c in client.iter_download(msg, offset=off, limit=ln, request_size=chunk):
                    buf.extend(c)
                    meter.add(len(c))
                    if len(buf) >= ln:
                        return
            await asyncio.wait_for(_pump(), timeout=120)
            if len(buf) >= ln:
                return bytes(buf[:ln])
        except Exception as ex:
            if t == 0:
                log(j, "  segfail off=%d %s" % (off, type(ex).__name__))
            await asyncio.sleep(1.0 + t * 2)
    return None


async def dl_one(j, ent, it, progst, meter):
    size = int(it["size"])
    key = str(it["msg_id"])
    seg = j["seg_mb"] * 1024 * 1024
    out = j["out_dir"]
    parts = os.path.join(out, j["parts_dir_name"])
    os.makedirs(parts, exist_ok=True)
    client = await get_client(j)
    m = (await client.get_messages(ent, ids=[it["msg_id"]]))[0]
    if m is None or m.document is None:
        return "skip"
    base = build_name(j, it)
    names = progst.setdefault("#names", {})
    final_name = names.get(key)
    if not final_name:
        cand = base
        if os.path.exists(os.path.join(out, cand)):
            if os.path.getsize(os.path.join(out, cand)) == size:
                return "exists"
            cand = unique_name(out, base, names, key)
        final_name = cand
        names[key] = final_name
    final = os.path.join(out, final_name)
    if os.path.exists(final) and os.path.getsize(final) == size:
        return "exists"
    part = os.path.join(parts, "%s.part" % it["msg_id"])
    segs = int(math.ceil(size / float(seg)))
    st = progst.get(key) or {"done": [], "size": size}
    done = set(st["done"]) if st.get("size") == size else set()
    if not os.path.exists(part) or os.path.getsize(part) != size:
        with open(part, "wb") as f:
            f.truncate(size)
        done = set()
    todo = [i for i in range(segs) if i not in done]
    if not todo:
        os.replace(part, final)
        progst.pop(key, None)
        jsave(j["progress_file"], progst)
        return "ok"
    meter.cur = final_name[:44]
    lock = asyncio.Lock()
    q = list(todo)
    stuck = [0]
    alive = [True]

    async def worker():
        while alive[0]:
            async with lock:
                if not q or not alive[0]:
                    return
                i = q.pop(0)
            off = i * seg
            ln = min(seg, size - off)
            cl = await get_client(j)
            b = await fetch_seg(j, cl, m, off, ln, meter)
            if b is None:
                cl = await get_client(j, force=True)
                b = await fetch_seg(j, cl, m, off, ln, meter)
            if b is None:
                async with lock:
                    stuck[0] += 1
                    if stuck[0] >= 3:
                        alive[0] = False
                        return
                    q.append(i)
                await asyncio.sleep(3)
                continue
            with open(part, "r+b") as f:
                f.seek(off)
                f.write(b)
            async with lock:
                done.add(i)
                progst[key] = {"done": sorted(done), "size": size}
                jsave(j["progress_file"], progst)

    await asyncio.gather(*[worker() for _ in range(min(j["workers"], len(todo)))])
    if len(done) >= segs:
        os.replace(part, final)
        progst.pop(key, None)
        jsave(j["progress_file"], progst)
        log(j, "OK %s %.1fMB" % (final_name[:44], size / 1048576.0))
        return "ok"
    meter.fail += 1
    log(j, "PART %s segs %d/%d" % (final_name[:44], len(done), segs))
    return "partial"


async def cmd_run(name, limit=0):
    j = load_job(name)
    idx = jload(j["index_file"], None)
    if not idx or not idx.get("items"):
        idx = await cmd_index(name)
    items = idx["items"]
    if limit:
        items = items[:limit]
    progst = jload(j["progress_file"], {})
    meter = Meter()
    asyncio.create_task(meter_task(j, meter))
    log(j, "BOOT job=%s total=%d workers=%d seg=%dMB chunk=%dKB proxy=%s crypto=%s"
        % (j["name"], len(items), j["workers"], j["seg_mb"], j["chunk_kb"],
           proxy_label(), CRYPTO_DESC))
    rnd = 0
    while True:
        rnd += 1
        cl = await get_client(j, force=(rnd > 1))
        # 用 username 解析实体：索引里的数字 id 在公开频道上会被误判成 PeerUser
        ent = await cl.get_entity(idx.get("channel_username") or norm_channel(j["channel"]))
        try:
            existing = set(os.listdir(j["out_dir"]))
        except Exception:
            existing = set()
        todo = []
        for it in items:
            nm = progst.get("#names", {}).get(str(it["msg_id"])) or build_name(j, it)
            if nm in existing:
                continue
            todo.append(it)
        todo.sort(key=lambda x: x["size"])
        meter.total = len(todo)
        meter.done = 0
        log(j, "ROUND %d todo=%d" % (rnd, len(todo)))
        deferred = []
        for it in todo:
            try:
                r = await dl_one(j, ent, it, progst, meter)
            except Exception as ex:
                log(j, "ERR %s %s" % (it["msg_id"], type(ex).__name__))
                await get_client(j, force=True)
                r = "err"
            if r in ("ok", "exists"):
                meter.done += 1
            elif r in ("partial", "err"):
                deferred.append(it)
        if not deferred:
            log(j, "ALL DONE job=%s" % j["name"])
            await asyncio.sleep(120)
            continue
        log(j, "ROUND %d end deferred=%d -> retry pass" % (rnd, len(deferred)))
        items = deferred
        await asyncio.sleep(10)


# ---------------------------------------------------------------- CLI
def cmd_new(args):
    name = args.name
    j = {"name": name, "channel": norm_channel(args.channel), "out_dir": args.out,
         "selector": args.selector, "keywords": [k for k in (args.keywords or "").split(",") if k],
         "min_mb": args.min_mb, "max_mb": args.max_mb, "name_mode": args.name_mode,
         "workers": args.workers, "seg_mb": 2, "chunk_kb": 512, "ext": args.ext}
    p = os.path.join(JOBD, name + ".json")
    if os.path.exists(p) and not args.force:
        raise SystemExit("任务已存在: %s （加 --force 覆盖）" % p)
    if not os.path.isdir(j["out_dir"]):
        os.makedirs(j["out_dir"], exist_ok=True)
    jsave(p, j)
    print("已创建任务: %s" % p)
    print(json.dumps(j, ensure_ascii=False, indent=2))
    return j


def cmd_list(_args):
    rows = []
    for fn in sorted(os.listdir(JOBD)):
        if not fn.endswith(".json"):
            continue
        j = jload(os.path.join(JOBD, fn), {})
        if not j:
            continue
        idx = jload(os.path.join(SD, j.get("index_file", "")), {}) if j.get("index_file") else {}
        rows.append((fn, j.get("channel"), j.get("selector"), j.get("out_dir"),
                     len(idx.get("items") or []), round((idx.get("bytes") or 0) / 1073741824.0, 2)))
    if not rows:
        print("（还没有任务，用 python tgarchiver.py new 创建）")
        return
    print("%-22s %-24s %-9s %6s %8s  %s" % ("任务", "频道", "筛选", "条数", "GB", "输出目录"))
    for r in rows:
        print("%-22s %-24s %-9s %6d %8s  %s" % (r[0], str(r[1]), str(r[2]), r[4], r[5], r[3]))


async def cmd_probe(ch):
    j = {"name": "probe", "channel": norm_channel(ch), "out_dir": os.getcwd(),
         "speed_file": os.path.join(SD, "probe_speed.json"), "log_file": os.path.join(SD, "probe.log"),
         "selector": "all", "keywords": [], "min_mb": 0, "max_mb": 0, "name_mode": "orig",
         "workers": 1, "seg_mb": 2, "chunk_kb": 512, "ext": ".mp4",
         "progress_file": os.path.join(SD, "probe_progress.json"), "index_file": os.path.join(SD, "probe_index.json")}
    cl = await get_client(j)
    ent = await cl.get_entity(norm_channel(ch))
    full = await cl.get_messages(ent, limit=0)
    print("频道: %s" % getattr(ent, "title", "?"))
    print("标识: %s" % norm_channel(ch))
    print("id  : %s" % ent.id)
    print("消息: %s" % getattr(full, "total", "?"))
    print("公开: %s" % (not getattr(ent, "restricted", False)))


def main():
    config.require_credentials()
    ap = argparse.ArgumentParser(description="Telegram 频道批量归档器")
    sub = ap.add_subparsers(dest="cmd")

    n = sub.add_parser("new", help="新建下载任务")
    n.add_argument("--name", required=True)
    n.add_argument("--channel", required=True)
    n.add_argument("--out", required=True)
    n.add_argument("--selector", default="caption", choices=["caption", "keyword", "all"])
    n.add_argument("--keywords", default="")
    n.add_argument("--min-mb", type=float, default=0)
    n.add_argument("--max-mb", type=float, default=0)
    n.add_argument("--name-mode", default="orig", choices=["caption", "orig", "msgid"])
    n.add_argument("--workers", type=int, default=8)
    n.add_argument("--ext", default=".mp4")
    n.add_argument("--force", action="store_true")

    for c, h in (("index", "扫描频道生成索引"), ("run", "开始/继续下载")):
        p = sub.add_parser(c, help=h)
        p.add_argument("job")
        if c == "run":
            p.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件（试跑用）")
    sub.add_parser("list", help="列出任务")
    pr = sub.add_parser("probe", help="查看频道信息")
    pr.add_argument("channel")

    a = ap.parse_args()
    if a.cmd == "new":
        cmd_new(a)
    elif a.cmd == "list":
        cmd_list(a)
    elif a.cmd == "probe":
        asyncio.run(cmd_probe(a.channel))
    elif a.cmd == "index":
        asyncio.run(cmd_index(a.job))
    elif a.cmd == "run":
        asyncio.run(cmd_run(a.job, a.limit))
    else:
        ap.print_help()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
