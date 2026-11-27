"""第二层：真实模型重复运行评估。
用法：python3 run_layer2.py pilot     试运行，每条用例 5 次，不计入正式评估
      python3 run_layer2.py formal    正式评估，每条用例 10 次，合格线 80%
需要环境变量 MODEL_API_KEY。每轮都从用例的 initial_db 独立开始。c6 只属于第一层，这里跳过。
结构字段自动判定；状态内容写进 review.md，由人工判定语义，人工判定不符的轮次计为未命中。"""
import json, os, sys, time
from xin import db, engine
from xin.models import DeepSeekModel

def main(mode="pilot"):
    n = {"pilot": 5, "formal": 10}[mode]
    fx = json.load(open("fixtures/e1_cases.json", encoding="utf-8"))
    params = json.load(open("e1_params.json", encoding="utf-8"))
    out = os.path.join("runs", f"l2-{mode}-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    json.dump(params, open(os.path.join(out, "params.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    summary, review = [], ["# 第二层人工语义判定表", "", "每行在“判定”栏填 ✓ 或 ✗（✗ 请写原因）。结构已失败的轮次不用判。", ""]
    for case in fx["cases"]:
        if 2 not in case.get("layers", [1, 2]):
            continue
        hits, rounds = 0, []
        review += [f"## {case['case_id']}　{case['purpose']}", ""]
        for i in range(1, n + 1):
            run_id = f"{fx['fixture_id']}:L2:{mode}:{case['case_id']}:r{i}"
            path = os.path.join(out, f"{case['case_id']}-r{i}.db")
            conn = db.connect(path)
            flog = engine.FailLog(os.path.join(out, f"{case['case_id']}-r{i}.fail.jsonl"))
            db.seed(conn, run_id, case["initial_db"])
            names, errs, contents = {}, [], []
            for step in case["steps"]:
                before = engine.directory(conn)
                model = DeepSeekModel(params)
                r = engine.process_event(conn, run_id, step["event"], step.get("chat_context", []), model, flog, params["max_attempts"])
                exp = step["expect"]
                tag = f"{step['step_id']}"
                if r["status"] != "done":
                    errs.append(f"{tag}: 处理失败（{r['status']}，尝试 {r['attempts']} 次）"); break
                row = conn.execute("SELECT result, reason FROM event_log WHERE run_id=? AND event_unique_key=?",
                                   (run_id, step["event"]["event_unique_key"])).fetchone()
                if row["result"] != exp["result"]:
                    errs.append(f"{tag}: 结论预期 {exp['result']}，实际 {row['result']}")
                new = [dict(conn.execute("SELECT * FROM states WHERE state_id=?", (s,)).fetchone()) for s in r.get("new_state_ids", [])]
                ec = exp.get("candidates", [])
                if len(new) != len(ec):
                    errs.append(f"{tag}: 新增状态预期 {len(ec)} 条，实际 {len(new)}")
                prev = {s["state_key"] for s in before["states"]}
                for s, e in zip(new, ec):
                    key = e["state_key"]
                    if key.startswith("$"):
                        names.setdefault(key, s["state_key"]); key = names[key]
                    if s["state_key"] != key: errs.append(f"{tag}: key 预期 {key}，实际 {s['state_key']}")
                    if (s["state_key"] not in prev) != e["is_new"]: errs.append(f"{tag}: 新建判定不符（{s['state_key']}）")
                    if s["status_tag"] != e["status_tag"]: errs.append(f"{tag}: status_tag 预期 {e['status_tag']}，实际 {s['status_tag']}")
                    if s["state_version"] != e["state_version"]: errs.append(f"{tag}: 版本预期 {e['state_version']}，实际 {s['state_version']}")
                    contents.append((step["step_id"], step["event"]["text"], e.get("meaning", ""), s["content"], s["status_tag"]))
                for l in exp.get("conflict_links", []):
                    b = r["new_state_ids"][0] if l["b"] == "$THIS" else l["b"]
                    got = [{x[0], x[1]} for x in conn.execute("SELECT state_id_a, state_id_b FROM conflict_link")]
                    if {l["a"], b} not in got: errs.append(f"{tag}: 缺少冲突关联")
                if not exp.get("conflict_links") and conn.execute("SELECT COUNT(*) FROM conflict_link").fetchone()[0] > len(case["initial_db"]["conflict_links"]):
                    errs.append(f"{tag}: 出现了预期之外的冲突关联")
                cv = engine.directory(conn)["current_valid"]
                want = {names.get(k, k): v for k, v in exp.get("current_valid", {}).items()}
                if cv != want: errs.append(f"{tag}: 当前有效预期 {want}，实际 {cv}")
                for s in new[len(ec):]:
                    contents.append((step["step_id"], step["event"]["text"], "（预期之外的新增）", s["content"] + f" key={s['state_key']}", s["status_tag"]))
                if not new:
                    contents.append((step["step_id"], step["event"]["text"], "（预期不新增）", f"[{row['result']}] {row['reason']}", ""))
            conn.close()
            ok = not errs
            hits += ok
            rounds.append({"round": i, "structure_ok": ok, "errors": errs})
            print(f"{case['case_id']} 第{i}轮 {'结构通过' if ok else '结构失败'}" + ("" if ok else "：" + "；".join(errs)))
            for sid, text, meaning, content, tag in contents:
                review.append(f"- r{i} {sid}｜消息：{text}｜预期含义：{meaning}｜实际：{content} {tag}｜结构：{'✓' if ok else '✗'}｜判定：")
        review.append("")
        summary.append({"case": case["case_id"], "rounds": n, "structure_hits": hits, "structure_rate": f"{hits * 100 // n}%",
                        "details": rounds})
    json.dump(summary, open(os.path.join(out, "summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(os.path.join(out, "review.md"), "w", encoding="utf-8").write("\n".join(review))
    print("\n结构命中率（人工语义判定之前）：")
    for s in summary:
        mark = "" if mode == "pilot" else ("　达标" if s["structure_hits"] * 10 >= 8 * s["rounds"] else "　未达标")
        print(f"  {s['case']}: {s['structure_hits']}/{s['rounds']}{mark}")
    print(f"\n结果目录：{out}（summary.json、review.md、每轮数据库与失败日志）")

if __name__ == "__main__":
    main(*(sys.argv[1:] or ["pilot"]))
