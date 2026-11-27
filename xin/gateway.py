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


def execute(conn, run_id, req, dir_, current_key, now_iso, tz):
    """返回 (给模型看的文本, 日志)。"""
    log = {"request": req, "valid": True, "tiers": [], "hits": [], "returned": 0, "chars": 0, "stopped": None, "notes": []}
    errs = validate(req)
    if errs:
        log.update(valid=False, errors=errs)
        return "检索请求不合法：" + "；".join(errs) + "。没有执行检索。", log
    if req["clarification_needed"]:
        log["stopped"] = "clarify"
        return "你在请求里标了需要先澄清，按约定没有执行检索；请先向对方问清楚。", log
    kws = req["keywords"]
    hits, tier = [], None
    # 一级：当前 ref 内精准召回
    refs = set()
    all_refs = {r for s in dir_["states"] for r in s["evidence_ref_list"]}
    for rid in req["preferred_ref_ids"]:
        if rid in all_refs: refs.add(rid)
        else: log["notes"].append(f"忽略不在当前 ref 内的 ID：{rid}")
    sk = req["state_key"]
    if sk:
        cur = dir_["current_valid"].get(sk)
        st = [s for s in dir_["states"] if s["state_key"] == sk and (cur is None or s["state_version"] == cur)]
        if st: refs |= set(st[-1]["evidence_ref_list"])
        else: log["notes"].append(f"state_key 不存在：{sk}")
    if refs:
        q = ",".join("?" * len(refs))
        evs = _events(conn, run_id, f"event_unique_key IN ({q})", list(refs), None, now_iso)
        log["tiers"].append({"tier": 1, "scope": f"当前 ref {len(refs)} 个", "found": len(evs)})
        if evs: hits, tier = evs, 1
    # 二级：给定时间区间
    rng = None
    if not hits:
        tr = req["time_range"] or {}
        if tr.get("start") and tr.get("end"):
            rng = {"start_utc": tr["start"], "end_utc": tr["end"], "phrase": "模型给出的区间", "start_local": tr["start"], "end_local": tr["end"], "tz": "UTC"}
        elif req["time_expression"]:
            rng = history.resolve(req["time_expression"], now_iso, tz)
            if not rng: log["notes"].append(f"时间说法无法解析：{req['time_expression']}")
        if rng:
            evs = _events(conn, run_id, "json_extract(payload,'$.time') >= ? AND json_extract(payload,'$.time') < ?",
                          (rng["start_utc"], rng["end_utc"]), current_key, now_iso)
            m = _kw(evs, kws)
            log["tiers"].append({"tier": 2, "scope": f"[{rng['start_local']}, {rng['end_local']}) {rng['tz']}", "in_range": len(evs), "found": len(m)})
            if m: hits, tier = m, 2
    # 三级：放宽时间窗口（前后各 widen_days 天，必须有关键词）
    if not hits and rng and kws:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        s = (datetime.strptime(rng["start_utc"], fmt) - timedelta(days=BUDGET["widen_days"])).strftime(fmt)
        e = (datetime.strptime(rng["end_utc"], fmt) + timedelta(days=BUDGET["widen_days"])).strftime(fmt)
        evs = _kw(_events(conn, run_id, "json_extract(payload,'$.time') >= ? AND json_extract(payload,'$.time') < ?", (s, e), current_key, now_iso), kws)
        log["tiers"].append({"tier": 3, "scope": f"[{history.local(s, tz)}, {history.local(e, tz)})", "found": len(evs)})
        if evs: hits, tier = evs, 3
    # 兜底：关键词召回（仅有关键词时；限量）
    if not hits and kws:
        evs = _kw(_events(conn, run_id, "1=1", (), current_key, now_iso), kws)[:BUDGET["fallback_items"]]
        log["tiers"].append({"tier": 4, "scope": "本 run 事件日志关键词召回", "found": len(evs)})
        if evs: hits, tier = evs, 4
    hits = hits[:BUDGET["max_items"]]
    lines, chars = [], 0
    for h in hits:
        mark = "（就是对方刚才这条消息）" if h["id"] == current_key else ""
        line = f"- [{h['id']}] {history.local(h['time'], tz)} {h['who']}：{h['text']}{mark}"
        if chars + len(line) > BUDGET["max_chars"]:
            log["notes"].append("达到字数预算，截断"); break
        lines.append(line); chars += len(line)
    log.update(hits=[h["id"] for h in hits[:len(lines)]], returned=len(lines), chars=chars, stopped=f"tier{tier}" if tier else "not_found")
    scope = "；".join(f"第{t['tier']}级 {t['scope']}：{t['found']} 条" for t in log["tiers"]) or "没有可执行的检索条件"
    if not lines:
        return f"检索范围：{scope}。在这些范围内没有找到相关的原始消息。范围之外的记录无法确认。", log
    return f"检索范围：{scope}。找到的原始消息（第{tier}级）：\n" + "\n".join(lines), log
