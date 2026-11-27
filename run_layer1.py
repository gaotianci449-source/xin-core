"""第一层：程序机制验收（Mock 模型）。用法：python3 run_layer1.py [fixtures/e1_cases.json] [输出目录]"""
import json, os, sys, tempfile
from xin import db, engine
from xin.models import MockModel, InjectedAbort

def main(fx_path="fixtures/e1_cases.json", out_dir=None):
    fx = json.load(open(fx_path, encoding="utf-8"))
    out_dir = out_dir or tempfile.mkdtemp(prefix="xin-l1-")
    os.makedirs(out_dir, exist_ok=True)
    total, failed = 0, []
    for case in fx["cases"]:
        run_id = f"{fx['fixture_id']}:L1:{case['case_id']}"
        path = os.path.join(out_dir, case["case_id"] + ".db")
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(path + suf):
                os.remove(path + suf)
        conn = db.connect(path)
        flog = engine.FailLog(os.path.join(out_dir, case["case_id"] + ".fail.jsonl"))
        open(flog.path, "w").close()
        db.seed(conn, run_id, case["initial_db"])
        names = {}                       # $NEW1 -> 实际 key
        exp_states = [dict(s) for s in case["initial_db"]["states"]]
        exp_links = [dict(c) for c in case["initial_db"]["conflict_links"]]
        results = {}
        for step in case["steps"]:
            total += 1
            errs = []
            exp = step["expect"]
            def chk(cond, msg):
                if not cond:
                    errs.append(msg)
            # 1. 目录比对（实际从库组装 vs 预期）
            actual_dir = engine.directory(conn)
            if "resubmit_of" not in step:
                exp_dir = step.get("expected_directory")
                if exp_dir == "from_db" or exp_dir is None:
                    exp_dir = expected_directory(exp_states, exp_links)
                d = compare_dir(actual_dir, exp_dir, names)
                if d:
                    errs.append("前置目录不一致: " + d)
            # 2. 处理
            inject = list(step.get("inject", []))
            hook = None
            if "abort_inside_transaction" in inject:
                inject.remove("abort_inside_transaction")
                def hook(where):
                    raise InjectedAbort("注入：事务内异常")
            model = MockModel(step.get("mock_output"), inject)
            before = row_counts(conn)
            n_fail_before = len(flog.entries(step["event"]["event_unique_key"]))
            r = engine.process_event(conn, run_id, step["event"], step.get("chat_context", []), model, flog,
                                     fx["max_attempts"], hook)
            after = row_counts(conn)
            new_fail = len(flog.entries(step["event"]["event_unique_key"])) - n_fail_before
            # 3. 断言
            if "model_called" in exp:
                chk((model.calls > 0) == exp["model_called"], f"model_called 预期 {exp['model_called']}，实际调用 {model.calls} 次")
            if exp.get("duplicate"):
                chk(r["status"] == "duplicate", f"预期重复事件，实际 {r['status']}")
            if "attempts" in exp:
                chk(r["attempts"] == exp["attempts"], f"attempts 预期 {exp['attempts']}，实际 {r['attempts']}")
            if "failure_log_entries" in exp:
                fl = flog.entries(step["event"]["event_unique_key"])
                n = len([x for x in fl[n_fail_before:] if x.get("stage") != "give_up"])
                chk(n == exp["failure_log_entries"], f"失败日志预期 {exp['failure_log_entries']} 条，实际 {n}")
            if "new_rows" in exp:
                chk(after == before, f"预期不新增任何行，实际 {before} -> {after}")
            if exp.get("main_db_event_log") == "none":
                chk(not engine.processed(conn, run_id, step["event"]["event_unique_key"]), "主库不应有该事件的处理记录")
            if exp.get("rolled_back"):
                chk(r.get("rolled_back") is True, "预期事务回滚")
            if exp.get("result") == "fail":
                chk(r["status"] == "fail", f"预期 fail，实际 {r['status']}")
            elif exp.get("result") in ("keep", "write", "conflict"):
                chk(r["status"] == "done", f"预期处理成功，实际 {r['status']}")
                row = conn.execute("SELECT result FROM event_log WHERE run_id=? AND event_unique_key=?",
                                   (run_id, step["event"]["event_unique_key"])).fetchone()
                chk(row is not None and row["result"] == exp["event_log"], f"事件日志预期 {exp.get('event_log')}，实际 {row and row['result']}")
                new = [dict(conn.execute("SELECT * FROM states WHERE state_id=?", (sid,)).fetchone()) for sid in r.get("new_state_ids", [])]
                ec = exp.get("candidates", [])
                chk(len(new) == len(ec), f"新增状态预期 {len(ec)} 条，实际 {len(new)}")
                prev_keys = {s["state_key"] for s in actual_dir["states"]}
                for s, e in zip(new, ec):
                    key = e["state_key"]
                    if key.startswith("$"):
                        if key not in names:
                            names[key] = s["state_key"]
                        key = names[key]
                    chk(s["state_key"] == key, f"state_key 预期 {key}，实际 {s['state_key']}")
                    chk((s["state_key"] not in prev_keys) == e["is_new"], f"{s['state_key']} 新建判定不符")
                    chk(s["status_tag"] == e["status_tag"], f"status_tag 预期 {e['status_tag']}，实际 {s['status_tag']}")
                    chk(s["state_version"] == e["state_version"], f"版本预期 {e['state_version']}，实际 {s['state_version']}")
                    evl = json.loads(s["evidence_ref_list"])
                    for m in exp.get("evidence_must_include", []):
                        chk(m in evl, f"证据缺少 {m}")
                    chk(s["target_event_id"] == step["event"]["event_unique_key"], "target_event_id 不是当前事件")
                    exp_states.append({"state_id": s["state_id"], "state_key": s["state_key"], "state_version": s["state_version"],
                                       "status_tag": s["status_tag"], "content": None})
                # 冲突
                links = [dict(x) for x in conn.execute("SELECT state_id_a, state_id_b FROM conflict_link")]
                want = []
                for l in exp.get("conflict_links", []):
                    b = r["new_state_ids"][0] if l["b"] == "$THIS" else l["b"]
                    want.append({l["a"], b})
                    exp_links.append({"state_id_a": l["a"], "state_id_b": b})
                got_new = [set(x.values()) for x in links][len(links) - len(want):] if want else []
                chk(len(links) == len(exp_links), f"conflict_link 预期共 {len(exp_links)} 条，实际 {len(links)}")
                for w in want:
                    chk(w in [set(x.values()) for x in links], f"缺少冲突关联 {w}")
                for sid in exp.get("old_state_unchanged", []):
                    old = next(s for s in case["initial_db"]["states"] if s["state_id"] == sid)
                    cur = conn.execute("SELECT * FROM states WHERE state_id=?", (sid,)).fetchone()
                    chk(cur and cur["status_tag"] == old["status_tag"] and cur["content"] == old["content"]
                        and cur["state_version"] == old["state_version"], f"旧状态 {sid} 被改动")
            if "current_valid" in exp:
                cv = engine.directory(conn)["current_valid"]
                want = {names.get(k, k): v for k, v in exp["current_valid"].items()}
                chk(cv == want, f"当前有效状态预期 {want}，实际 {cv}")
            tag = "通过" if not errs else "失败"
            print(f"[{tag}] {case['case_id']}/{step['step_id']}  {case['purpose'] if step is case['steps'][0] else ''}")
            for e in errs:
                print("      - " + e)
            if errs:
                failed.append(f"{case['case_id']}/{step['step_id']}")
        snap = os.path.join(out_dir, case["case_id"] + ".snapshot.db")
        db.snapshot(conn, snap)
        conn.close()
    print(f"\n第一层：{total - len(failed)}/{total} 步通过" + ("" if not failed else "；失败：" + ", ".join(failed)))
    print("数据与日志：" + out_dir)
    return 0 if not failed else 1

