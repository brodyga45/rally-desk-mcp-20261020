"""Original Rally Desk domain logic, MIT, prepared by a coding agent.

Casual doubles rounds, not an official sports rules implementation.
The planner minimizes a declared next-round objective, not a season optimum.
"""
from collections import Counter
from contextlib import closing
import hashlib
from itertools import combinations
import json
from pathlib import Path
import sqlite3
import uuid


class Conflict(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def valid_final_score(a, b, target):
    if type(a) is not int or type(b) is not int or not 0 <= min(a, b) <= max(a, b) <= 30:
        return False
    high, low = max(a, b), min(a, b)
    return ((high == target and low <= target - 2)
            or (target < high < 30 and low == high - 2)
            or (high == 30 and low in (28, 29)))


def statistics(data):
    games = Counter({player: 0 for player in data["players"]})
    partners, opponents = Counter(), Counter()
    for match in data["matches"]:
        if match["status"] != "completed":
            continue
        left, right = match["teams"]
        games.update(left + right)
        partners.update((tuple(sorted(left)), tuple(sorted(right))))
        opponents.update(tuple(sorted((a, b))) for a in left for b in right)
    return games, partners, opponents


def arrangements(players):
    """Each doubles arrangement exactly once, ignoring court order and sides."""
    if not players:
        yield []
        return
    first, rest = players[0], players[1:]
    for partner in rest:
        remaining = tuple(p for p in rest if p != partner)
        for other in combinations(remaining, 2):
            unused = tuple(p for p in remaining if p not in other)
            for tail in arrangements(unused):
                yield [[first, partner], list(other)] + tail


def preview(data, version):
    if any(match["status"] == "pending" for match in data["matches"]):
        raise Conflict("Finish or cancel the current round before planning another")
    if data["round"] >= 100:
        raise ValueError("This local session has reached its 100-round limit")
    games, partners, opponents = statistics(data)
    count = min(data["courts"], len(data["players"]) // 4) * 4
    ranked = sorted(data["players"], key=lambda p: (games[p], p))
    cutoff = games[ranked[count - 1]]
    required = [p for p in ranked if games[p] < cutoff]
    optional = [p for p in ranked if games[p] == cutoff]
    last_round_players = {p for m in data["matches"] if m["round"] == data["round"]
                          and m["status"] == "completed" for team in m["teams"] for p in team}
    previous_bench = set(data["players"]) - last_round_players if data["round"] else set()
    best, candidates = None, 0
    for extra in combinations(optional, count - len(required)):
        chosen = tuple(sorted(required + list(extra)))
        bench = sorted(set(data["players"]) - set(chosen))
        for flat in arrangements(chosen):
            # arrangements yields two teams per court.
            matches = [flat[i:i + 2] for i in range(0, len(flat), 2)]
            repeated_partners = sum(partners[tuple(sorted(team))] for team in flat)
            repeated_opponents = sum(opponents[tuple(sorted((a, b)))]
                                     for left, right in matches for a in left for b in right)
            score = (len(set(bench) & previous_bench), repeated_partners,
                     repeated_opponents, canonical(matches))
            candidates += 1
            if best is None or score < best[0]:
                projected = [games[p] + int(p in chosen) for p in data["players"]]
                best = (score, matches, bench, max(projected) - min(projected))
    score, matches, bench, gap = best
    plan = {"expected_version": version, "round": data["round"] + 1,
            "courts": [{"court": i + 1, "teams": teams} for i, teams in enumerate(matches)],
            "resting": bench,
            "explanation": {"policy": "Least completed games first; then avoid consecutive rests, repeated partners, repeated opponents; deterministic tie break",
                            "consecutive_rests": score[0], "prior_partner_occurrences": score[1],
                            "prior_opponent_occurrences": score[2], "projected_game_count_gap": gap,
                            "arrangements_evaluated": candidates,
                            "scope": "Best next round under the declared least-games policy, not a globally optimal tournament"}}
    plan["preview_hash"] = digest(plan)
    return plan


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(session_id TEXT NOT NULL, version INTEGER NOT NULL, detail TEXT NOT NULL,
                                                UNIQUE(session_id, version));
            """)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5, isolation_level=None)

    def snapshot(self, session_id, db=None):
        if db is None:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN")
                return self.snapshot(session_id, connection)
        row = db.execute("SELECT version,data FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown session")
        version, serialized = row
        data = json.loads(serialized)
        games, _, _ = statistics(data)
        audit = db.execute("SELECT version,detail FROM events WHERE session_id=? ORDER BY version DESC LIMIT 20",
                           (session_id,)).fetchall()
        return {"session_id": session_id, "version": version, **data,
                "completed_games": dict(games),
                "audit": [{"version": v, **json.loads(detail)} for v, detail in reversed(audit)]}

    def _mutate(self, method, session_id, expected_version, request_id, arguments, change):
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("request_id must be a UUID") from None
        fingerprint = digest([method, session_id, expected_version, arguments])
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute("SELECT fingerprint,response FROM receipts WHERE request_id=?", (request_id,)).fetchone()
                if old:
                    if old[0] != fingerprint:
                        raise Conflict("request_id already belongs to a different action")
                    receipt = json.loads(old[1])
                    result = {"receipt": receipt, "replayed": True,
                              "state": self.snapshot(receipt["session_id"], db)}
                    db.commit()
                    return result
                if method == "create":
                    data, version = arguments.copy(), 0
                    session_id = str(uuid.uuid4())
                else:
                    state = self.snapshot(session_id, db)
                    version = state["version"]
                    if type(expected_version) is not int or expected_version != version:
                        raise Conflict(f"Stale version: expected {expected_version}, current {version}; read and preview again")
                    data = {key: state[key] for key in ("players", "courts", "target", "round", "matches")}
                detail = change(data, version)
                version += 1
                db.execute("INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,data=excluded.data",
                           (session_id, version, canonical(data)))
                db.execute("INSERT INTO events VALUES(?,?,?)", (session_id, version, canonical(detail)))
                receipt = {"request_id": request_id, "session_id": session_id,
                           "committed_version": version, "change": detail}
                db.execute("INSERT INTO receipts VALUES(?,?,?)", (request_id, fingerprint, canonical(receipt)))
                result = {"receipt": receipt, "replayed": False, "state": self.snapshot(session_id, db)}
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    def create(self, players, courts, target, request_id):
        if (not isinstance(players, list) or not 4 <= len(players) <= 12
                or any(not isinstance(p, str) or not 1 <= len(p) <= 24
                       or p != p.strip() or any(ord(c) < 32 for c in p) for p in players)
                or len({p.casefold() for p in players}) != len(players)):
            raise ValueError("Use 4–12 distinct short player labels")
        if type(courts) is not int or not 1 <= courts <= 2 or courts * 4 > len(players):
            raise ValueError("Use one or two courts, with at least four players per court")
        if type(target) is not int or target not in (11, 15, 21):
            raise ValueError("Casual target must be 11, 15 or 21")
        data = {"players": sorted(players), "courts": courts, "target": target, "round": 0, "matches": []}
        return self._mutate("create", None, None, request_id, data,
                            lambda d, v: {"action": "session_created", "players": d["players"]})

    def plan(self, session_id):
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            state = self.snapshot(session_id, db)
            return preview(state, state["version"])

    def commit(self, session_id, expected_version, preview_hash, request_id):
        def change(data, version):
            plan = preview(data, version)
            if preview_hash != plan["preview_hash"]:
                raise Conflict("Preview no longer matches this round; read and preview again")
            data["round"] = plan["round"]
            for court in plan["courts"]:
                data["matches"].append({"id": f"R{plan['round']}C{court['court']}",
                                        "round": plan["round"], **court, "status": "pending", "score": None})
            return {"action": "round_committed", "plan": plan}
        return self._mutate("commit", session_id, expected_version, request_id, preview_hash, change)

    def report(self, session_id, expected_version, match_id, a, b, request_id, correction_reason=""):
        if not isinstance(correction_reason, str) or len(correction_reason) > 200:
            raise ValueError("Correction reason must be a short string")
        def change(data, version):
            if not valid_final_score(a, b, data["target"]):
                raise ValueError("Invalid completed casual score: first to target, by two, cap30, no points after a win")
            match = next((m for m in data["matches"] if m["id"] == match_id), None)
            if match is None:
                raise ValueError("Unknown match")
            old_score = match["score"]
            if match["status"] == "completed" and not correction_reason.strip():
                raise Conflict("A completed result needs an explicit correction reason")
            if old_score == [a, b]:
                raise Conflict("Result already matches; reuse the original request_id for a retry")
            match.update(status="completed", score=[a, b])
            return {"action": "result_corrected" if old_score else "result_recorded", "match_id": match_id,
                    "old_score": old_score, "new_score": [a, b], "reason": correction_reason.strip()}
        return self._mutate("report", session_id, expected_version, request_id,
                            [match_id, a, b, correction_reason], change)
