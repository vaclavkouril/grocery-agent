"""Bounded text commands shared by backend email and SimpleX handling.

Parsing is local and has no side effects. Transports submit requests through HTTP;
the backend remains responsible for authorization and confirmation execution.
"""

import re
import shlex
from typing import Literal
from uuid import UUID

from grocery_agent.application.parameters import parse_owned_stock
from grocery_agent.contracts import Capabilities
from grocery_agent.models.common import DomainModel
from grocery_agent.recipes import RecipeRequest

HELP_TEXT = """Commands (an optional leading / is accepted):
help
verify TOKEN
confirm TOKEN
status JOB_ID
cancel JOB_ID
recipe [key=value ...] (meal and meals are aliases)

Recipe parameters: provider, model, source, profile (or profile_fingerprint),
profiles (comma-separated SOURCE:FINGERPRINT),
cache_policy, servings, language (cs/en), meal_style
(or style), have, budget, max_stores, exclude (or exclusions),
retailer (or retailer_ids), allow_loyalty, min_protein (or min_protein_g),
max_kcal, max_minutes, use_first, have_seasonings (or seasonings_available).
Retailers and use_first accept comma-separated IDs. Boolean values are true/false.
Protein is 0..300 g, calories are >0..10000; max_minutes is an integer from 1 to 480.
Use quotes for values containing spaces. Parameters and aliases must be unique.
Examples:
/recipe servings=2 have=rice=500g,lentils=available budget=80.0000
meal provider=template style=main exclude=chicken
/status 12345678-1234-1234-1234-123456789abc

Provider, source and cache defaults come from server capabilities. Models and
ingredient IDs must be advertised by the server. Budgets are CZK per serving.
Commands are limited to 4096 characters and 64 tokens.
For verification, send verify with the token issued to your channel. A recipe
requiring confirmation returns a confirmation token; send confirm with that
token to approve it. Verification and confirmation tokens are distinct, private,
and contain 20 to 200 URL-safe characters. Job IDs are UUIDs.
"""


class ChannelAction(DomainModel):
    action: Literal["help", "verify", "confirm", "status", "cancel"]
    argument: str | None = None


_FIELDS = {
    "language": "language",
    "provider": "provider",
    "model": "model",
    "source": "source_ids",
    "sources": "source_ids",
    "profiles": "profile_fingerprints",
    "profile": "profile_fingerprint",
    "profile_fingerprint": "profile_fingerprint",
    "cache_policy": "cache_policy",
    "servings": "servings",
    "meal_style": "meal_style",
    "style": "meal_style",
    "have": "pantry",
    "budget": "max_cost_per_serving_czk",
    "max_stores": "max_stores",
    "exclude": "exclusions",
    "exclusions": "exclusions",
    "retailer": "retailer_ids",
    "retailer_ids": "retailer_ids",
    "retailers": "retailer_ids",
    "allow_loyalty": "allow_loyalty",
    "min_protein": "min_protein_g",
    "min_protein_g": "min_protein_g",
    "max_kcal": "max_kcal",
    "max_minutes": "max_minutes",
    "use_first": "use_first",
    "have_seasonings": "seasonings_available",
    "seasonings_available": "seasonings_available",
}


