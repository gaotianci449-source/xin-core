"""单元测试：校验器拒绝非法输出、场景 B、唯一约束、快照。python3 -m unittest -v"""
import json, os, sqlite3, tempfile, unittest
from xin import db, engine
from xin.models import MockModel

EV = {"event_unique_key": "t:c:s1", "sender": "阿凯", "time": "2026-11-27T00:00:00Z", "session": "x", "text": "hi"}
DIR = {"states": [{"state_id": "S1", "state_key": "a.b", "state_version": 1, "status_tag": "valid", "content": "x",
                   "target_event_id": "t:pre", "evidence_ref_list": ["t:pre"]}], "conflict_links": [], "current_valid": {"a.b": 1}}
KNOWN = {"t:pre", "t:c:s1"}

def cand(**kw):
    c = {"state_key": "a.b", "is_new": False, "status_tag": "valid", "content": "y", "conflict_with": [], "evidence_ref_list": ["t:c:s1"]}
    c.update(kw); return c

class Validate(unittest.TestCase):
    def bad(self, out):
        with self.assertRaises(engine.InvalidOutput):
            engine.validate(out, DIR, EV, KNOWN)
    def ok(self, out):
        engine.validate(out, DIR, EV, KNOWN)

    def test_good(self):
        self.ok({"result": "write", "candidates": [cand()], "reason": "r"})
        self.ok({"result": "keep", "candidates": [], "reason": "r"})
        self.ok({"result": "conflict", "candidates": [cand(status_tag="conflict", conflict_with=["S1"])], "reason": "r"})
    def test_enum_and_fields(self):
        self.bad({"result": "valid", "candidates": [cand()], "reason": "r"})
        self.bad({"result": "write", "candidates": [cand()]})
        self.bad({"result": "write", "candidates": [cand(status_tag="conflict")], "reason": "r"})
        self.bad({"result": "keep", "candidates": [cand()], "reason": "r"})
        self.bad({"result": "write", "candidates": [], "reason": "r"})
    def test_key_rules(self):
        self.bad({"result": "write", "candidates": [cand(state_key="a.c")], "reason": "r"})            # 已有却不在目录
        self.bad({"result": "write", "candidates": [cand(is_new=True)], "reason": "r"})              # 新建却已存在
        self.bad({"result": "write", "candidates": [cand(state_key="A B", is_new=True)], "reason": "r"})
        self.bad({"result": "write", "candidates": [cand(state_version=2)], "reason": "r"})
    def test_conflict_and_evidence(self):
        self.bad({"result": "conflict", "candidates": [cand(status_tag="conflict", conflict_with=["S9"])], "reason": "r"})
        self.bad({"result": "conflict", "candidates": [cand(status_tag="conflict")], "reason": "r"})
        self.bad({"result": "write", "candidates": [cand(evidence_ref_list=["t:pre"])], "reason": "r"})
        self.bad({"result": "write", "candidates": [cand(evidence_ref_list=["t:c:s1", "nope"])], "reason": "r"})

class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.d, "m.db"))
        self.flog = engine.FailLog(os.path.join(self.d, "f.jsonl"))
        db.seed(self.conn, "r", {"events": [dict(EV, event_unique_key="t:pre")], "states": [], "conflict_links": []})

    def test_scene_b_two_conflicting_new_states(self):
        out = {"result": "conflict", "reason": "r", "candidates": [
            {"state_key": "x.y", "is_new": True, "status_tag": "conflict", "content": "p", "conflict_with": ["#1"], "evidence_ref_list": ["t:c:s1"]},
            {"state_key": "x.y", "is_new": True, "status_tag": "conflict", "content": "q", "conflict_with": ["#0"], "evidence_ref_list": ["t:c:s1"]}]}
        r = engine.process_event(self.conn, "r", EV, [], MockModel(out), self.flog)
        self.assertEqual(r["status"], "done")
        vers = [x[0] for x in self.conn.execute("SELECT state_version FROM states WHERE state_key='x.y' ORDER BY 1")]
        self.assertEqual(vers, [1, 2])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conflict_link").fetchone()[0], 1)
        self.assertEqual(engine.directory(self.conn)["current_valid"], {})

    def test_pending_keeps_old_valid(self):
        w = {"result": "write", "reason": "r", "candidates": [cand(state_key="k", is_new=True)]}
        engine.process_event(self.conn, "r", EV, [], MockModel(w), self.flog)
        e2 = dict(EV, event_unique_key="t:c:s2")
        p = {"result": "write", "reason": "r", "candidates": [cand(state_key="k", status_tag="pending", evidence_ref_list=["t:c:s2"])]}
        engine.process_event(self.conn, "r", e2, [], MockModel(p), self.flog)
        self.assertEqual(engine.directory(self.conn)["current_valid"], {"k": 1})

    def test_unique_constraints(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO event_log VALUES ('r','t:pre','keep','{}',NULL,'t')")
        self.conn.execute("INSERT INTO states VALUES ('A','e','k',1,'valid','c','t','[]')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO states VALUES ('B','e','k',1,'valid','c','t','[]')")

    def test_snapshot_consistent(self):
        w = {"result": "write", "reason": "r", "candidates": [cand(state_key="k", is_new=True)]}
        engine.process_event(self.conn, "r", EV, [], MockModel(w), self.flog)
        p = os.path.join(self.d, "snap.db")
        db.snapshot(self.conn, p)
        s = sqlite3.connect(p)
        self.assertEqual(s.execute("SELECT COUNT(*) FROM states").fetchone()[0], 1)
        self.assertEqual(s.execute("PRAGMA integrity_check").fetchone()[0], "ok")

if __name__ == "__main__":
    unittest.main()
