"""从《自然聊天先导实验_最终执行修订稿_V0.3_主审修订版.md》原文解析出执行材料（不手抄）。
用法：python3 natpilot/build_materials.py <V0.3 文档路径> <序列表 CSV 路径>
输出：natpilot/materials.json（含两个源文件的 sha256，供核对）"""
import csv, hashlib, json, os, re, sys

doc_path, csv_path = sys.argv[1], sys.argv[2]
raw = open(doc_path, "rb").read(); doc = raw.decode("utf-8")
quotes = re.findall(r"^「(.+)」$", doc, re.M)
base_system = next(q for q in quotes if q.startswith("你是一个对话模型"))
guidance = next(q for q in quotes if q.startswith("请像自然熟人聊天"))
feedback = next(q for q in quotes if q.startswith("接下来请更像自然聊天"))
samples = {}
for m in re.finditer(r"^### (S\d\d)｜(.+?)（(.+?)）\n\n((?:\d\. .+\n)+)", doc, re.M):
    turns = [re.sub(r"^\d\. ", "", l) for l in m.group(4).strip().split("\n")]
    samples[m.group(1)] = {"scene": m.group(2), "type": m.group(3), "turns": turns}
order = []
for m in re.finditer(r"^\|(\d+)\|(S\d\d-[ABCD])\|(S\d\d)\|([ABCD])\|$", doc, re.M):
    order.append({"run_order": int(m.group(1)), "session_id": m.group(2), "sample_id": m.group(3), "condition": m.group(4)})
craw = open(csv_path, "rb").read()
corder = [{"run_order": int(r["run_order"]), "session_id": r["session_id"], "sample_id": r["sample_id"], "condition": r["condition"]}
          for r in csv.DictReader(craw.decode("utf-8").splitlines())]
assert len(samples) == 9 and all(len(s["turns"]) == 7 for s in samples.values()), "刺激解析不完整"
assert len(order) == 36 and order == corder, "文档内顺序与 CSV 不一致"
out = {"experiment_version": "natural-chat-pilot-V0.3", "source_doc": os.path.basename(doc_path),
       "source_doc_sha256": hashlib.sha256(raw).hexdigest(), "source_csv": os.path.basename(csv_path),
       "source_csv_sha256": hashlib.sha256(craw).hexdigest(),
       "base_system": base_system, "guidance": guidance, "guidance_join": "\n\n", "feedback": feedback,
       "samples": samples, "order": order,
       "conditions": {"A": {"guidance": False, "feedback": False}, "B": {"guidance": True, "feedback": False},
                      "C": {"guidance": False, "feedback": True}, "D": {"guidance": True, "feedback": True}},
       "feedback_after_round": 3}
json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "materials.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("ok", out["source_doc_sha256"][:12], len(samples), len(order))
