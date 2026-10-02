from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from grocery_agent.acquisition.profiles import (
    FilteringAdapter,
    effective_profile,
    profile_adapter,
    profiled_adapter,
)
from grocery_agent.catalogue.profiles import (
    AcquisitionProfile,
    CachePolicy,
    Coverage,
    combined_profile,
)
from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.models.common import utc_now
from grocery_agent.pipeline.service import ScrapePipeline
from grocery_agent.stores.base import AcquisitionItem, AdapterContext, SourceEvidence, StoreAdapter
from grocery_agent.stores.kupi.adapter import KupiAdapter
from grocery_agent.stores.kupi.config import KupiSettings
from grocery_agent.stores.makro.adapter import MakroAdapter
from grocery_agent.stores.makro.config import MakroSettings
from grocery_agent.stores.mock.adapter import MockStore
from grocery_agent.stores.registry import StoreRegistry
from grocery_agent.stores.rohlik.adapter import RohlikAdapter
from grocery_agent.stores.rohlik.config import RohlikSettings
from grocery_agent.stores.tesco.adapter import TescoAdapter
from grocery_agent.stores.tesco.config import TescoSettings


class ItemsAdapter(StoreAdapter):
    def __init__(self, items, failure=None):
        self.items = items
        self.failure = failure

    @property
    def store_id(self):
        return "mock"

    async def fetch_offers(self, context):
        for item in self.items:
            yield item
        if self.failure:
            raise self.failure


def item(candidate):
    return AcquisitionItem(
        SourceEvidence(
            b"original evidence",
            "https://example.invalid",
            "text/html",
            utc_now(),
            "original locator",
            {"provenance": "retained"},
        ),
        candidate=deepcopy(candidate),
    )


async def collect(adapter):
    async with httpx.AsyncClient() as http:
        return [value async for value in adapter.fetch_offers(AdapterContext(http))]


def test_canonical_fingerprint_and_legacy_helpers():
    first = AcquisitionProfile(
        source_id="mock",
        categories=("b", "a", "a"),
        search=(" MILK ", "milk"),
        retailer_ids=("tesco", "mock"),
    )
    second = AcquisitionProfile(
        source_id="mock", categories=("a", "b"), search=("milk",), retailer_ids=("mock", "tesco")
    )
    assert first.fingerprint == second.fingerprint
    other = AcquisitionProfile(source_id="tesco")
    assert combined_profile((first, other)) == combined_profile((other, first))
    with pytest.raises(ValueError):
        combined_profile((first, second))
    with pytest.raises(ValueError):
        combined_profile(())
    Coverage(profile_fingerprint=first.fingerprint, complete=True).require_publishable()
    with pytest.raises(ValueError):
        Coverage(profile_fingerprint=first.fingerprint, complete=False).require_publishable()
    with pytest.raises(ValueError):
        Coverage(
            profile_fingerprint=first.fingerprint, complete=True, expected=2, observed=1
        ).require_publishable()


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "x" * 65},
        {"name": "bad/name"},
        {"search": ("  ",)},
        {"scope": ""},
        {"eligibility": ("cheap",)},
        {"options": {"session_path": "secret"}},
        {"options": {"max_pages": 1.5}},
        {"options": {"category_ids": [True]}},
        {"options": {"category_slugs": []}},
        {"options": {"credentials": "secret"}},
    ],
)
def test_unsafe_profiles_rejected(fields):
    with pytest.raises(ValueError):
        AcquisitionProfile(source_id="mock", **fields)


@pytest.mark.parametrize("mode", ["local", "refresh", "cache-only", "no-cache"])
def test_cache_requires_complete_and_fresh(mode):
    now = datetime.now(UTC)
    policy = CachePolicy(mode=mode)
    assert not policy.usable(finished_at=now, now=now, complete=False)
    assert policy.usable(finished_at=now, now=now, complete=True) == (mode != "no-cache")
    assert not policy.usable(finished_at=now - timedelta(hours=37), now=now, complete=True)
    assert not policy.usable(finished_at=now + timedelta(seconds=1), now=now, complete=True)


