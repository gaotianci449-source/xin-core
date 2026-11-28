"""自然聊天先导实验 V0.3 执行脚本。
用法：python3 natpilot/run_nat.py dry   # 只检查材料与流程，不调用模型
      python3 natpilot/run_nat.py run   # 正式运行，需要环境变量 MODEL_API_KEY
材料：natpilot/materials.json（由 build_materials.py 从 V0.3 原文解析，含源文件 sha256）
参数：e1_params.json 的 model / base_url / reasoning_effort（与第二十轮相同）；max_tokens=16000；temperature/top_p 不设置。
输出：runs/natpilot-<时间>/
  raw.jsonl            每次 API 尝试的完整请求与响应
  messages.jsonl       逐条消息日志（V0.3 第10节字段）
  blind_sheet.md       主盲评副本（隐去条件、system；第3轮后反馈及其回复整体遮蔽，A/B 同位置放同一占位符）
  feedback_aux.md      反馈响应辅助记录（仅 C/D，使用独立编号）
  key.json             揭盲表（主盲评编号、辅助编号 → 会话）
  summary.md           运行概况
运行顺序严格按材料中的序号 1—36，不重新随机。只报告输出，不做判定。"""
import hashlib, json, os, random, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
M = json.load(open(os.path.join(HERE, "materials.json"), encoding="utf-8"))
P = json.load(open(os.path.join(HERE, "..", "e1_params.json"), encoding="utf-8"))
MAX_TOKENS = 16000
PLACEHOLDER = "[实验提示与响应已遮蔽]"
ENDPOINT = P["base_url"].rstrip("/") + "/chat/completions"
CNT = {"model_calls": 0, "attempts": 0, "retries": 0, "truncated": 0, "content_filter": 0, "failed_rounds": 0}

def system_for(cond):
    s = M["base_system"]
    if M["conditions"][cond]["guidance"]: s += M["guidance_join"] + M["guidance"]
    return s

def sha(s): return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

def _post(body):
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["MODEL_API_KEY"]})
    with urllib.request.urlopen(req, timeout=P.get("timeout_s", 300)) as r:
        return r.status, json.loads(r.read())

def judge(d):
    """返回 (标记, 是否允许重试)。V0.3 第9节：接口错误/超时/未获得完整有效响应可重试1次；截断、内容过滤不重试。"""
    ch = (d.get("choices") or [{}])[0]; m = ch.get("message") or {}; fr = ch.get("finish_reason")
    if fr == "stop": return ("完整", False) if (m.get("content") or "").strip() else ("空响应", True)
    if fr in ("length", "truncated"): return "输出截断", False
    if fr == "content_filter": return "内容过滤", False
    if fr is None: return "完整性无法核验（未返回 finish_reason）", False
    return f"完整性待核验（finish_reason：{fr}）", False

def call(messages, raw, meta):
    body = {"model": P["model"], "messages": messages, "max_tokens": MAX_TOKENS}
    if P.get("reasoning_effort"): body["reasoning_effort"] = P["reasoning_effort"]
    CNT["model_calls"] += 1
    for attempt in (1, 2):
        CNT["attempts"] += 1
        if attempt == 2: CNT["retries"] += 1
        rec = {**meta, "attempt": attempt, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "request": body}
        t0 = time.time(); d = None
        try:
            status, d = _post(body); flag, retry = judge(d)
            rec.update({"status_code": status, "response": d, "error": None, "integrity": flag})
        except urllib.error.HTTPError as e:
            flag, retry = "接口错误", True
            rec.update({"status_code": e.code, "error": e.read()[:500].decode("utf-8", "replace"), "integrity": flag})
        except Exception as e:
            flag, retry = "调用异常/超时", True
            rec.update({"status_code": None, "error": repr(e), "integrity": flag})
        rec["latency_s"] = round(time.time() - t0, 1)
        raw.write(json.dumps(rec, ensure_ascii=False) + "\n"); raw.flush()
        if not retry: return d, flag, attempt, rec["latency_s"], rec.get("error")
    return None, "失败（两次尝试均未获得有效响应）", 2, rec["latency_s"], rec.get("error")

