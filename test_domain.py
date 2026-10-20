"""State invariants, replay/concurrency behavior and independent C++ score oracle."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import uuid

from domain import Conflict, Store, valid_final_score

ROOT = Path(__file__).resolve().parent


def nonce():
    return str(uuid.uuid4())


class DomainChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="rally-desk-domain-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "state.sqlite3")

    def session(self, size=4, courts=1):
        return self.store.create([f"P{i:02}" for i in range(size)], courts, 11, nonce())["state"]

    def active(self, size=4, courts=1):
        state = self.session(size, courts)
        plan = self.store.plan(state["session_id"])
        result = self.store.commit(state["session_id"], state["version"], plan["preview_hash"], nonce())
        return result["state"]

    def test_six_rounds_twelve_players_two_courts(self):
        state = self.session(12, 2)
        participation = Counter({p: 0 for p in state["players"]})
        partner_history = Counter()
        observed = []
        for round_number in range(1, 7):
            started = time.monotonic()
            plan = self.store.plan(state["session_id"])
            selected = [p for court in plan["courts"] for team in court["teams"] for p in team]
            self.assertEqual(len(selected), 8)
            self.assertEqual(len(set(selected)), 8)
            self.assertEqual(set(selected) | set(plan["resting"]), set(state["players"]))
            self.assertFalse(set(selected) & set(plan["resting"]))
            actual_prior_partner_occurrences = sum(partner_history[tuple(sorted(team))]
                                                   for court in plan["courts"] for team in court["teams"])
            self.assertEqual(actual_prior_partner_occurrences, plan["explanation"]["prior_partner_occurrences"])
            result = self.store.commit(state["session_id"], state["version"], plan["preview_hash"], nonce())
            state = result["state"]
            for match in [m for m in state["matches"] if m["status"] == "pending"]:
                state = self.store.report(state["session_id"], state["version"], match["id"], 11, 7, nonce())["state"]
                for team in match["teams"]:
                    participation.update(team)
                    partner_history[tuple(sorted(team))] += 1
            self.assertEqual(dict(participation), state["completed_games"])
            self.assertLessEqual(max(participation.values()) - min(participation.values()), 1)
            observed.append({"round": round_number, "seconds": round(time.monotonic() - started, 4),
                             "prior_partner_occurrences": actual_prior_partner_occurrences,
                             "arrangements": plan["explanation"]["arrangements_evaluated"]})
        self.assertEqual(set(participation.values()), {4})
        print(json.dumps({"six_rounds_measured": observed}), flush=True)

    def test_create_retry_and_canonical_uuid(self):
        request = uuid.uuid4()
        first = self.store.create(["A", "B", "C", "D"], 1, 11, request.hex)
        second = self.store.create(["A", "B", "C", "D"], 1, 11, str(request))
        self.assertTrue(second["replayed"])
        self.assertEqual(first["state"], second["state"])

    def test_delayed_retry_keeps_current_snapshot(self):
        state = self.session()
        plan = self.store.plan(state["session_id"])
        request = nonce()
        initial = self.store.commit(state["session_id"], 1, plan["preview_hash"], request)
        updated = self.store.report(state["session_id"], 2, "R1C1", 11, 7, nonce())
        replay = self.store.commit(state["session_id"], 1, plan["preview_hash"], request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["receipt"]["committed_version"], 2)
        self.assertEqual(replay["state"]["version"], 3)
        self.assertEqual(replay["state"], updated["state"])

    def test_stale_and_altered_preview_do_not_commit(self):
        state = self.session()
        before = self.store.snapshot(state["session_id"])
        with self.assertRaises(Conflict):
            self.store.commit(state["session_id"], 1, "altered", nonce())
        self.assertEqual(before, self.store.snapshot(state["session_id"]))
        plan = self.store.plan(state["session_id"])
        self.store.commit(state["session_id"], 1, plan["preview_hash"], nonce())
        with self.assertRaises(Conflict):
            self.store.report(state["session_id"], 1, "R1C1", 11, 7, nonce())
        self.assertEqual(self.store.snapshot(state["session_id"])["version"], 2)

    def test_correction_audit_and_request_collision(self):
        state = self.active()
        request = nonce()
        first = self.store.report(state["session_id"], 2, "R1C1", 11, 7, request)
        with self.assertRaises(Conflict):
            self.store.report(state["session_id"], 2, "R1C1", 7, 11, request)
        with self.assertRaises(Conflict):
            self.store.report(state["session_id"], 3, "R1C1", 7, 11, nonce())
        corrected = self.store.report(state["session_id"], 3, "R1C1", 7, 11, nonce(), "Sides were transposed")
        self.assertEqual(corrected["state"]["completed_games"], first["state"]["completed_games"])
        self.assertEqual(corrected["state"]["audit"][-1]["old_score"], [11, 7])
        self.assertEqual(corrected["state"]["audit"][-1]["new_score"], [7, 11])
        reopened = Store(self.store.path).snapshot(state["session_id"])
        self.assertEqual(reopened, corrected["state"])

    def test_concurrent_distinct_reports_one_wins(self):
        state = self.active()
        barrier = threading.Barrier(2)
        def writer(score):
            barrier.wait()
            try:
                return self.store.report(state["session_id"], 2, "R1C1", *score, nonce())["replayed"]
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(writer, [(11, 7), (7, 11)]))
        self.assertEqual(results.count(False), 1)
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(self.store.snapshot(state["session_id"])["version"], 3)

    def test_concurrent_identical_retry_commits_once(self):
        state = self.active()
        request, barrier = nonce(), threading.Barrier(2)
        def writer(_):
            barrier.wait()
            return self.store.report(state["session_id"], 2, "R1C1", 11, 7, request)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(writer, range(2)))
        self.assertEqual(sorted(r["replayed"] for r in results), [False, True])
        self.assertEqual(results[0]["state"]["version"], 3)
        self.assertEqual(results[0]["state"], results[1]["state"])

    def test_score_validator_against_reachable_cpp_states(self):
        program = r'''
#include "Score.hpp"
#include <iostream>
#include <map>
#include <queue>
int main() {
  for (int t=0;t<3;++t) {
    std::map<std::pair<unsigned,unsigned>,Rally::Score> states;
    std::queue<Rally::Score> queue;
    Rally::Score start; start.reset(t); queue.push(start); states[{0,0}]=start;
    while (!queue.empty()) {
      auto s=queue.front();queue.pop();
      if (s.winner()>=0) {std::cout << unsigned(Rally::Score::targets[t]) << " " << s.side(0) << " " << s.side(1) << "\n";continue;}
      for (int side=0;side<2;++side) {
        auto next=s;if(!next.add(side))continue;
        auto key=std::make_pair(next.side(0),next.side(1));
        if(states.emplace(key,next).second)queue.push(next);
      }
    }
  }
}'''
        with tempfile.TemporaryDirectory(prefix="rally-desk-cpp-") as directory:
            folder = Path(directory)
            (folder / "oracle.cpp").write_text(program)
            subprocess.run(["c++", "-std=c++17", "-O2", "-I", str(ROOT / "reference"),
                            str(folder / "oracle.cpp"), "-o", str(folder / "oracle")],
                           check=True, capture_output=True, timeout=30)
            output = subprocess.run([str(folder / "oracle")], text=True, check=True,
                                    capture_output=True, timeout=5).stdout
        reachable = {tuple(map(int, line.split())) for line in output.splitlines()}
        for target in (11, 15, 21):
            for a in range(31):
                for b in range(31):
                    self.assertEqual(valid_final_score(a, b, target), (target, a, b) in reachable,
                                     (target, a, b))
        print(json.dumps({"cpp_reachable_final_states": len(reachable), "score_pairs_checked": 2883}), flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
