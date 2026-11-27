"""E2-A.1 v7：x2-A / x2-B 链式试运行。
x2-A：o1 猫煤球、o2 狗旺财，n1 “旺财送我妈那边养了 现在家里就煤球一个”。
x2-B：在每一轮 x2-A 的真实结果上原样接续，新消息 “旺财最近怎么样？”。不人工修改任何输出。
用法：python3 run_x2chain.py [轮数，默认 10]    需要 MODEL_API_KEY
输出：每轮正文、evidence_ref_list、B 层给出的 reason（自检归因，只作诊断）、自动初判；最终 P0–P3 由人工判定。"""
import json, os, re, sys, time
from xin import db, engine
from xin.models import DeepSeekModel
from run_evidence import Recording

AT_HOME = re.compile(r"旺财[^。；]*(还在家|仍在家|回家|回来了|接回)")  # 不用“在家里”：会误中“现在家里只有煤球”

def short(refs):
    return [r.split(":")[-1] for r in refs]

def main(n="10"):
    n = int(n)
    fx = json.load(open("fixtures/e2a1_evidence_r3.json", encoding="utf-8"))
    case = next(c for c in fx["cases"] if c["case_id"] == "x2")
    params = json.load(open("e1_params.json", encoding="utf-8"))
    out = os.path.join("runs", "x2chain-" + time.strftime("%Y%m%d-%H%M%S")); os.makedirs(out, exist_ok=True)
    raw = os.path.join(out, "raw.jsonl")
    o2 = case["initial_db"]["events"][1]["event_unique_key"]
    rows = []
    for i in range(1, n + 1):
        run_id = f"x2chain:r{i}"
        conn = db.connect(os.path.join(out, f"r{i}.db"))
        flog = engine.FailLog(os.path.join(out, f"r{i}.fail.jsonl"))
        db.seed(conn, run_id, case["initial_db"])
        rec = {"round": i, "prompt": params.get("prompt_version")}
        # x2-A
        ra = engine.process_event(conn, run_id, case["event"], case["chat_context"], Recording(DeepSeekModel(params), raw, f"r{i}-A"), flog, params["max_attempts"])
        if ra["status"] != "done":
            rec["A"] = {"status": ra["status"]}; rows.append(rec); print(f"第{i}轮 x2-A 处理失败"); continue
        a_reason = conn.execute("SELECT reason, result FROM event_log WHERE run_id=? AND event_unique_key=?", (run_id, case["event"]["event_unique_key"])).fetchone()
        sa = dict(conn.execute("SELECT * FROM states WHERE state_id=?", (ra["new_state_ids"][0],)).fetchone()) if ra.get("new_state_ids") else None
        a_ev = json.loads(sa["evidence_ref_list"]) if sa else []
        a_flags = []
        if sa and o2 in a_ev: a_flags.append("保留o2（看理由：P0 还是 P2）")
        if sa and AT_HOME.search(sa["content"]): a_flags.append("正文疑似写旺财在家（P0 候选）")
        if sa and "狗" in sa["content"] and o2 not in a_ev: a_flags.append("P3：去掉o2但正文保留“狗”")
        rec["A"] = {"result": a_reason["result"], "content": sa and sa["content"], "evidence": short(a_ev), "reason": a_reason["reason"], "flags": a_flags}
        # x2-B：原样接续
        evb = {"event_unique_key": f"x2chain:r{i}:b1", "sender": "阿凯", "time": "2026-11-26T13:00:00Z", "session": "qq-private-akai", "text": "旺财最近怎么样？"}
        ctx = case["chat_context"] + [case["event"]]
        rb = engine.process_event(conn, run_id, evb, ctx, Recording(DeepSeekModel(params), raw, f"r{i}-B"), flog, params["max_attempts"])
        b_flags, bres = [], None
        if rb["status"] != "done":
            b_flags.append(f"处理失败 {rb['status']}")
        else:
            row = conn.execute("SELECT reason, result FROM event_log WHERE run_id=? AND event_unique_key=?", (run_id, evb["event_unique_key"])).fetchone()
            bres = {"result": row["result"], "reason": row["reason"], "new_states": []}
            for sid in rb.get("new_state_ids", []):
                s = dict(conn.execute("SELECT * FROM states WHERE state_id=?", (sid,)).fetchone())
                ev = json.loads(s["evidence_ref_list"])
                bres["new_states"].append({"key": s["state_key"], "v": s["state_version"], "tag": s["status_tag"], "content": s["content"], "evidence": short(ev)})
                if o2 in ev and o2 not in a_ev: b_flags.append("恢复了o2（P1 候选）")
                if AT_HOME.search(s["content"]): b_flags.append("正文疑似写旺财在家/回家（P1 候选）")
            if row["result"] != "keep": b_flags.append(f"询问消息判成 {row['result']}（人工看是否推出了新状态）")
        rec["B"] = {**(bres or {}), "flags": b_flags}
        rec["current_valid_after_B"] = engine.directory(conn)["current_valid"]
        conn.close()
        rows.append(rec)
        print(f"第{i}轮　A：{rec['A']['evidence']} {('｜' + '；'.join(a_flags)) if a_flags else ''}　B：{bres and bres['result']} {('｜' + '；'.join(b_flags)) if b_flags else ''}")
    json.dump(rows, open(os.path.join(out, "chain.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n—— 人工判定用明细 ——")
    for r in rows:
        a, b = r.get("A", {}), r.get("B", {})
        print(f"\n[第{r['round']}轮] x2-A 正文：{a.get('content')}\n  引用：{a.get('evidence')}　理由：{a.get('reason')}")
        print(f"  x2-B：{b.get('result')}　理由：{b.get('reason')}")
        for s in b.get("new_states", []):
            print(f"  x2-B 新状态 v{s['v']} {s['tag']}：{s['content']}　引用：{s['evidence']}")
    print(f"\n汇总：o2 保留 {sum(1 for r in rows if 'o2' in r.get('A',{}).get('evidence',[]))}/{n}；"
          f"x2-B 判 keep {sum(1 for r in rows if r.get('B',{}).get('result')=='keep')}/{n}；"
          f"有 P0/P1 候选标记 {sum(1 for r in rows if any('P0' in f or 'P1' in f for f in r.get('A',{}).get('flags',[])+r.get('B',{}).get('flags',[])))}/{n}")
    print(f"文件：{out}/chain.json（含原始返回 raw.jsonl）")

if __name__ == "__main__":
    main(*sys.argv[1:])
