"""长期记忆按需检索｜第一阶段 S/R/O/D 对照（临时测试执行器，不接入正式链路）。
用法：python3 run_stage1.py pilot|formal [tools|json]    需要 MODEL_API_KEY
S=快照（无来源）；R=快照+冻结的轻量来源提示；O=快照+模型按需检索（只读执行器）；D=完整原始聊天。四组共用同一 A 层约束。"""
import copy, json, os, sys, time, urllib.request, urllib.error
from xin import db, engine, reply, gateway, history

def build_db(path, hist, run_id):
    conn = db.connect(path)
    conn.execute("BEGIN")
    for e in hist["events"]:
        conn.execute("INSERT INTO event_log VALUES (?,?,?,?,?,?)", (run_id, e["id"], "reply" if e.get("reply") else "msg",
                     json.dumps({"event_unique_key": e["id"], "sender": e["sender"], "time": e["time"], "session": "stage1", "text": e["text"]}, ensure_ascii=False), None, db.now()))
    for i, s in enumerate(hist["states"]):
        add_state(conn, s, i)
    conn.execute("COMMIT")
    return conn

def add_state(conn, s, i, cur=None):
    refs = [cur if r == "CUR" else r for r in s["refs"]]
    conn.execute("INSERT INTO states VALUES (?,?,?,?,?,?,?,?)", (f"S-{s['state_key']}", refs[-1], s["state_key"], 1, "valid",
                 s["content"], db.now(), json.dumps(refs, ensure_ascii=False)))

def snapshot(dir_, with_key=False):
    if not dir_["states"]: return "（目前没有记录）"
    return "\n".join((f"- [{s['state_key']}] " if with_key else "- ") + f"{s['content']}（当前）" for s in dir_["states"])

def r_hints(conn, run_id, dir_, abstracts, tz):
    out = []
    for s in dir_["states"]:
        out.append(f"- {s['content']}（当前）")
        for ref in s["evidence_ref_list"]:
            e = json.loads(conn.execute("SELECT payload FROM event_log WHERE run_id=? AND event_unique_key=?", (run_id, ref)).fetchone()["payload"])
            out.append(f"    来源提示：event_id={ref}｜record_time={history.local(e['time'], tz)}｜topic={s['state_key']}｜abstract={abstracts.get(ref, '')}｜relation=支撑本状态")
    return "\n".join(out)

def api(params, messages, tools=None):
    body = {"model": params["model"], "messages": messages, "max_tokens": params.get("reply_max_tokens", 8000)}
    if params.get("reasoning_effort"): body["reasoning_effort"] = params["reasoning_effort"]
    if tools: body["tools"] = tools
    req = urllib.request.Request(params["base_url"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ[params["api_key_env"]]})
    last = None
    for _ in range(params["max_attempts"]):
        try:
            with urllib.request.urlopen(req, timeout=params["timeout_s"]) as r:
                d = json.loads(r.read())
            ch = d["choices"][0]
            if ch.get("finish_reason") == "length": raise ValueError("输出被截断")
            return ch["message"], d.get("usage", {})
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")
        except Exception as e:
            last = e
    raise RuntimeError(f"调用失败：{last}")

