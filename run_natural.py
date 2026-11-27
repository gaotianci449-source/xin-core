"""自然交流 + 主动分享｜A/B 对照（离线，不接 QQ）。
用法：python3 run_natural.py probes|long|all [pilot]    需要 MODEL_API_KEY
A = 现有 A 层（SEED + 约束 + 记忆层 O 组：快照 + 按需检索）
B = A + prompts/natural_v1.txt（自然交流纯提示词候选稿 V1，原文只去掉了标题行）
probes：固定剧本探针（fixtures/natural_cases.json），状态固定，不跑 B 层。
long：三天时间线（fixtures/natural_long.json），阿凯由模拟模型扮演；每条阿凯消息先过 B 层写状态；含自己醒来的时刻。
pilot：每项只跑 1 轮，用来先看链路通不通。"""
import copy, json, os, re, sys, time
from collections import Counter
from datetime import datetime, timedelta
from xin import db, engine, reply, gateway, history
from xin.models import DeepSeekModel
from run_stage1 import build_db, api, make_judge, O_NOTE

NATURAL = open("prompts/natural_v1.txt", encoding="utf-8").read().strip()
TZ_OFF = timedelta(hours=8)

def z(local):  # "2026-11-27 12:30" 上海时间 → UTC ISO Z
    return (datetime.strptime(local, "%Y-%m-%d %H:%M") - TZ_OFF).strftime("%Y-%m-%dT%H:%M:%SZ")

