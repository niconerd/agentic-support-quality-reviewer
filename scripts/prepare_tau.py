"""Normalize a pinned τ³ package without accessing its evaluation fields.

Usage: uv run python scripts/prepare_tau.py TRAJECTORIES_JSON RETAIL_DB_JSON
Only the public, vendored Retail tools execute; source conversations are data.
"""
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

from pydantic import BaseModel
from retail_vendor import RetailDB, RetailTools

COMMIT = "1901a301961cbbe3fd11f3e84a2a376530c759e3"
SOURCE = "https://huggingface.co/datasets/xiaomingneu/tau3-qwen35-4b-retail-baseline"
ROOT = Path(__file__).resolve().parents[1]


def categories(objective):
    text = objective.lower()
    tags = []
    for label, pattern in {
        "Returns": r"return|refund",
        "Exchanges": r"exchange",
        "Cancellations": r"cancel",
        "Order changes": r"modify|change|switch|instead",
        "Address & account": r"address|move|moving",
        "Product & order info": r"know|check|information|status|price|details",
        "Conditional requests": r"\bif\b|unless|otherwise",
        "Changed intent": r"\bchange your mind\b|\bchanged (?:your|my) mind\b",
        "Policy boundaries": r"delivered|processed|human|not possible|cannot",
    }.items():
        if re.search(pattern, text):
            tags.append(label)
    if len(set(tags) & {"Returns", "Exchanges", "Cancellations", "Address & account"}) > 1:
        tags.append("Multi-intent")
    return tags or ["Product & order info"]


def parsed(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def normalize(task, simulation, base):
    instructions = task["user_scenario"]["instructions"]
    objective = "\n".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in instructions.items() if k != "domain" and v) if isinstance(instructions, dict) else instructions
    if task.get("initial_state") is not None:
        raise ValueError("This importer only accepts the pinned package's unmodified initial DB")
    db = RetailDB.model_validate(base)
    initial = db.model_dump(mode="json")
    toolkit = RetailTools(db)
    transcript, calls, pending = [], [], {}
    for turn_index, message in enumerate(simulation["messages"]):
        role = message["role"]
        if role in ("user", "assistant"):
            if message.get("content"):
                transcript.append({"role": role, "content": message["content"]})
            for call in message.get("tool_calls") or []:
                pending[call["id"]] = {"id": call["id"], "name": call["name"], "arguments": call["arguments"], "after_message": len(transcript) - 1, "turn_index": turn_index}
        elif role == "tool":
            call = pending.pop(message["id"])
            result = parsed(message["content"])
            # Calculator has no side effects; never execute expressions from data.
            if call["name"] != "calculate":
                fn = getattr(toolkit, call["name"], None)
                if fn is None or not hasattr(fn, "kind"):
                    raise ValueError("Unknown tool in source")
                try:
                    replay = fn(**call["arguments"])
                except ValueError as error:
                    if not message.get("error") or result != f"Error: {error}":
                        raise
                else:
                    if message.get("error"):
                        raise ValueError("Replay succeeded for a recorded failed tool")
                    replay = replay.model_dump(mode="json") if isinstance(replay, BaseModel) else parsed(replay)
                    if replay != result:
                        raise ValueError(f"Replay mismatch in task {task['id']}: {call['name']}")
            calls.append({**call, "result": result, "error": bool(message.get("error"))})
    if pending:
        raise ValueError("Incomplete tool activity")
    final = db.model_dump(mode="json")
    # Keep complete records touched or mentioned, including all changed records.
    mentions = json.dumps([objective, transcript, calls], ensure_ascii=False)
    ids = {kind: {key for key in initial[kind] if key in mentions or initial[kind][key] != final[kind][key]} for kind in initial}
    for key in ids["orders"]:
        ids["users"].add(initial["orders"][key]["user_id"])
    for key in ids["users"]:
        ids["orders"].update(initial["users"][key]["orders"])
    for key in ids["orders"]:
        for item in initial["orders"][key]["items"]:
            ids["products"].add(item["product_id"])
    subset = lambda state: {kind: {key: state[kind][key] for key in sorted(keys)} for kind, keys in ids.items()}
    intent = instructions.get("reason_for_call", "") + " " + instructions.get("task_instructions", "") if isinstance(instructions, dict) else instructions
    tags = categories(intent)
    return {
        "id": f"tau3-retail-{int(task['id']):03d}", "scenario": tags[0], "tags": tags,
        "objective": objective, "policy": simulation["policy"],
        "transcript": transcript, "tool_calls": calls,
        "initial_state": subset(initial), "final_state": subset(final),
        "provenance": {"dataset": "τ³ Retail", "source_url": SOURCE, "source_commit": COMMIT,
                       "task_id": task["id"], "trajectory_id": simulation["id"], "trial": simulation["trial"],
                       "state_method": "Official pinned Retail tool replay; every non-calculator response verified. Complete relevant records; all changed records retained."},
    }


def main():
    source_path, db_path = map(Path, sys.argv[1:3])
    source, base = json.loads(source_path.read_text()), json.loads(db_path.read_text())
    if source["info"]["git_commit"] != COMMIT:
        raise ValueError("Source version does not match vendored replay tools")
    tasks = {t["id"]: t for t in source["tasks"]}
    # Trial 0 only, then deterministic round-robin coverage. No reward/label access.
    pool = defaultdict(deque)
    for sim in sorted(source["simulations"], key=lambda s: hashlib.sha256(s["task_id"].encode()).hexdigest()):
        if sim["trial"] != 0:
            continue
        instructions = tasks[sim["task_id"]]["user_scenario"]["instructions"]
        intent = instructions.get("reason_for_call", "") + " " + instructions.get("task_instructions", "") if isinstance(instructions, dict) else instructions
        for tag in categories(intent):
            pool[tag].append(sim)
    selected, seen = [], set()
    while len(selected) < 50:
        before = len(selected)
        for tag in sorted(pool):
            while pool[tag] and pool[tag][0]["task_id"] in seen:
                pool[tag].popleft()
            if pool[tag] and len(selected) < 50:
                sim = pool[tag].popleft()
                selected.append(normalize(tasks[sim["task_id"]], sim, base))
                seen.add(sim["task_id"])
        if len(selected) == before:
            raise ValueError("Fewer than 50 distinct complete cases")
    selected.sort(key=lambda c: c["id"])
    (ROOT / "data/tau3-retail-50.json").write_text(json.dumps(selected, ensure_ascii=False, indent=2) + "\n")
    manifest = {"source": SOURCE, "source_commit": COMMIT,
                "trajectory_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "database_sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
                "selection": "Trial 0; SHA-256 task ordering; round-robin scenario tags. Independent of benchmark performance. Tags are keyword-based coverage aids, not quality labels.",
                "count": len(selected), "coverage": dict(Counter(t for c in selected for t in c["tags"])),
                "excluded": ["reward_info", "evaluation_criteria", "review", "user_only_review", "auth_classification", "hallucination_check", "raw_data", "generation usage and costs"]}
    (ROOT / "data/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