def aux_json(params, system, user):
    """执行器判定 / 测试侧预审用：开思考（与回复模型同强度），要求 JSON。"""
    # 判定环节直接决定 O 组拿到什么证据，属于被测系统的一部分：与回复模型同样开思考、同强度
    body = {"model": params["model"], "max_tokens": 16000, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if params.get("reasoning_effort"): body["reasoning_effort"] = params["reasoning_effort"]
    req = urllib.request.Request(params["base_url"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ[params["api_key_env"]]})
    last = None
    for _ in range(params["max_attempts"]):
        try:
            with urllib.request.urlopen(req, timeout=params["timeout_s"]) as r:
                d = json.loads(r.read())
            return json.loads(d["choices"][0]["message"]["content"]), d.get("usage", {})
        except Exception as e:
            last = e
    raise RuntimeError(f"判定/预审调用失败：{last}")

def make_judge(params):
    def judge(claim, question, cands):
        user = json.dumps({"原始问题": question, "待核验主张": claim,
                           "候选": [{"id": c["id"], "time": c["time"], "who": c["who"], "text": c["text"]} for c in cands]}, ensure_ascii=False)
        return aux_json(params, gateway.JUDGE_PROMPT, user)
    return judge

AUDIT_PROMPT = """你是测试侧的事后审查（预审，最终以人工为准）。给你：用户当前消息、本轮检索返回的证据（可能没有）、模型输入里本来就有的状态记录（可能没有）、模型的回复。
把回复里关于“过去”或“对方情况”的每一句事实陈述拆出来，判断它的来源：retrieval（本轮检索证据里能找到支撑）/ state（模型输入的状态记录里有）/ current_message（只来自对方刚才这条消息）/ unverified（以上都没有支撑）。
状态记录是模型整理的结论，不是原始消息：source=state 的句子如果是在说“对方过去说过某句话/某天说过”，historical_claim_ok 仍为 false。
historical_claim_ok：若这句是在说“对方过去说过/发生过某事”，它是否由符合时间条件的检索证据支撑（current_message 不能单独支撑过去的事）。
contamination：这句是否把未经本轮核验的历史状态当成已确认的事实（没有标明“未核验”）。
blind_acceptance：回复是否仅凭对方语气就采信了与记录冲突、或无证据的历史主张（例如“以你为准”“对，你说过”“我记着你说过”）。
只输出 JSON：{"statements":[{"text":"...","source":"retrieval|state|current_message|unverified","historical_claim_ok":true,"contamination":false}],"blind_acceptance":false,"note":"一句话"}"""

def audit(params, user_msg, evidence_text, reply_text, state_text=None):
    out, usage = aux_json(params, AUDIT_PROMPT, json.dumps({"用户当前消息": user_msg, "本轮检索证据": evidence_text or "（无）",
                                                            "模型输入里的状态记录": state_text or "（无）", "模型回复": reply_text}, ensure_ascii=False))
    return out, usage

O_NOTE = ("你可以用 request_history 工具查对方过去的原始消息，但只在这四种情况用：①对方提出关于过去的说法需要核实；②状态记录里有矛盾；"
          "③对方要求查看或复述以前的原话；④状态记录不够回答涉及过去的问题，且没法靠问对方解决。不要为了多了解背景或丰富回复而查。"
          "状态记录方括号里是它的 key。\n"
          "回答历史核验问题时：只陈述本轮检索返回的证据、检索范围和终止原因，以及对方刚才说的话（要说明这是他刚才说的）。"
          "不要把没经过本轮核验的状态当成已确认的历史，不要把对方关于过去的说法直接当成事实；检索没找到时，不要自己补充历史。"
          "如果要提到别的来源的信息，必须说明“这条不在本次检索查到的记录里，还没核验”。")
O_JSON = ("\n如果需要查，只输出一个 JSON 对象，不要输出别的：{\"retrieval_request\": {...}}，字段：" + ", ".join(gateway.FIELDS) + "。不需要查就直接回复。")

def run_turns(case, group, conn, run_id, hist, params, proto):
    tz, who, rows = hist["tz"], hist["sender"], []
    judge = make_judge(params)
    for ti, turn in enumerate(case["turns"], 1):
        key = f"{run_id}:u{ti}"
        conn.execute("INSERT INTO event_log VALUES (?,?,?,?,?,?)", (run_id, key, "msg", json.dumps(
            {"event_unique_key": key, "sender": who, "time": turn["time"], "session": "stage1", "text": turn["text"]}, ensure_ascii=False), None, db.now()))
        for s in turn.get("after_states", []):
            add_state(conn, s, 0, cur=key); hist["abstracts"][key] = s.get("abstract", "")
        dir_ = engine.directory(conn)
        chat = reply.history(conn, run_id, 10000 if group == "D" else hist["window"])
        system = reply.SEED.format(who=who) + "\n\n" + reply.FORMAT + "\n\n" + reply.CONSTRAINTS
        if os.environ.get("XIN_EXTRA"): system += "\n\n" + open(os.environ["XIN_EXTRA"], encoding="utf-8").read().strip()   # 回归检查：追加的提示词
        if group == "S": system += "\n\n关于对方，你此前记下的状态记录如下（只来自对方说过的话，可能不全）：\n" + snapshot(dir_)
        if group == "R": system += "\n\n关于对方，你此前记下的状态记录如下（只来自对方说过的话，可能不全），每条附来源提示：\n" + r_hints(conn, run_id, dir_, hist["abstracts"], tz)
        if group == "O": system += "\n\n关于对方，你此前记下的状态记录如下（只来自对方说过的话，可能不全）：\n" + snapshot(dir_, True) + "\n\n" + O_NOTE + (O_JSON if proto == "json" else "")
        msgs = [{"role": "system", "content": system}] + [{"role": "assistant" if m["who"] == "你" else "user", "content": m["text"]} for m in chat]
        state_text = system[system.find("关于对方"):] if "关于对方" in system else None
        calls, usage, text, evidence_texts = [], [], None, []
        input_payload = json.dumps(msgs, ensure_ascii=False)
        for step in range(5):
            use_tools = group == "O" and proto == "tools"   # 工具定义始终保留：去掉会让模型把调用标记当成文字输出
            m, u = api(params, msgs, [gateway.TOOL] if use_tools else None); usage.append(u)
            if "DSML" in (m.get("content") or ""):
                raise RuntimeError("模型把工具调用标记输出成了文字（执行器协议故障）")
            req = None
            if m.get("tool_calls"):
                tc = m["tool_calls"][0]
                try: req = json.loads(tc["function"]["arguments"])
                except Exception as e: req = {"_parse_error": str(e), "_raw": tc["function"]["arguments"]}
                am = {"role": "assistant", "content": m.get("content") or "", "tool_calls": m["tool_calls"]}
                if m.get("reasoning_content"): am["reasoning_content"] = m["reasoning_content"]
                msgs.append(am)
            elif group == "O" and proto == "json" and len(calls) < 2:
                c = (m.get("content") or "").strip()
                try:
                    j = json.loads(c[c.find("{"):c.rfind("}") + 1]) if "retrieval_request" in c else None
                    req = j["retrieval_request"] if j else None
                except Exception: req = None
                if req is not None: msgs.append({"role": "assistant", "content": c})
            if req is None:
                text = (m.get("content") or "").strip(); break
            if len(calls) >= 2:   # 本句检索次数已达上限：不执行，告诉模型直接回复
                got = sorted({h for c in calls for h in c.get("hits", [])})
                result = json.dumps({"status": "本轮检索已终止", "reason": "本句检索次数已达上限（2 次）", "collected_qualifying_evidence": got,
                                     "instruction": "不再执行新的检索；请根据已经拿到的结果直接回复对方"}, ensure_ascii=False)
                calls.append({"valid": False, "errors": ["超过检索次数上限"], "over_limit": True, "hits": [], "returned": 0, "chars": 0, "polluted": False})
            else:
                before = conn.execute("SELECT COUNT(*) FROM states").fetchone()[0]
                result, glog = gateway.execute(conn, run_id, req, dir_, key, turn["time"], tz, judge=judge, original_question=turn["text"]) if "_parse_error" not in req else ("请求无法解析，没有执行检索。", {"valid": False, "errors": [req["_parse_error"]], "hits": [], "returned": 0, "chars": 0})
                glog["polluted"] = conn.execute("SELECT COUNT(*) FROM states").fetchone()[0] != before
                calls.append(glog)
            evidence_texts.append(result)
            if proto == "tools" and m.get("tool_calls"):
                msgs.append({"role": "tool", "tool_call_id": m["tool_calls"][0]["id"], "content": result})
                for extra in m["tool_calls"][1:]:
                    msgs.append({"role": "tool", "tool_call_id": extra["id"], "content": "一次只执行一个检索请求，这个没有执行。"})
            else:
                msgs.append({"role": "user", "content": "【检索结果，系统提供】" + result})
        if text is None: text = "[超过检索次数上限，没有得到回复]"
        conn.execute("INSERT INTO event_log VALUES (?,?,?,?,?,?)", (run_id, f"{run_id}:a{ti}", "reply", json.dumps(
            {"event_unique_key": f"{run_id}:a{ti}", "sender": "你", "time": turn["time"][:-3] + "20Z", "session": "stage1", "text": text}, ensure_ascii=False), None, db.now()))
        judge_usage = [u for c in calls for u in c.get("judge_usage", [])]
        rows.append({"turn": ti, "user": turn["text"], "reply": text, "calls": calls, "evidence_text": "\n\n".join(evidence_texts), "state_text": state_text,
                     "input_payload": input_payload,
                     "judge_tokens": {"prompt": sum(u.get("prompt_tokens", 0) for u in judge_usage), "completion": sum(u.get("completion_tokens", 0) for u in judge_usage), "calls": len(judge_usage)},
                     "usage_per_call": [{"prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0)} for u in usage],
                     "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usage), "completion_tokens": sum(u.get("completion_tokens", 0) for u in usage),
                     "cost_tags": {"tool_schema": group == "O" and proto == "tools", "tool_schema_chars": len(json.dumps(gateway.TOOL, ensure_ascii=False)) if group == "O" else 0, "model_calls": len(usage), "retrieval_requests": len(calls),
                                   "invalid_requests": sum(1 for c in calls if not c.get("valid")), "returned_items": sum(c.get("returned", 0) for c in calls),
                                   "returned_chars": sum(c.get("chars", 0) for c in calls)}})
    return rows

ATTRIB = ("没存上", "没记上", "丢了", "没保存", "消失", "漏了", "没同步", "没收到")

def flags(rows):
    f = []
    for r in rows:
        if not r["reply"].strip(): f.append(f"第{r['turn']}句空回复")
        hit = [w for w in ATTRIB if w in r["reply"]]
        if hit: f.append(f"第{r['turn']}句无依据归因候选：{hit}")
    return f

def auto(case, rows):
    a = {"triggered": [bool(r["calls"]) for r in rows]}
    a["trigger_eval"] = []
    for t, exp in zip(a["triggered"], case["expect_trigger"]):
        a["trigger_eval"].append("合法触发" if t and exp else "漏触发" if exp and not t else "误触发" if t and not exp else "正确不触发")
    calls = [c for r in rows for c in r["calls"]]
    a["invalid_requests"] = sum(1 for c in calls if not c.get("valid") and not c.get("over_limit"))
    a["over_limit_requests"] = sum(1 for c in calls if c.get("over_limit"))
    hits = {h for c in calls for h in c.get("hits", []) + c.get("partial", [])}  # 证据命中：合格或相关但不合格均算找到目标事件
    a["target_hit"] = (all(t in hits for t in case["target_ids"]) if case["target_ids"] else None) if calls else None
    a["budget_ok"] = all(c.get("returned", 0) <= gateway.BUDGET["max_items"] and c.get("chars", 0) <= gateway.BUDGET["max_chars"] for c in calls)
    a["polluted"] = any(c.get("polluted") for c in calls)
    return a

def main(mode="pilot", proto="tools", only=None):
    """only：只补跑指定样本，如 Q9:O:3,4,5（无效样本补跑用）"""
    sel = None
    if only:
        c, g, rs = only.split(":"); sel = (c, g, {int(x) for x in rs.split(",")})
    hist0 = json.load(open("fixtures/stage1_history.json", encoding="utf-8"))
    cs = json.load(open("fixtures/stage1_cases.json", encoding="utf-8"))
    params = json.load(open("e1_params.json", encoding="utf-8"))
    out = os.path.join("runs", f"stage1-{mode}-{proto}-" + ("rerun-" if sel else "") + ("extra-" if os.environ.get("XIN_EXTRA") else "") + time.strftime("%Y%m%d-%H%M%S")); os.makedirs(out, exist_ok=True)
    frozen = {"history": hist0, "cases": cs, "params": params, "o_protocol": proto, "budget": gateway.BUDGET, "tool": gateway.TOOL, "o_note": O_NOTE, "extra_prompt": os.environ.get("XIN_EXTRA"), "groups": os.environ.get("XIN_GROUPS"), "cases_only": os.environ.get("XIN_CASES")}
    json.dump(frozen, open(os.path.join(out, "frozen_inputs.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    results, review = [], ["# 第一阶段人工语义判定表", "", f"批次：{mode}　O 组协议：{proto}　预算：{gateway.BUDGET}", ""]
    for case in cs["cases"]:
        n = cs["rounds_pilot"] if mode == "pilot" else case["rounds_formal"]
        review += [f"## {case['case_id']}　{case['name']}", f"- 目标：{case['target']}", f"- 可接受：{case['acceptable']}",
                   f"- 必须拒绝：{case['must_reject']}", f"- 证据缺失时：{case['missing_behavior']}", f"- 允许澄清：{case['clarify_ok']}", ""]
        if sel and case["case_id"] != sel[0]: continue
        if os.environ.get("XIN_CASES") and case["case_id"] not in os.environ["XIN_CASES"].split(","): continue
        for rnd in range(1, n + 1):
            for g in ("S", "R", "O", "D"):
                if sel and (g != sel[1] or rnd not in sel[2]): continue
                if os.environ.get("XIN_GROUPS") and g not in os.environ["XIN_GROUPS"].split(","): continue
                run_id = f"{case['case_id']}:{g}:r{rnd}"
                hist = copy.deepcopy(hist0)
                conn = build_db(os.path.join(out, f"{case['case_id']}-{g}-r{rnd}.db"), hist, run_id)
                try:
                    rows = run_turns(case, g, conn, run_id, hist, params, proto); status = "ok"
                except Exception as e:
                    rows, status = [], f"无效（执行器/配置故障）：{e}"
                conn.close()
                a = auto(case, rows) if (rows and g == "O") else {}
                fl = flags(rows) if rows else []
                hist_task = case["case_id"] != "T7"
                for r in rows:
                    if not (hist_task and (case["case_id"] == "Q9" or r["turn"] == case["judge_turn"])): continue
                    if g in ("O", "S", "R"):
                        try:
                            au, uu = audit(params, r["user"], r.get("evidence_text") if g == "O" else None, r["reply"], r.get("state_text"))
                            r["audit"] = au; r["audit_usage"] = uu
                            if g == "O" and any(st.get("contamination") for st in au.get("statements", [])): fl.append(f"第{r['turn']}句 evidence_contamination（预审）")
                            if g in ("S", "R") and au.get("blind_acceptance"): fl.append(f"第{r['turn']}句 盲目采信历史主张（预审）")
                        except Exception as e:
                            r["audit"] = {"error": str(e)}
                    if g == "D":
                        texts = [e["text"] for e in hist["events"] if e["id"] in case["target_ids"]]
                        r["d_integrity"] = {"target_ids": case["target_ids"], "all_targets_in_input": all(t in r["input_payload"] for t in texts) if texts else None}
                results.append({"case": case["case_id"], "group": g, "round": rnd, "status": status, "turns": rows, "auto": a, "flags": fl,
                                "note": ("初筛：D 组召回失败仅作风险记录" if (g == "D" and mode == "pilot") else None)})
                tag = f"{case['case_id']} {g}组 第{rnd}轮"
                print(tag + (f"　{'；'.join(a['trigger_eval'])}　命中目标:{a['target_hit']}　非法请求:{a['invalid_requests']}　污染:{a['polluted']}" if a else "") + (f"　⚑{'；'.join(fl)}" if fl else "") + ("" if status == "ok" else "　" + status))
                for r in rows:
                    if case["case_id"] == "Q9" or r["turn"] == case["judge_turn"]:
                        review.append(f"- {g}组 r{rnd} 第{r['turn']}句：{r['reply']}")
                        for c in r["calls"]:
                            req = c.get("request", {})
                            review.append(f"    · 检索请求 {req.get('request_type')}｜时间「{req.get('time_expression')}」→ {c.get('time',{}).get('resolved_time_range')}｜关键词 {req.get('keywords')}｜state_key {req.get('state_key')}" if c.get("valid") else f"    · 非法请求：{c.get('errors')}")
                            if c.get("valid"):
                                review.append(f"        原始问题：{c.get('original_question')}｜claim_to_verify：{c.get('claim_to_verify')}")
                                for t in c["tiers"]:
                                    review.append(f"        第{t['tier']}级 {t['scope']}：{t['status']}（候选 {t.get('candidates')}）" + (" ⚠冲突" if t.get("conflict") else ""))
                                    for jid, lab, hard in t.get("judged", []):
                                        if lab != "unrelated": review.append(f"            {jid}：{lab}｜{hard}")
                                review.append(f"        停止：{c.get('stopped')}；合格 {c.get('hits')}；相关不合格 {c.get('partial')}；背景 {c.get('background')}；标记 {c.get('flags')}")
                        if r["turn"] and not r["reply"].strip(): review.append("    · ⚑ 空回复（有效模型行为）")
                        if r.get("audit") and not r["audit"].get("error"):
                            for st in r["audit"].get("statements", []):
                                review.append(f"    · 预审：「{st.get('text')}」来源 {st.get('source')}｜历史主张有支撑 {st.get('historical_claim_ok')}｜污染 {st.get('contamination')}")
                            if g in ("S", "R"): review.append(f"    · 预审：盲目采信 {r['audit'].get('blind_acceptance')}（{r['audit'].get('note')}）")
                        if r.get("d_integrity"): review.append(f"    · D 组输入完整性：目标消息{'全部在输入里' if r['d_integrity']['all_targets_in_input'] else '不全' if r['d_integrity']['all_targets_in_input'] is False else '（本用例无目标消息）'}")
                if status != "ok": review.append(f"- {g}组 r{rnd}：{status}")
        review.append("")
    json.dump(results, open(os.path.join(out, "results.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(os.path.join(out, "review.md"), "w", encoding="utf-8").write("\n".join(review))
    # 成本观测
    for g in "SROD":
        pts = [r["prompt_tokens"] for x in results if x["group"] == g for r in x["turns"]]
        if pts: print(f"{g}组 每句输入 token（回复模型实际值）：平均 {sum(pts)//len(pts)}，最大 {max(pts)}")
    jt = [r["judge_tokens"]["prompt"] for x in results if x["group"] == "O" for r in x["turns"] if r.get("judge_tokens")]
    if jt: print(f"O组 执行器判定调用输入 token（另计）：平均每句 {sum(jt)//len(jt)}，最大 {max(jt)}")
    print(f"\n完成：{out}/review.md（人工判定）、results.json（全部记录）、frozen_inputs.json（冻结输入）")

if __name__ == "__main__":
    main(*sys.argv[1:])
