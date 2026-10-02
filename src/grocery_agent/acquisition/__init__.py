"""Profile-aware acquisition without persistence or application service dependencies."""

from grocery_agent.acquisition.profiles import (
    FilteringAdapter,
    effective_profile,
    profile_adapter,
    profiled_adapter,
)

__all__ = ["FilteringAdapter", "effective_profile", "profile_adapter", "profiled_adapter"]
