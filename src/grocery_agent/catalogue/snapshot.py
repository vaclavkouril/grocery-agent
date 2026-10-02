"""Construction helpers for pinned single- and multi-source catalogues."""

from collections.abc import Sequence

from grocery_agent.catalogue.models import CatalogueSnapshot, CatalogueState
from grocery_agent.catalogue.profiles import AcquisitionProfile, combined_profile


def pin_states(
    states: Sequence[CatalogueState], profiles: Sequence[AcquisitionProfile] = ()
) -> CatalogueSnapshot:
    if not states:
        raise ValueError("at least one catalogue state is required")
    if len({state.source_id for state in states}) != len(states):
        raise ValueError("a snapshot cannot contain duplicate sources")
    if profiles and len(profiles) != len(states):
        raise ValueError("profiles and catalogue states must be aligned")
    if profiles and any(
        state.source_id != profile.source_id or state.profile_fingerprint != profile.fingerprint
        for state, profile in zip(states, profiles, strict=True)
    ):
        raise ValueError("pinned profiles must match source and collection identity")
    fingerprints = tuple(state.profile_fingerprint for state in states)
    warnings = tuple(warning for state in states for warning in state.warnings)
    complete = all(state.coverage_complete for state in states)
    return CatalogueSnapshot(
        source_ids=tuple(state.source_id for state in states),
        profile_fingerprints=fingerprints,
        run_ids=tuple(state.run_id for state in states),
        complete=complete,
        warnings=warnings,
    )


def combined_fingerprint(profiles: Sequence[AcquisitionProfile]) -> str:
    return combined_profile(tuple(profiles))
