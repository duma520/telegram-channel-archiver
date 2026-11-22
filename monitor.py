# -*- coding: utf-8 -*-
"""实时看板：http://127.0.0.1:47060/

四件事
  1) 实时速度：自适应单位（B/s ~ GB/s）+ 五级配色，2 秒轮询。用 JS 局部刷新，
     不用整页 reload —— 否则正在粘贴的代理链接会被刷掉。
  2) 粘贴代理：把 t.me/proxy|socks|webproxy 链接贴进来，自动解析、测活、入候选表。
  3) 选线路：候选表里点「用这条」就写进 state/proxy.json，下载器下次重连即生效；
     连不通的会被标 dead，下载器也会自动轮换到下一条。
  4) 加密后端提示：没装 cryptg 时速度会掉到 1/100，看板上直接标红。

环境变量：
  TG_SPEED_FILE   遥测文件路径（默认 state/speed.json）
  TG_LOG_FILE     日志文件路径（默认 state/download.log）
  TG_PORT_MONITOR 端口（默认 47060）
"""
import asyncio
import html
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import proxy_rotate
import proxylinks
import proxytest

SD = config.STATE_DIR
SPEED = os.environ.get("TG_SPEED_FILE") or os.path.join(SD, "speed.json")
LOG = os.environ.get("TG_LOG_FILE") or os.path.join(SD, "download.log")
PORT = int(os.environ.get("TG_PORT_MONITOR") or config.PORT_MONITOR)

TIERS = [
    (10 * 1024,          "#ff5a5a", "极慢 · 换线路"),
    (300 * 1024,         "#ff9f43", "偏慢 · 可换"),
    (1024 * 1024,        "#ffd54a", "一般"),
    (3 * 1024 * 1024,    "#5ee08a", "良好"),
    (10 ** 18,           "#35e0ff", "很快"),
]


def fmt(bps):
    """返回 (文本, 颜色, 质量标签)。"""
    if bps is None:
        return "0 B/s", TIERS[0][1], TIERS[0][2]
    if bps < 1024:
        txt = "%.0f B/s" % bps
    elif bps < 1024 ** 2:
        txt = "%.0f KB/s" % (bps / 1024.0)
    elif bps < 1024 ** 3:
        txt = "%.2f MB/s" % (bps / 1024.0 ** 2)
    else:
        txt = "%.2f GB/s" % (bps / 1024.0 ** 3)
    for lim, col, lbl in TIERS:
        if bps < lim:
            return txt, col, lbl
    return txt, TIERS[-1][1], TIERS[-1][2]


def crypto_note(v):
    """加密后端提示：没装 cryptg 时速度会掉到 1/100，必须在看板上说得明白。"""
    if not v:
        return "未知（遥测没有 crypto 字段）"
    if "cryptg" in v:
        return v + " ✓"
    return v + " ✗ 缺 cryptg，速度会掉到 1/100"


def read(p, d):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return d


def tail_log(n=26):
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except Exception:
        return []


def _run_async(coro):
    """在 POST 线程里跑一段 asyncio（看板是 ThreadingHTTPServer，不阻塞 GET）。"""
    try:
        return asyncio.run(coro)
    except Exception as ex:  # noqa: BLE001
        return "ERR %s: %s" % (type(ex).__name__, ex)


def test_candidates(keys=None):
    """测候选表里的条目，把结果写回状态。返回简短汇总文本。"""
    items = [it for it in proxy_rotate.candidates()
             if it["status"] in ("untested", "dead")
             and (not keys or it["key"] in keys)]
    if not items:
        return "没有需要测的条目"
    entries = [{"key": it["key"], "scheme": it["scheme"], "host": it["host"],
                "port": it["port"], "secret": it.get("secret") or "",
                "user": it.get("user") or "", "pass": it.get("pass") or ""}
               for it in items]

    def on_result(e, st, rtt, note):
        proxy_rotate.set_status(e["key"], st, note, rtt)

    _run_async(proxytest.test_batch(entries, on_result=on_result))
    allc = proxy_rotate.candidates()
    alive = [it for it in allc if it["status"] == "alive"]
    return "测试完成：可用 %d / 共 %d" % (len(alive), len(allc))