def run_session(item, raw, mlog, blind_id):
    sid, sample, cond = item["session_id"], item["sample_id"], item["condition"]
    sysmsg = system_for(cond)
    base = {"experiment_version": M["experiment_version"], "session_order": item["run_order"], "session_id": sid,
            "sample_id": sample, "condition": cond, "blind_copy_id": blind_id}
    common = {"model_name": P["model"], "endpoint": ENDPOINT, "system_prompt_hash": sha(sysmsg),
              "params": {"thinking": "开启（接口默认）", "reasoning_effort": P.get("reasoning_effort"), "max_tokens": MAX_TOKENS,
                         "temperature": "未设置（接口默认）", "top_p": "未设置（接口默认）"}}
    def logmsg(**kw):
        mlog.write(json.dumps({**base, **common, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **kw}, ensure_ascii=False) + "\n"); mlog.flush()
    msgs = [{"role": "system", "content": sysmsg}]
    logmsg(message_index=0, role="system", exact_content=sysmsg, feedback_event=False)
    transcript, flags, stopped = [], [], None
    steps = []
    for i, t in enumerate(M["samples"][sample]["turns"], 1):
        steps.append((i, t, False))
        if i == M["feedback_after_round"] and M["conditions"][cond]["feedback"]:
            steps.append(("反馈", M["feedback"], True))
    for idx, text, fb in steps:
        msgs.append({"role": "user", "content": text})
        logmsg(message_index=idx, role="user", exact_content=text, feedback_event=fb)
        d, flag, attempt, lat, err = call(msgs, raw, {**base, "message_index": idx, "feedback_event": fb})
        flags.append((idx, flag))
        if d is None:
            CNT["failed_rounds"] += 1
            logmsg(message_index=idx, role="assistant", exact_content=None, feedback_event=fb, error=err, retry=attempt - 1,
                   truncation=False, content_filter=False, integrity=flag, latency_s=lat)
            transcript.append((idx, fb, text, None, flag)); stopped = idx
            break
        ch = d["choices"][0]; m = ch.get("message") or {}; content = m.get("content") or ""
        if flag == "输出截断": CNT["truncated"] += 1
        if flag == "内容过滤": CNT["content_filter"] += 1
        logmsg(message_index=idx, role="assistant", exact_content=content, reasoning_content=m.get("reasoning_content"),
               feedback_event=fb, response_id=d.get("id"), returned_model=d.get("model"), finish_reason=ch.get("finish_reason"),
               usage=d.get("usage"), latency_s=lat, error=None, retry=attempt - 1, truncation=flag == "输出截断",
               content_filter=flag == "内容过滤", integrity=flag)
        msgs.append({"role": "assistant", "content": content})
        transcript.append((idx, fb, text, content, flag))
    return transcript, flags, stopped

def check():
    """执行前核对（V0.3 第10、11节）：36会话唯一、每会话7轮、反馈只在 C/D 第3轮后且仅一次。"""
    o = M["order"]
    assert [x["run_order"] for x in o] == list(range(1, 37))
    assert len({x["session_id"] for x in o}) == 36
    for x in o:
        assert x["session_id"] == f"{x['sample_id']}-{x['condition']}"
        assert len(M["samples"][x["sample_id"]]["turns"]) == 7
    return o