def expected_directory(states, links):
    bad = {x for c in links for x in (c["state_id_a"], c["state_id_b"])}
    cur = {}
    for s in states:
        if s["status_tag"] == "valid" and s["state_id"] not in bad and s["state_version"] > cur.get(s["state_key"], 0):
            cur[s["state_key"]] = s["state_version"]
    return {"states": states, "conflict_links": links, "current_valid": cur}

def compare_dir(actual, exp, names):
    def norm(states, use_content):
        out = set()
        for s in states:
            out.add((s["state_key"], s["state_version"], s["status_tag"], s["content"] if use_content else None))
        return out
    has_content = all(s.get("content") is not None for s in exp["states"])
    ek = {(names.get(a, a), b, c, d) for a, b, c, d in norm(exp["states"], has_content)}
    ak = norm(actual["states"], has_content)
    diffs = []
    if ek != ak:
        diffs.append(f"状态 预期{sorted(ek, key=str)} 实际{sorted(ak, key=str)}")
    el = {frozenset((c["state_id_a"], c["state_id_b"])) for c in exp["conflict_links"]}
    al = {frozenset((c["state_id_a"], c["state_id_b"])) for c in actual["conflict_links"]}
    if el != al:
        diffs.append("冲突关联不一致")
    ec = {names.get(k, k): v for k, v in exp["current_valid"].items()}
    if ec != actual["current_valid"]:
        diffs.append(f"当前有效 预期{ec} 实际{actual['current_valid']}")
    return "；".join(diffs)

def row_counts(conn):
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("event_log", "states", "state_change_log", "conflict_link"))

if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
