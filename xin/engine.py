"""事件处理：目录组装、模型输出校验、重试、短事务落盘。按 E1 执行契约实现。"""
import json, re, uuid
from .db import now

RESULTS = ("keep", "write", "conflict", "fail")
KEY_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


class InvalidOutput(Exception):
    pass


class FailLog:
    """外部运行日志（JSONL），与主库分离；不作为成功凭证。"""
    def __init__(self, path):
        self.path = path

    def write(self, **rec):
        rec["at"] = now()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def entries(self, event_key=None):
        try:
            with open(self.path, encoding="utf-8") as f:
                rows = [json.loads(l) for l in f if l.strip()]
        except FileNotFoundError:
            return []
        return [r for r in rows if event_key is None or r.get("event_unique_key") == event_key]


# ---------- 读 ----------
def processed(conn, run_id, key):
    return conn.execute("SELECT 1 FROM event_log WHERE run_id=? AND event_unique_key=?", (run_id, key)).fetchone() is not None


def directory(conn):
    """从当前 run 数据库组装状态目录。E1 规模小，给全量。"""
    states = [dict(r) for r in conn.execute("SELECT * FROM states ORDER BY state_key, state_version")]
    for s in states:
        s["evidence_ref_list"] = json.loads(s["evidence_ref_list"])
        s.pop("create_at", None)
    links = [dict(r) for r in conn.execute("SELECT conflict_id, state_id_a, state_id_b, reason FROM conflict_link")]
    bad = {x for l in links for x in (l["state_id_a"], l["state_id_b"])}
    cur = {}
    for s in states:
        if s["status_tag"] == "valid" and s["state_id"] not in bad and s["state_version"] > cur.get(s["state_key"], 0):
            cur[s["state_key"]] = s["state_version"]
    return {"states": states, "conflict_links": links, "current_valid": cur}


def evidence_text(conn, run_id, refs):
    out = {}
    for r in refs:
        row = conn.execute("SELECT payload FROM event_log WHERE run_id=? AND event_unique_key=?", (run_id, r)).fetchone()
        if row:
            out[r] = json.loads(row["payload"])
    return out


def build_input(conn, run_id, event, chat_context, dir_):
    """模型输入四块：当前事件、聊天上下文、状态目录、证据原文。"""
    refs = sorted({r for s in dir_["states"] for r in s["evidence_ref_list"]})
    return {"event": event, "chat_context": chat_context, "state_directory": dir_,
            "evidence": evidence_text(conn, run_id, refs)}


# ---------- 校验 ----------
def validate(out, dir_, event, known_events):
    def bad(msg):
        raise InvalidOutput(msg)
    if not isinstance(out, dict):
        bad("输出不是 JSON 对象")
    for f in ("result", "candidates", "reason"):
        if f not in out:
            bad(f"缺字段 {f}")
    res, cands = out["result"], out["candidates"]
    if res not in RESULTS:
        bad(f"result 越界: {res}")
    if not isinstance(cands, list):
        bad("candidates 不是数组")
    if not isinstance(out["reason"], str):
        bad("reason 不是字符串")
    if res == "fail":
        bad("模型自报 fail")
    if res == "keep" and cands:
        bad("keep 不应带候选")
    if res in ("write", "conflict") and not cands:
        bad(f"{res} 至少要一个候选")
    keys = {s["state_key"] for s in dir_["states"]}
    ids = {s["state_id"] for s in dir_["states"]}
    for i, c in enumerate(cands):
        if not isinstance(c, dict):
            bad(f"候选 {i} 不是对象")
        if "state_version" in c or "state_id" in c:
            bad(f"候选 {i} 不得指定 state_version/state_id")
        for f, t in (("state_key", str), ("is_new", bool), ("status_tag", str), ("content", str),
                     ("conflict_with", list), ("evidence_ref_list", list)):
            if not isinstance(c.get(f), t):
                bad(f"候选 {i} 字段 {f} 缺失或类型错")
        if not KEY_RE.match(c["state_key"]):
            bad(f"候选 {i} state_key 格式非法: {c['state_key']}")
        if c["is_new"] and c["state_key"] in keys:
            bad(f"候选 {i} 标为新建，但 {c['state_key']} 已在目录中")
        if not c["is_new"] and c["state_key"] not in keys:
            bad(f"候选 {i} 标为已有，但 {c['state_key']} 不在目录中")
        if not c["content"].strip():
            bad(f"候选 {i} content 为空")
        allowed = {"valid", "pending"} if res == "write" else {"conflict"}
        if c["status_tag"] not in allowed:
            bad(f"候选 {i} status_tag={c['status_tag']} 不符合 result={res}")
        if res == "write" and c["conflict_with"]:
            bad(f"候选 {i}：write 不应带 conflict_with")
        if res == "conflict" and not c["conflict_with"]:
            bad(f"候选 {i}：conflict 必须指明冲突对象")
        for t in c["conflict_with"]:
            if isinstance(t, str) and t.startswith("#"):
                j = t[1:]
                if not j.isdigit() or int(j) == i or int(j) >= len(cands):
                    bad(f"候选 {i} 冲突引用 {t} 非法")
            elif t not in ids:
                bad(f"候选 {i} 冲突对象 {t} 不在目录中")
        ev = c["evidence_ref_list"]
        if event["event_unique_key"] not in ev:
            bad(f"候选 {i} evidence_ref_list 未包含当前事件")
        for r in ev:
            if r not in known_events:
                bad(f"候选 {i} 证据 {r} 不存在")
    return out


