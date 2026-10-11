"""Stable cuisine preferences and bilingual discovery labels.

Styles guide model-written recipes, not nutrition rules or authenticity guarantees.
"""

from typing import Literal, get_args

CuisineId = Literal[
    "greek",
    "chinese",
    "vietnamese",
    "indian",
    "mexican",
    "italian",
    "thai",
    "japanese",
    "korean",
    "french",
    "spanish",
    "mediterranean",
    "czech",
    "middle_eastern",
]

CUISINE_IDS: tuple[CuisineId, ...] = get_args(CuisineId)

CUISINE_LABELS: dict[str, dict[str, str]] = {
    "greek": {"cs": "Řecká", "en": "Greek"},
    "chinese": {"cs": "Čínská", "en": "Chinese"},
    "vietnamese": {"cs": "Vietnamská", "en": "Vietnamese"},
    "indian": {"cs": "Indická", "en": "Indian"},
    "mexican": {"cs": "Mexická", "en": "Mexican"},
    "italian": {"cs": "Italská", "en": "Italian"},
    "thai": {"cs": "Thajská", "en": "Thai"},
    "japanese": {"cs": "Japonská", "en": "Japanese"},
    "korean": {"cs": "Korejská", "en": "Korean"},
    "french": {"cs": "Francouzská", "en": "French"},
    "spanish": {"cs": "Španělská", "en": "Spanish"},
    "mediterranean": {"cs": "Středomořská", "en": "Mediterranean"},
    "czech": {"cs": "Česká", "en": "Czech"},
    "middle_eastern": {"cs": "Blízkovýchodní", "en": "Middle Eastern"},
}
