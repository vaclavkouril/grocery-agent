"""Synthetic prices on captured public Makro card structure, never live price claims."""

from collections.abc import AsyncIterator
from pathlib import Path

from bs4 import BeautifulSoup

from grocery_agent.stores.base import AdapterContext
from grocery_agent.stores.makro.adapter import CatalogDocument, MakroAdapter
from grocery_agent.stores.makro.config import MakroSettings
from grocery_agent.stores.makro.parser import category_url

FIXTURES = Path(__file__).parent / "fixtures" / "makro"
URL = category_url("potraviny")


def priced_source() -> bytes:
    soup = BeautifulSoup((FIXTURES / "anonymous.html").read_bytes(), "lxml")
    cards = soup.select(".sd-articlecard")
    examples = [
        (
            "Test Rice 6 x 500 g",
            "Balení po 6",
            "100,00",
            "112,00",
            "Bez zálohy; Bez dalších poplatků",
        ),
        (
            "Test Drink 6 x 500 ml",
            "Balení po 6",
            "60,00",
            "72,60",
            "Záloha vč. DPH 18,00 Kč; Bez dalších poplatků",
        ),
        ("Test Eggs 180 ks", "1 balení", "100,00", "112,00", ""),
        ("Test Beef váž. cca 8 kg", "cca. 8 kg / ks", "100,00", "112,00", "Cena / kg"),
    ]
    for card in cards[len(examples) :]:
        card.decompose()
    for card, (name, package, net, gross, charges) in zip(cards, examples, strict=False):
        title = card.select_one("a.title h4")
        packaging = card.select_one(".bundle.packaging-type")
        bottom = card.select_one(".bottom-part")
        assert title is not None and packaging is not None and bottom is not None
        title.string, packaging.string = name, package
        bottom.clear()
        markup = BeautifulSoup(
            f'<div class="price-display"><div class="price-display-main-row">'
            f'<span class="primary">{net} Kč</span></div><div class="secondary-price-row">'
            f'<span class="secondary">vč. DPH {gross} Kč</span></div>'
            f'<div class="additional-price-info-row">{charges}</div></div>',
            "lxml",
        )
        assert markup.body is not None
        bottom.append(markup.body.div)
    count = soup.select_one("p.text-default span")
    assert count is not None
    count.string = "Zobrazeno 4 z 4 výsledků"
    return str(soup).encode()


class FixtureMakro(MakroAdapter):
    def __init__(self, documents: list[CatalogDocument] | None = None) -> None:
        super().__init__(MakroSettings(_env_file=None, category_paths=("potraviny",)))
        self.documents = documents or [CatalogDocument(priced_source(), URL, "potraviny")]

    async def _documents(self, context: AdapterContext) -> AsyncIterator[CatalogDocument]:
        for document in self.documents:
            yield document


def adapter() -> MakroAdapter:
    return FixtureMakro()
