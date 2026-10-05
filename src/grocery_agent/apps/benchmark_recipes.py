"""Sequential, local-only recipe benchmark with pinned public quotes and raw evidence."""

import argparse
import hashlib
import json
import os
import platform
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import httpx

from grocery_agent.config import Settings
from grocery_agent.meals.catalog import MealCatalog, Recipe
from grocery_agent.meals.planner import matches, plan_meals, rejection_reason
from grocery_agent.models.common import utc_now
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.reader import SQLAlchemyCurrentOfferReader
from grocery_agent.recipe_config import RecipeSettings
from grocery_agent.recipe_service import RecipeService, SnapshotReader, effective_catalog
from grocery_agent.recipes import RecipeDraft, RecipeRequest, draft_schema, recipe_prompt

CASES: dict[str, dict[str, Any]] = {
    "high-protein-dinner": {
        "servings": 2,
        "min_protein_g": "70",
        "max_kcal": "850",
        "max_cost_per_serving_czk": "100",
        "max_minutes": 45,
        "max_stores": 2,
    },
    "vegetarian-pantry": {
        "servings": 2,
        "min_protein_g": "30",
        "max_kcal": "750",
        "max_cost_per_serving_czk": "40",
        "max_minutes": 30,
        "max_stores": 2,
        "exclusions": ("chicken", "turkey"),
        "pantry": {"rice": "250g", "lentils": "300g", "oil": "available"},
        "use_first": ("rice", "lentils"),
        "seasonings_available": True,
    },
    "oat-breakfast": {
        "servings": 1,
        "meal_style": "breakfast",
        "min_protein_g": "12",
        "max_kcal": "600",
        "max_cost_per_serving_czk": "25",
        "max_minutes": 30,
        "max_stores": 2,
        "exclusions": ("chicken", "turkey", "rice", "lentils"),
        "pantry": {"oats": "100g", "oil": "available"},
        "seasonings_available": True,
    },
}
WITNESSES = {
    "high-protein-dinner": {
        "chicken": "600",
        "lentils": "160",
        "carrot": "300",
        "onion": "100",
        "oil": "20",
    },
    "vegetarian-pantry": {
        "lentils": "240",
        "rice": "120",
        "carrot": "200",
        "onion": "100",
        "oil": "10",
    },
    "oat-breakfast": {"oats": "90", "carrot": "100", "onion": "30", "oil": "3"},
}


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summarize(evidence: dict[str, Any]) -> dict[str, Any]:
    """Aggregate measured calls without treating failures as fast successful responses."""
    summaries = {}
    for metadata in evidence["models"]:
        name = metadata["name"]
        runs = [row for row in evidence["runs"] if row["model"] == name]
        walls = [row["wall_seconds"] for row in runs]
        successes = [row["wall_seconds"] for row in runs if row["status"] == "ok"]
        bodies = [call["response"] for row in runs for call in row["calls"] if "response" in call]
        tokens = sum(body.get("eval_count", 0) for body in bodies)
        generation_seconds = sum(body.get("eval_duration", 0) for body in bodies) / 1e9
        valid = first_valid = 0
        for row in runs:
            for index in {0, len(row["calls"]) - 1}:
                if index < 0 or not row["calls"] or "response" not in row["calls"][index]:
                    continue
                try:
                    draft = RecipeDraft.model_validate_json(
                        row["calls"][index]["response"]["response"]
                    )
                    ids = [item.ingredient for item in draft.ingredients]
                    known = evidence["cases"][row["case"]]["inputs"]["ingredient_context"]
                    if len(set(ids)) != len(ids) or set(ids) - known.keys():
                        continue
                    if index == 0:
                        first_valid += 1
                    if index == len(row["calls"]) - 1:
                        valid += 1
                except (ValueError, KeyError):
                    continue
        summaries[name] = {
            "requests": len(runs),
            "statuses": dict(Counter(row["status"] for row in runs)),
            "first_valid_drafts": first_valid,
            "final_valid_drafts": valid,
            "requests_with_repair": sum(len(row["calls"]) > 1 for row in runs),
            "provider_calls": sum(len(row["calls"]) for row in runs),
            "median_request_seconds": statistics.median(walls) if walls else None,
            "min_request_seconds": min(walls) if walls else None,
            "max_request_seconds": max(walls) if walls else None,
            "median_success_seconds": statistics.median(successes) if successes else None,
            "total_request_seconds": sum(walls),
            "successful_within_120_seconds": sum(value <= 120 for value in successes),
            "successful_all_calls_within_120_seconds": sum(
                row["status"] == "ok" and all(call["wall_seconds"] <= 120 for call in row["calls"])
                for row in runs
            ),
            "returned_provider_responses": len(bodies),
            "output_tokens": tokens,
            "weighted_output_tokens_per_second": tokens / generation_seconds
            if generation_seconds
            else None,
            "total_prompt_eval_seconds": sum(body.get("prompt_eval_duration", 0) for body in bodies)
            / 1e9,
            "total_generation_seconds": generation_seconds,
        }
    return summaries


