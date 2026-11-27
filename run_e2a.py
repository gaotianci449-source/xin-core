"""E2-A：B 层状态对回复的支撑。同一剧本，三组对照，从相同初始条件独立开始。
用法：python3 run_e2a.py pilot   每组每剧本 1 轮
      python3 run_e2a.py formal  每组每剧本 5 轮
需要环境变量 MODEL_API_KEY。模拟 QQ 适配器：按剧本逐条喂消息，不走真实 QQ。"""
import json, os, sys, time, urllib.request
from xin import db, engine, reply
from xin.models import DeepSeekModel

def chat_call(params, messages):
    body = {"model": params["model"], "messages": messages, "max_tokens": params["reply_max_tokens"]}
    if params.get("reasoning_effort"):
        body["reasoning_effort"] = params["reasoning_effort"]
    req = urllib.request.Request(params["base_url"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ[params["api_key_env"]]})
    last = None
    for _ in range(params["max_attempts"]):
        try:
            with urllib.request.urlopen(req, timeout=params["timeout_s"]) as r:
                ch = json.loads(r.read())["choices"][0]
            if ch.get("finish_reason") == "length":
                raise ValueError("回复被截断")
            return (ch["message"].get("content") or "").strip()
        except Exception as e:
            last = e
    return f"[回复失败：{last}]"

def run_one(fx, params, script, group, rnd, out):
    run_id = f"{fx['fixture_id']}:{group}:{script['script_id']}:r{rnd}"
    conn = db.connect(os.path.join(out, f"{script['script_id']}-{group}-r{rnd}.db"))
    flog = engine.FailLog(os.path.join(out, f"{script['script_id']}-{group}-r{rnd}.fail.jsonl"))
    who, transcript = fx["sender"], []
    for m in script["messages"]:
        key = f"{run_id}:u{m['step']}"
        ev = {"event_unique_key": key, "sender": who, "time": db.now(), "session": "e2a", "text": m["text"]}
        if group == "A":
            chat_ctx = reply.history(conn, run_id, fx["window"])
            chat_ctx = [{"event_unique_key": "", "sender": c["who"], "time": c["time"], "session": "e2a", "text": c["text"]} for c in chat_ctx]
            r = engine.process_event(conn, run_id, ev, chat_ctx, DeepSeekModel(params), flog, params["max_attempts"])
            if r["status"] != "done":       # B 层失败也要把消息存下，聊天不能断
                reply.store(conn, run_id, key, who, m["text"], "msg")
        else:
            reply.store(conn, run_id, key, who, m["text"], "msg")
        limit = fx["long_window"] if group == "C" else fx["window"]
        chat = reply.history(conn, run_id, limit)
        states = reply.states_text(engine.directory(conn)) if group == "A" else None
        msgs = reply.build_messages(who, chat, states)
        text = chat_call(params, msgs)
        reply.store(conn, run_id, f"{run_id}:a{m['step']}", "你", text, "reply")
        transcript.append({"step": m["step"], "user": m["text"], "reply": text,
                           "states_shown": states if group == "A" else None})
    conn.close()
    return transcript

def main(mode="pilot"):
    fx = json.load(open("fixtures/e2a_scripts.json", encoding="utf-8"))
    params = json.load(open("e1_params.json", encoding="utf-8"))
    params.setdefault("reply_max_tokens", 8000)
    n = fx["rounds_pilot"] if mode == "pilot" else fx["rounds_formal"]
    out = os.path.join("runs", f"e2a-{mode}-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    review = ["# E2-A 关键节点人工判定表", "", "每个节点按各项标 ✓ / ✗。组别：" + "；".join(f"{k}={v}" for k, v in fx["groups"].items()), ""]
    allt = {}
    for script in fx["scripts"]:
        cps = {c["step"]: c for c in script["checkpoints"]}
        for rnd in range(1, n + 1):
            for group in ("A", "B", "C"):
                t = run_one(fx, params, script, group, rnd, out)
                allt[f"{script['script_id']}-{group}-r{rnd}"] = t
                print(f"{script['script_id']} {group}组 第{rnd}轮 完成")
        review += [f"## {script['script_id']}　{script['purpose']}", ""]
        for step, cp in cps.items():
            crit = "；".join(f"{k}：{v}" for k, v in cp.items() if k not in ("step", "name"))
            review += [f"### 第 {step} 条「{script['messages'][step-1]['text']}」—— {cp['name']}", f"判定标准：{crit}", ""]
            for rnd in range(1, n + 1):
                for group in ("A", "B", "C"):
                    r = allt[f"{script['script_id']}-{group}-r{rnd}"][step - 1]["reply"]
                    review.append(f"- {group}组 r{rnd}：{r}")
            review.append("")
    json.dump(allt, open(os.path.join(out, "transcripts.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(os.path.join(out, "review.md"), "w", encoding="utf-8").write("\n".join(review))
    print(f"\n完成。关键节点对照：{out}/review.md　完整对话：{out}/transcripts.json")

if __name__ == "__main__":
    main(*(sys.argv[1:] or ["pilot"]))
