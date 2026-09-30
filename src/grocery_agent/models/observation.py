from pydantic import AwareDatetime

from grocery_agent.models.common import DomainModel, NonEmpty
from grocery_agent.models.offer import Offer


class PriceObservation(DomainModel):
    offer: Offer
    observed_at: AwareDatetime
    snapshot_id: NonEmpty
