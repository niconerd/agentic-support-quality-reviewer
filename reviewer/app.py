import asyncio
import hashlib
import json
import os
import math
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI, APIConnectionError, APIStatusError
from pydantic import Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import db
from .schema import Case, EvidenceValidationError, FRAMEWORK, PASSING, PROMPT_VERSION, CHECK_PROMPTS, Evaluation, StrictModel, citation_input, selection_schema, selected_evaluation

load_dotenv(db.ROOT / ".env")


def now():
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app):
    db.initialize()
    app.state.workers = set()
    yield
    for task in app.state.workers:
        task.cancel()
    await asyncio.gather(*app.state.workers, return_exceptions=True)


app = FastAPI(title="Agentic Support Quality Reviewer", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
app.mount("/static", StaticFiles(directory=db.ROOT / "reviewer/static"), name="static")


@app.middleware("http")
async def local_mutations(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin writes are not allowed"}, status_code=403)
        if int(request.headers.get("content-length", "0")) > 20_000_000:
            return JSONResponse({"detail": "Import exceeds the 20 MB limit"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/")
def index():
    return FileResponse(db.ROOT / "reviewer/static/index.html")


@app.get("/api/config")
def config():
    return {"api_key_configured": bool(os.getenv("OPENAI_API_KEY")), "model": os.getenv("REVIEWER_MODEL", "gpt-6-luna"),
            "framework": FRAMEWORK, "passing": sorted(PASSING), "prompt_version": PROMPT_VERSION, "check_prompts": CHECK_PROMPTS, "pacing": pacing_settings(),
            "pricing": {"input_price": price_setting(None, "REVIEWER_INPUT_PRICE"),
                        "cached_input_price": price_setting(None, "REVIEWER_CACHED_INPUT_PRICE"),
                        "output_price": price_setting(None, "REVIEWER_OUTPUT_PRICE")},
            "dataset": json.loads((db.ROOT / "data/manifest.json").read_text())}


@app.get("/api/cases")
def cases():
    return [{k: c[k] for k in ("id", "scenario", "tags", "objective", "provenance")} for c in db.get_cases()]


@app.get("/api/cases/{case_id}")
def case_detail(case_id: str):
    with db.connect() as conn:
        row = conn.execute("SELECT payload FROM cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Case not found")
    return {"case": json.loads(row[0]), "evaluation": None, "status": "not evaluated"}


class ImportBody(StrictModel):
    cases: list[Case] = Field(min_length=1, max_length=200)


@app.post("/api/cases/import", status_code=201)
def import_cases(body: ImportBody):
    ids = [c.id for c in body.cases]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Case identifiers must be unique")
    with db.connect() as conn:
        for case in body.cases:
            conn.execute("INSERT INTO cases VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (case.id, case.model_dump_json()))
    return {"imported": len(ids)}


class RunBody(StrictModel):
    mode: Literal["openai"] = "openai"
    stop_on_error: bool = False
    model: str = Field(default="", max_length=100)
    case_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)
    input_price: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    cached_input_price: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    output_price: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)


def price_setting(value, key):
    if value is not None:
        return value
    raw = os.getenv(key)
    if not raw:
        return None
    try:
        number = float(raw)
        if not 0 <= number <= 10000:
            raise ValueError()
        return number
    except ValueError:
        raise HTTPException(422, f"Invalid configured price: {key}")


def pacing_settings():
    settings = {}
    for key, default in (("case_delay_seconds", 5), ("check_delay_seconds", 1)):
        try:
            value = float(os.getenv("REVIEWER_" + key.upper(), default))
            if not math.isfinite(value) or not 0 <= value <= 300:
                raise ValueError()
        except ValueError:
            raise HTTPException(422, f"REVIEWER_{key.upper()} must be between 0 and 300 seconds")
        settings[key] = value
    return settings


def estimate_cost(usage, config):
    if config["input_price"] is None or config["output_price"] is None:
        return None
    # Without a cached price, use the ordinary input rate as an upper estimate.
    cached_price = config["cached_input_price"] if config["cached_input_price"] is not None else config["input_price"]
    return ((usage["input_tokens"] - usage["cached_tokens"]) * config["input_price"] + usage["cached_tokens"] * cached_price + usage["output_tokens"] * config["output_price"]) / 1_000_000


@app.post("/api/runs", status_code=201)
async def create_run(body: RunBody):
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(400, "Set OPENAI_API_KEY in .env and restart the app to run live evaluations.")
    if body.case_ids and len(set(body.case_ids)) != len(body.case_ids):
        raise HTTPException(422, "Duplicate case IDs")
    selected = [Case.model_validate(c) for c in db.get_cases() if body.case_ids is None or c["id"] in body.case_ids]
    if body.case_ids and len(selected) != len(body.case_ids):
        raise HTTPException(404, "One or more cases were not found")
    selected = selected[:50]
    if not selected:
        raise HTTPException(400, "Import cases before creating a run")
    settings = {"model": body.model.strip() or os.getenv("REVIEWER_MODEL", "gpt-6-luna"), "prompt_version": PROMPT_VERSION,
                "prompt_sha256": hashlib.sha256(json.dumps(CHECK_PROMPTS, sort_keys=True).encode()).hexdigest(), "check_prompts": CHECK_PROMPTS,
                **pacing_settings(), "rate_limit_retries": 5,
                "max_output_tokens": 6000, "stop_on_error": body.stop_on_error, "validation_version": "evidence-v3-source-selection",
                "input_price": price_setting(body.input_price, "REVIEWER_INPUT_PRICE"),
                "cached_input_price": price_setting(body.cached_input_price, "REVIEWER_CACHED_INPUT_PRICE"),
                "output_price": price_setting(body.output_price, "REVIEWER_OUTPUT_PRICE")}
    run_id = uuid4().hex[:12]
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
            raise HTTPException(409, "A run is already in progress")
        conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (run_id, now(), None, "running", body.mode, json.dumps(settings)))
        for case in selected:
            conn.execute("INSERT INTO results (run_id,case_id,case_payload,status,evaluation) VALUES (?,?,?,?,?)", (run_id, case.id, case.model_dump_json(), "pending", None))
    start_worker(run_id, settings)
    return {"id": run_id}


def start_worker(run_id, settings):
    task = asyncio.create_task(evaluate_run(run_id, settings))
    app.state.workers.add(task)
    task.add_done_callback(app.state.workers.discard)


def can_resume(run, settings, rows):
    failures = [row for row in rows if row["status"] == "failed"]
    connection_only = bool(failures) and all(json.loads(row["diagnostics"] or "{}").get("phase") == "connection" for row in failures)
    return settings.get("prompt_version") == PROMPT_VERSION and (
        run["status"] in ("paused", "interrupted") or
        run["status"] in ("partial", "failed") and connection_only)


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str):
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(400, "Set OPENAI_API_KEY and restart before resuming.")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "Run not found")
        settings = json.loads(run["config"])
        rows = conn.execute("SELECT status,diagnostics FROM results WHERE run_id=?", (run_id,)).fetchall()
        if not can_resume(run, settings, rows):
            raise HTTPException(409, "Only paused, interrupted, or connection-failed runs using the current prompts can be resumed.")
        if conn.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
            raise HTTPException(409, "A run is already in progress")
        conn.execute("UPDATE results SET status='pending',error=NULL,diagnostics=NULL WHERE run_id=? AND status!='completed'", (run_id,))
        conn.execute("UPDATE runs SET status='running',completed_at=NULL WHERE id=?", (run_id,))
    start_worker(run_id, settings)
    return {"id": run_id}


