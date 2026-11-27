"""A 层：回复模型。读种子、聊天、（可选）B 层状态，生成要发出去的话。"""
import json
from .db import now

SEED = """这是一个全新的运行实例。

当前实例不包含旧系统的聊天记录、记忆、人物设定或运行状态。

你的可用工具、信息权限和记忆范围，以当前实际运行环境为准。

与你互动的人是{who}。

以上内容用于说明当前运行背景。其他信息以实际获得的上下文和运行记录为准。"""

FORMAT = "你们在 QQ 私聊。直接输出你要发出去的话，不要加引号、说明或旁白。"


def history(conn, run_id, limit):
    rows = conn.execute("SELECT result, payload FROM event_log WHERE run_id=? AND result IN ('msg','keep','write','conflict','reply') "
                        "ORDER BY rowid DESC LIMIT ?", (run_id, limit)).fetchall()
    out = []
    for r in reversed(rows):
        e = json.loads(r["payload"])
        out.append({"who": "你" if r["result"] == "reply" else e["sender"], "time": e["time"], "text": e["text"]})
    return out


def states_text(dir_):
    if not dir_["states"]:
        return "（目前没有记录）"
    cur = dir_["current_valid"]
    bad = {x for c in dir_["conflict_links"] for x in (c["state_id_a"], c["state_id_b"])}
    lines = []
    for s in dir_["states"]:
        if s["state_id"] in bad:
            tag = "有矛盾，待核实"
        elif s["status_tag"] == "pending":
            tag = "未确认"
        elif cur.get(s["state_key"]) == s["state_version"]:
            tag = "当前"
        else:
            continue  # 已被新版本取代的旧版本不给
        lines.append(f"- {s['content']}（{tag}）")
    return "\n".join(lines) or "（目前没有记录）"


def build_messages(who, chat, states=None):
    system = SEED.format(who=who) + "\n\n" + FORMAT
    if states is not None:
        system += "\n\n关于对方，你此前记下的状态记录如下（只来自对方说过的话，可能不全）：\n" + states
    msgs = [{"role": "system", "content": system}]
    for m in chat:
        role = "assistant" if m["who"] == "你" else "user"
        msgs.append({"role": role, "content": m["text"]})
    return msgs


def store(conn, run_id, key, sender, text, result):
    conn.execute("INSERT INTO event_log VALUES (?,?,?,?,?,?)",
                 (run_id, key, result, json.dumps({"event_unique_key": key, "sender": sender, "time": now(),
                                                   "session": "e2a", "text": text}, ensure_ascii=False), None, now()))


# ---------- E2-A.1：A 层约束、带来源的状态、检索结果 ----------
CONSTRAINTS = """关于记录和过去的事：
- 状态记录是你对对方情况的综合整理，是模型归纳，不是聊天记录本身，也不等于对方的原话。
- 判断对方过去有没有说过某件事，要看原始消息和它的时间；不能只凭状态记录，也不能只凭对方现在的说法，就认定他以前说过。
- 原始消息只能查到一定范围；在范围内没找到，只能说“在能查到的记录里没看到”，不能断言他从没说过。
- 证据不足就说没法确认；不要无依据地认错，不要补造自己没有的记忆，也不要把刚才才出现的信息说成以前就聊过。"""


def states_with_sources(conn, run_id, dir_, current_key, tz):
    from .history import local
    if not dir_["states"]:
        return "（目前没有记录）"
    cur = dir_["current_valid"]
    bad = {x for c in dir_["conflict_links"] for x in (c["state_id_a"], c["state_id_b"])}
    lines = []
    for s in dir_["states"]:
        if s["state_id"] in bad: tag = "有矛盾，待核实"
        elif s["status_tag"] == "pending": tag = "未确认"
        elif cur.get(s["state_key"]) == s["state_version"]: tag = "当前"
        else: continue
        lines.append(f"- {s['content']}（{tag}；类型：模型归纳）")
        for ref in s["evidence_ref_list"]:
            row = conn.execute("SELECT payload FROM event_log WHERE run_id=? AND event_unique_key=?", (run_id, ref)).fetchone()
            if not row:
                lines.append(f"    来源 {ref}：原文取不到"); continue
            e = json.loads(row["payload"])
            mark = "（就是刚才这条消息）" if ref == current_key else ""
            lines.append(f"    来源：{local(e['time'], tz)} 对方原话「{e['text']}」{mark}")
    return "\n".join(lines) or "（目前没有记录）"


def retrieval_text(rng, hits, tz):
    from .history import local
    if not rng:
        return None
    head = (f"对方提到「{rng['phrase']}」。程序按 {rng['tz']} 时区、用{rng['field']}检索了 "
            f"[{rng['start_local']}, {rng['end_local']}) 这段时间（左闭右开）的原始消息：")
    if not hits:
        return head + "\n（这段时间里没有任何消息）"
    return head + "\n" + "\n".join(f"- {local(h['time'], tz)} {h['who']}：{h['text']}" for h in hits)


def build_messages_v2(who, chat, mode, states=None, retrieval=None):
    system = SEED.format(who=who) + "\n\n" + FORMAT
    if mode in ("constrained", "provenance", "raw"):
        system += "\n\n" + CONSTRAINTS
    if states is not None:
        system += "\n\n关于对方，你此前记下的状态记录如下（只来自对方说过的话，可能不全）：\n" + states
    if retrieval:
        system += "\n\n" + retrieval
    msgs = [{"role": "system", "content": system}]
    for m in chat:
        msgs.append({"role": "assistant" if m["who"] == "你" else "user", "content": m["text"]})
    return msgs