def plus(t, s):
    return (datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")

def store(conn, run_id, key, sender, text, result, t):
    conn.execute("INSERT INTO event_log VALUES (?,?,?,?,?,?)", (run_id, key, result, json.dumps(
        {"event_unique_key": key, "sender": sender, "time": t, "session": "natural", "text": text}, ensure_ascii=False), None, db.now()))

def snapshot_current(dir_):
    """只给当前生效的版本（长线会产生多版本），带 key。"""
    cur = dir_["current_valid"]
    bad = {x for c in dir_["conflict_links"] for x in (c["state_id_a"], c["state_id_b"])}
    out = []
    for s in dir_["states"]:
        if s["state_id"] in bad: tag = "有矛盾，待核实"
        elif s["status_tag"] == "pending": tag = "未确认"
        elif cur.get(s["state_key"]) == s["state_version"]: tag = "当前"
        else: continue
        out.append(f"- [{s['state_key']}] {s['content']}（{tag}）")
    return "\n".join(out) or "（目前没有记录）"

def system_for(group, who, dir_):
    s = reply.SEED.format(who=who) + "\n\n" + reply.FORMAT + "\n\n" + reply.CONSTRAINTS
    if group == "B":
        s += "\n\n" + NATURAL
    s += "\n\n关于对方，你此前记下的状态记录如下（只来自对方说过的话，可能不全）：\n" + snapshot_current(dir_) + "\n\n" + O_NOTE
    return s

def o_reply(conn, run_id, key, now_iso, tz, system, chat, params, judge, extra_user=None, question=""):
    """O 组回复：工具定义始终保留；每句最多执行 2 次检索。"""
    msgs = [{"role": "system", "content": system}] + [{"role": "assistant" if m["who"] == "你" else "user", "content": m["text"]} for m in chat]
    if extra_user: msgs.append({"role": "user", "content": extra_user})
    dir_ = engine.directory(conn)
    calls, usage, text = [], [], None
    for _ in range(5):
        m, u = api(params, msgs, [gateway.TOOL]); usage.append(u)
        if "DSML" in (m.get("content") or ""):
            raise RuntimeError("模型把工具调用标记输出成了文字（执行器协议故障）")
        if not m.get("tool_calls"):
            text = (m.get("content") or "").strip(); break
        tc = m["tool_calls"][0]
        am = {"role": "assistant", "content": m.get("content") or "", "tool_calls": m["tool_calls"]}
        if m.get("reasoning_content"): am["reasoning_content"] = m["reasoning_content"]
        msgs.append(am)
        try: req = json.loads(tc["function"]["arguments"])
        except Exception as e: req = None; err = str(e)
        if len(calls) >= 2:
            result = json.dumps({"status": "本轮检索已终止", "reason": "本句检索次数已达上限（2 次）",
                                 "instruction": "不再执行新的检索；请根据已经拿到的结果直接回复对方"}, ensure_ascii=False)
            calls.append({"valid": False, "over_limit": True})
        elif req is None:
            result = "请求无法解析，没有执行检索。"; calls.append({"valid": False, "errors": [err]})
        else:
            result, glog = gateway.execute(conn, run_id, req, dir_, key, now_iso, tz, judge=judge, original_question=question)
            calls.append({k: glog.get(k) for k in ("valid", "errors", "request", "hits", "partial", "stopped", "flags")})
        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        for extra in m["tool_calls"][1:]:
            msgs.append({"role": "tool", "tool_call_id": extra["id"], "content": "一次只执行一个检索请求，这个没有执行。"})
    if text is None: text = "[超过检索次数上限，没有得到回复]"
    return text, calls, {"prompt": sum(u.get("prompt_tokens", 0) for u in usage), "completion": sum(u.get("completion_tokens", 0) for u in usage)}

# ---------- 机械统计（只看分布，不代替人工判定） ----------
EMPATHY = ("理解", "懂你", "我懂", "辛苦", "抱抱", "心疼", "别难过", "别生气", "太难了", "确实挺", "换谁都")

def lcs_len(a, b):
    best, prev = 0, [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b, 1):
            v = prev[j - 1] + 1 if ca == cb else 0
            cur.append(v); best = max(best, v)
        prev = cur
    return best

def mech(text, prev_user):
    t = text.strip()
    lines = [l for l in t.splitlines() if l.strip()]
    return {"len": len(t),
            "ends_q": bool(re.search(r"[?？]\s*$|[吗呢]\s*[。！!~]?\s*$", t)),
            "empathy_open": any(w in t[:14] for w in EMPATHY),
            "echo": bool(prev_user) and lcs_len(re.sub(r"\s", "", t), re.sub(r"\s", "", prev_user)) >= 6,
            "structured": len(lines) >= 4 or any(re.match(r"\s*([-•·*]|\d+[.、)]|[①②③④⑤])", l) for l in lines),
            "opener": re.sub(r"[\s，,。.!！?？~]", "", t)[:2]}

def summarize(ms):
    if not ms: return {}
    n = len(ms); op = Counter(m["opener"] for m in ms).most_common(3)
    return {"回复数": n, "平均字数": round(sum(m["len"] for m in ms) / n), "问句结尾": f"{sum(m['ends_q'] for m in ms)}/{n}",
            "开头共情词": f"{sum(m['empathy_open'] for m in ms)}/{n}", "复述对方≥6字": f"{sum(m['echo'] for m in ms)}/{n}",
            "列条目/≥4行": f"{sum(m['structured'] for m in ms)}/{n}", "最常见开头": op}

# ---------- 固定剧本探针 ----------
def run_probes(params, out, rounds):
    fx = json.load(open("fixtures/natural_cases.json", encoding="utf-8"))
    hist0 = json.load(open(fx["base_history"], encoding="utf-8"))
    judge, results, review = make_judge(params), [], ["# 自然交流探针｜人工判定", ""]
    ms = {"A": [], "B": []}
    for case in fx["cases"]:
        review += [f"## {case['id']}　{case['name']}", f"- 看什么：{case['focus']}", f"- 好：{case['good']}", f"- 差：{case['bad']}", ""]
        for rnd in range(1, rounds + 1):
            for g in ("A", "B"):
                run_id = f"{case['id']}:{g}:r{rnd}"
                hist = copy.deepcopy(hist0)
                conn = build_db(os.path.join(out, f"{case['id']}-{g}-r{rnd}.db"), hist, run_id)
                rows, status = [], "ok"
                try:
                    for ti, turn in enumerate(case["turns"], 1):
                        key = f"{run_id}:u{ti}"
                        store(conn, run_id, key, hist["sender"], turn["text"], "msg", turn["time"])
                        chat = reply.history(conn, run_id, hist["window"])
                        sysm = system_for(g, hist["sender"], engine.directory(conn))
                        text, calls, tok = o_reply(conn, run_id, key, turn["time"], hist["tz"], sysm, chat, params, judge, question=turn["text"])
                        store(conn, run_id, f"{run_id}:a{ti}", "你", text, "reply", plus(turn["time"], 20))
                        mm = mech(text, turn["text"]); ms[g].append(mm)
                        rows.append({"turn": ti, "user": turn["text"], "reply": text, "calls": calls, "tokens": tok, "mech": mm})
                except Exception as e:
                    status = f"无效（执行器/配置故障）：{e}"
                conn.close()
                results.append({"case": case["id"], "group": g, "round": rnd, "status": status, "turns": rows})
                review.append(f"### {g}组 r{rnd}" + ("" if status == "ok" else f"　{status}"))
                for r in rows:
                    review.append(f"- 阿凯：{r['user']}")
                    review.append(f"  溟：{r['reply']}" + (f"　〔检索 {len(r['calls'])} 次〕" if r["calls"] else ""))
                review.append("")
                print(f"{case['id']} {g}组 r{rnd} {status}")
    return results, review, ms

# ---------- 三天时间线 ----------
def sim_say(params, fx, intent, transcript, now_local, opening=False):
    fmt = "这次是你主动去找对方，发出你的第一条（或几条）消息。" + fx["sim_format"].replace("不回就只输出 [不回]。", "") if opening else fx["sim_format"]
    user = ("最近的聊天记录（“阿凯”是你，“对方”是和你聊天的人）：\n" + (transcript or "（无）") +
            f"\n\n现在是 {now_local}。这次你想聊的：{intent}\n\n" + fmt)
    m, u = api(params, [{"role": "system", "content": fx["persona"]}, {"role": "user", "content": user}])
    text = (m.get("content") or "").strip()
    if opening and (not text or "[不回]" in text):   # 主动找对方的第一句不允许空着，重试一次
        m, u = api(params, [{"role": "system", "content": fx["persona"]}, {"role": "user", "content": user}])
        text = (m.get("content") or "").strip()
    if not text or "[不回]" in text: return [], u
    return [l.strip() for l in text.splitlines() if l.strip()][:3], u

def transcript_text(conn, run_id, tz, n=16):
    rows = reply.history(conn, run_id, n)
    return "\n".join(f"{history.local(m['time'], tz)} {'对方' if m['who'] == '你' else '阿凯'}：{m['text']}" for m in rows)

def akai_send(conn, run_id, key, lines, t, fx, hist, bmodel, flog, params):
    """每条阿凯消息先过 B 层（写状态），失败则只记消息。"""
    for i, line in enumerate(lines):
        k = f"{key}.{i}"
        ev = {"event_unique_key": k, "sender": hist["sender"], "time": plus(t, i * 10), "session": "natural", "text": line}
        ctx = [{"event_unique_key": "", "sender": c["who"], "time": c["time"], "session": "natural", "text": c["text"]}
               for c in reply.history(conn, run_id, fx["window"])]
        r = engine.process_event(conn, run_id, ev, ctx, bmodel, flog, params["max_attempts"])
        if r["status"] != "done":
            store(conn, run_id, k, hist["sender"], line, "msg", ev["time"])
    return f"{key}.{len(lines) - 1}", plus(t, len(lines) * 10)

def run_long(params, out, rounds):
    fx = json.load(open("fixtures/natural_long.json", encoding="utf-8"))
    hist0 = json.load(open(fx["base_history"], encoding="utf-8"))
    judge, results, review = make_judge(params), [], ["# 三天时间线｜人工判定", "",
                                                      "看什么：语气和关系前后是否一致、有没有突然变；自己醒来时发不发、发的是分享还是汇报；场次里的自然度（参照探针的好/差）。", ""]
    ms = {"A": [], "B": []}; wakes = {"A": [], "B": []}
    for rnd in range(1, rounds + 1):
        for g in ("A", "B"):
            run_id = f"long:{g}:r{rnd}"
            hist = copy.deepcopy(hist0); tz, who = hist["tz"], hist["sender"]
            conn = build_db(os.path.join(out, f"long-{g}-r{rnd}.db"), hist, run_id)
            flog = engine.FailLog(os.path.join(out, f"long-{g}-r{rnd}.fail.jsonl"))
            bmodel = DeepSeekModel(params)
            log, status, sim_tok = [], "ok", 0
            try:
                for item in fx["timeline"]:
                    t = z(item["local"]); entry = {"id": item["id"], "type": item["type"], "turns": []}
                    if item["type"] == "wake":
                        feed = "\n".join(f"- {x}" for x in item["feed"])
                        note = fx["wake_note"].format(now=item["local"], feed=feed, who=who)
                        chat = reply.history(conn, run_id, fx["window"])
                        text, calls, tok = o_reply(conn, run_id, None, t, tz, system_for(g, who, engine.directory(conn)), chat, params, judge,
                                                   extra_user="【系统提示，不是对方发的消息】" + note)
                        silent = (not text) or "[不发]" in text
                        entry["wake"] = {"sent": not silent, "text": text, "calls": calls}
                        wakes[g].append({"id": item["id"], "sent": not silent, "len": 0 if silent else len(text)})
                        if silent: log.append(entry); continue
                        store(conn, run_id, f"{run_id}:{item['id']}:w", "你", text, "reply", t)
                        ms[g].append(mech(text, ""))
                        t = plus(t, 120); turns_left = fx["max_turns_after_wake"]
                        intent = "对方突然给你发了消息，你看情况回不回、回多少（按你这个时间点在干嘛）"
                    else:
                        turns_left = fx["max_turns_per_session"]; intent = item["intent"]
                    ti = 0
                    while turns_left > 0:
                        lines, su = sim_say(params, fx, intent, transcript_text(conn, run_id, tz), history.local(t, tz),
                                            opening=(item["type"] == "session" and ti == 0))
                        sim_tok += su.get("prompt_tokens", 0) + su.get("completion_tokens", 0)
                        if not lines: entry["turns"].append({"akai": "[不回]"}); break
                        ti += 1; key = f"{run_id}:{item['id']}:u{ti}"
                        last_key, t = akai_send(conn, run_id, key, lines, t, fx, hist, bmodel, flog, params)
                        chat = reply.history(conn, run_id, fx["window"])
                        text, calls, tok = o_reply(conn, run_id, last_key, t, tz, system_for(g, who, engine.directory(conn)), chat, params, judge,
                                                   question="\n".join(lines))
                        store(conn, run_id, f"{key}:a", "你", text, "reply", plus(t, 20))
                        mm = mech(text, lines[-1]); ms[g].append(mm)
                        entry["turns"].append({"time": history.local(t, tz), "akai": lines, "reply": text, "calls": calls, "mech": mm})
                        t = plus(t, 90); turns_left -= 1
                        intent = item.get("intent", intent) + "（接着刚才的聊，聊完了或者没兴趣了就不回）"
                    log.append(entry)
            except Exception as e:
                status = f"无效（执行器/配置故障）：{e}"
            final_states = snapshot_current(engine.directory(conn))
            conn.close()
            results.append({"group": g, "round": rnd, "status": status, "log": log, "final_states": final_states, "sim_tokens": sim_tok})
            review.append(f"## {g}组 r{rnd}" + ("" if status == "ok" else f"　{status}"))
            for e in log:
                if e["type"] == "wake":
                    w = e.get("wake", {})
                    review.append(f"### {e['id']} 自己醒来：" + ("发了" if w.get("sent") else "没发"))
                    if w.get("sent"): review.append(f"  溟：{w['text']}")
                else:
                    review.append(f"### {e['id']} 阿凯来聊")
                for tr in e["turns"]:
                    if tr.get("akai") == "[不回]": review.append("- 阿凯：[不回]"); continue
                    review.append(f"- {tr['time']} 阿凯：" + " / ".join(tr["akai"]))
                    review.append(f"  溟：{tr['reply']}" + (f"　〔检索 {len(tr['calls'])} 次〕" if tr["calls"] else ""))
            review += ["", "最终状态记录：", final_states, ""]
            print(f"long {g}组 r{rnd} {status}")
    return results, review, ms, wakes

def main(what="all", pilot=None):
    params = json.load(open("e1_params.json", encoding="utf-8")); params.setdefault("reply_max_tokens", 8000)
    out = os.path.join("runs", f"natural-{what}-" + ("pilot-" if pilot else "") + time.strftime("%Y%m%d-%H%M%S")); os.makedirs(out, exist_ok=True)
    json.dump({"params": params, "natural_prompt": NATURAL, "o_note": O_NOTE, "cases": json.load(open("fixtures/natural_cases.json", encoding="utf-8")),
               "long": json.load(open("fixtures/natural_long.json", encoding="utf-8"))},
              open(os.path.join(out, "frozen_inputs.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    allres, stats = {}, []
    if what in ("probes", "all"):
        res, rev, ms = run_probes(params, out, 1 if pilot else json.load(open("fixtures/natural_cases.json", encoding="utf-8"))["rounds"])
        allres["probes"] = res
        open(os.path.join(out, "review_probes.md"), "w", encoding="utf-8").write("\n".join(rev))
        stats += ["## 探针机械统计"] + [f"- {g}组：{summarize(ms[g])}" for g in "AB"]
    if what in ("long", "all"):
        res, rev, ms, wk = run_long(params, out, 1 if pilot else json.load(open("fixtures/natural_long.json", encoding="utf-8"))["rounds"])
        allres["long"] = res
        open(os.path.join(out, "review_long.md"), "w", encoding="utf-8").write("\n".join(rev))
        stats += ["## 时间线机械统计"] + [f"- {g}组：{summarize(ms[g])}" for g in "AB"]
        stats += ["## 自己醒来"] + [f"- {g}组：发了 {sum(w['sent'] for w in wk[g])}/{len(wk[g])}　" + "，".join(f"{w['id']}{'发' if w['sent'] else '不发'}" for w in wk[g]) for g in "AB"]
    json.dump(allres, open(os.path.join(out, "results.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(os.path.join(out, "stats.md"), "w", encoding="utf-8").write("\n".join(stats))
    print("\n".join(stats))
    print(f"\n完成：{out}/  review_probes.md　review_long.md　stats.md　results.json")

if __name__ == "__main__":
    main(*sys.argv[1:])
