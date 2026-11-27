"""E0 环境预检。用法：
  python3 e0.py            只做本地检查（不调 API）
  python3 e0.py --api      另外实测 DeepSeek 结构化输出（需要环境变量 MODEL_API_KEY）
结果写到 runs/e0-<时间>/report.json，屏幕打印摘要。"""
import json, os, sqlite3, sys, tempfile, time, platform
from xin import db, engine
from xin.models import DeepSeekModel

def check(name, fn, rep):
    try:
        detail = fn()
        rep.append({"check": name, "ok": True, "detail": detail})
        print(f"[通过] {name}" + (f"：{detail}" if detail else ""))
    except Exception as e:
        rep.append({"check": name, "ok": False, "detail": f"{type(e).__name__}: {e}"})
        print(f"[失败] {name}：{type(e).__name__}: {e}")

def local(out, rep):
    d = tempfile.mkdtemp(dir=out)
    def py():
        assert sys.version_info >= (3, 9), sys.version
        return f"Python {platform.python_version()}，SQLite {sqlite3.sqlite_version}"
    check("Python 与 SQLite 版本", py, rep)
    def iso():
        p = os.path.join(d, "fresh.db")
        assert not os.path.exists(p)
        c = db.connect(p)
        n = sum(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("event_log", "states", "state_change_log", "conflict_link"))
        assert n == 0, "新库里有数据"
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal", mode
        return "独立新库为空，WAL 已开启"
    check("独立实验库与 WAL", iso, rep)
    def uniq():
        c = db.connect(os.path.join(d, "u.db"))
        c.execute("INSERT INTO event_log VALUES ('r','k','keep','{}',NULL,'t')")
        try:
            c.execute("INSERT INTO event_log VALUES ('r','k','keep','{}',NULL,'t')"); raise AssertionError("(run_id,event_unique_key) 唯一约束没生效")
        except sqlite3.IntegrityError: pass
        c.execute("INSERT INTO states VALUES ('A','e','k',1,'valid','c','t','[]')")
        try:
            c.execute("INSERT INTO states VALUES ('B','e','k',1,'valid','c','t','[]')"); raise AssertionError("(state_key,state_version) 唯一约束没生效")
        except sqlite3.IntegrityError: pass
    check("唯一约束", uniq, rep)
    def rollback():
        c = db.connect(os.path.join(d, "r.db"))
        c.execute("BEGIN")
        c.execute("INSERT INTO event_log VALUES ('r','k','keep','{}',NULL,'t')")
        c.execute("ROLLBACK")
        assert c.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 0
    check("事务回滚", rollback, rep)
    def snap():
        c = db.connect(os.path.join(d, "s.db"))
        c.execute("INSERT INTO event_log VALUES ('r','k','keep','{}',NULL,'t')")
        p = os.path.join(d, "s.snapshot.db"); db.snapshot(c, p)
        s = sqlite3.connect(p)
        assert s.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert s.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 1
        return "Backup API 快照可读、完整"
    check("一致性快照", snap, rep)
    def layer1():
        import io, contextlib, run_layer1
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run_layer1.main("fixtures/e1_cases.json", os.path.join(out, "layer1"))
        last = [l for l in buf.getvalue().splitlines() if l.startswith("第一层")]
        assert code == 0, buf.getvalue()
        return last[0] if last else ""
    check("最小事件处理链路（Mock 跑第一层）", layer1, rep)

def api(out, rep):
    p = json.load(open("e1_params.json", encoding="utf-8"))
    if not os.environ.get(p["api_key_env"]):
        print(f"[跳过] 没有环境变量 {p['api_key_env']}，不测 API"); return
    fx = json.load(open("fixtures/e1_cases.json", encoding="utf-8"))
    case = fx["cases"][1]            # c2：有已有 key 的用例，输出更有代表性
    step = case["steps"][0]
    payload = {"event": step["event"], "chat_context": step["chat_context"], "state_directory": step["expected_directory"],
               "evidence": {e["event_unique_key"]: e for e in case["initial_db"]["events"]}}
    schema = {"type": "json_schema", "json_schema": {"name": "b_layer", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["result", "candidates", "reason"],
        "properties": {"result": {"type": "string", "enum": ["keep", "write", "conflict"]}, "reason": {"type": "string"},
            "candidates": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["state_key", "is_new", "status_tag", "content", "conflict_with", "evidence_ref_list"],
                "properties": {"state_key": {"type": "string"}, "is_new": {"type": "boolean"},
                    "status_tag": {"type": "string", "enum": ["valid", "pending", "conflict"]}, "content": {"type": "string"},
                    "conflict_with": {"type": "array", "items": {"type": "string"}},
                    "evidence_ref_list": {"type": "array", "items": {"type": "string"}}}}}}}}}
    variants = [("json_schema", schema), ("json_object", {"type": "json_object"}), ("无约束", None)]
    for name, rf in variants:
        def probe(rf=rf):
            m = DeepSeekModel(dict(p, response_format=rf))
            t = time.time()
            body = m.body(payload)
            try:
                resp = m.raw(body)
            except Exception as e:
                msg = getattr(e, "read", lambda: b"")()
                raise RuntimeError(f"{e} {msg[:300].decode('utf-8','replace')}")
            ch = resp["choices"][0]; content = ch["message"].get("content") or ""
            usage = resp.get("usage", {})
            rec = {"秒": round(time.time() - t, 1), "finish_reason": ch.get("finish_reason"), "usage": usage,
                   "content": content[:1500], "reasoning_chars": len(ch["message"].get("reasoning_content") or "")}
            try:
                engine.validate(json.loads(content), payload["state_directory"], step["event"],
                                {e["event_unique_key"] for e in case["initial_db"]["events"]} | {step["event"]["event_unique_key"]})
                rec["校验"] = "通过"
            except Exception as e:
                rec["校验"] = f"不通过：{e}"
            json.dump(rec, open(os.path.join(out, f"api_{name}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            return f"{rec['秒']}s finish={rec['finish_reason']} 校验{rec['校验']}"
        check(f"API 结构化输出：{name}", probe, rep)

def main():
    out = os.path.join("runs", "e0-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    rep = []
    local(out, rep)
    if "--api" in sys.argv:
        api(out, rep)
    json.dump(rep, open(os.path.join(out, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = all(r["ok"] for r in rep if not r["check"].startswith("API"))
    print(f"\nE0 本地检查：{'全部通过' if ok else '有失败'}；报告：{out}/report.json")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
