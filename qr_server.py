# -*- coding: utf-8 -*-
"""本地登录页：二维码自动刷新 + 手机号/验证码/两步验证表单。只监听 127.0.0.1。

必须和 login.py 同时在跑：本页面只负责显示二维码、把输入写进文件，
真正的登录逻辑在 login.py。
"""
import http.server
import json
import os
import socketserver
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

SD = config.STATE_DIR
QR = os.path.join(SD, "qr.png")
STATUS = os.path.join(SD, "login_status.json")
PORT = config.PORT_LOGIN

PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram 登录</title><style>
:root{color-scheme:dark}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f1115;
 color:#e8e8ea;font-family:"HarmonyOS Sans SC","Noto Sans SC","Source Han Sans SC","Alibaba PuHuiTi",system-ui,sans-serif}
.wrap{display:flex;gap:26px;flex-wrap:wrap;justify-content:center;padding:26px}
.card{background:#171a21;border-radius:16px;padding:24px 26px;box-shadow:0 12px 40px rgba(0,0,0,.5)}
h1{font-size:18px;margin:0 0 4px;font-weight:600}
p{font-size:12.5px;color:#9aa0aa;margin:4px 0 14px;line-height:1.7}
img{width:380px;height:380px;image-rendering:pixelated;border-radius:10px;background:#fff;padding:6px;display:block}
.st{margin-top:12px;font-size:13.5px;color:#f0b429;text-align:center}
.st.ok{color:#5fd08a}
.st.err{color:#ef6461}
input{width:100%;box-sizing:border-box;margin:6px 0;padding:10px 12px;border-radius:9px;border:1px solid #2c313c;
 background:#0f1115;color:#e8e8ea;font-size:14px;outline:none}
input:focus{border-color:#3b82f6}
button{width:100%;margin-top:8px;padding:10px;border:0;border-radius:9px;background:#2563eb;color:#fff;
 font-size:14px;cursor:pointer}
button:hover{background:#1d4ed8}
button.ghost{background:#232833;color:#c9ced8}
button.ghost:hover{background:#2c3240}
.hd{font-size:12px;color:#707784;margin:16px 0 4px}
</style></head><body><div class="wrap">
<div class="card">
 <h1>扫码登录</h1>
 <p>手机 Telegram → 设置 → 设备 → 关联桌面设备 → 扫下面这张<br>图片每 2 秒自动刷新，随时扫都有效</p>
 <img id="q" src="/qr.png" alt="QR">
 <div class="st" id="s">等待扫码…</div>
</div>
<div class="card" style="width:330px">
 <h1>手机号登录</h1>
 <p>扫码不方便就走这里。验证码会发到你已登录的 Telegram 里。</p>
 <input id="phone" placeholder="***已脱敏***-手机号（含国家码）">
 <button onclick="post('/api/phone','phone')">发送验证码</button>
 <div class="hd">验证码</div>
 <input id="code" placeholder="12345">
 <button onclick="post('/api/code','code')">登录</button>
 <div class="hd">两步验证密码（若账号开启）</div>
 <input id="pwd" type="password" placeholder="两步验证密码">
 <button class="ghost" onclick="post('/api/pwd','pwd')">提交密码</button>
 <div class="st" id="s2"></div>
</div>
</div><script>
const MAP={WAITING:['等待扫码…',""],AUTHORIZED:['登录成功，可以关掉页面',"ok"],
 PHONE_CODE_SENT:['验证码已发送，请填入右侧',""],NEED_2FA:['需要两步验证密码',""],
 CODE_BAD:['验证码不正确',"err"],PWD_BAD:['密码不正确',"err"],TIMEOUT:['登录窗口已过期',"err"],
 CONNECTING:['正在连接 Telegram…',""],ERROR:['出错了，看控制台日志',"err"]};
setInterval(()=>{document.getElementById('q').src='/qr.png?_='+Date.now();},2000);
function poll(){fetch('/status').then(r=>r.json()).then(d=>{
  const m=MAP[d.state]||['状态：'+d.state,""];
  const el=document.getElementById('s');el.textContent=m[0]+'（更新于 '+d.t+'）';el.className='st '+m[1];
  const el2=document.getElementById('s2');el2.textContent=m[0];el2.className='st '+m[1];
}).catch(()=>{});}
setInterval(poll,1500);poll();
function post(u,id){const v=document.getElementById(id).value.trim();
 if(!v){alert('请先填写');return;}
 fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({v})})
  .then(r=>r.json()).then(d=>{if(!d.ok)alert(d.msg||'提交失败');else{document.getElementById(id).value='';}})
  .catch(e=>alert('提交失败'));}
</script></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif p == "/qr.png":
            try:
                with open(QR, "rb") as f:
                    self._send(200, "image/png", f.read())
            except Exception:
                self._send(404, "text/plain", b"no qr yet")
        elif p == "/status":
            try:
                with open(STATUS, encoding="utf-8") as f:
                    self._send(200, "application/json", f.read().encode("utf-8"))
            except Exception:
                self._send(200, "application/json", b'{"state":"UNKNOWN","t":"-","left":0}')
        else:
            self._send(404, "text/plain", b"404")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else "{}"
        try:
            v = json.loads(raw).get("v", "").strip()
        except Exception:
            v = ""
        name = {"/api/phone": "in_phone.txt",
                "/api/code": "in_code.txt",
                "/api/pwd": "in_pwd.txt"}.get(self.path)
        if not name:
            self._send(404, "application/json", b'{"ok":false,"msg":"bad path"}')
            return
        if not v:
            self._send(200, "application/json", b'{"ok":false,"msg":"empty"}')
            return
        try:
            os.makedirs(SD, exist_ok=True)
            with open(os.path.join(SD, name), "w", encoding="utf-8") as f:
                f.write(v)
            self._send(200, "application/json", b'{"ok":true}')
        except Exception as ex:
            self._send(500, "application/json",
                       ('{"ok":false,"msg":"%s"}' % type(ex).__name__).encode())


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    print("登录页: http://127.0.0.1:%d/" % PORT, flush=True)
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), H) as srv:
        srv.serve_forever()
