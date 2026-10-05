"""Shared English/Czech UI messages. User and provider text is never translated."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

Language = Literal["cs", "en"]

# English is also the compatibility key, keeping existing rendered wording stable.
CS = {
    "Already owned": "Již doma",
    "Pantry estimate": "Odhad ze zásob",
    "nutrition source": "zdroj výživových hodnot",
    "edible yield": "jedlý podíl",
    "source": "zdroj",
    "run": "běh",
    "shopping context": "nákupní kontext",
    "unspecified": "neuvedeno",
    "start not specified": "začátek neuveden",
    "end not specified": "konec neuveden",
    "no terms reported": "podmínky neuvedeny",
    "stock": "dostupnost",
    "observed": "zjištěno",
    "Seasonings already available: 0.00 Kč.": "Koření je již doma: 0,00 Kč.",
    "Plus {cost} Kč pantry seasoning allowance per serving.": (
        "Navíc {cost} Kč na koření ze zásob na porci."
    ),
    "Uses {grams} g of ingredients marked use first.": (
        "Využívá {grams} g surovin označených ke spotřebě přednostně."
    ),
    "minutes": "minut",
    "serving(s)": "porcí",
    "No store trip needed": "Bez nákupu",
    "protein": "bílkoviny",
    "carbs": "sacharidy",
    "fat": "tuky",
    "serving": "porce",
    "Ingredient": "Surovina",
    "Use": "Použít",
    "Already have": "Již doma",
    "Buy": "Koupit",
    "Retailer": "Obchod",
    "Quote": "Cena",
    "Used cost": "Cena spotřeby",
    "Table quantities cover all servings; macros and headline cost are per serving. "
    "Weigh rice/lentils dry and meat raw; vegetables before trimming.": (
        "Množství v tabulce platí pro všechny porce; živiny a hlavní cena jsou na porci. "
        "Rýži a čočku važte suché, maso syrové a zeleninu před očištěním."
    ),
    "Cook it": "Postup",
    "Offer terms and nutrition sources": "Podmínky nabídek a zdroje živin",
    "use-first stock, then ": "zásob ke spotřebě přednostně, pak ",
    "Protein meals": "Jídla s bílkovinami",
    "Good food. More protein.": "Dobré jídlo. Více bílkovin.",
    "Today's advertised prices.": "Dnešní akční ceny.",
    "Meal style": "Typ jídla",
    "minimum": "nejméně",
    "maximum": "nejvýše",
    "Lactose-free": "Bez laktózy",
    "loyalty prices": "věrnostní ceny",
    "ranked by": "řazeno podle",
    "True": "ano",
    "False": "ne",
    "main": "hlavní jídlo",
    "breakfast": "snídaně",
    "snack": "svačina",
    "protein per czk": "bílkovin na Kč",
    "For the report date only. Check offer dates and outlet terms before shopping. "
    "Availability is not guaranteed.": (
        "Platí pouze pro datum reportu. Před nákupem ověřte platnost nabídek a podmínky "
        "prodejny. Dostupnost není zaručena."
    ),
    "Costs estimate usage, not a checkout total. Whole packs may cost more; unknown pack sizes "
    "and travel are not priced. Already-owned quantities cost zero; oil and spices use estimates "
    "unless marked available. Each dish is an alternative using the same stock, not a combined "
    "shopping plan. Stock is not deducted automatically; update it after cooking. Macros use "
    "generic ingredient data, trimming yields, and include oil; optional seasonings are excluded. "
    "Select plain ingredients and check labels for lactose.": (
        "Cena odhaduje spotřebu, nikoli cenu celého nákupu. Celá balení mohou stát více; "
        "neznámé velikosti balení a doprava nejsou započteny. Domácí zásoby mají nulovou cenu; "
        "olej a koření mají odhadovanou cenu, pokud nejsou označeny jako dostupné. Každé jídlo "
        "je alternativou se stejnými zásobami, nikoli společným nákupním plánem. Zásoby se "
        "neodečítají automaticky; po vaření je aktualizujte. Živiny vycházejí z obecných údajů "
        "a jedlého podílu a zahrnují olej; volitelné koření je vynecháno. Vybírejte neochucené "
        "suroviny a ověřte na etiketách obsah laktózy."
    ),
    "Generated": "Vygenerováno",
    "batch": "dávka",
    "This report is out of date. Run grocery-agent meals to generate current suggestions.": (
        "Tento report je zastaralý. Spusťte grocery-agent meals pro aktuální návrhy."
    ),
    "No feasible recipe.": "Žádný vyhovující recept.",
    "No configured template remains after exclusions.": "Po vyloučení surovin nezbyl žádný recept.",
    "No complete recipe meets the configured limits.": (
        "Žádný úplný recept nesplňuje zadané limity."
    ),
    "Verify your grocery account: verify {token}": "Ověřte svůj účet: verify {token}",
    "Channel verified. ": "Kanál ověřen. ",
    "Recipe parameters: ": "Parametry receptu: ",
    ". Reply confirm {token} within 15 minutes.": ". Do 15 minut odpovězte confirm {token}.",
    "Command rejected; check help, channel verification and server limits.": (
        "Příkaz odmítnut; zkontrolujte help, ověření kanálu a limity serveru."
    ),
    "Request could not be queued; check pending jobs and try again.": (
        "Požadavek nelze zařadit; zkontrolujte čekající úlohy a zkuste to znovu."
    ),
    "Already accepted job {job}.": "Úloha {job} již byla přijata.",
    "Accepted job {job}. Use status {job} to check progress.": (
        "Úloha {job} přijata. Průběh ověřte příkazem status {job}."
    ),
    "Job not found.": "Úloha nenalezena.",
    "Job {job}: {state}, {phase}.": "Úloha {job}: {state}, {phase}.",
    "Job {job}: {state}.": "Úloha {job}: {state}.",
    "Cancelled job {job}.": "Úloha {job} zrušena.",
    "Job already finished.": "Úloha již skončila.",
    "Grocery Agent recipe": "Grocery Agent recept",
    "Grocery Agent command": "Grocery Agent příkaz",
    "queued": "ve frontě",
    "running": "probíhá",
    "succeeded": "dokončena",
    "failed": "selhala",
    "cancelled": "zrušena",
    "pending": "čeká",
    "prepare": "příprava",
    "generate": "generování",
    "execute": "zpracování",
    "completed": "dokončeno",
    "created": "vytvořeno",
    "preparing": "příprava",
    "generating": "generování",
    "available": "dostupné",
    "unavailable": "nedostupné",
    "unknown": "neznámé",
    "Acquisition coverage is unknown.": "Rozsah získaných dat není znám.",
    "Legacy collection has unknown acquisition coverage; no profile is inferred.": (
        "Starší kolekce má neznámý rozsah dat; profil není odvozován."
    ),
    "Refresh failed for {source}; using its fresh previous profile collection.": (
        "Obnovení zdroje {source} selhalo; používá se jeho předchozí aktuální kolekce profilu."
    ),
    "Latest rescan is {status}; using complete cached run {run} from {date} "
    "within its freshness limit.": (
        "Poslední sběr má stav {status}; používá se úplný uložený běh {run} "
        "z {date} v rámci limitu stáří."
    ),
}


def translate(message: str, language: Language = "en", **values: object) -> str:
    if language not in {"cs", "en"}:
        raise ValueError("language must be cs or en")
    translated = CS.get(message, message) if language == "cs" else message
    return translated.format(**values) if values else translated


def format_decimal(value: Decimal, language: Language = "en", spec: str = "g") -> str:
    """Format Decimal directly, never passing through a binary float."""
    text = format(value, spec)
    return text.replace(".", ",") if language == "cs" else text


def format_date(value: date | datetime, language: Language = "en") -> str:
    if language == "en":
        return value.isoformat()
    result = value.strftime("%d.%m.%Y")
    if isinstance(value, datetime):
        result += value.strftime(" %H:%M:%S %Z").rstrip()
    return result


HELP_CS = """Příkazy (volitelné úvodní /):
help
verify TOKEN
confirm TOKEN
status JOB_ID
cancel JOB_ID
recipe [key=value ...] (aliasy meal a meals)

