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
FIELDS = {"request_type": str, "claim": str, "entities": list, "time_expression": str, "time_range": dict, "keywords": list,
          "topic": str, "state_key": (str, type(None)), "verification_goal": str, "preferred_ref_ids": list, "clarification_needed": bool}
BUDGET = {"max_items": 8, "max_chars": 1200, "widen_days": 1, "fallback_items": 5}

TOOL = {"type": "function", "function": {
    "name": "request_history",
    "description": "查对方过去的原始消息。只在这四种情况使用：" + "；".join(f"{k}={v}" for k, v in WHITELIST.items()) +
                   "。不要为了多了解背景或丰富回复而使用。不确定的字段留空，不要编造 ID、日期、实体。",
    "parameters": {"type": "object", "required": list(FIELDS), "properties": {
        "request_type": {"type": "string", "enum": list(WHITELIST)},
        "claim": {"type": "string", "description": "需要核实的说法"},
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


def _classify(evs, kws, rng, current_key, ids, base_relevant):
    """命中状态：qualifying_hit / partial_match / non_qualifying_hit / no_match。硬性条件由程序校验。"""
    if not evs:
        return "no_match", [], []
    # 一级（当前 ref）的事件本身就是状态的证据，视为相关；其余级别有关键词时按关键词判断相关性
    rel = evs if base_relevant == "ref" else [e for e in evs if (kws and any(k in e["text"] for k in kws)) or (not kws and base_relevant)]
    def hard_ok(e):
        reasons = []
        if e["id"] == current_key: reasons.append("是当前消息，不能替代历史")
        if rng and not (rng["start_utc"] <= e["time"] < rng["end_utc"]): reasons.append("不在要求的时间范围内")
        if ids and e["id"] not in ids: reasons.append("不是指定的事件 ID")
        return reasons
    qual = [e for e in rel if not hard_ok(e)]
    part = [dict(e, why="；".join(hard_ok(e))) for e in rel if hard_ok(e)]
    if qual: return "qualifying_hit", qual, part
    if part: return "partial_match", [], part
    return "non_qualifying_hit", [], []


def execute(conn, run_id, req, dir_, current_key, now_iso, tz):
    """返回 (给模型看的文本, 日志)。只有 qualifying_hit 才终止；其余继续下一级，直到预算或级数用完。"""
    log = {"request": req, "valid": True, "tiers": [], "hits": [], "returned": 0, "chars": 0, "stopped": None, "notes": [],
           "time": {"time_expression": req.get("time_expression") if isinstance(req, dict) else None, "anchor": now_iso, "tz": tz, "resolved_time_range": None}}
    errs = validate(req)
    if errs:
        log.update(valid=False, errors=errs)
        return "检索请求不合法：" + "；".join(errs) + "。没有执行检索。", log
    if req["clarification_needed"]:
        log["stopped"] = "clarify"
        return "你在请求里标了需要先澄清，按约定没有执行检索；请先向对方问清楚。", log
    kws = [k for k in req["keywords"] if k]
    # 时间硬性条件
    rng = None
    tr = req["time_range"] or {}
    if tr.get("start") and tr.get("end"):
        rng = {"start_utc": tr["start"], "end_utc": tr["end"], "start_local": tr["start"], "end_local": tr["end"], "tz": "UTC"}
    elif req["time_expression"]:
        rng = history.resolve(req["time_expression"], now_iso, tz)
        if not rng: log["notes"].append(f"时间说法无法解析：{req['time_expression']}")
    if rng: log["time"]["resolved_time_range"] = f"[{rng['start_local']}, {rng['end_local']}) {rng['tz']}（左闭右开，按自然日）"
    all_refs = {r for s in dir_["states"] for r in s["evidence_ref_list"]}
    ids = set()
    for rid in req["preferred_ref_ids"]:
        if rid in all_refs: ids.add(rid)
        else: log["notes"].append(f"忽略不在当前 ref 内的 ID：{rid}")
    qualifying, partial, context, stop = [], {}, [], None
    def record(tier, scope, evs, base_relevant):
        status, q, p = _classify(evs, kws, rng, current_key, ids, base_relevant)
        log["tiers"].append({"tier": tier, "scope": scope, "returned": len(evs), "status": status,
                             "basis": f"关键词{kws or '无'}；时间条件{'有' if rng else '无'}；指定ID{sorted(ids) or '无'}",
                             "hard_fail": [(e["id"], e["why"]) for e in p]})
        for e in p: partial.setdefault(e["id"], e)
        return status, q
    # 一级：当前 ref
    refs = set(ids)
    sk = req["state_key"]
    if sk:
        cur = dir_["current_valid"].get(sk)
        st = [x for x in dir_["states"] if x["state_key"] == sk and (cur is None or x["state_version"] == cur)]
        if st: refs |= set(st[-1]["evidence_ref_list"])
        else: log["notes"].append(f"state_key 不存在：{sk}")
    if refs:
        q = ",".join("?" * len(refs))
        evs = _events(conn, run_id, f"event_unique_key IN ({q})", list(refs), None, now_iso)
        status, qual = record(1, f"当前 ref {len(refs)} 个", evs, "ref")
        if status == "qualifying_hit": qualifying, stop = qual, "tier1 qualifying_hit"
    # 二级：时间区间
    if not stop and rng:
        evs = _events(conn, run_id, "json_extract(payload,'$.time') >= ? AND json_extract(payload,'$.time') < ?", (rng["start_utc"], rng["end_utc"]), current_key, now_iso)
        status, qual = record(2, f"[{rng['start_local']}, {rng['end_local']}) {rng['tz']}", evs, True)
        if status == "qualifying_hit": qualifying, stop = qual, "tier2 qualifying_hit"
        elif status == "non_qualifying_hit": context = evs
    # 三级：放宽（必须有关键词）
    if not stop and rng and kws:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        s0 = (datetime.strptime(rng["start_utc"], fmt) - timedelta(days=BUDGET["widen_days"])).strftime(fmt)
        e0 = (datetime.strptime(rng["end_utc"], fmt) + timedelta(days=BUDGET["widen_days"])).strftime(fmt)
        evs = _events(conn, run_id, "json_extract(payload,'$.time') >= ? AND json_extract(payload,'$.time') < ?", (s0, e0), current_key, now_iso)
        status, qual = record(3, f"[{history.local(s0, tz)}, {history.local(e0, tz)})", evs, False)
        if status == "qualifying_hit": qualifying, stop = qual, "tier3 qualifying_hit"
    # 兜底：关键词
    if not stop and kws:
        evs = _kw(_events(conn, run_id, "1=1", (), current_key, now_iso), kws)[:BUDGET["fallback_items"]]
        status, qual = record(4, "本 run 事件日志关键词召回", evs, False)
        if status == "qualifying_hit": qualifying, stop = qual, "tier4 qualifying_hit"
    log["stopped"] = stop or "级数用完，未找到满足全部硬性条件的证据"
    # 组织回传：合格证据 > 相关但不满足硬性条件 > 时间段内的其他消息（上下文）
    blocks, chars, used = [], 0, []
    def add(title, evs, why=False):
        nonlocal chars
        lines = []
        for e in evs:
            if len(used) >= BUDGET["max_items"]: log["notes"].append("达到条数预算"); break
            mark = "（就是对方刚才这条消息）" if e["id"] == current_key else ""
            line = f"- [{e['id']}] {history.local(e['time'], tz)} {e['who']}：{e['text']}{mark}" + (f"　← {e['why']}" if why and e.get("why") else "")
            if chars + len(line) > BUDGET["max_chars"]: log["notes"].append("达到字数预算"); break
            lines.append(line); chars += len(line); used.append(e["id"])
        if lines: blocks.append(title + "\n" + "\n".join(lines))
    add("满足全部条件的原始消息：", qualifying)
    add("相关但不满足硬性条件的消息（不能当作对方说法的证据）：", list(partial.values()), True)
    if not qualifying: add("要求的时间段内的其他消息（与核验目标无关，仅供参考）：", context)
    log.update(hits=[e["id"] for e in qualifying], partial=list(partial), returned=len(used), chars=chars)
    scope = "；".join(f"第{t['tier']}级 {t['scope']}→{t['status']}" for t in log["tiers"]) or "没有可执行的检索条件"
    head = f"检索范围：{scope}。" + (f"时间按 {log['time']['resolved_time_range']} 解析。" if rng else "")
    if qualifying:
        return head + "\n" + "\n\n".join(blocks), log
    return head + "在本次检索覆盖的范围内，没有找到满足全部条件的原始消息；范围之外的情况无法确认，也不要推测原因。" + ("\n" + "\n\n".join(blocks) if blocks else ""), log