@pytest.mark.parametrize(
    "adapter, expected",
    [
        (
            KupiAdapter(KupiSettings(category_slugs=("pecivo",), max_pages=7)),
            {"category_slugs": ["pecivo"], "listing_url": None, "max_pages": 7},
        ),
        (
            TescoAdapter(TescoSettings(category_slugs=None, max_pages=8)),
            {"category_slugs": None, "max_pages": 8},
        ),
        (
            MakroAdapter(
                MakroSettings(
                    category_paths=("potraviny",),
                    account_scope="alias",
                    store_name="Praha",
                    max_pages=9,
                )
            ),
            {
                "category_paths": ["potraviny"],
                "account_scope": "alias",
                "store_name": "Praha",
                "max_pages": 9,
            },
        ),
        (
            RohlikAdapter(
                RohlikSettings(
                    category_ids=(22, 11), expected_warehouse_id=3, page_size=20, max_pages=10
                )
            ),
            {
                "category_ids": [11, 22],
                "expected_warehouse_id": 3,
                "page_size": 20,
                "max_pages": 10,
            },
        ),
    ],
)
def test_native_effective_options(adapter, expected):
    profile = effective_profile(adapter)
    assert profile.options == expected
    assert profile.scope is None
    assert effective_profile(adapter, profile).fingerprint == profile.fingerprint


def test_overrides_use_configured_base_and_do_not_mutate_environment(monkeypatch):
    source = TescoAdapter(
        TescoSettings(category_slugs=("pecivo",), max_pages=17, request_delay_seconds=0.25)
    )
    registry = StoreRegistry()
    registry.register("tesco", lambda: source)
    monkeypatch.setenv("GROCERY_TESCO_MAX_PAGES", "999")
    adapter, profile = profiled_adapter(
        registry,
        AcquisitionProfile(
            source_id="tesco", categories=("ovoce-a-zelenina",), options={"max_pages": 4}
        ),
    )
    assert isinstance(adapter, FilteringAdapter)
    assert adapter.adapter.settings.category_slugs == ("ovoce-a-zelenina",)
    assert adapter.adapter.settings.max_pages == 4
    assert adapter.adapter.settings.request_delay_seconds == 0.25
    assert source.settings.category_slugs == ("pecivo",)
    assert source.settings.max_pages == 17
    assert effective_profile(source).options["max_pages"] == 17
    assert profile.options["max_pages"] == 4


@pytest.mark.parametrize(
    "options,categories",
    [
        ({"max_pages": True}, ()),
        ({"max_pages": "2"}, ()),
        ({"max_pages": 1001}, ()),
        ({"account_scope": "foo"}, ()),
        ({}, ("top-vyber",)),
        ({"category_slugs": ["pecivo"]}, ("ovoce-a-zelenina",)),
    ],
)
def test_native_override_validation(options, categories):
    with pytest.raises(ValueError):
        effective_profile(
            TescoAdapter(TescoSettings()),
            AcquisitionProfile(source_id="tesco", options=options, categories=categories),
        )


def test_kupi_listing_url_and_categories():
    source = KupiAdapter(KupiSettings(listing_url="https://www.kupi.cz/slevy/pecivo"))
    profile = effective_profile(source)
    assert profile.categories == ("pecivo",)
    assert effective_profile(source, profile).fingerprint == profile.fingerprint
    registry = StoreRegistry()
    registry.register("kupi", lambda: source)
    adapter, profile = profiled_adapter(
        registry, AcquisitionProfile(source_id="kupi", categories=("ovoce-a-zelenina",))
    )
    assert adapter.adapter.settings.listing_url is None
    assert adapter.adapter.settings.listing_urls == ("https://www.kupi.cz/slevy/ovoce-a-zelenina",)
    assert effective_profile(
        source,
        AcquisitionProfile(source_id="kupi", options={"category_slugs": ["ovoce-a-zelenina"]}),
    ).categories == ("ovoce-a-zelenina",)


def test_mock_and_custom_reject_native_selectors():
    for source in (MockStore(), ItemsAdapter([])):
        assert effective_profile(source).options == {}
        for fields in ({"categories": ("food",)}, {"options": {"max_pages": 1}}):
            with pytest.raises(ValueError):
                effective_profile(source, AcquisitionProfile(source_id="mock", **fields))
    with pytest.raises(ValueError):
        effective_profile(MockStore(), AcquisitionProfile(source_id="tesco"))


async def test_filters_preserve_original_candidate_and_evidence(candidate):
    candidate["product"]["name"] = "ＭＩＬＫ   Fresh"
    candidate["product"]["brand"] = "Brand"
    candidate["availability"] = "available"
    selected = item(candidate)
    wrapper = FilteringAdapter(
        ItemsAdapter([selected]),
        AcquisitionProfile(
            source_id="mock",
            search=("milk fresh", "BRAND"),
            retailer_ids=("tesco", "mock"),
            scope=candidate["scope"],
            eligibility=("exact", "available", "non-loyalty"),
        ),
    )
    assert await collect(wrapper) == [selected]
    assert (await collect(wrapper))[0] is selected
    assert selected.candidate["product"]["name"] == "ＭＩＬＫ   Fresh"
    assert wrapper.actual_scopes == (candidate["scope"],)
    assert wrapper.complete


