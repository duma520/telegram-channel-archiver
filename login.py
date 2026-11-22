# -*- coding: utf-8 -*-
"""登录：二维码自动重建 + 手机号/验证码/两步验证兜底。

跑起来之后：
  * 控制台会打印登录页地址（默认 http://127.0.0.1:47050/）
  * 用手机 Telegram 扫码即可；二维码每 18 秒自动换新，随时扫都有效
  * 扫码不方便就走页面右侧的手机号登录
  * 登录成功后 session 存在 state/tg_session.session，之后下载/索引都复用它

所有异常按单步捕获：连接掉线自动重连，不会因为一次网络抖动就退出。
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

SD = config.STATE_DIR
SESS = config.SESSION
QRPNG = os.path.join(SD, "qr.png")
STATUS = os.path.join(SD, "login_status.json")
F_PHONE = os.path.join(SD, "in_phone.txt")
F_CODE = os.path.join(SD, "in_code.txt")
F_PWD = os.path.join(SD, "in_pwd.txt")
WINDOW = 7200  # 登录窗口 2 小时

os.makedirs(SD, exist_ok=True)
DEADLINE = time.time() + WINDOW


def save_status(st, extra=None):
    d = {"state": st, "t": time.strftime("%Y-%m-%d %H:%M:%S"),
         "left": max(0, int(DEADLINE - time.time()))}
    if extra:
        d.update(extra)
    try:
        with open(STATUS, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def take(path):
    try:
        with open(path, encoding="utf-8") as f:
            v = f.read().strip()
        os.remove(path)
        return v or None
    except Exception:
        return None


def make_qr(url, tag=""):
    import qrcode
    img = qrcode.make(url).resize((480, 480))
    img.save(QRPNG)
    print("QR %s %s" % (tag, time.strftime("%H:%M:%S")), flush=True)


def new_client():
    px, conn = config.proxy_and_conn()
    kw = {"connection": conn} if conn else {}
    return TelegramClient(SESS, config.API_ID, config.API_HASH,
                          proxy=px,
                          connection_retries=None, retry_delay=1, timeout=20,
                          request_retries=8, auto_reconnect=True, **kw)


async def main():
    client = new_client()
    qr = None
    last_new = 0.0
    while time.time() < DEADLINE:
        try:
            if not client.is_connected():
                await client.connect()
                await asyncio.sleep(0.2)
            if await client.is_user_authorized():
                me = await client.get_me()
                save_status("AUTHORIZED", {"uid": me.id,
                                           "name": me.username or me.first_name or ""})
                print("AUTHORIZED uid=%s" % me.id, flush=True)
                await client.disconnect()
                return 0
        except Exception as ex:
            print("connect: %s %s" % (type(ex).__name__, str(ex)[:60]), flush=True)
            save_status("CONNECTING")
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(2)
            continue

        handled = False
        ph = take(F_PHONE)
        if ph:
            handled = True
            try:
                await client.send_code_request(ph)
                print("code sent", flush=True)
                save_status("PHONE_CODE_SENT")
            except Exception as ex:
                print("send_code: %s %s" % (type(ex).__name__, str(ex)[:60]), flush=True)
                save_status("ERROR", {"msg": type(ex).__name__})
        code = take(F_CODE)
        if code:
            handled = True
            try:
                await client.sign_in(code=code)
                print("code ok", flush=True)
            except SessionPasswordNeededError:
                save_status("NEED_2FA")
            except Exception as ex:
                print("sign_in code: %s" % type(ex).__name__, flush=True)
                save_status("CODE_BAD", {"msg": type(ex).__name__})
        pwd = take(F_PWD)
        if pwd:
            handled = True
            try:
                await client.sign_in(password=pwd)
                print("2fa ok", flush=True)
            except Exception as ex:
                print("sign_in pwd: %s" % type(ex).__name__, flush=True)
                save_status("PWD_BAD", {"msg": type(ex).__name__})
        if handled:
            await asyncio.sleep(0.5)
            continue

        try:
            if qr is None:
                qr = await client.qr_login()
                make_qr(qr.url, "new")
                last_new = time.time()
                save_status("WAITING")
            elif time.time() - last_new > 18:
                await qr.recreate()
                make_qr(qr.url, "renew")
                last_new = time.time()
                save_status("WAITING")
        except Exception as ex:
            print("qr init: %s %s" % (type(ex).__name__, str(ex)[:60]), flush=True)
            qr = None
            await asyncio.sleep(1)
            continue

        try:
            await qr.wait(timeout=5)
            print("scanned", flush=True)
            continue
        except asyncio.TimeoutError:
            pass
        except SessionPasswordNeededError:
            print("need 2fa", flush=True)
            save_status("NEED_2FA")
            qr = None
        except Exception as ex:
            print("qr wait: %s %s" % (type(ex).__name__, str(ex)[:60]), flush=True)
            await asyncio.sleep(1)

    save_status("TIMEOUT")
    print("登录窗口已过期，重新运行即可", flush=True)
    return 3


if __name__ == "__main__":
    config.require_credentials()
    print("登录页: http://127.0.0.1:%d/  （另开一个窗口跑 python qr_server.py）"
          % config.PORT_LOGIN, flush=True)
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