def quota_error(exc):
    return any(value and ("quota" in value or "billing" in value or "balance" in value or "credit" in value or "spend" in value or value == "organization_usage_limit_exceeded")
               for value in (exc.code, exc.type))


def retry_delay(exc, retry):
    """Respect server cooldowns; otherwise use bounded exponential backoff with jitter."""
    headers = exc.response.headers
    for header, scale in (("retry-after-ms", 0.001), ("retry-after", 1)):
        raw = headers.get(header)
        if raw is None:
            continue
        try:
            delay = float(raw) * scale
        except ValueError:
            try:
                delay = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(delay) and delay >= 0:
            return max(1, delay)
    return min(60, 2 ** (retry + 1)) + random.uniform(0, 1)


def progress(run_id, case_id, message, diagnostics=None):
    with db.connect() as conn:
        conn.execute("UPDATE results SET error=?,diagnostics=? WHERE run_id=? AND case_id=?",
                     (message, json.dumps(diagnostics) if diagnostics else None, run_id, case_id))


async def request_check(client, run_id, case_id, signal, settings, **request):
    waited = 0
    for retry in range(settings["rate_limit_retries"] + 1):
        try:
            return await client.responses.create(**request)
        except APIStatusError as exc:
            if exc.status_code != 429 or quota_error(exc) or retry == settings["rate_limit_retries"]:
                raise
            delay = retry_delay(exc, retry)
            # Long cooldowns need a manual resume instead of keeping a local worker asleep indefinitely.
            if waited + delay > 300:
                raise
            waited += delay
            diagnostics = {"phase": "rate_limit", "signal": signal, "retry": retry + 1,
                           "retry_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()}
            progress(run_id, case_id, f"API rate limit reached. Retrying {signal} in {delay:.0f}s (retry {retry + 1}/{settings['rate_limit_retries']}).", diagnostics)
            await asyncio.sleep(delay)
            progress(run_id, case_id, f"Retrying {signal} after the API cooldown.")


async def evaluate_run(run_id, settings):
    try:
        async with AsyncOpenAI(timeout=120, max_retries=0) as client:
            with db.connect() as conn:
                rows = conn.execute("SELECT * FROM results WHERE run_id=? AND status='pending' ORDER BY case_id", (run_id,)).fetchall()
            for case_index, row in enumerate(rows):
                case = Case.model_validate_json(row["case_payload"])
                if case_index and settings["case_delay_seconds"]:
                    progress(run_id, case.id, f"Waiting {settings['case_delay_seconds']:g}s between conversations.")
                    await asyncio.sleep(settings["case_delay_seconds"])
                with db.connect() as conn:
                    conn.execute("UPDATE results SET status='running',error=NULL,diagnostics=NULL WHERE run_id=? AND case_id=?", (run_id, case.id))
                checks = json.loads(row["check_results"] or "{}")
                usage = {key: row[key] for key in ("input_tokens", "cached_tokens", "output_tokens", "total_tokens")}
                latency, cost = row["latency_ms"], row["cost"]
                payload, sources = citation_input(case)
                error, diagnostics, pause = None, None, False
                for signal in settings["check_prompts"]:
                    if signal in checks:
                        continue
                    if checks and settings["check_delay_seconds"]:
                        await asyncio.sleep(settings["check_delay_seconds"])
                    progress(run_id, case.id, f"Reviewing {signal} ({len(checks) + 1}/4 checks).")
                    started, response, judgment = time.perf_counter(), None, None
                    try:
                        response = await request_check(client, run_id, case.id, signal, settings,
                            model=settings["model"], instructions=settings["check_prompts"][signal],
                            input=json.dumps(payload, ensure_ascii=False), store=False, max_output_tokens=settings["max_output_tokens"],
                            text={"format": {"type": "json_schema", "name": signal + "_review", "strict": True, "schema": selection_schema(len(sources), signal)}},
                        )
                        if response.status != "completed" or not response.output_text:
                            raise ValueError("Model refused or returned an incomplete response")
                        judgment = selected_evaluation(response.output_text, sources, case, signal)
                    except APIStatusError as exc:
                        pause = exc.status_code == 429
                        error = ("API quota or billing limit reached. Check your API account, then resume this run." if quota_error(exc) else
                                 "API rate limit persists. Let the cooldown finish, then resume this run.") if pause else f"OpenAI HTTP {exc.status_code}. Check API access and model configuration."
                        diagnostics = {"phase": "quota" if pause and quota_error(exc) else "rate_limit" if pause else "api",
                                       "status_code": exc.status_code, "code": exc.code, "type": exc.type,
                                       "retry_after": exc.response.headers.get("retry-after")}
                    except APIConnectionError:
                        pause = True
                        error = "OpenAI connection failed or timed out. Check connectivity, then resume unfinished checks."
                        diagnostics = {"phase": "connection"}
                    except EvidenceValidationError as exc:
                        error = f"Review rejected: {exc}"
                        diagnostics = {"phase": "evidence", "issues": exc.issues}
                    except ValidationError as exc:
                        error = "Review rejected: output does not satisfy the review schema or severity rules."
                        diagnostics = {"phase": "structure", "issues": exc.errors(include_url=False, include_input=False, include_context=False)}
                    except (ValueError, TypeError, KeyError, AttributeError) as exc:
                        error = "Review rejected: invalid response."
                        diagnostics = {"phase": "response", "issues": [{"reason": type(exc).__name__}]}
                        if response and (response.status != "completed" or not response.output_text):
                            error = "Review incomplete or refused by the model. No quality judgment was recorded."
                    except Exception:
                        error = "Evaluation failed unexpectedly. No quality judgment was recorded."
                        diagnostics = {"phase": "unexpected"}
                    elapsed = (time.perf_counter() - started) * 1000
                    latency += elapsed
                    call_usage = None
                    if response and response.usage:
                        call_usage = dict(input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
                                          total_tokens=response.usage.total_tokens,
                                          cached_tokens=response.usage.input_tokens_details.cached_tokens if response.usage.input_tokens_details else 0)
                        for key in usage:
                            usage[key] += call_usage[key]
                        cost = estimate_cost(usage, settings)
                    if judgment:
                        checks[signal] = {"judgment": judgment.model_dump()[signal], "response_id": response.id,
                                          "response_model": response.model, "usage": call_usage, "latency_ms": elapsed}
                    if diagnostics:
                        diagnostics["signal"] = signal
                    evaluation = Evaluation.model_validate({key: check["judgment"] for key, check in checks.items()}) if len(checks) == 4 else None
                    with db.connect() as conn:
                        conn.execute("""UPDATE results SET status=?,evaluation=?,error=?,check_results=?,input_tokens=?,cached_tokens=?,output_tokens=?,total_tokens=?,latency_ms=?,cost=?,response_id=?,response_model=?,model_output=?,diagnostics=? WHERE run_id=? AND case_id=?""",
                                     ("completed" if evaluation else "pending" if pause else "failed" if error else "running",
                                      evaluation.model_dump_json() if evaluation else None, error, json.dumps(checks),
                                      usage["input_tokens"], usage["cached_tokens"], usage["output_tokens"], usage["total_tokens"], latency, cost,
                                      response.id if response else None, response.model if response else None,
                                      response.output_text if response and error else None, json.dumps(diagnostics) if diagnostics else None, run_id, case.id))
                    if pause:
                        with db.connect() as conn:
                            conn.execute("UPDATE runs SET status='paused' WHERE id=?", (run_id,))
                        return
                    if error:
                        break
                if error and settings.get("stop_on_error"):
                    break
        with db.connect() as conn:
            failed = conn.execute("SELECT COUNT(*) FROM results WHERE run_id=? AND status='failed'", (run_id,)).fetchone()[0]
            done = conn.execute("SELECT COUNT(*) FROM results WHERE run_id=? AND status='completed'", (run_id,)).fetchone()[0]
            conn.execute("UPDATE runs SET status=?,completed_at=? WHERE id=?", ("completed" if not failed else "partial" if done else "failed", now(), run_id))
    except BaseException:
        with db.connect() as conn:
            conn.execute("UPDATE results SET status='failed',error='Run interrupted. Resume to continue unfinished checks.' WHERE run_id=? AND status IN ('pending','running')", (run_id,))
            conn.execute("UPDATE runs SET status='interrupted',completed_at=? WHERE id=?", (now(), run_id))
        raise


def summarize(run, rows):
    result = dict(run)
    result["config"] = json.loads(result["config"])
    result["can_resume"] = can_resume(run, result["config"], rows)
    result["config"].pop("instructions", None)
    result["config"].pop("check_prompts", None)
    result["progress"] = next((r["error"] for r in rows if r["status"] in ("running", "pending", "failed") and r["error"]), None)
    result["counts"] = {status: sum(r["status"] == status for r in rows) for status in ("completed", "failed", "running", "pending")}
    result["total_cases"] = len(rows)
    result["distributions"] = {f["key"]: {label: 0 for label in f["outcomes"]} for f in FRAMEWORK}
    for row in rows:
        if row["evaluation"]:
            for key, signal in json.loads(row["evaluation"]).items():
                result["distributions"][key][signal["result"]] += 1
    result["metrics"] = {key: sum(r[key] for r in rows) for key in ("input_tokens", "cached_tokens", "output_tokens", "total_tokens", "latency_ms")}
    attempted = sum(r["latency_ms"] > 0 for r in rows)
    result["metrics"]["average_latency_ms"] = result["metrics"]["latency_ms"] / attempted if attempted else 0
    costs = [r["cost"] for r in rows if r["status"] in ("completed", "failed") or r["latency_ms"] > 0]
    result["metrics"]["estimated_cost"] = sum(costs) if costs and all(c is not None for c in costs) else None
    return result


@app.get("/api/runs")
def runs():
    with db.connect() as conn:
        return [summarize(run, conn.execute("SELECT * FROM results WHERE run_id=?", (run["id"],)).fetchall()) for run in conn.execute("SELECT * FROM runs ORDER BY created_at DESC")]


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    with db.connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "Run not found")
        rows = conn.execute("SELECT * FROM results WHERE run_id=? ORDER BY case_id", (run_id,)).fetchall()
    result = summarize(run, rows)
    result["results"] = []
    for row in rows:
        case = json.loads(row["case_payload"])
        result["results"].append({"case_id": row["case_id"], "scenario": case["scenario"], "tags": case["tags"], "objective": case["objective"],
                                  "status": row["status"], "error": row["error"], "completed_checks": len(json.loads(row["check_results"] or "{}")), "evaluation": json.loads(row["evaluation"]) if row["evaluation"] else None})
    return result


@app.get("/api/runs/{run_id}/cases/{case_id}")
def result_detail(run_id: str, case_id: str):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM results WHERE run_id=? AND case_id=?", (run_id, case_id)).fetchone()
    if not row:
        raise HTTPException(404, "Result not found")
    result = dict(row)
    result["case"] = json.loads(result.pop("case_payload"))
    result["evaluation"] = json.loads(result["evaluation"]) if result["evaluation"] else None
    result["diagnostics"] = json.loads(result["diagnostics"]) if result["diagnostics"] else None
    result["check_results"] = json.loads(result["check_results"] or "{}")
    return result
