"""第一阶段临时只读检索执行器（只用于测试，不接入正式链路）。
接收模型的结构化检索意图 → 校验白名单与字段 → 按阶梯在当前 run 事件日志里检索 → 返回有限原文。
只做 SELECT，不写快照、不写状态、不留存检索原文。"""
import json
from datetime import datetime, timedelta
from . import history

WHITELIST = {"historical_claim": "对方提出关于过去的说法，需要核实",
             "snapshot_conflict": "状态记录内部有矛盾，需要原始消息仲裁",
             "recall_request": "对方要求查看或复述以前的记录",
             "insufficient_snapshot": "状态记录不够回答涉及过去的问题，且没法靠问对方解决"}
FIELDS = {"request_type": str, "claim": str, "claim_to_verify": str, "entities": list, "time_expression": str, "time_range": dict, "keywords": list,
          "topic": str, "state_key": (str, type(None)), "verification_goal": str, "preferred_ref_ids": list, "clarification_needed": bool}
BUDGET = {"max_items": 8, "max_chars": 1200, "widen_days": 1, "fallback_items": 5}

TOOL = {"type": "function", "function": {
    "name": "request_history",
    "description": "查对方过去的原始消息。只在这四种情况使用：" + "；".join(f"{k}={v}" for k, v in WHITELIST.items()) +
                   "。不要为了多了解背景或丰富回复而使用。不确定的字段留空，不要编造 ID、日期、实体。",
    "parameters": {"type": "object", "required": list(FIELDS), "properties": {
        "request_type": {"type": "string", "enum": list(WHITELIST)},
        "claim": {"type": "string", "description": "需要核实的说法"},
        "claim_to_verify": {"type": "string", "description": "忠实对齐对方原始问题的待核验主张，不放大、不缩小、不替换。例：对方问“我昨天不是跟你说过我要搬家吗”，写“对方是否在昨天的消息中明确表达过搬家意图”，不能改成“昨天是否谈过房租或住处”"},
        "entities": {"type": "array", "items": {"type": "string"}},
        "time_expression": {"type": "string", "description": "对方原话里的时间说法，如“昨天”“上周”，没有就空字符串"},
        "time_range": {"type": "object", "properties": {"start": {"type": ["string", "null"]}, "end": {"type": ["string", "null"]}}},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "topic": {"type": "string"},
        "state_key": {"type": ["string", "null"], "description": "状态记录里方括号中的 key，不确定就 null"},
        "verification_goal": {"type": "string"},
        "preferred_ref_ids": {"type": "array", "items": {"type": "string"}, "description": "只填上下文里明确出现过的事件 ID，否则空"},
        "clarification_needed": {"type": "boolean"}}}}}


def validate(req):
    errs = []
    if not isinstance(req, dict):
        return ["请求不是对象"]
    for f, t in FIELDS.items():
        if f not in req:
            errs.append(f"缺字段 {f}")
        elif not isinstance(req[f], t):
            errs.append(f"字段 {f} 类型错")
    if req.get("request_type") not in WHITELIST:
        errs.append(f"request_type 不在白名单：{req.get('request_type')}")
    return errs


def _events(conn, run_id, where, args, exclude, before_iso):
    rows = conn.execute(f"SELECT event_unique_key, result, payload FROM event_log WHERE run_id=? AND {where} "
                        "ORDER BY json_extract(payload,'$.time')", (run_id, *args)).fetchall()
    out = []
    for r in rows:
        e = json.loads(r["payload"])
        if r["event_unique_key"] == exclude or e["time"] > before_iso:
            continue
        out.append({"id": r["event_unique_key"], "who": "你" if r["result"] == "reply" else e["sender"], "time": e["time"], "text": e["text"]})
    return out


def _kw(evs, kws):
    kws = [k for k in kws if k]
    return [e for e in evs if any(k in e["text"] for k in kws)] if kws else evs


JUDGE_PROMPT = """你是检索执行器的证据判定环节，只判断候选消息与待核验主张的关系，不回答用户。
对每条候选，标 supports（直接支持主张）/ refutes（直接反驳主张）/ background（只是背景、侧面相关、附带信息）/ unrelated（无关）。
“没提到”不等于反驳；只有消息内容与主张无法同时成立才是 refutes。
如果待核验主张本身模糊到无法证伪，claim_status 填 ambiguous；为空或不是可核验的说法，填 invalid；否则填 ok。
只输出 JSON：{"claim_status":"ok|ambiguous|invalid","labels":[{"id":"...","label":"supports|refutes|background|unrelated","why":"一句话"}]}"""