def build_inputs(
    catalog: MealCatalog, database: Path, now: datetime
) -> tuple[dict[str, Any], dict[str, int]]:
    engine = create_database_engine(f"sqlite:///{database.resolve()}", read_only=True)
    try:
        reader = SQLAlchemyCurrentOfferReader(engine)
        batch = reader.latest_batch(catalog.policy.source_id)
        all_offers = list(reader.iter_offers(batch))
    finally:
        engine.dispose()
    if not timedelta(0) <= now - batch.finished_at <= timedelta(hours=catalog.policy.max_age_hours):
        raise ValueError("benchmark snapshot must be fresh at the recorded evaluation time")
    today = now.astimezone(ZoneInfo(catalog.policy.timezone)).date()
    accepted = []
    rejected: Counter[str] = Counter()
    for item in all_offers:
        reason = rejection_reason(item.offer, catalog.policy)
        if (
            not timedelta(0)
            <= now - item.observed_at
            <= timedelta(hours=catalog.policy.max_age_hours)
        ):
            reason = "stale"
        elif (item.offer.valid_from and item.offer.valid_from > today) or (
            item.offer.valid_until and item.offer.valid_until < today
        ):
            reason = "validity"
        if reason:
            rejected[reason] += 1
        elif any(matches(ingredient, item.offer) for ingredient in catalog.ingredients.values()):
            accepted.append(item)
        else:
            rejected["no_registry_match"] += 1
    inputs = {
        "run_id": batch.run_id,
        "source_id": batch.source_id,
        "finished_at": batch.finished_at.isoformat(),
        "warnings": [],
        "latest_run_status": "success",
        "profile_fingerprint": "legacy",
        "coverage_complete": False,
        "source_scopes": {batch.source_id: [catalog.policy.scope]},
        "offers": [
            {
                "offer": item.offer.model_dump(mode="json", exclude_computed_fields=True),
                "observed_at": item.observed_at.isoformat(),
                "observation_id": item.observation_id,
                "source_id": item.source_id,
                "run_id": item.run_id,
            }
            for item in accepted
        ],
        "prompt_version": "recipe-draft-v1",
    }
    return inputs, {"total": len(all_offers), "eligible_registry_quotes": len(accepted), **rejected}


def case_inputs(
    base: MealCatalog, request: RecipeRequest, pinned: dict[str, Any]
) -> dict[str, Any]:
    catalog = effective_catalog(base, request)
    reader = SnapshotReader(pinned)
    selected = {
        key: ingredient.model_dump(mode="json")
        for key, ingredient in catalog.ingredients.items()
        if ingredient.pantry_price_per_kg_czk is not None
        or key in catalog.pantry.items
        or any(matches(ingredient, row.offer) for row in reader.offers)
    }
    return {
        **pinned,
        "catalog": catalog.model_dump(mode="json"),
        "ingredient_context": {key: selected[key] for key in sorted(selected)},
        "effective_request": request.model_dump(mode="json"),
    }


def evaluate_draft(
    request: RecipeRequest,
    inputs: dict[str, Any],
    value: dict[str, Any],
    now: datetime,
    *,
    relaxed: bool = False,
) -> dict[str, Any]:
    draft = RecipeDraft.model_validate(value)
    ids = [line.ingredient for line in draft.ingredients]
    if len(ids) != len(set(ids)) or set(ids) - inputs["ingredient_context"].keys():
        raise ValueError("use unique known eligible ingredient IDs")
    catalog = MealCatalog.model_validate(inputs["catalog"])
    recipe = Recipe(
        title=draft.title,
        minutes=draft.minutes,
        steps=draft.steps,
        meal_style=catalog.policy.meal_style,
        grams={
            line.ingredient: line.quantity / Decimal(request.servings) for line in draft.ingredients
        },
    )
    policy = catalog.policy.model_dump()
    if relaxed:
        policy.update(
            min_protein_g=Decimal(0),
            max_kcal=Decimal(10000),
            max_cost_per_serving_czk=Decimal(10000),
            max_minutes=480,
        )
    catalog = MealCatalog.model_validate(
        {**catalog.model_dump(), "recipes": (recipe,), "policy": policy}
    )
    reader = SnapshotReader(inputs)
    return plan_meals(reader, catalog, now, source_scopes=inputs["source_scopes"]).model_dump(
        mode="json"
    )


