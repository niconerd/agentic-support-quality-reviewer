"""Evaluate the corpus sequentially; stop on any error and resume from a ledger."""
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8765"
LEDGER = Path(__file__).resolve().parents[1] / "data/conversation-retry.json"


def api(path, body=None):
    request = Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def save(ledger):
    temporary = LEDGER.with_suffix(".tmp")
    temporary.write_text(json.dumps(ledger, indent=2) + "\n")
    temporary.replace(LEDGER)


def main():
    config = api("/api/config")
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else {"attempts": []}
    passed = {a["case_id"] for a in ledger["attempts"]
              if a.get("status") == "completed" and a["prompt_version"] == config["prompt_version"]}
    cases = api("/api/cases")
    if ledger["attempts"] and ledger["attempts"][-1]["status"] != "completed":
        retry_id = ledger["attempts"][-1]["case_id"]
        cases.sort(key=lambda case: case["id"] != retry_id)
    started_case = False
    for case in cases:
        case_id = case["id"]
        if case_id in passed:
            continue
        attempt = next((a for a in reversed(ledger["attempts"]) if a["case_id"] == case_id
                        and a["prompt_version"] == config["prompt_version"]
                        and (a["status"] == "running" or a.get("run_status") in ("paused", "interrupted"))), None)
        if started_case:
            time.sleep(config.get("pacing", {}).get("case_delay_seconds", 5))
        started_case = True
        if attempt:
            existing = api("/api/runs/" + attempt["run_id"])
            if existing["status"] in ("paused", "interrupted"):
                api("/api/runs/" + attempt["run_id"] + "/resume", {})
                attempt["status"] = "running"
                save(ledger)
        if attempt is None:
            run = api("/api/runs", {"model": "gpt-6-luna", "case_ids": [case_id]})
            attempt = {"case_id": case_id, "run_id": run["id"], "prompt_version": config["prompt_version"], "status": "running"}
            ledger["attempts"].append(attempt)
            save(ledger)
        print(json.dumps({"evaluating": case_id, "run_id": attempt["run_id"], "validated": len(passed)}), flush=True)
        while True:
            run = api("/api/runs/" + attempt["run_id"])
            if run["status"] != "running":
                break
            time.sleep(2)
        result = run["results"][0]
        attempt.update(status=result["status"], run_status=run["status"], error=result["error"], metrics=run["metrics"])
        save(ledger)
        if result["status"] != "completed":
            detail = api(f"/api/runs/{attempt['run_id']}/cases/{case_id}")
            print(json.dumps({"stopped": case_id, "error": result["error"], "diagnostics": detail["diagnostics"]}), flush=True)
            return 1
        passed.add(case_id)
        print(json.dumps({"validated": len(passed), "total": len(cases), "case_id": case_id}), flush=True)
    print(json.dumps({"complete": True, "validated": len(passed), "attempts": len(ledger["attempts"])}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
