from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer


def reject_float(value: Any) -> Any:
    if isinstance(value, (float, bool)):
        raise ValueError("decimal values must be decimal strings, integers, or Decimal; not floats")
    return value


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


ExactDecimal = Annotated[
    Decimal, BeforeValidator(reject_float), PlainSerializer(decimal_text, return_type=str)
]
MoneyAmount = Annotated[ExactDecimal, Field(ge=0, max_digits=16, decimal_places=4)]
PositiveAmount = Annotated[ExactDecimal, Field(gt=0, max_digits=16, decimal_places=6)]
NonEmpty = Annotated[str, Field(min_length=1, max_length=500)]
StoreId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class DomainModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, revalidate_instances="always"
    )


def utc_now() -> datetime:
    return datetime.now(UTC)
