# -*- coding: utf-8 -*-
"""环境自检：python / telethon / 状态目录 / 登录态 / .env，输出 JSON。"""
import json
import os
import sys


def find_home():
    env = os.environ.get("TGARCHIVER_HOME")
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for cand in (here, r"G:\Python_Code\telegram-channel-archiver"):
        if os.path.isfile(os.path.join(cand, "tgarchiver.py")):
            return cand
    return here


def main():
    home = find_home()
    out = {"home": home, "python": sys.version.split()[0], "ok": False}
    try:
        import telethon
        out["telethon"] = getattr(telethon, "__version__", "?")
    except Exception as e:  # noqa: BLE001
        out["telethon"] = None
        out["telethon_error"] = "%s: %s" % (type(e).__name__, str(e)[:80])
    # cryptg 是硬依赖：缺了 MTProto 会跑 pyaes 纯 Python AES-IGE，速度掉到 1/100
    try:
        from telethon.crypto import aes as _aes
        out["cryptg"] = bool(getattr(_aes, "cryptg", None) is not None)
        if out["cryptg"]:
            try:
                from importlib.metadata import version as _ver
                out["cryptg_version"] = _ver("cryptg")
            except Exception:
                out["cryptg_version"] = "unknown"
    except Exception as e:  # noqa: BLE001
        out["cryptg"] = None
        out["cryptg_error"] = "%s: %s" % (type(e).__name__, str(e)[:80])
    state = os.path.join(home, "state")
    out["state_dir"] = state
    out["state_exists"] = os.path.isdir(state)
    sess = os.path.join(state, "tg_session.session")
    out["session"] = os.path.isfile(sess)
    out["env_file"] = os.path.isfile(os.path.join(home, ".env"))
    try:
        sys.path.insert(0, home)
        import config
        out["api_configured"] = bool(config.API_ID and config.API_HASH)
        out["proxy"] = config.proxy_label()
    except SystemExit as e:
        out["api_configured"] = False
        out["config_error"] = str(e)[:120]
    except Exception as e:  # noqa: BLE001
        out["config_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    out["jobs"] = sorted(f[:-5] for f in os.listdir(os.path.join(state, "jobs"))
                         if f.endswith(".json")) if os.path.isdir(os.path.join(state, "jobs")) else []
    out["ok"] = bool(out.get("telethon") and out["session"] and out.get("api_configured"))
    # 能跑但没 cryptg 的，单独标出来：任务会跑得动，但只有 1/100 速度
    if out["ok"] and not out.get("cryptg"):
        out["warning"] = "缺 cryptg：下载速度会掉到 1/100。装：pip install cryptg -i https://pypi.org/simple"
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
