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