Parametry: provider, model, source, profile (profile_fingerprint), profiles
(SOURCE:FINGERPRINT oddělené čárkami), cache_policy, servings, language (cs/en),
meal_style (style), have, budget, max_stores, exclude (exclusions), retailer
(retailer_ids), allow_loyalty, min_protein (min_protein_g), max_kcal, max_minutes,
use_first, have_seasonings (seasonings_available).
ID obchodů a use_first oddělujte čárkami. Logické hodnoty jsou true/false.
Bílkoviny: 0..300 g, kalorie: >0..10000, max_minutes: celé číslo 1..480.
Hodnoty s mezerami pište v uvozovkách. Parametry a aliasy musí být jedinečné.
Příklad: /recipe language=cs servings=2 have=rice=500g,lentils=available budget=80.0000
Výchozí provider, source a cache_policy určuje server. Modely a ID surovin
musí být povoleny serverem. Rozpočet je v Kč na porci. Limit: 4096 znaků a 64 tokenů.
Pro ověření pošlete verify s tokenem pro váš kanál. Recept vyžadující potvrzení
vrátí token; schvalte jej příkazem confirm TOKEN. Ověřovací a potvrzovací tokeny
jsou odlišné a soukromé: 20 až 200 znaků bezpečných pro URL. ID úloh jsou UUID.
"""


def channel_help(language: Language = "en") -> str:
    from grocery_agent.channel_commands import HELP_TEXT

    return HELP_CS if language == "cs" else HELP_TEXT


def localized_notice(message: str, language: Language = "en") -> str:
    """Translate known application notices; preserve external evidence verbatim."""
    if language == "en":
        return message
    for pattern, template in (
        (
            r"Refresh failed for (?P<source>[^;]+); using its fresh previous profile collection\.",
            "Refresh failed for {source}; using its fresh previous profile collection.",
        ),
        (
            r"Latest rescan is (?P<status>[^;]+); using complete cached run (?P<run>\S+) "
            r"from (?P<date>\S+) within its freshness limit\.",
            "Latest rescan is {status}; using complete cached run {run} from {date} "
            "within its freshness limit.",
        ),
    ):
        match = re.fullmatch(pattern, message)
        if match:
            return translate(template, language, **match.groupdict())
    return translate(message, language)