def status_obj():
    s = read(SPEED, {})
    bps = s.get("bps")
    if bps is None:
        bps = int(float(s.get("mbps") or 0) * 1024 * 1024)
    speed, color, label = fmt(bps)
    total = s.get("files_total") or 0
    done = s.get("files_done") or 0
    cur = proxy_rotate.current()
    cands = proxy_rotate.candidates()
    return {
        "speed": speed, "color": color, "label": label,
        "pct": round(100.0 * done / total, 1) if total else 0,
        "done": done, "total": total,
        "sess": s.get("session_mb", 0), "fail": s.get("fail", 0),
        "proxy": s.get("proxy") or proxy_rotate.label_of(cur) or "-",
        "cur": s.get("current") or "-", "ts": s.get("ts") or "-",
        "job": s.get("job") or "-",
        "crypto": crypto_note(s.get("crypto") or ""),
        "log": tail_log(),
        "cands": [{
            "key": it["key"], "label": proxy_rotate.label_of(proxy_rotate.entry_to_obj(it)),
            "status": it["status"], "zh": proxy_rotate.STATUS_ZH.get(it["status"], it["status"]),
            "rtt": it.get("rtt_ms"), "note": (it.get("note") or "")[:90],
        } for it in cands],
    }


def _ok_badge(st):
    cls = {"alive": "s-alive", "dead": "s-dead", "untested": "s-untested"}.get(st, "s-unsupported")
    return '<span class="%s">%s</span>' % (cls, html.escape(proxy_rotate.STATUS_ZH.get(st, st)))


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>归档看板</title>
<style>
body{background:#14161a;color:#e6e6e6;margin:0;padding:22px;
 font:15px/1.6 "HarmonyOS Sans SC","Source Han Sans SC","Noto Sans SC","Alibaba PuHuiTi",system-ui,sans-serif}
