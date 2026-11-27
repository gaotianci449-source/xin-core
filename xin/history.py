"""历史检索：把对方消息里的相对日期（今天/昨天/前天/N天前）按对话时区解析成 [start, end)，
按“消息发生时间”字段（事件原文 time）检索原始消息。解析由程序完成，不交给模型。"""
import json, re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}

def parse_days_ago(text):
    if "前天" in text: return 2, "前天"
    if "昨天" in text or "昨晚" in text: return 1, "昨天"
    m = re.search(r"([一两二三四五六七\d])天前", text)
    if m:
        v = m.group(1); return (int(v) if v.isdigit() else CN_NUM[v]), m.group(0)
    if "今天" in text: return 0, "今天"
    return None, None

def to_utc_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def resolve(text, msg_time_iso, tz_name):
    """返回 None，或 {phrase, tz, field, start_local, end_local, start_utc, end_utc}"""
    tz = ZoneInfo(tz_name)
    now_local = datetime.strptime(msg_time_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(tz)
    if "上周" in text or "上星期" in text:
        monday = (now_local - timedelta(days=now_local.weekday() + 7)).replace(hour=0, minute=0, second=0, microsecond=0)
        day, end, phrase = monday, monday + timedelta(days=7), "上周"
    else:
        days, phrase = parse_days_ago(text)
        if days is None:
            return None
        day = (now_local - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = day + timedelta(days=1)
    return {"phrase": phrase, "tz": tz_name, "field": "消息发生时间（事件原文 time）",
            "start_local": day.strftime("%Y-%m-%d %H:%M"), "end_local": end.strftime("%Y-%m-%d %H:%M"),
            "start_utc": to_utc_iso(day), "end_utc": to_utc_iso(end)}

def search(conn, run_id, rng, exclude_key=None):
    """左闭右开 [start, end)；只按消息发生时间，不用记录时间。"""
    rows = conn.execute(
        "SELECT event_unique_key, result, payload FROM event_log WHERE run_id=? "
        "AND json_extract(payload,'$.time') >= ? AND json_extract(payload,'$.time') < ? ORDER BY json_extract(payload,'$.time')",
        (run_id, rng["start_utc"], rng["end_utc"])).fetchall()
    out = []
    for r in rows:
        if r["event_unique_key"] == exclude_key:
            continue
        e = json.loads(r["payload"])
        out.append({"key": r["event_unique_key"], "who": "你" if r["result"] == "reply" else e["sender"], "time": e["time"], "text": e["text"]})
    return out

def local(t_iso, tz_name):
    return datetime.strptime(t_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name)).strftime("%m-%d %H:%M")