def parse_channel_command(
    text: str, capabilities: Capabilities, language: Literal["cs", "en"] | None = None
) -> RecipeRequest | ChannelAction:
    """Parse strict key=value syntax; invalid commands raise ValueError.

    RecipeRequest performs final field validation. Capability checks further
    constrain the selected provider/model, cache policy, source and ingredients.
    Neither errors nor help interpolate verification or confirmation tokens.
    """
    if len(text) > 4096:
        raise ValueError("command must be at most 4096 characters")
    tokens = shlex.split(text)
    if not tokens or len(tokens) > 64:
        raise ValueError("command must contain between 1 and 64 tokens; use help")
    action = tokens[0].removeprefix("/")
    arguments = tokens[1:]
    if action == "help":
        if arguments:
            raise ValueError("help accepts no arguments")
        return ChannelAction(action="help")
    if action in {"verify", "confirm"}:
        if len(arguments) != 1 or re.fullmatch(r"[A-Za-z0-9_-]{20,200}", arguments[0]) is None:
            raise ValueError("verify/confirm require one 20..200 character URL-safe token")
        return ChannelAction.model_validate({"action": action, "argument": arguments[0]})
    if action in {"status", "cancel"}:
        if len(arguments) != 1:
            raise ValueError("status/cancel require one job UUID")
        try:
            job_id = str(UUID(arguments[0]))
        except ValueError:
            raise ValueError("status/cancel require a valid job UUID") from None
        return ChannelAction.model_validate({"action": action, "argument": job_id})
    if action not in {"recipe", "meal", "meals"}:
        raise ValueError("unsupported command; use help")

    supplied: dict[str, str] = {}
    for token in arguments:
        key, separator, value = token.partition("=")
        field = _FIELDS.get(key)
        if field is None or field not in RecipeRequest.model_fields:
            raise ValueError("unsupported recipe parameter; use help")
        if not separator or not value.strip() or field in supplied:
            raise ValueError("parameters must be unique nonempty key=value pairs")
        supplied[field] = value

    if not capabilities.sources:
        raise ValueError("server advertises no recipe sources")
    values: dict[str, object] = {
        "provider": capabilities.default_provider,
        "source_ids": (capabilities.sources[0],),
        "cache_policy": capabilities.default_cache_policy,
    }
    if language is not None:
        values["language"] = language
    for field, value in supplied.items():
        if field in {"servings", "max_stores", "max_minutes"}:
            if not value.isascii() or not value.isdigit():
                raise ValueError("servings/max_stores/max_minutes must be integers")
            values[field] = int(value)
        elif field in {"allow_loyalty", "seasonings_available"}:
            if value not in {"true", "false"}:
                raise ValueError("boolean parameters must be true or false")
            values[field] = value == "true"
        elif field == "pantry":
            entries = value.split(",")
            # Reuse CLI quantity and duplicate validation, retaining raw strings
            # rather than replacing quantities with floats or parsed grams.
            parsed = parse_owned_stock(entries)
            values[field] = {
                key: entry.partition("=")[2] for key, entry in zip(parsed, entries, strict=True)
            }
        elif field in {"exclusions", "retailer_ids", "use_first"}:
            items = tuple(item.strip() for item in value.split(","))
            if not all(items) or len(set(items)) != len(items):
                raise ValueError("selections must contain unique nonempty IDs")
            values[field] = items
        elif field == "source_ids":
            values[field] = tuple(value.split(","))
        elif field == "profile_fingerprints":
            profiles: dict[str, str] = {}
            for entry in value.split(","):
                source, separator, fingerprint = entry.partition(":")
                if not separator or not fingerprint or source in profiles:
                    raise ValueError("profiles must contain unique SOURCE:FINGERPRINT entries")
                profiles[source] = fingerprint
            values[field] = profiles
        else:
            # In particular, pass budget's raw decimal text to model validation.
            values[field] = value

    request = RecipeRequest.model_validate(values)
    if request.provider not in capabilities.providers:
        raise ValueError("provider is not permitted by server capabilities")
    models = capabilities.models.get(request.provider, ())
    if (request.provider == "ollama" and request.model is None) or (
        request.model is not None
        and (request.provider == "template" or request.model not in models)
    ):
        raise ValueError("model is not configured for this provider")
    if request.cache_policy not in capabilities.cache_policies:
        raise ValueError("cache policy is not permitted by server capabilities")
    if any(source not in capabilities.sources for source in request.source_ids):
        raise ValueError("source is not advertised by server capabilities")
    if len(request.source_ids) > 1 and not capabilities.combined_sources:
        raise ValueError("combined sources are not enabled by server capabilities")
    if request.meal_style not in capabilities.meal_styles:
        raise ValueError("meal style is not advertised by server capabilities")
    if (request.pantry.keys() | set(request.exclusions) | set(request.use_first)) - set(
        capabilities.ingredients
    ):
        raise ValueError("pantry, exclusions and use_first must use advertised ingredient IDs")
    return request