def norm_time(v, tz):
    """把模型给的时间转成 UTC ISO。接受 ISO（带 Z）、YYYY-MM-DD、YYYY-MM-DD HH:MM[:SS]（按对话时区）。无法解析返回 None。"""
    from zoneinfo import ZoneInfo
    from datetime import timezone as _tz
    if not isinstance(v, str): return None
    v = v.strip()
    for fmt, local in (("%Y-%m-%dT%H:%M:%SZ", False), ("%Y-%m-%dT%H:%M:%S", True), ("%Y-%m-%d %H:%M:%S", True), ("%Y-%m-%d %H:%M", True), ("%Y-%m-%d", True)):
        try:
            d = datetime.strptime(v, fmt)
        except ValueError:
            continue
        d = d.replace(tzinfo=ZoneInfo(tz) if local else _tz.utc)
        return d.astimezone(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def hard_check(e, rng, current_key, ids):
    reasons = []
    if e["id"] == current_key: reasons.append("是当前消息，不能替代历史")
    if rng and not (rng["start_utc"] <= e["time"] < rng["end_utc"]): reasons.append("不在要求的时间范围内")
    if ids and e["id"] not in ids: reasons.append("不是指定的事件 ID")
    return reasons


def execute(conn, run_id, req, dir_, current_key, now_iso, tz, judge=None, original_question=""):
    """两阶段：候选召回（只缩小范围）→ 证据支撑判定（硬性条件由程序校验，语义由判定环节标注）。
    只有 qualifying_hit（硬性条件全过 + 直接支持或反驳 + 无未解决冲突）才终止。"""
    log = {"request": req, "valid": True, "tiers": [], "hits": [], "returned": 0, "chars": 0, "stopped": None, "notes": [],
           "original_question": original_question, "claim_to_verify": req.get("claim_to_verify") if isinstance(req, dict) else None,
           "time": {"time_expression": req.get("time_expression") if isinstance(req, dict) else None, "anchor": now_iso, "tz": tz, "resolved_time_range": None},
           "judge_calls": 0, "judge_usage": [], "flags": []}
    errs = validate(req)
    if errs:
        log.update(valid=False, errors=errs)
        return "检索请求不合法：" + "；".join(errs) + "。没有执行检索。", log
    if req["clarification_needed"]:
        log["stopped"] = "clarify"
        return "你在请求里标了需要先澄清，按约定没有执行检索；请先向对方问清楚。", log
    claim = req["claim_to_verify"].strip()
    if not claim:
        log.update(stopped="claim_invalid"); log["flags"].append("claim_invalid")
        return "检索请求里没有可核验的主张（claim_to_verify 为空），没有执行检索；需要的话先向对方问清楚。", log
    kws = [k for k in req["keywords"] if k]
    rng = None
    tr = req["time_range"] or {}
    if tr.get("start") or tr.get("end"):
        s_utc, e_utc = norm_time(tr.get("start"), tz), norm_time(tr.get("end"), tz)
        if s_utc and e_utc and s_utc < e_utc:
            if len(str(tr.get("end")).strip()) == 10:   # 只给日期的结束日，按当天结束（左闭右开取次日 00:00）
                e_utc = (datetime.strptime(e_utc, "%Y-%m-%dT%H:%M:%SZ") + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rng = {"start_utc": s_utc, "end_utc": e_utc, "start_local": history.local(s_utc, tz), "end_local": history.local(e_utc, tz), "tz": tz + "（模型给出的区间）"}
        else:
            log["notes"].append(f"time_range 无法解析或不完整：{tr}，改用 time_expression")
            log["flags"].append("time_range_unparsable")
    if not rng and req["time_expression"]:
        rng = history.resolve(req["time_expression"], now_iso, tz)
        if not rng: log["notes"].append(f"时间说法无法解析：{req['time_expression']}")
    if rng: log["time"]["resolved_time_range"] = f"[{rng['start_local']}, {rng['end_local']}) {rng['tz']}（左闭右开，按自然日）"
    all_refs = {r for x in dir_["states"] for r in x["evidence_ref_list"]}
    ids = set()
    for rid in req["preferred_ref_ids"]:
        if rid in all_refs: ids.add(rid)
        else: log["notes"].append(f"忽略不在当前 ref 内的 ID：{rid}")
    labels = {}       # 判定缓存：id → (label, why)
    qualifying, partial, background, stop = {}, {}, {}, None

    def judge_all(evs):
        todo = [e for e in evs if e["id"] not in labels]
        if not todo: return "ok"
        if judge is None:
            for e in todo: labels[e["id"]] = ("background", "没有判定环节")
            return "ok"
        out, usage = judge(claim, original_question, todo)
        log["judge_calls"] += 1; log["judge_usage"].append(usage)
        for l in out.get("labels", []):
            if l.get("id") in {e["id"] for e in todo}:
                labels[l["id"]] = (l.get("label", "unrelated"), l.get("why", ""))
        for e in todo: labels.setdefault(e["id"], ("unrelated", "判定环节未返回"))
        return out.get("claim_status", "ok")

    def tier(n, scope, evs):
        nonlocal stop
        evs = evs[:12]   # 每级候选上限
        cs = judge_all(evs) if evs else "ok"
        if cs in ("ambiguous", "invalid"):
            log["flags"].append(f"claim_{cs}"); stop = f"claim_{cs}"
            log["tiers"].append({"tier": n, "scope": scope, "candidates": len(evs), "status": f"claim_{cs}"}); return
        q_new, detail = [], []
        for e in evs:
            lab, why = labels[e["id"]]
            hf = hard_check(e, rng, current_key, ids)
            detail.append((e["id"], lab, "；".join(hf) or "硬性条件通过"))
            if lab in ("supports", "refutes"):
                (partial if hf else qualifying).setdefault(e["id"], dict(e, label=lab, why=why, hard="；".join(hf)))
                if not hf: q_new.append(e["id"])
            elif lab == "background" and not hf:
                background.setdefault(e["id"], dict(e, label=lab, why=why))
        st = "qualifying_hit" if q_new else "partial_match" if any(d[1] in ("supports", "refutes") for d in detail) else \
             "non_qualifying_hit" if any(d[1] == "background" for d in detail) else "no_match"
        conflict = {"supports", "refutes"} <= {v["label"] for v in qualifying.values()}
        if conflict and "conflicting_evidence" not in log["flags"]: log["flags"].append("conflicting_evidence")
        log["tiers"].append({"tier": n, "scope": scope, "candidates": len(evs), "status": st, "conflict": conflict, "judged": detail})
        if st == "qualifying_hit" and not conflict: stop = f"tier{n} qualifying_hit"

    refs = set(ids)
    sk = req["state_key"]
    if sk:
        cur = dir_["current_valid"].get(sk)
        st = [x for x in dir_["states"] if x["state_key"] == sk and (cur is None or x["state_version"] == cur)]
        if st: refs |= set(st[-1]["evidence_ref_list"])
        else: log["notes"].append(f"state_key 不存在：{sk}")
    if refs:
        q = ",".join("?" * len(refs))
        tier(1, f"当前 ref {len(refs)} 个", _events(conn, run_id, f"event_unique_key IN ({q})", list(refs), None, now_iso))
    if not stop and rng:
        evs = _events(conn, run_id, "json_extract(payload,'$.time') >= ? AND json_extract(payload,'$.time') < ?", (rng["start_utc"], rng["end_utc"]), current_key, now_iso)
        tier(2, f"[{rng['start_local']}, {rng['end_local']}) {rng['tz']}", evs)
    if not stop and rng and kws:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        s0 = (datetime.strptime(rng["start_utc"], fmt) - timedelta(days=BUDGET["widen_days"])).strftime(fmt)
        e0 = (datetime.strptime(rng["end_utc"], fmt) + timedelta(days=BUDGET["widen_days"])).strftime(fmt)
        tier(3, f"[{history.local(s0, tz)}, {history.local(e0, tz)})", _kw(_events(conn, run_id, "json_extract(payload,'$.time') >= ? AND json_extract(payload,'$.time') < ?", (s0, e0), current_key, now_iso), kws))
    if not stop and kws:
        tier(4, "本 run 事件日志关键词召回", _kw(_events(conn, run_id, "1=1", (), current_key, now_iso), kws)[:BUDGET["fallback_items"]])
    if not stop:
        stop = "级数用完，存在未解决的证据冲突" if "conflicting_evidence" in log["flags"] else "级数用完，未找到满足全部条件的证据"
    log["stopped"] = stop
    blocks, chars, used = [], 0, []
    def add(title, evs, extra):
        nonlocal chars
        lines = []
        for e in evs:
            if len(used) >= BUDGET["max_items"]: log["notes"].append("达到条数预算"); break
            mark = "（就是对方刚才这条消息）" if e["id"] == current_key else ""
            line = f"- [{e['id']}] {history.local(e['time'], tz)} {e['who']}：{e['text']}{mark}" + (f"　← {extra(e)}" if extra(e) else "")
            if chars + len(line) > BUDGET["max_chars"]: log["notes"].append("达到字数预算"); break
            lines.append(line); chars += len(line); used.append(e["id"])
        if lines: blocks.append(title + "\n" + "\n".join(lines))
    lab_cn = {"supports": "支持该主张", "refutes": "反驳该主张"}
    add("能直接支持或反驳该主张、且满足全部条件的原始消息：", list(qualifying.values()), lambda e: lab_cn[e["label"]])
    add("与主张直接相关、但不满足硬性条件的消息（不能作为该主张的证据）：", list(partial.values()), lambda e: f"{lab_cn[e['label']]}，但{e['hard']}")
    if not qualifying: add("要求范围内的其他消息（只是背景，不能支持或反驳该主张）：", list(background.values()), lambda e: "")
    log.update(hits=list(qualifying), partial=list(partial), background=list(background), returned=len(used), chars=chars)
    scope = "；".join(f"第{t['tier']}级 {t['scope']}→{t['status']}" for t in log["tiers"]) or "没有可执行的检索条件"
    head = f"待核验主张：{claim}。检索范围：{scope}。" + (f"时间按 {log['time']['resolved_time_range']} 解析。" if rng else "") + f"终止原因：{stop}。"
    if stop.startswith("claim_"):
        return head + "这个主张不够明确，无法核验，没有继续扩大检索；需要的话先向对方问清楚。", log
    if "conflicting_evidence" in log["flags"]:
        head += "已收集的证据之间存在直接矛盾，不能据此给出确定结论。"
    elif not qualifying:
        head += "在本次检索覆盖的范围内，没有找到能支持或反驳该主张的原始消息；范围之外的情况无法确认，也不要推测原因。"
    return head + ("\n" + "\n\n".join(blocks) if blocks else ""), log