def main(mode):
    order = check()
    print(f"材料：{M['source_doc']}  sha256 {M['source_doc_sha256'][:16]}…；序列表 sha256 {M['source_csv_sha256'][:16]}…")
    for c in "ABCD": print(f"system[{c}] hash {sha(system_for(c))}：{system_for(c)!r}")
    if mode == "dry":
        for x in order:
            rounds = ["1", "2", "3"] + (["反馈"] if M["conditions"][x["condition"]]["feedback"] else []) + ["4", "5", "6", "7"]
            print(f"{x['run_order']:>2} {x['session_id']}  调用顺序：{' → '.join(rounds)}")
        n = sum(7 + M["conditions"][x["condition"]]["feedback"] for x in order)
        print(f"计划会话 36；计划模型调用 {n}（不含重试）"); return
    out = os.path.join(HERE, "..", "runs", "natpilot-" + time.strftime("%Y%m%d-%H%M%S")); os.makedirs(out)
    seed = int(time.time()); rng = random.Random(seed)
    blind = dict(zip([x["session_id"] for x in order], rng.sample(range(1000, 9999), 36)))
    fb_sessions = [x["session_id"] for x in order if M["conditions"][x["condition"]]["feedback"]]
    aux = dict(zip(fb_sessions, rng.sample(range(100, 999), len(fb_sessions))))
    raw = open(os.path.join(out, "raw.jsonl"), "w", encoding="utf-8")
    mlog = open(os.path.join(out, "messages.jsonl"), "w", encoding="utf-8")
    res = {}
    for x in order:
        sid = x["session_id"]
        res[sid] = run_session(x, raw, mlog, blind[sid])
        bad = [f"轮{i}:{f}" for i, f in res[sid][1] if f != "完整"]
        print(f"[{x['run_order']}/36] {sid} 完成" + (f"（{'；'.join(bad)}）" if bad else ""), flush=True)
    raw.close(); mlog.close()
    # 主盲评副本
    L = ["# 自然聊天先导｜主盲评副本", "",
         f"条件、system 与运行顺序已隐去。第3轮之后统一显示「{PLACEHOLDER}」（所有会话相同位置）。评分完成前不要打开 key.json。", ""]
    for sid in sorted(res, key=lambda s: blind[s]):
        L += [f"## #{blind[sid]}", ""]
        for idx, fb, u, a, flag in res[sid][0]:
            if fb: continue
            L.append(f"**轮{idx}·用户**：{u}"); L.append("")
            L.append(f"**轮{idx}·模型**：{a if a is not None else '（该轮失败：' + flag + '）'}" + ("" if flag == "完整" or a is None else f"　〔{flag}〕")); L.append("")
            if idx == M["feedback_after_round"]: L.append(PLACEHOLDER); L.append("")
        if res[sid][2]: L += [f"（会话在轮{res[sid][2]}因调用失败中止）", ""]
    open(os.path.join(out, "blind_sheet.md"), "w", encoding="utf-8").write("\n".join(L))
    # 反馈响应辅助记录
    A = ["# 自然聊天先导｜反馈响应辅助记录（仅含有反馈的会话）", "",
         "本材料本身会暴露“有反馈”条件；编号与主盲评编号无关。只编码模型对反馈的即时回复（确认/复述要求、解释或辩护、提出澄清、未回应反馈、其他），不纳入指标1—4。", "",
         f"反馈原文：「{M['feedback']}」", ""]
    for sid in sorted(fb_sessions, key=lambda s: aux[s]):
        r = next((t for t in res[sid][0] if t[1]), None)
        A += [f"## 辅助#{aux[sid]}", "", f"**模型对反馈的回复**：{r[3] if r and r[3] is not None else '（未获得）'}", ""]
    open(os.path.join(out, "feedback_aux.md"), "w", encoding="utf-8").write("\n".join(A))
    json.dump({"主盲评编号": {str(blind[s]): s for s in blind}, "辅助编号": {str(aux[s]): s for s in aux}},
              open(os.path.join(out, "key.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    planned = sum(7 + M["conditions"][x["condition"]]["feedback"] for x in order)
    odd = [f"- {s}：" + "；".join(f"轮{i} {f}" for i, f in v[1] if f != "完整") for s, v in res.items() if any(f != "完整" for _, f in v[1])]
    S = ["# 运行概况", f"- 实验版本：{M['experiment_version']}（{M['source_doc']}，sha256 {M['source_doc_sha256']}）",
         f"- 模型：{P['model']}　接口：{ENDPOINT}　思考：开启（接口默认）强度 {P.get('reasoning_effort')}　max_tokens：{MAX_TOKENS}　temperature/top_p：未设置（接口默认）",
         f"- system 哈希：A/C {sha(system_for('A'))}　B/D {sha(system_for('B'))}",
         f"- 编号随机种子：{seed}（运行顺序按序列表固定，未重新随机）",
         f"- 计划会话 36　完成 {sum(1 for v in res.values() if not v[2])}　中途中止 {sum(1 for v in res.values() if v[2])}",
         f"- 模型调用 {CNT['model_calls']}（计划 {planned}）　实际尝试 {CNT['attempts']}　重试 {CNT['retries']}　失败轮 {CNT['failed_rounds']}　截断 {CNT['truncated']}　内容过滤 {CNT['content_filter']}"]
    if odd: S += ["", "## 非“完整”的响应（保留，不删除）"] + odd
    open(os.path.join(out, "summary.md"), "w", encoding="utf-8").write("\n".join(S))
    print("\n".join(S)); print(f"\n输出：{out}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dry")
