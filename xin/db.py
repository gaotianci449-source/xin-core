"""SQLite 主库：表结构、连接、一致性快照。"""
import sqlite3, json, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_log (           -- 事件处理日志：成功处理路径必写
  run_id TEXT NOT NULL,
  event_unique_key TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('keep','write','conflict','seed')),
  payload TEXT NOT NULL,                          -- 事件原文（JSON），供证据回看
  reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (run_id, event_unique_key)
);
CREATE TABLE IF NOT EXISTS states (
  state_id TEXT PRIMARY KEY,
  target_event_id TEXT NOT NULL,
  state_key TEXT NOT NULL,
  state_version INTEGER NOT NULL,
  status_tag TEXT NOT NULL CHECK (status_tag IN ('valid','pending','conflict')),
  content TEXT NOT NULL,
  create_at TEXT NOT NULL,
  evidence_ref_list TEXT NOT NULL,                -- JSON 数组
  UNIQUE (state_key, state_version)
);
CREATE TABLE IF NOT EXISTS state_change_log (     -- 状态变更日志：按需写
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  event_unique_key TEXT NOT NULL,
  state_id TEXT NOT NULL REFERENCES states(state_id),
  change TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conflict_link (
  conflict_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  state_id_a TEXT NOT NULL REFERENCES states(state_id),
  state_id_b TEXT NOT NULL REFERENCES states(state_id),
  create_at TEXT NOT NULL,
  reason TEXT,
  CHECK (state_id_a <> state_id_b)
);
"""

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def connect(path):
    conn = sqlite3.connect(path, isolation_level=None)  # 手动 BEGIN/COMMIT
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn

def snapshot(conn, dest_path):
    """方案 A：SQLite Backup API 一致性快照。"""
    dest = sqlite3.connect(dest_path)
    try:
        conn.backup(dest)
    finally:
        dest.close()

def seed(conn, run_id, initial):
    """写入用例的初始数据库状态（一次事务）。"""
    conn.execute("BEGIN")
    try:
        for e in initial.get("events", []):
            conn.execute("INSERT INTO event_log VALUES (?,?,?,?,?,?)",
                         (run_id, e["event_unique_key"], "seed", json.dumps(e, ensure_ascii=False), "初始数据", now()))
        for s in initial.get("states", []):
            conn.execute("INSERT INTO states VALUES (?,?,?,?,?,?,?,?)",
                         (s["state_id"], s["target_event_id"], s["state_key"], s["state_version"], s["status_tag"],
                          s["content"], now(), json.dumps(s["evidence_ref_list"], ensure_ascii=False)))
        for c in initial.get("conflict_links", []):
            conn.execute("INSERT INTO conflict_link VALUES (?,?,?,?,?,?)",
                         (c["conflict_id"], run_id, c["state_id_a"], c["state_id_b"], now(), c.get("reason")))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