@pytest.mark.parametrize(
    "fields",
    [
        {"search": ("absent",)},
        {"search": ("milk", "absent")},
        {"retailer_ids": ("tesco",)},
        {"scope": "configured-warehouse-label"},
        {"eligibility": ("available",)},
    ],
)
async def test_filters_select_actual_quotes(candidate, fields):
    candidate["availability"] = "unknown"
    wrapper = FilteringAdapter(
        ItemsAdapter([item(candidate)]), AcquisitionProfile(source_id="mock", **fields)
    )
    assert await collect(wrapper) == []
    assert wrapper.actual_scopes == ()
    assert wrapper.complete


@pytest.mark.parametrize(
    "eligibility,changes",
    [
        (("exact",), {"price_qualifier": "from"}),
        (("non-loyalty",), {"promotion": {"kind": "loyalty", "requires_loyalty": True}}),
    ],
)
async def test_eligibility_filters(candidate, eligibility, changes):
    candidate.update(changes)
    wrapper = FilteringAdapter(
        ItemsAdapter([item(candidate)]),
        AcquisitionProfile(source_id="mock", eligibility=eligibility),
    )
    assert await collect(wrapper) == []


async def test_invalid_and_wrong_source_pass_to_pipeline(candidate, repository, snapshots):
    invalid = deepcopy(candidate)
    invalid["current_price"] = "-1"
    wrong_source = deepcopy(candidate)
    wrong_source["product"]["store_id"] = "tesco"
    wrong_item = item(wrong_source)
    parser_error = AcquisitionItem(wrong_item.evidence, error="parse failure")
    items = [item(invalid), wrong_item, parser_error]
    wrapper = FilteringAdapter(
        ItemsAdapter(items),
        AcquisitionProfile(
            source_id="mock", search=("never match",), retailer_ids=("mock",), scope="absent"
        ),
    )
    assert await collect(wrapper) == items
    assert not wrapper.complete
    pipeline = ScrapePipeline(repository, snapshots, ExactGTINResolver())
    async with httpx.AsyncClient() as http:
        result = await pipeline.run(wrapper, AdapterContext(http))
    assert (result.fetched, result.accepted, result.rejected, result.status) == (3, 0, 3, "partial")


async def test_exhaustion_failure_partial_and_actual_scopes(candidate):
    first = item(candidate)
    candidate["scope"] = "actual-second"
    second = item(candidate)
    wrapper = FilteringAdapter(ItemsAdapter([first, second]), AcquisitionProfile(source_id="mock"))
    await collect(wrapper)
    assert wrapper.actual_scopes == tuple(sorted({first.candidate["scope"], "actual-second"}))
    partial = FilteringAdapter(
        ItemsAdapter([first]), AcquisitionProfile(source_id="mock", coverage="partial")
    )
    await collect(partial)
    assert not partial.complete
    failed = FilteringAdapter(
        ItemsAdapter([first], ValueError("native coverage failure")),
        AcquisitionProfile(source_id="mock"),
    )
    with pytest.raises(ValueError, match="native coverage failure"):
        await collect(failed)
    assert not failed.complete


def test_direct_helper_applies_native_options_and_reuses_effective_wrapper():
    source = RohlikAdapter(RohlikSettings(category_ids=(11, 22), expected_warehouse_id=2))
    requested = AcquisitionProfile(
        source_id="rohlik", categories=("22",), options={"expected_warehouse_id": 3}
    )
    wrapped, effective = profile_adapter(source, requested)
    assert wrapped.adapter.settings.category_ids == (22,)
    assert wrapped.adapter.settings.expected_warehouse_id == 3
    assert source.settings.expected_warehouse_id == 2
    again, repeated = profile_adapter(wrapped, effective)
    assert again is wrapped
    assert repeated.fingerprint == effective.fingerprint
    assert effective_profile(wrapped).options == effective.options
    changed, _ = profile_adapter(
        wrapped, AcquisitionProfile(source_id="rohlik", categories=("11",), search=("new search",))
    )
    assert isinstance(changed.adapter, RohlikAdapter)
    assert changed.adapter.settings.category_ids == (11,)


async def test_direct_helper_applies_filters_and_replaces_existing_filters(candidate):
    selected = item(candidate)
    source = ItemsAdapter([selected])
    wrapped, effective = profile_adapter(
        source, AcquisitionProfile(source_id="mock", search=("no matching phrase",))
    )
    assert await collect(wrapped) == []
    assert profile_adapter(wrapped, effective)[0] is wrapped
    assert effective_profile(wrapped).fingerprint == effective.fingerprint
    replaced, _ = profile_adapter(wrapped, AcquisitionProfile(source_id="mock"))
    assert replaced.adapter is source
    assert await collect(replaced) == [selected]
