"""模型客户端：Mock（第一层）与 DeepSeek（第二层 / E0）。"""
import json, os, urllib.request

class InjectedAbort(Exception):
    pass

class MockModel:
    """按剧本返回固定 JSON；inject 依次在每次调用前生效。"""
    def __init__(self, output, inject=()):
        self.output, self.inject, self.calls = output, list(inject), 0

    def call(self, payload):
        self.calls += 1
        if self.inject:
            kind = self.inject.pop(0)
            if kind == "timeout":
                raise TimeoutError("注入：请求超时")
            if kind == "bad_json":
                return '{"result": "write", "candidates": ['
            if kind == "schema_invalid":
                return json.dumps({"result": "write"})
            raise ValueError(f"未知注入 {kind}")
        if self.output is None:
            raise RuntimeError("剧本没有提供 mock_output")
        return json.dumps(self.output, ensure_ascii=False)


PROMPT_VERSION = "b-layer-v5"  # v2：主题粒度；v3：重复确认 keep、语义变化才 write；v4：完整快照、计划不覆盖事实；v5：status_tag 判整体资格，不确定性写在正文（2026-11-27）
SYSTEM = """你负责维护一份关于对话对象的状态记录。每次会收到：当前事件、之前的聊天、现有状态目录、证据原文。
判断当前事件对状态记录意味着什么，只输出一个 JSON 对象：
{"result": "keep|write|conflict", "candidates": [...], "reason": "简短理由"}
每个候选：{"state_key": "...", "is_new": true/false, "status_tag": "valid|pending|conflict", "content": "一句话陈述", "conflict_with": [state_id...], "evidence_ref_list": [event_unique_key...]}
规则：
- keep：没有新信息，candidates 为空。纯重复确认、同义改写、只是附和或表态，都算 keep，即使措辞和已有状态不同。
- 判断依据是状态的意思有没有变化，不是字面是否相同：新增了时间、有效期、范围、前提、约束或补充事实，或者更正、更新了原来的内容，才算 write。比如已有“在深圳上班”，“还在深圳 老地方”是 keep；“还在深圳 至少待到年底”加了时间约束，是 write。
- write：新增状态，status_tag 只能是 valid 或 pending。status_tag 判断的是这个候选整体能不能作为当前状态，不标记快照里每一句的确定性：
  - valid：快照整体准确、完整，可以作为当前状态。快照里含有计划、可能性、推测也可以是 valid，不确定的地方在 content 里写清楚（“可能”“未定”）。
  - pending：只用于整条候选信息不足、形成不了合格状态快照的情况，比如孤立的传闻、对方自己都说不清真假的事。
  - 不能因为正文里出现“可能”“也许”“未定”就判 pending。
- conflict：与目录里已有状态矛盾且没有说明变化原因；新候选 status_tag 为 conflict，conflict_with 填目录里对应的 state_id。说明了原因或时间的变化是更新，用 write。
- 同一主题必须使用目录里已有的 state_key，原样照抄，is_new=false；目录里没有的主题才新建，is_new=true，key 用小写英文、数字、下划线和点。
- 一个 state_key 对应这个人的一个方面（比如工作城市、作息、口味、某个爱好）。关于这个方面的近况补充、计划、可能的变化，都记在同一个 key 下，不另开新 key。
- 一条消息里如果有几个不同方面的新信息，每个方面各出一个候选。
- 每个候选的 content 写成这个方面到目前为止的完整状态：把目录里这个 key 当前仍然成立的内容和这次的新信息合在一起写，不能只写这次新增的部分。
- 新的计划、可能性不覆盖当前事实：比如已有“在深圳上班”，“年后可能调去成都”要写成“目前在深圳上班，年后可能调去成都，未定”，标 valid。只有对方明确更正、撤销或替代，确认旧内容已经不成立时，才去掉旧内容。
- “上周”“最近”这类时间说法不等于信息已经失效，旧内容是否仍然成立按意思判断。
- evidence_ref_list 必须包含当前事件的 event_unique_key。
- 不要输出 state_version 或 state_id 字段。"""


class DeepSeekModel:
    def __init__(self, params):
        self.p = params
        self.key = os.environ.get(params.get("api_key_env", "MODEL_API_KEY"), "")
        self.last_raw = None

    def body(self, payload):
        b = {"model": self.p["model"],
             "messages": [{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
             "max_tokens": self.p["max_tokens"]}
        if self.p.get("reasoning_effort"):
            b["reasoning_effort"] = self.p["reasoning_effort"]
        if self.p.get("thinking") is not None:
            b["thinking"] = {"type": "enabled" if self.p["thinking"] else "disabled"}
        if self.p.get("response_format"):
            b["response_format"] = self.p["response_format"]
        return b

    def raw(self, body):
        req = urllib.request.Request(self.p["base_url"].rstrip("/") + "/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key})
        with urllib.request.urlopen(req, timeout=self.p["timeout_s"]) as r:
            return json.loads(r.read())

    def call(self, payload):
        resp = self.raw(self.body(payload))
        self.last_raw = resp
        ch = resp["choices"][0]
        if ch.get("finish_reason") == "length":
            raise ValueError("输出被截断（finish_reason=length）")
        return ch["message"].get("content") or ""
