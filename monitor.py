# -*- coding: utf-8 -*-
"""实时速度看板：http://127.0.0.1:47060/

速度单位按实际量级自动换算（B/s / KB/s / MB/s / GB/s），并按速度分级配色，
一眼就能看出代理线路好坏。数据来自下载器每 5 秒写的 telemetry JSON。

环境变量：
  TG_SPEED_FILE  遥测文件路径（默认 state/speed.json）
  TG_LOG_FILE    日志文件路径（默认 state/download.log）
  TG_PORT_MONITOR 端口（默认 47060）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SD = config.STATE_DIR
SPEED = os.environ.get("TG_SPEED_FILE") or os.path.join(SD, "speed.json")
LOG = os.environ.get("TG_LOG_FILE") or os.path.join(SD, "download.log")

TIERS = [
    (10 * 1024, "#ff5a5a", "很慢"),
    (300 * 1024, "#ff9f43", "偏慢"),
    (1024 * 1024, "#ffd54a", "一般"),
    (3 * 1024 * 1024, "#5ee08a", "良好"),
    (10 ** 18, "#35e0ff", "很快"),
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


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="2"><title>归档看板</title>
<style>
body{background:#14161a;color:#e6e6e6;margin:0;padding:22px;
 font:15px/1.6 "HarmonyOS Sans SC","Source Han Sans SC","Noto Sans SC","Alibaba PuHuiTi",system-ui,sans-serif}
h1{font-size:18px;margin:0 0 4px;color:#8ecbff;font-weight:600}
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
    max-height:400px;font-size:12.5px;color:#b9c2cf;white-space:pre-wrap}
</style></head><body>
<h1>Telegram 频道归档 · 实时看板</h1>
<div class="sub">单位按实际速度自动换算，颜色代表当前链路质量 · 任务 {{job}}</div>
<div class="big" style="color:{{color}}">{{speed}}<span class="tag" style="color:{{color}}">{{label}}</span></div>
<div class="legend">
<i style="background:#ff5a5a"></i>B/s 级
<i style="background:#ff9f43"></i>百 KB/s
<i style="background:#ffd54a"></i>接近 1 MB/s
<i style="background:#5ee08a"></i>MB/s 级
<i style="background:#35e0ff"></i>&gt;3 MB/s
</div>
<div class="bar"><span style="width:{{pct}}%;background:{{color}}"></span></div>
<div class="grid">
<div class="card"><div class="k">已完成 / 待下</div><div class="v">{{done}} / {{total}}</div></div>
<div class="card"><div class="k">本次运行累计</div><div class="v">{{sess}} MB</div></div>
<div class="card"><div class="k">失败重试队列</div><div class="v">{{fail}}</div></div>
<div class="card"><div class="k">代理出口</div><div class="v" style="font-size:13px;word-break:break-all">{{proxy}}</div></div>
<div class="card"><div class="k">当前文件</div><div class="v" style="font-size:13px">{{cur}}</div></div>
<div class="card"><div class="k">更新时间</div><div class="v" style="font-size:13px">{{ts}}</div></div>
</div>
<pre>{{log}}</pre>
</body></html>"""


def read(p, d):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return d


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        s = read(SPEED, {})
        try:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()[-26:]
        except Exception:
            lines = []
        bps = s.get("bps")
        if bps is None:
            bps = int(float(s.get("mbps") or 0) * 1024 * 1024)
        speed, color, label = fmt(bps)
        total = s.get("files_total") or 0
        done = s.get("files_done") or 0
        pct = (100.0 * done / total) if total else 0
        html = (PAGE.replace("{{job}}", str(s.get("job", "-")))
                    .replace("{{speed}}", speed).replace("{{color}}", color)
                    .replace("{{label}}", label).replace("{{pct}}", str(round(pct, 1)))
                    .replace("{{done}}", str(done)).replace("{{total}}", str(total))
                    .replace("{{sess}}", str(s.get("session_mb", 0)))
                    .replace("{{fail}}", str(s.get("fail", 0)))
                    .replace("{{proxy}}", str(s.get("proxy", "-")))
                    .replace("{{cur}}", str(s.get("current", "-") or "-"))
                    .replace("{{ts}}", str(s.get("ts", "-")))
                    .replace("{{log}}", "\n".join(lines) or "（暂无日志）"))
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = config.PORT_MONITOR
    print("看板已启动: http://127.0.0.1:%d/" % port, flush=True)
    print("遥测文件: %s" % SPEED, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
