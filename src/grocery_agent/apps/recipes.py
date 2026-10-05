"""Quick recipe CLI with explicit optional delegation through the shared HTTP client."""

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from grocery_agent.application.parameters import parse_owned_stock
from grocery_agent.config import Settings
from grocery_agent.configuration import shared_config_context
from grocery_agent.http_client import BackendError, GroceryClient
from grocery_agent.meals.report import atomic_write
from grocery_agent.recipe_config import RecipeSettings
from grocery_agent.recipe_service import RecipeService
from grocery_agent.recipes import RecipeRequest

# Compatibility export for older callers/tests; local execution uses RecipeService.
from grocery_agent.recipes import generate_recipe as generate_recipe


def save_reports(
    settings: Settings,
    request: RecipeRequest,
    result: dict[str, Any],
    html: str,
    output_json: Path | None = None,
    output_html: Path | None = None,
    *,
    inputs: dict[str, Any] | None = None,
) -> None:
    """Publish an immutable request directory, then optional explicit output copies."""
    parent = settings.report_dir / "requests"
    for output in (output_json, output_html):
        if output is not None and output.resolve().is_relative_to(parent.resolve()):
            raise ValueError("explicit output paths must be outside immutable request reports")
    if output_json is not None and output_html is not None:
        if output_json.resolve() == output_html.resolve():
            raise ValueError("JSON and HTML output paths must differ")
    parent.mkdir(parents=True, exist_ok=True)
    directory = parent / str(request.request_id)
    if directory.exists():
        raise FileExistsError(f"request report already exists: {request.request_id}")
    json_text = json.dumps(result, ensure_ascii=False, indent=2)
    staging = Path(tempfile.mkdtemp(prefix=".report-", dir=parent))
    try:
        atomic_write(staging / "report.json", json_text)
        atomic_write(staging / "report.html", html)
        atomic_write(staging / "request.json", request.model_dump_json(indent=2))
        if inputs is not None:
            atomic_write(staging / "inputs.json", json.dumps(inputs, ensure_ascii=False, indent=2))
        os.rename(staging, directory)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if output_json is not None:
        atomic_write(output_json, json_text)
    if output_html is not None:
        atomic_write(output_html, html)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grocery-recipes")
    parser.add_argument("--provider", choices=("codex", "ollama", "template"))
    parser.add_argument("--model")
    parser.add_argument("--language", choices=("cs", "en"), default="en")
    parser.add_argument("--servings", type=int, default=2)
    parser.add_argument("--meal-style", default="main")
    parser.add_argument("--have", action="append", default=[])
    parser.add_argument("--budget")
    parser.add_argument("--max-stores", type=int, default=1)
    parser.add_argument("--retailer", action="append", default=[])
    loyalty = parser.add_mutually_exclusive_group()
    loyalty.add_argument("--allow-loyalty", dest="allow_loyalty", action="store_true")
    loyalty.add_argument("--no-loyalty", dest="allow_loyalty", action="store_false")
    parser.set_defaults(allow_loyalty=None)
    parser.add_argument("--min-protein")
    parser.add_argument("--max-kcal")
    parser.add_argument("--max-minutes", type=int)
    parser.add_argument("--use-first", action="append", default=[])
    parser.add_argument("--have-seasonings", action="store_true", default=None)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--source", action="append", help="source ID; repeat for multiple sources")
    parser.add_argument("--profile-fingerprint", help="select one cached source profile")
    parser.add_argument("--profile", action="append", default=[], metavar="SOURCE=FP")
    cache = parser.add_mutually_exclusive_group()
    cache.add_argument("--cache-policy", choices=("local", "refresh", "no-cache", "cache-only"))
    cache.add_argument(
        "--cache-only", dest="cache_policy", action="store_const", const="cache-only"
    )
    cache.add_argument("--no-cache", dest="cache_policy", action="store_const", const="no-cache")
    parser.add_argument(
        "--config", type=Path, help="recipe options TOML (default: config/recipes.toml)"
    )
    parser.add_argument("--shared-config", type=Path, help="shared application defaults TOML")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-html", type=Path)
    parser.add_argument("--api-url", help="explicitly delegate to a configured backend")
    parser.add_argument(
        "--token-env",
        default="GROCERY_API_TOKEN",
        help="environment variable containing the session token",
    )
    parser.add_argument(
        "--no-wait", action="store_true", help="print the backend receipt immediately"
    )
    parser.add_argument("--wait-seconds", type=float, default=120)
    args = parser.parse_args(argv)
    if args.wait_seconds <= 0:
        parser.error("--wait-seconds must be positive")
    if args.no_wait and not args.api_url:
        parser.error("--no-wait requires --api-url")
    if args.no_wait and (args.output_json is not None or args.output_html is not None):
        parser.error("report output requires waiting for the backend result")
    with shared_config_context(args.shared_config):
        return _run(args, parser)


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        parse_owned_stock(args.have)
        pantry = dict(item.split("=", 1) for item in args.have)
        values: dict[str, object] = {
            "language": args.language,
            "servings": args.servings,
            "meal_style": args.meal_style,
            "pantry": pantry,
            "max_stores": args.max_stores,
            "exclusions": args.exclude,
            "retailer_ids": args.retailer,
            "allow_loyalty": args.allow_loyalty,
            "min_protein_g": args.min_protein,
            "max_kcal": args.max_kcal,
            "max_minutes": args.max_minutes,
            "use_first": args.use_first,
            "seasonings_available": args.have_seasonings,
        }
        if args.model:
            values["model"] = args.model
        if args.budget:
            values["max_cost_per_serving_czk"] = args.budget
        if args.profile_fingerprint is not None:
            values["profile_fingerprint"] = args.profile_fingerprint
        if args.profile:
            profiles: dict[str, str] = {}
            for selection in args.profile:
                source, separator, fingerprint = selection.partition("=")
                if not separator or not source or not fingerprint:
                    raise ValueError("--profile must be SOURCE=FP")
                if source in profiles:
                    raise ValueError(f"duplicate profile selection for {source}")
                profiles[source] = fingerprint
            values["profile_fingerprints"] = profiles
        if args.api_url:
            token = os.environ.get(args.token_env)
            if not token:
                parser.error(f"set {args.token_env} to your backend session token")
            with GroceryClient(args.api_url, token) as client:
                supported = client.capabilities()
                values["provider"] = args.provider or supported.default_provider
                values["cache_policy"] = args.cache_policy or supported.default_cache_policy
                values["source_ids"] = tuple(args.source or (supported.sources[0],))
                request = RecipeRequest.model_validate(values)
                receipt = client.submit(request)
                if args.no_wait:
                    print(receipt.model_dump_json())
                    return 0
                print(f"Accepted job {receipt.job_id}", file=sys.stderr)
                job = client.wait(receipt.job_id, max_wait_seconds=args.wait_seconds)
                if job.status != "succeeded":
                    print(job.model_dump_json())
                    return 1
                result = client.result(job.job_id)
                if args.output_json is not None or args.output_html is not None:
                    save_reports(
                        Settings(),
                        request,
                        result,
                        client.report(job.job_id),
                        args.output_json,
                        args.output_html,
                    )
                print(json.dumps(result, ensure_ascii=False))
                return 0
        options = RecipeSettings.load(args.config)
        values["provider"] = args.provider or options.default_provider
        values["model"] = args.model if args.model is not None else options.model
        values["cache_policy"] = args.cache_policy or options.default_cache_policy
        if args.source:
            values["source_ids"] = tuple(args.source)
        request = RecipeRequest.model_validate(values)
        settings = Settings()
        service = RecipeService(settings, options)
        inputs = service.prepare(request)
        result, html = service.execute(request, inputs)
        save_reports(
            settings, request, result, html, args.output_json, args.output_html, inputs=inputs
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, BackendError, httpx.HTTPError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
