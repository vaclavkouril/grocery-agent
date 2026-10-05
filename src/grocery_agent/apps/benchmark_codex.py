"""Benchmark budget Codex models against the pinned local recipe cases."""

import argparse
import hashlib
import json
import os
import selectors
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from grocery_agent.apps.benchmark_recipes import evaluate_draft, save, summarize
from grocery_agent.config import Settings
from grocery_agent.models.common import utc_now
from grocery_agent.recipe_config import RecipeSettings
from grocery_agent.recipe_service import RecipeService
from grocery_agent.recipes import RecipeRequest, draft_schema, recipe_prompt


def read_limits(timeout: float = 30) -> dict[str, Any]:
    """Read quota windows through the documented protocol, without reading credentials."""
    with tempfile.TemporaryDirectory(prefix="grocery-quota-") as cwd:
        proc = subprocess.Popen(
            ["codex", "app-server", "-c", "mcp_servers={}"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(
                (
                    json.dumps(
                        {
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "clientInfo": {"name": "recipe_benchmark", "version": "1.0"},
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            proc.stdin.flush()
            deadline = time.monotonic() + timeout
            buffer = b""
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        break
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        event = json.loads(line)
                        if event.get("id") == 1:
                            if "error" in event:
                                return {"error": event["error"]}
                            proc.stdin.write(b'{"method":"initialized"}\n')
                            proc.stdin.write(b'{"id":2,"method":"account/rateLimits/read"}\n')
                            proc.stdin.flush()
                        elif event.get("id") == 2:
                            if "error" in event:
                                return {"error": event["error"]}
                            result = event.get("result", {})
                            buckets = result.get("rateLimitsByLimitId") or {
                                "codex": result.get("rateLimits", {})
                            }
                            return {
                                "at": utc_now().isoformat(),
                                "buckets": {
                                    key: {
                                        field: value.get(field)
                                        for field in (
                                            "primary",
                                            "secondary",
                                            "planType",
                                            "rateLimitReachedType",
                                        )
                                    }
                                    for key, value in buckets.items()
                                    if isinstance(value, dict)
                                },
                            }
            return {"error": "quota read timed out or disconnected"}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


class MeasuredCodexProvider:
    def __init__(self, model: str, timeout: int = 240) -> None:
        self.model, self.timeout = model, timeout
        self.calls: list[dict[str, Any]] = []

    def generate(self, request: RecipeRequest, context: str) -> dict[str, Any]:
        # Match the old local prompts exactly; provider routing isn't task information.
        prompt = recipe_prompt(
            request.model_copy(update={"model": None, "provider": "ollama"}), context
        )
        started = utc_now()
        record: dict[str, Any] = {
            "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "started_at": started.isoformat(),
            "provider": "codex",
        }
        self.calls.append(record)
        clock = time.perf_counter()
        try:
            with tempfile.TemporaryDirectory(prefix="grocery-benchmark-codex-") as cwd:
                schema = Path(cwd) / "schema.json"
                answer = Path(cwd) / "answer.json"
                save(schema, draft_schema(context))
                command = [
                    "codex",
                    "exec",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                    "--json",
                    "--model",
                    self.model,
                    "-c",
                    'model_reasoning_effort="low"',
                    "-c",
                    'web_search="disabled"',
                    "-c",
                    "features.shell_tool=false",
                    "--output-schema",
                    str(schema),
                    "--output-last-message",
                    str(answer),
                    "-",
                ]
                completed = subprocess.run(
                    command,
                    input=prompt,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    check=False,
                )
                record["returncode"] = completed.returncode
                events = [
                    json.loads(line) for line in completed.stdout.splitlines() if line.strip()
                ]
                # Keep usage and final public recipe evidence, not account IDs or reasoning text.
                record["usage_events"] = [
                    e["usage"] for e in events if e.get("type") == "turn.completed" and "usage" in e
                ]
                record["event_types"] = [e.get("type") for e in events]
                record["errors"] = [
                    e.get("message", e.get("error"))
                    for e in events
                    if e.get("type") in {"error", "turn.failed"}
                ]
                record["item_types"] = [e["item"].get("type") for e in events if "item" in e]
                if completed.returncode:
                    raise RuntimeError(f"Codex exited {completed.returncode}: {record['errors']}")
                if not answer.exists():
                    raise ValueError("Codex did not write a final recipe")
                raw = answer.read_text(encoding="utf-8")
                record["response"] = {
                    "response": raw,
                    "eval_count": sum(u.get("output_tokens", 0) for u in record["usage_events"]),
                }
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("model returned a non-object draft")
                return value
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            record.update(error=str(exc), error_type=type(exc).__name__)
            raise RuntimeError(f"Codex benchmark provider failed: {type(exc).__name__}") from exc
        finally:
            finished = utc_now()
            record.update(
                wall_seconds=time.perf_counter() - clock,
                finished_at=finished.isoformat(),
                calendar_wall_seconds=(finished - started).total_seconds(),
            )


def usage_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Include repair usage; missing counters are unknown rather than zero."""
    usages = [u for row in runs for call in row["calls"] for u in call.get("usage_events", [])]
    fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    complete = all(call.get("usage_events") for row in runs for call in row["calls"])
    totals = {
        k: sum(u[k] for u in usages)
        if complete and usages and all(k in u for u in usages)
        else None
        for k in fields
    }
    requests = len(runs)
    return {
        "reported_turns": len(usages),
        "totals": totals,
        "mean_per_request": {
            k: v / requests if v is not None and requests else None for k, v in totals.items()
        },
        "weekly_scenarios": {
            str(n): {
                k: round(v * n / requests) if v is not None and requests else None
                for k, v in totals.items()
            }
            for n in (7, 21, 100)
        },
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.output.exists():
        raise ValueError("choose a new output directory")
    previous = json.loads(args.inputs_from.read_text(encoding="utf-8"))
    now = datetime.fromisoformat(previous["evaluation_at"])
    args.output.mkdir(parents=True)
    evidence = {k: previous[k] for k in ("evaluation_at", "snapshot", "offer_counts", "cases")}
    evidence.update(
        started_at=utc_now().isoformat(),
        inputs_from=str(args.inputs_from),
        models=[{"name": name, "provider": "codex"} for name in args.models],
        options={
            "reasoning_effort": "low",
            "repeats": args.repeats,
            "timeout_seconds": args.timeout,
            "speed": "standard",
            "tools": "shell disabled, web disabled, user config ignored",
            "output_token_cap": None,
            "auth": "existing ChatGPT CLI login",
        },
        quota_before=read_limits(),
        runs=[],
    )
    save(args.output / "results.json", evidence)
    for model in args.models:
        for repeat in range(args.repeats):
            for name, case in evidence["cases"].items():
                request = RecipeRequest.model_validate(case["request"]).model_copy(
                    update={"provider": "codex", "model": model}
                )
                provider = MeasuredCodexProvider(model, args.timeout)
                service = RecipeService(
                    Settings(meal_config=args.catalog),
                    RecipeSettings(),
                    clock=lambda: now,
                    provider=provider,
                )
                row: dict[str, Any] = {"model": model, "case": name, "repeat": repeat + 1}
                start = time.perf_counter()
                print(f"Starting {model} {name} repeat {repeat + 1}", flush=True)
                try:
                    result, _ = service.execute(request, case["inputs"])
                    row.update(status=result["status"], result=result)
                except (ValueError, RuntimeError) as exc:
                    row.update(
                        status="provider-failure", error=str(exc), error_type=type(exc).__name__
                    )
                row.update(wall_seconds=time.perf_counter() - start, calls=provider.calls)
                if provider.calls and "response" in provider.calls[-1]:
                    try:
                        draft = json.loads(provider.calls[-1]["response"]["response"])
                        row["diagnostic_report"] = evaluate_draft(
                            request, case["inputs"], draft, now, relaxed=True
                        )
                    except (ValueError, TypeError, KeyError) as exc:
                        row["diagnostic_error"] = str(exc)
                evidence["runs"].append(row)
                save(args.output / "results.json", evidence)
                print(f"Finished {row['status']} in {row['wall_seconds']:.2f}s", flush=True)
    evidence.update(
        finished_at=utc_now().isoformat(), quota_after=read_limits(), summary=summarize(evidence)
    )
    evidence["token_usage"] = {
        m: usage_summary([r for r in evidence["runs"] if r["model"] == m]) for m in args.models
    }
    save(args.output / "results.json", evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=["gpt-6-luna", "gpt-6.1-sol"])
    parser.add_argument("--inputs-from", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=Path("config/meals.toml"))
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument(
        "--quota-only", action="store_true", help="Read quota without any model calls"
    )
    args = parser.parse_args()
    if not 30 <= args.timeout <= 600:
        parser.error("timeout must be 30 to 600 seconds")
    if args.quota_only:
        if args.output.exists():
            parser.error("choose a new output directory")
        args.output.mkdir(parents=True)
        version = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10)
        save(
            args.output / "results.json",
            {"quota": read_limits(), "codex_version": version.stdout.strip()},
        )
    else:
        if args.inputs_from is None:
            parser.error("--inputs-from is required for model benchmarks")
        run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
