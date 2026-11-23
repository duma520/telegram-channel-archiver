# -*- coding: utf-8 -*-
"""台账 / 断点续传 / seg_mb 指纹门禁 端到端自测（v1.2.0 起）。

跑法： D:\\tg_fetch_venv\\Scripts\\python.exe scaffold\\t_ledger_e2e.py
不联网、不需要凭据：fetch_seg / get_client 都被桩替换，数据来自内存 BLOB。
三个场景必须全 PASS，任一失败即视为回归。
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tgarchiver as T

SIZE = 3 * 1024 * 1024 + 12345          # 故意不是整 MB，验证尾段
BLOB = (bytes((i * 37 + 11) % 256 for i in range(1024)) * (SIZE // 1024 + 2))[:SIZE]
EXPECT = hashlib.sha256(BLOB).hexdigest()


class Doc(object):
    pass


class Msg(object):
    def __init__(self):
        self.document = Doc()


class FakeCli(object):
    async def get_messages(self, ent, ids=None):
        return [Msg()]


class FakeEnt(object):
    pass


def make_env(seg_mb=1):
    tmp = tempfile.mkdtemp(prefix="tga_e2e_")
    out = os.path.join(tmp, "out")
    os.makedirs(out)
    j = {"name": "e2e", "channel": "@x", "out_dir": out, "selector": "all", "keywords": [],
         "min_mb": 0, "max_mb": 0, "name_mode": "orig", "workers": 4, "seg_mb": seg_mb,
         "chunk_kb": 512, "ext": ".mp4", "parts_dir_name": "__parts",
         "progress_file": os.path.join(tmp, "p.json"),
         "done_file": os.path.join(tmp, "d.json"),
         "log_file": os.path.join(tmp, "x.log"),
         "index_file": os.path.join(tmp, "i.json"),
         "speed_file": os.path.join(tmp, "s.json")}
    return tmp, out, j


IT = {"msg_id": 777, "size": SIZE, "title": "片", "year": 2024, "name": "", "caption": "片"}


async def main():
    async def fake_get_client(jj, force=False):
        return FakeCli()

    T.get_client = fake_get_client

    # ---------------- 场景 A：一次跑完，sha256 必须与源一致 ----------------
    tmp, out, j = make_env(seg_mb=1)
    calls = {"n": 0}

    async def full_fetch(jj, cl, m, off, ln, meter, tries=5):
        calls["n"] += 1
        meter.add(ln)
        return BLOB[off:off + ln]

    T.fetch_seg = full_fetch
    progst, donest = {}, {}
    r = await T.dl_one(j, FakeEnt(), IT, progst, T.Meter(), donest)
    nm = T.build_name(j, IT)
    got = hashlib.sha256(open(os.path.join(out, nm), "rb").read()).hexdigest()
    assert r == "ok", "A 应返回 ok，实得 %r" % r
    assert got == EXPECT, "A 产出文件内容错（期望 %s 实得 %s）" % (EXPECT, got)
    assert os.path.getsize(os.path.join(out, nm)) == SIZE
    d = json.load(open(j["done_file"], encoding="utf-8"))
    assert str(777) in d and d[str(777)]["size"] == SIZE, "A 台账没写对"
    assert progst.get("#names", {}).get("777") == nm, "A #names 没记"
    print("A PASS 一次跑完 sha256 一致，fetch 次数=%d，台账条目=%s" % (calls["n"], d[str(777)]["name"]))

    # ---------------- 场景 B：中途断掉，重启续上且 sha 一致 ----------------
    tmp2, out2, j2 = make_env(seg_mb=1)
    st = {"served": 0, "limit": 2}

    async def short_fetch(jj, cl, m, off, ln, meter, tries=5):
        if st["served"] >= st["limit"]:
            return None
        st["served"] += 1
        meter.add(ln)
        return BLOB[off:off + ln]

    T.fetch_seg = short_fetch
    progst2, donest2 = {}, {}
    r1 = await T.dl_one(j2, FakeEnt(), IT, progst2, T.Meter(), donest2)
    assert r1 == "partial", "B 第一轮应 partial，实得 %r" % r1
    done_before = set(progst2["777"]["done"])
    assert done_before, "B 断点前应有已完成段"
    st["limit"] = 10 ** 9
    T.fetch_seg = full_fetch
    r2 = await T.dl_one(j2, FakeEnt(), IT, progst2, T.Meter(), donest2)
    got2 = hashlib.sha256(open(os.path.join(out2, T.build_name(j2, IT)), "rb").read()).hexdigest()
    assert r2 == "ok", "B 第二轮应 ok，实得 %r" % r2
    assert got2 == EXPECT, "B 续传后 sha256 不对（说明拼接错位）"
    print("B PASS 断点续传 sha256 一致，断点前已完成 %d 段" % len(done_before))

    # ---------------- 场景 C：seg_mb 1→2，旧段必须全部作废 ----------------
    tmp3, out3, j3 = make_env(seg_mb=1)
    st3 = {"served": 0, "limit": 2}

    async def short3(jj, cl, m, off, ln, meter, tries=5):
        if st3["served"] >= st3["limit"]:
            return None
        st3["served"] += 1
        meter.add(ln)
        return BLOB[off:off + ln]

    T.fetch_seg = short3
    progst3, donest3 = {}, {}
    await T.dl_one(j3, FakeEnt(), IT, progst3, T.Meter(), donest3)
    n_before = len(progst3["777"]["done"])
    assert n_before > 0, "C 前置：应已下过若干段"

    j3["seg_mb"] = 2
    offsets = []

    async def spy(jj, cl, m, off, ln, meter, tries=5):
        offsets.append(off)
        meter.add(ln)
        return BLOB[off:off + ln]

    T.fetch_seg = spy
    r3 = await T.dl_one(j3, FakeEnt(), IT, progst3, T.Meter(), donest3)
    got3 = hashlib.sha256(open(os.path.join(out3, T.build_name(j3, IT)), "rb").read()).hexdigest()
    assert r3 == "ok", "C 应 ok，实得 %r" % r3
    assert got3 == EXPECT, "C 指纹门禁失效：拼出错位文件（sha 不符）"
    assert offsets and offsets[0] == 0, "C 旧段未作废，起始 offset=%r 应为 0" % (offsets[0] if offsets else None)
    print("C PASS seg_mb 1→2 旧段作废（起始 offset=0），重下后 sha256 一致")

    print("ALL_E2E_PASS")


if __name__ == "__main__":
    asyncio.run(main())