h1{font-size:18px;margin:0 0 4px;color:#8ecbff;font-weight:600}
h2{font-size:15px;margin:26px 0 8px;color:#9fb0c6;font-weight:600}
.sub{color:#7d8797;font-size:12.5px;margin-bottom:16px}
.big{font-size:52px;font-weight:700;letter-spacing:1px;font-variant-numeric:tabular-nums;line-height:1.1}
.tag{display:inline-block;margin-left:12px;font-size:14px;padding:3px 10px;border-radius:20px;
     background:#20242b;border:1px solid #2f3540;vertical-align:middle}
.legend{margin:10px 0 4px;font-size:12.5px;color:#8b95a5}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 5px 0 14px}
.bar{height:12px;background:#23272e;border-radius:7px;overflow:hidden;margin:12px 0 18px}
.bar>span{display:block;height:100%;border-radius:7px;transition:width .4s}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px;margin-bottom:16px}
.card{background:#1c1f25;border:1px solid #2a2f38;border-radius:10px;padding:11px 13px}
.k{color:#8b95a5;font-size:12px}.v{font-size:17px;margin-top:3px;font-variant-numeric:tabular-nums}
pre{background:#1c1f25;border:1px solid #2a2f38;border-radius:10px;padding:12px;overflow:auto;
    max-height:340px;font-size:12.5px;color:#b9c2cf;white-space:pre-wrap}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #262b33}
th{color:#8b95a5;font-weight:500;font-size:12px}
tr:hover td{background:#1b1e24}
.s-alive{color:#5ee08a}.s-dead{color:#ff7a7a}.s-unsupported{color:#8b95a5}.s-untested{color:#ffd54a}
textarea{width:100%;min-height:88px;background:#1c1f25;border:1px solid #2f3540;border-radius:8px;
         color:#e6e6e6;font:13px/1.5 Consolas,monospace;padding:10px;box-sizing:border-box;resize:vertical}
textarea:focus{outline:none;border-color:#4a90d9}
button{background:#2b6cb0;border:0;color:#fff;padding:7px 15px;border-radius:7px;font-size:13px;cursor:pointer}
button:hover{background:#3a7cc4}
button.ghost{background:#262b33;border:1px solid #333a45;color:#b9c2cf;padding:5px 11px;font-size:12px}
button.ghost:hover{background:#2f3540}
.hint{color:#7d8797;font-size:12px;margin:6px 0 10px}
.cur{background:#17301f;border:1px solid #2c5138;border-radius:8px;padding:9px 12px;
     font-size:13px;color:#9be8b4;margin-bottom:10px}
.crypto-bad{color:#ff9f43}
</style></head><body>
<h1>Telegram 频道归档 · 实时看板</h1>
<div class="sub">单位按实际速度自动换算，颜色代表链路质量 · 任务 <b id="job">{{job}}</b></div>
<div class="big" style="color:{{color}}"><span id="sp">{{speed}}</span><span class="tag" id="lb" style="color:{{color}}">{{label}}</span></div>
<div class="legend">
<i style="background:#ff5a5a"></i>B/s 级（&lt;10 KB/s 极慢）
<i style="background:#ff9f43"></i>百 KB/s（偏慢）
<i style="background:#ffd54a"></i>接近 1 MB/s（一般）
<i style="background:#5ee08a"></i>MB/s 级（良好）
<i style="background:#35e0ff"></i>&gt;3 MB/s（很快）
</div>
<div class="bar"><span id="bar" style="width:{{pct}}%;background:{{color}}"></span></div>
<div class="grid">
<div class="card"><div class="k">已完成 / 待下</div><div class="v"><span id="done">{{done}}</span> / <span id="total">{{total}}</span></div></div>
<div class="card"><div class="k">本次运行累计</div><div class="v"><span id="sess">{{sess}}</span> MB</div></div>
<div class="card"><div class="k">失败重试队列</div><div class="v" id="fail">{{fail}}</div></div>
<div class="card"><div class="k">当前线路</div><div class="v" id="proxy" style="font-size:14px">{{proxy}}</div></div>
<div class="card"><div class="k">加密后端</div><div class="v" id="crypto" style="font-size:14px">{{crypto}}</div></div>
</div>
<div class="card" style="margin-bottom:16px"><div class="k">当前文件</div><div class="v" id="cur" style="font-size:14px">{{cur}}</div></div>

<h2>粘贴代理（每行一条）</h2>
<div class="hint">支持 t.me/proxy?… 、t.me/socks?… 、tg://proxy?… 、socks5://user:pass@host:port 。
贴进来点「添加并测活」，测通的会标成「可用」，然后点「用这条」切换线路。</div>
<form method="post" action="/add">
<textarea name="links" placeholder="https://t.me/proxy?server=1.2.3.4&amp;port=443&amp;secret=dd00112233445566778899aabbccddeeff&#10;socks5://user:pass@5.6.7.8:1080"></textarea>
<div style="margin-top:10px">
<button type="submit">添加并测活</button>
<button class="ghost" type="submit" formaction="/test">只测已有候选</button>
</div>
</form>

<h2>候选线路</h2>
<div class="cur" id="curl">当前生效：<b>{{proxy}}</b></div>
<table><thead><tr><th>线路</th><th>状态</th><th>RTT</th><th>备注</th><th></th></tr></thead>
<tbody id="cbody">{{rows}}</tbody></table>

<h2>最近日志</h2>
<pre id="log">{{log}}</pre>

<script>
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){
 return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
function row(c){
 var btn = c.status==="alive" ? '<button class="ghost" onclick="useKey(this)" data-k="'+esc(c.key)+'">用这条</button>' : "";
 var zh = '<span class="'+({alive:"s-alive",dead:"s-dead",untested:"s-untested"}[c.status]||"s-unsupported")+'">'+esc(c.zh)+'</span>';
 return "<tr><td>"+esc(c.label)+"</td><td>"+zh+"</td><td>"+(c.rtt==null?"-":c.rtt+" ms")+
        "</td><td>"+esc(c.note||"")+"</td><td>"+btn+"</td></tr>";
}
function tick(){
 fetch("/api/status").then(function(r){return r.json();}).then(function(d){
   document.getElementById("sp").textContent=d.speed;
   document.getElementById("sp").style.color=d.color;
   var lb=document.getElementById("lb"); lb.textContent=d.label; lb.style.color=d.color;
   var bar=document.getElementById("bar"); bar.style.width=d.pct+"%"; bar.style.background=d.color;
   document.getElementById("done").textContent=d.done;
   document.getElementById("total").textContent=d.total;
   document.getElementById("sess").textContent=d.sess;
   document.getElementById("fail").textContent=d.fail;
   document.getElementById("proxy").textContent=d.proxy;
   document.getElementById("cur").textContent=d.cur;
   document.getElementById("job").textContent=d.job;
   var cr=document.getElementById("crypto");
   cr.textContent=d.crypto;
   cr.className="v"+(d.crypto.indexOf("✗")>=0?" crypto-bad":"");
   document.getElementById("curl").innerHTML="当前生效：<b>"+esc(d.proxy)+"</b>";
   document.getElementById("cbody").innerHTML=d.cands.map(row).join("");
   document.getElementById("log").textContent=(d.log||[]).join("\n");
 }).catch(function(){});
}
function useKey(el){
 var k=el.getAttribute("data-k");
 var f=document.createElement("form"); f.method="post"; f.action="/use";
 var i=document.createElement("input"); i.type="hidden"; i.name="key"; i.value=k;
 f.appendChild(i); document.body.appendChild(f); f.submit();
}
setInterval(tick,2000); tick();
</script>
</body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def _redirect(self, msg, to="/"):
        # Location 头只能是 ASCII：中文必须 percent-encode，否则 http.server 抛
        # UnicodeEncodeError，客户端看到的是 "Empty reply from server"。
        from urllib.parse import quote
        self.send_response(303)
        self.send_header("Location", "%s?m=%s" % (to, quote(msg)))
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/status":
            return self._send(json.dumps(status_obj(), ensure_ascii=False),
                              ctype="application/json; charset=utf-8")
        if u.path not in ("/", "/index.html"):
            return self._send("not found", 404)
        st = status_obj()
        rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                html.escape(c["label"]), _ok_badge(c["status"]),
                "-" if c["rtt"] is None else "%d ms" % c["rtt"],
                html.escape(c["note"] or ""),
                ('<button class="ghost" onclick="useKey(this)" data-k="%s">用这条</button>'
                 % html.escape(c["key"])) if c["status"] == "alive" else "",
            ) for c in st["cands"])
        page = (PAGE
                .replace("{{color}}", st["color"])
                .replace("{{speed}}", html.escape(st["speed"]))
                .replace("{{label}}", html.escape(st["label"]))
                .replace("{{pct}}", str(st["pct"]))
                .replace("{{done}}", str(st["done"]))
                .replace("{{total}}", str(st["total"]))
                .replace("{{sess}}", str(st["sess"]))
                .replace("{{fail}}", str(st["fail"]))
                .replace("{{proxy}}", html.escape(str(st["proxy"])))
                .replace("{{cur}}", html.escape(str(st["cur"])))
                .replace("{{job}}", html.escape(str(st["job"])))
                .replace("{{crypto}}", html.escape(str(st["crypto"])))
                .replace("{{rows}}", rows)
                .replace("{{log}}", html.escape("\n".join(st["log"]))))
        m = parse_qs(u.query).get("m")
        if m:
            page = page.replace("<h2>粘贴代理", '<div class="cur">%s</div><h2>粘贴代理' % html.escape(m[0]), 1)
        return self._send(page)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        form = parse_qs(raw)
        u = urlparse(self.path)
        if u.path == "/add":
            text = (form.get("links") or [""])[0]
            added, dup, unsup = proxy_rotate.add_text(text, source="看板粘贴")
            msg = "新增 %d 条，重复 %d 条" % (added, dup)
            if unsup:
                msg += "；不支持 %d 条" % len(unsup)
            if added:
                msg += "。" + test_candidates()
            cur = proxy_rotate.current()
            if added and not _cur_is_alive(cur):
                it, obj = proxy_rotate.rotate()
                if it:
                    msg += "；已自动切到 %s" % proxy_rotate.label_of(obj)
            return self._redirect(msg)
        if u.path == "/test":
            return self._redirect(test_candidates())
        if u.path == "/use":
            key = (form.get("key") or [""])[0]
            ok, obj = proxy_rotate.apply(key, note="看板手选 %s" % time.strftime("%m-%d %H:%M"))
            if not ok:
                return self._redirect("没找到这条线路")
            return self._redirect("已切换，下载器下次重连生效")
        return self._send("not found", 404)


def _cur_is_alive(cur):
    if not cur:
        return False
    for it in proxy_rotate.candidates():
        if (it["host"] == cur.get("host") and int(it["port"]) == int(cur.get("port") or 0)
                and (it.get("user") or "") == (cur.get("user") or "")):
            return it["status"] == "alive"
    return True  # 不在候选表里（比如手改的），不擅自替换


if __name__ == "__main__":
    print("看板: http://127.0.0.1:%d/  （Ctrl+C 停止）" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
