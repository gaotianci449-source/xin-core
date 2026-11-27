"""E2-A.1 第一轮：证据继承验证（旧信息全部保留，新消息只加一条）。
用法：python3 run_evidence.py pilot|formal    需要 MODEL_API_KEY
每次调用记录 B 层原始返回（raw.jsonl），并与程序接收结果、最终入库结果对照。"""
import json, os, sys, time
from xin import db, engine
from xin.models import DeepSeekModel

class Recording:
    def __init__(self, inner, sink, tag):
        self.inner, self.sink, self.tag = inner, sink, tag
    def call(self, payload):
        raw = self.inner.call(payload)
        with open(self.sink, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tag": self.tag, "raw": raw}, ensure_ascii=False) + "\n")
        return raw

def main(mode="pilot"):
    fx = json.load(open("fixtures/e2a1_evidence.json", encoding="utf-8"))
    params = json.load(open("e1_params.json", encoding="utf-8"))
    n = fx["rounds_pilot"] if mode == "pilot" else fx["rounds_formal"]
    out = os.path.join("runs", f"ev-{mode}-" + time.strftime("%Y%m%d-%H%M%S")); os.makedirs(out, exist_ok=True)
    rawlog = os.path.join(out, "raw.jsonl")
    report = []
    for case in fx["cases"]:
        hits = 0
        for i in range(1, n + 1):
            run_id = f"{fx['fixture_id']}:{mode}:{case['case_id']}:r{i}"
            conn = db.connect(os.path.join(out, f"{case['case_id']}-r{i}.db"))
            flog = engine.FailLog(os.path.join(out, f"{case['case_id']}-r{i}.fail.jsonl"))
            db.seed(conn, run_id, case["initial_db"])
            tag = f"{case['case_id']}-r{i}"
            r = engine.process_event(conn, run_id, case["event"], case["chat_context"],
                                     Recording(DeepSeekModel(params), rawlog, tag), flog, params["max_attempts"])
            e, errs, note = case["expect"], [], {}
            if r["status"] != "done":
                errs.append(f"处理失败 {r['status']}")
            else:
                s = dict(conn.execute("SELECT * FROM states WHERE state_id=?", (r["new_state_ids"][0],)).fetchone()) if r.get("new_state_ids") else None
                res = conn.execute("SELECT result FROM event_log WHERE run_id=? AND event_unique_key=?", (run_id, case["event"]["event_unique_key"])).fetchone()[0]
                if res != e["result"]: errs.append(f"结论 {res}")
                if not s: errs.append("没有新状态")
                else:
                    stored = json.loads(s["evidence_ref_list"])
                    raws = [json.loads(l) for l in open(rawlog, encoding="utf-8") if json.loads(l)["tag"] == tag]
                    try:
                        raw_ev = json.loads(raws[-1]["raw"])["candidates"][0]["evidence_ref_list"]
                    except Exception:
                        raw_ev = None
                    note = {"原始返回": raw_ev, "入库": stored, "一致": raw_ev == stored}
                    if raw_ev != stored: errs.append("原始返回与入库不一致")
                    if s["state_key"] != e["state_key"]: errs.append(f"key {s['state_key']}")
                    if s["state_version"] != e["state_version"]: errs.append(f"版本 {s['state_version']}")
                    if s["status_tag"] != e["status_tag"]: errs.append(f"status_tag {s['status_tag']}")
                    miss = [x for x in e["evidence_must_include"] if x not in stored]
                    if miss: errs.append("证据缺少 " + ",".join(m.split(":")[-1] for m in miss))
                    lost = [k for k in e["content_keywords"] if not any(w in s["content"] for w in k)]
                    if lost: errs.append("正文丢了 " + "/".join(k[0] for k in lost))
                    note["正文"] = s["content"]
            conn.close()
            ok = not errs; hits += ok
            print(f"{case['case_id']} 第{i}轮 {'通过' if ok else '失败：' + '；'.join(errs)}")
            report.append({"case": case["case_id"], "round": i, "ok": ok, "errors": errs, **note})
        line = f"{case['case_id']}: {hits}/{n}"
        if mode == "formal": line += "　达标" if hits >= fx["pass_rate"] * n else "　未达标"
        print("  → " + line)
    json.dump(report, open(os.path.join(out, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n结果：{out}/report.json（含原始返回与入库对照）；原始返回：{out}/raw.jsonl")

if __name__ == "__main__":
    main(*(sys.argv[1:] or ["pilot"]))