class MeasuredProvider:
    def __init__(
        self,
        client: httpx.Client,
        model: str,
        timeout: int,
        seed: int,
        *,
        think: bool | None = None,
    ) -> None:
        self.client, self.model, self.timeout, self.seed = client, model, timeout, seed
        self.think = think
        self.calls: list[dict[str, Any]] = []

    def generate(self, request: RecipeRequest, context: str) -> dict[str, Any]:
        # Model routing metadata is not task information; keep prompts identical across models.
        prompt = recipe_prompt(request.model_copy(update={"model": None}), context)
        started_at = utc_now()
        record: dict[str, Any] = {
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "prompt": prompt,
            "started_at": started_at.isoformat(),
        }
        self.calls.append(record)
        start = time.perf_counter()
        try:
            response = self.client.post(
                "/api/generate",
                json={
                    **({"think": self.think} if self.think is not None else {}),
                    "model": self.model,
                    "prompt": prompt,
                    "format": draft_schema(context),
                    "stream": False,
                    "keep_alive": "10m",
                    "options": {
                        "temperature": 0.2,
                        "seed": self.seed,
                        "num_ctx": 4096,
                        "num_predict": 512,
                        "num_thread": 2,
                    },
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
            record["response"] = body
            value = json.loads(body.get("response", ""))
            if not isinstance(value, dict):
                raise ValueError("model returned a non-object draft")
            return value
        except (ValueError, httpx.HTTPError) as exc:
            record["error"] = str(exc)
            record["error_type"] = type(exc).__name__
            raise
        finally:
            record["wall_seconds"] = time.perf_counter() - start
            finished_at = utc_now()
            record["finished_at"] = finished_at.isoformat()
            record["calendar_wall_seconds"] = (finished_at - started_at).total_seconds()


def run(args: argparse.Namespace) -> dict[str, Any]:
    output: Path = args.output
    if output.exists():
        raise ValueError("output directory already exists; choose a new directory")
    started = now = utc_now()
    prepared = {}
    if args.inputs_from:
        previous = json.loads(args.inputs_from.read_text(encoding="utf-8"))
        now = datetime.fromisoformat(previous["evaluation_at"])
        pinned, counts = previous["snapshot"], previous["offer_counts"]
        for name in CASES:
            case = previous["cases"][name]
            prepared[name] = (
                RecipeRequest.model_validate(case["request"]),
                case["inputs"],
                case["feasibility_witness"],
            )
    else:
        base = MealCatalog.load(args.catalog)
        pinned, counts = build_inputs(base, args.database, now)
        for name, parameters in CASES.items():
            request = RecipeRequest(
                provider="ollama",
                cache_policy="cache-only",
                request_id=uuid5(NAMESPACE_URL, name),
                **parameters,
            )
            inputs = case_inputs(base, request, pinned)
            witness = {
                "title": name,
                "minutes": 25,
                "steps": ["Benchmark feasibility witness."],
                "ingredients": [
                    {"ingredient": key, "quantity": grams, "unit": "g"}
                    for key, grams in WITNESSES[name].items()
                ],
            }
            prepared[name] = (request, inputs, evaluate_draft(request, inputs, witness, now))
    if any(not witness["meals"] for _, _, witness in prepared.values()):
        raise ValueError("every benchmark case must have a numerically feasible witness")
    with httpx.Client(base_url=args.api_url, timeout=args.timeout, trust_env=False) as client:
        tags = client.get("/api/tags").json()["models"]
        installed = {item["name"]: item for item in tags}
        missing = set(args.models) - installed.keys()
        if missing:
            raise ValueError(f"models must already be installed: {sorted(missing)}")
        residents = client.get("/api/ps").json().get("models", [])
        if residents:
            raise ValueError("another model is loaded; benchmark requires an idle local service")
        output.mkdir(parents=True)
        evidence: dict[str, Any] = {
            "started_at": started.isoformat(),
            "evaluation_at": now.isoformat(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "timing_clock": "perf_counter; monotonic elapsed time may exclude host suspension",
            "cpu_count": os.cpu_count(),
            "load_average_start": os.getloadavg(),
            "ollama_version": client.get("/api/version").json(),
            "models": [installed[model] for model in args.models],
            "options": {
                "temperature": 0.2,
                "num_ctx": 4096,
                "num_predict": 512,
                "num_thread": 2,
                "timeout_seconds": args.timeout,
                "repeats": args.repeats,
                "think": getattr(args, "think", None),
            },
            "database": str(args.database.resolve()),
            "inputs_from": str(args.inputs_from.resolve()) if args.inputs_from else None,
            "snapshot": pinned,
            "offer_counts": counts,
            "cases": {
                name: {
                    "request": req.model_dump(mode="json"),
                    "inputs": inp,
                    "feasibility_witness": witness,
                }
                for name, (req, inp, witness) in prepared.items()
            },
            "model_loads": [],
            "runs": [],
        }
        save(output / "results.json", evidence)
        for model in args.models:
            print(f"Loading {model}", flush=True)
            load_start = time.perf_counter()
            load = client.post(
                "/api/generate",
                json={
                    "model": model,
                    "keep_alive": "10m",
                    "options": {"num_ctx": 4096, "num_thread": 2},
                },
            )
            load.raise_for_status()
            evidence["model_loads"].append(
                {
                    "model": model,
                    "wall_seconds": time.perf_counter() - load_start,
                    "response": load.json(),
                    "resident": client.get("/api/ps").json(),
                }
            )
            try:
                for repeat in range(args.repeats):
                    for name, (original, inputs, _) in prepared.items():
                        request = original.model_copy(update={"model": model})
                        provider = MeasuredProvider(
                            client,
                            model,
                            args.timeout,
                            17 + repeat,
                            think=getattr(args, "think", None),
                        )
                        service = RecipeService(
                            Settings(meal_config=args.catalog),
                            RecipeSettings(),
                            clock=lambda: now,
                            provider=provider,
                        )
                        record: dict[str, Any] = {
                            "model": model,
                            "case": name,
                            "repeat": repeat + 1,
                            "seed": 17 + repeat,
                        }
                        start = time.perf_counter()
                        print(f"Starting {model} {name} repeat {repeat + 1}", flush=True)
                        try:
                            result, _ = service.execute(request, inputs)
                            record.update(status=result["status"], result=result)
                        except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                            record.update(
                                status="provider-failure",
                                error=str(exc),
                                error_type=type(exc).__name__,
                            )
                        record.update(
                            wall_seconds=time.perf_counter() - start, calls=provider.calls
                        )
                        if provider.calls and "response" in provider.calls[-1]:
                            try:
                                draft = json.loads(provider.calls[-1]["response"]["response"])
                                record["diagnostic_report"] = evaluate_draft(
                                    request, inputs, draft, now, relaxed=True
                                )
                            except (ValueError, TypeError, KeyError) as exc:
                                record["diagnostic_error"] = str(exc)
                        evidence["runs"].append(record)
                        save(output / "results.json", evidence)
                        print(
                            f"Finished {record['status']} in {record['wall_seconds']:.2f}s; "
                            f"{len(provider.calls)} call(s)",
                            flush=True,
                        )
            finally:
                client.post(
                    "/api/generate", json={"model": model, "keep_alive": 0}
                ).raise_for_status()
        evidence.update(finished_at=utc_now().isoformat(), load_average_end=os.getloadavg())
        evidence["summary"] = summarize(evidence)
        save(output / "results.json", evidence)
        return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--database", type=Path, default=Path("data/grocery.db"))
    parser.add_argument("--catalog", type=Path, default=Path("config/meals.toml"))
    parser.add_argument(
        "--inputs-from",
        type=Path,
        help="Replay pinned cases and evaluation date from results.json; no database read",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:11434")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--think", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument(
        "--timeout",
        type=int,
        choices=range(30, 601),
        default=240,
        metavar="SECONDS",
        help="Per-call timeout, from 30 to 600 seconds (default: 240)",
    )
    args = parser.parse_args()
    if args.api_url.rstrip("/") not in {"http://127.0.0.1:11434", "http://localhost:11434"}:
        parser.error("this benchmark is restricted to the local Ollama service")
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
