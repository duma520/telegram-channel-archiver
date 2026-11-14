# -*- coding: utf-8 -*-
"""proxypool.py —— MTProto 代理池（sqlite）。

设计要点
  * 去重键 key = scheme|host|port|secret，同一台机器换个端口算新条目
  * 见过的就更新 last_seen / seen_count，不重复测；失败过的按冷却时间再测
  * status: new（未测）/ alive（可用）/ dead（测过不通）/ unsupported（Telethon 用不了）/ invalid（secret 不合法）
  * 另记 sightings：哪个来源、哪条消息、什么时候见到，方便追溯
"""
import datetime
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

DB_DIR = os.path.join(config.STATE_DIR, "mtproxy")
DB_PATH = os.path.join(DB_DIR, "pool.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS proxies(
  key TEXT PRIMARY KEY,
  scheme TEXT NOT NULL,
  host TEXT NOT NULL,
  port INTEGER NOT NULL,
  secret TEXT DEFAULT '',
  tag TEXT DEFAULT '',
  sni TEXT DEFAULT '',
  user TEXT DEFAULT '',
  pass TEXT DEFAULT '',
  raw TEXT DEFAULT '',
  source TEXT DEFAULT '',
  status TEXT DEFAULT 'new',
  fail_count INTEGER DEFAULT 0,
  seen_count INTEGER DEFAULT 0,
  first_seen TEXT,
  last_seen TEXT,
  last_check TEXT,
  rtt_ms INTEGER,
  speed_bps INTEGER,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON proxies(status);
CREATE INDEX IF NOT EXISTS idx_host ON proxies(host);
CREATE TABLE IF NOT EXISTS sightings(
  key TEXT NOT NULL,
  source TEXT NOT NULL,
  msg_id INTEGER DEFAULT 0,
  seen_at TEXT,
  PRIMARY KEY(key, source, msg_id)
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

STATUS_ZH = {
    "new": "未测", "alive": "可用", "dead": "不通",
    "unsupported": "客户端专用", "invalid": "串不合法",
}


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect():
    os.makedirs(DB_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    cols = {r[1] for r in con.execute("PRAGMA table_info(proxies)")}
    if "sni" not in cols:  # 老库补列
        con.execute("ALTER TABLE proxies ADD COLUMN sni TEXT DEFAULT ''")
    return con


def upsert_many(items, source, msg_id=0, con=None):
    """写入一批解析结果。返回 (新增, 已存在, 不合法)。"""
    own = con is None
    con = con or connect()
    ts = now()
    added = known = bad = 0
    for e in items:
        if e.get("problem"):
            bad += 1
        key = e["key"]
        cur = con.execute("SELECT seen_count FROM proxies WHERE key=?", (key,))
        row = cur.fetchone()
        if row is None:
            con.execute(
                "INSERT INTO proxies(key,scheme,host,port,secret,tag,sni,user,pass,raw,source,status,"
                "fail_count,seen_count,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?)",
                (key, e["scheme"], e["host"], e["port"], e.get("secret", ""), e.get("tag", ""),
                 e.get("sni", ""), e.get("user", ""), e.get("pass", ""), e.get("raw", ""), source,
                 "invalid" if e.get("problem") else "new", ts, ts))
            con.execute("INSERT OR IGNORE INTO sightings(key,source,msg_id,seen_at) VALUES(?,?,?,?)",
                        (key, source, msg_id, ts))
            added += 1
        else:
            con.execute("UPDATE proxies SET seen_count=seen_count+1,last_seen=? WHERE key=?", (ts, key))
            con.execute("INSERT OR IGNORE INTO sightings(key,source,msg_id,seen_at) VALUES(?,?,?,?)",
                        (key, source, msg_id, ts))
            known += 1
    con.commit()
    if own:
        con.close()
    return added, known, bad


def mark(key, status, rtt_ms=None, note="", speed_bps=None, con=None):
    own = con is None
    con = con or connect()
    ts = now()
    if status == "dead":
        con.execute("UPDATE proxies SET status=?,last_check=?,fail_count=fail_count+1,note=? WHERE key=?",
                    (status, ts, (note or "")[:200], key))
    else:
        con.execute("UPDATE proxies SET status=?,last_check=?,rtt_ms=COALESCE(?,rtt_ms),note=?,"
                    "speed_bps=COALESCE(?,speed_bps) WHERE key=?",
                    (status, ts, rtt_ms, (note or "")[:200], speed_bps, key))
    con.commit()
    if own:
        con.close()


def rows(status=None, limit=0, order="status, rtt_ms"):
    con = connect()
    sql = "SELECT * FROM proxies"
    args = ()
    if status:
        sts = [s.strip() for s in status.split(",") if s.strip()]
        sql += " WHERE status IN (%s)" % ",".join("?" * len(sts))
        args = tuple(sts)
    sql += " ORDER BY " + order
    if limit:
        sql += " LIMIT %d" % int(limit)
    out = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return out


def due_for_check(cooldown_min=30, limit=0):
    """取该测的：new/invalid 从不跳过；dead 超过冷却时间再测；alive 不动。"""
    con = connect()
    cutoff = (datetime.datetime.now() - datetime.timedelta(minutes=cooldown_min)).strftime("%Y-%m-%d %H:%M:%S")
    sql = ("SELECT * FROM proxies WHERE status='new' "
           "OR (status='dead' AND (last_check IS NULL OR last_check<?)) "
           "ORDER BY (tag='dd') DESC, perr ASC LIMIT ?")
    sql = sql.replace("perr", "COALESCE(rtt_ms, 5000)")
    out = [dict(r) for r in con.execute(sql, (cutoff, limit or 100000)).fetchall()]
    con.close()
    return out


def stats():
    con = connect()
    out = {}
    for r in con.execute("SELECT status, COUNT(*) c FROM proxies GROUP BY status"):
        out[r["status"]] = r["c"]
    out["total"] = con.execute("SELECT COUNT(*) FROM proxies").fetchone()[0]
    out["sources"] = {r["source"]: r["c"] for r in
                      con.execute("SELECT source,COUNT(*) c FROM proxies GROUP BY source")}
    con.close()
    return out


def get_meta(k, default=""):
    con = connect()
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    con.close()
    return row["v"] if row else default


def set_meta(k, v):
    con = connect()
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, str(v)))
    con.commit()
    con.close()


def export_json(path, status="alive", limit=0):
    data = rows(status=status, limit=limit, order="COALESCE(rtt_ms,99999), speed_bps DESC")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return len(data)
