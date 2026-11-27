"""E2-A.1：跨天搬家案例，四组对照（A 原始基线 / B 提示词约束 / C 完整溯源 + 检索 / D 原始聊天）。
用法：python3 run_e2a1.py pilot|formal    需要 MODEL_API_KEY。模拟 QQ：消息时间取剧本里的时间。"""
import json, os, sys, time
from xin import db, engine, reply, history
from xin.models import DeepSeekModel
from run_e2a import chat_call

def store_t(conn, run_id, key, sender, text, result, t):
    conn.execute("INSERT INTO event_log VALUES (?,?,?,?,?,?)", (run_id, key, result, json.dumps(
        {"event_unique_key": key, "sender": sender, "time": t, "session": "e2a1", "text": text}, ensure_ascii=False), None, db.now()))

def plus_seconds(t, s):
    from datetime import datetime, timedelta
    return (datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")

def run_one(fx, params, group, rnd, out):
    run_id = f"{fx['fixture_id']}:{group}:r{rnd}"
    conn = db.connect(os.path.join(out, f"{group}-r{rnd}.db"))
    flog = engine.FailLog(os.path.join(out, f"{group}-r{rnd}.fail.jsonl"))
    who, tz, rows = fx["sender"], fx["tz"], []
    for m in fx["messages"]:
        key = f"{run_id}:u{m['step']}"
        ev = {"event_unique_key": key, "sender": who, "time": m["time"], "session": "e2a1", "text": m["text"]}
        if group in ("A", "B", "C"):
            ctx = [{"event_unique_key": "", "sender": c["who"], "time": c["time"], "session": "e2a1", "text": c["text"]}
                   for c in reply.history(conn, run_id, fx["window"])]
            r = engine.process_event(conn, run_id, ev, ctx, DeepSeekModel(params), flog, params["max_attempts"])
            if r["status"] != "done":
                store_t(conn, run_id, key, who, m["text"], "msg", m["time"])
        else:
            store_t(conn, run_id, key, who, m["text"], "msg", m["time"])
        chat = reply.history(conn, run_id, fx["long_window"] if group == "D" else fx["window"])
        dir_ = engine.directory(conn)
        states = retrieval = rng = None
        if group == "A":
            msgs = reply.build_messages(who, chat, reply.states_text(dir_))
        elif group == "B":
            states = reply.states_text(dir_)
            msgs = reply.build_messages_v2(who, chat, "constrained", states)
        elif group == "C":
            states = reply.states_with_sources(conn, run_id, dir_, key, tz)
            rng = history.resolve(m["text"], m["time"], tz)
            retrieval = reply.retrieval_text(rng, history.search(conn, run_id, rng, exclude_key=key) if rng else [], tz)
            msgs = reply.build_messages_v2(who, chat, "provenance", states, retrieval)
        else:
            msgs = reply.build_messages_v2(who, chat, "raw")
        text = chat_call(params, msgs)
        store_t(conn, run_id, f"{run_id}:a{m['step']}", "你", text, "reply", plus_seconds(m["time"], 20))
        rows.append({"step": m["step"], "user": m["text"], "reply": text, "system_prompt": msgs[0]["content"]})
    conn.close()
    return rows

def main(mode="pilot"):
    fx = json.load(open("fixtures/e2a1_move.json", encoding="utf-8"))
    params = json.load(open("e1_params.json", encoding="utf-8")); params.setdefault("reply_max_tokens", 8000)
    n = fx["rounds_pilot"] if mode == "pilot" else fx["rounds_formal"]
    out = os.path.join("runs", f"e2a1-{mode}-" + time.strftime("%Y%m%d-%H%M%S")); os.makedirs(out, exist_ok=True)
    allr = {}
    for rnd in range(1, n + 1):
        for g in ("A", "B", "C", "D"):
            allr[f"{g}-r{rnd}"] = run_one(fx, params, g, rnd, out); print(f"{g}组 第{rnd}轮 完成")
    json.dump(allr, open(os.path.join(out, "transcripts.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    lines = ["# E2-A.1 搬家案例关键节点", "", "；".join(f"{k}={v}" for k, v in fx["groups"].items()), ""]
    for cp in fx["checkpoints"]:
        lines += [f"## 第 {cp['step']} 条「{fx['messages'][cp['step']-1]['text']}」—— {cp['name']}", "判定项：" + "；".join(cp["items"]), ""]
        for rnd in range(1, n + 1):
            for g in ("A", "B", "C", "D"):
                lines.append(f"- {g}组 r{rnd}：{allr[f'{g}-r{rnd}'][cp['step']-1]['reply']}")
        lines.append("")
    c8 = allr["C-r1"][7]["system_prompt"]
    lines += ["## C 组第 8 条实际看到的状态与检索（第 1 轮）", "", c8[c8.find("关于对方"):]]
    open(os.path.join(out, "review.md"), "w", encoding="utf-8").write("\n".join(lines))
    print(f"\n完成：{out}/review.md")

if __name__ == "__main__":
    main(*(sys.argv[1:] or ["pilot"]))