# ---------- 处理一条事件 ----------
def process_event(conn, run_id, event, chat_context, model, faillog, max_attempts=3, hook=None):
    """返回 dict：status=duplicate|done|fail，及 attempts、result。hook 仅供测试在事务内注入异常。"""
    key = event["event_unique_key"]
    for f in ("event_unique_key", "sender", "time", "session", "text"):
        if not isinstance(event.get(f), str) or not event.get(f):
            faillog.write(run_id=run_id, event_unique_key=key, stage="input", error=f"字段 {f} 缺失")
            return {"status": "fail", "attempts": 0}
    if processed(conn, run_id, key):
        return {"status": "duplicate", "attempts": 0}

    dir_ = directory(conn)
    known = {r["event_unique_key"] for r in conn.execute("SELECT event_unique_key FROM event_log WHERE run_id=?", (run_id,))}
    known.add(key)
    payload = build_input(conn, run_id, event, chat_context, dir_)
    out, attempts = None, 0
    while attempts < max_attempts:
        attempts += 1
        try:
            raw = model.call(payload)
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as e:
                raise InvalidOutput(f"JSON 解析失败: {e}")
            out = validate(parsed, dir_, event, known)
            break
        except InvalidOutput as e:
            faillog.write(run_id=run_id, event_unique_key=key, attempt=attempts, stage="validate", error=str(e))
        except Exception as e:  # 超时、网络等
            faillog.write(run_id=run_id, event_unique_key=key, attempt=attempts, stage="model", error=f"{type(e).__name__}: {e}")
    if out is None:
        faillog.write(run_id=run_id, event_unique_key=key, stage="give_up", attempts=attempts, error="达到最大尝试次数")
        return {"status": "fail", "attempts": attempts}

    try:
        conn.execute("BEGIN IMMEDIATE")
        if processed(conn, run_id, key):          # 事务内最终幂等检查
            conn.execute("ROLLBACK")
            return {"status": "duplicate", "attempts": attempts}
        conn.execute("INSERT INTO event_log VALUES (?,?,?,?,?,?)",
                     (run_id, key, out["result"], json.dumps(event, ensure_ascii=False), out["reason"], now()))
        new_ids = []
        for c in out["candidates"]:
            ver = conn.execute("SELECT COALESCE(MAX(state_version),0)+1 FROM states WHERE state_key=?",
                               (c["state_key"],)).fetchone()[0]
            sid = "S-" + uuid.uuid4().hex[:12]
            conn.execute("INSERT INTO states VALUES (?,?,?,?,?,?,?,?)",
                         (sid, key, c["state_key"], ver, c["status_tag"], c["content"], now(),
                          json.dumps(c["evidence_ref_list"], ensure_ascii=False)))
            conn.execute("INSERT INTO state_change_log (run_id,event_unique_key,state_id,change,created_at) VALUES (?,?,?,?,?)",
                         (run_id, key, sid, f"new {c['status_tag']} v{ver}", now()))
            new_ids.append(sid)
        if hook:
            hook("after_states")
        seen = set()
        for i, c in enumerate(out["candidates"]):
            for t in c["conflict_with"]:
                other = new_ids[int(t[1:])] if t.startswith("#") else t
                pair = tuple(sorted((new_ids[i], other)))
                if pair in seen:
                    continue
                seen.add(pair)
                conn.execute("INSERT INTO conflict_link VALUES (?,?,?,?,?,?)",
                             ("C-" + uuid.uuid4().hex[:12], run_id, other, new_ids[i], now(), out["reason"]))
        conn.execute("COMMIT")
        return {"status": "done", "attempts": attempts, "result": out["result"], "new_state_ids": new_ids}
    except Exception as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        faillog.write(run_id=run_id, event_unique_key=key, stage="transaction", error=f"{type(e).__name__}: {e}")
        return {"status": "fail", "attempts": attempts, "rolled_back": True}
