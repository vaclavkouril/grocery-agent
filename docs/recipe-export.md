# Recipe exports for nutrition apps

Open a completed recipe, expand **Export to a nutrition app**, choose a format and language,
then download or copy the selected recipe. The preview supports manual copying when browser
clipboard access is blocked. History and Bookmarks export their original saved recipes,
without a model call, stock deduction or modification of the original result.
The export language selects headings and saved ingredient translations; the original recipe
title and preparation steps stay in their generated language.

| Format | Transfer method |
| --- | --- |
| Full recipe text | Manual entry in Kalorické tabulky; includes servings, preparation and nutrition |
| Ingredients only | Paste into Cronometer's recipe importer or MyFitnessPal's bulk ingredient entry |
| Recipe JSON | Paste schema.org Recipe JSON into an importer such as Mealie's HTML/JSON import |

The existing report JSON is an application archive, not a third-party recipe import format.
Recipe JSON exports one meal without account, job or shopping metadata. Private backend URLs
cannot serve third-party URL importers. Nothing is published or sent to another service automatically.

## Kalorické tabulky

Use full text as a manual-entry reference. Add foods using the app's food search, enter their
edible raw weights, and save the combination as a meal. For one serving, divide each exported
whole-recipe ingredient quantity by the serving count. In a recipe editor with a serving count,
enter the whole-recipe amounts instead; do not divide twice.

Kalorické tabulky's [official guide](https://blog.kaloricketabulky.cz/2024/02/recepty-ve-vasi-aplikaci-kaloricke-tabulky/)
describes database food selection and saving meals. Its
[public recipe form](https://www.kaloricketabulky.cz/recipe/suggest) requires database ingredient
matches and an original photograph for a submitted recipe. Neither establishes a supported
recipe-file importer; this export is a manual-entry aid, not a direct file import.

## Paste import and structured recipes

Choose **Ingredients only**, preferably English for food matching, and paste the list into
the target app. Enter the recipe title and serving count separately. Check every matched food
and quantity before saving: target apps calculate nutrition from their own database, so totals
can differ from these estimates.

Cronometer's [recipe importer](https://support.cronometer.com/hc/en-us/articles/4403966087828-Mobile-Recipe-Importer)
accepts one ingredient per line and is a Gold feature. MyFitnessPal's
[recipe creation guide](https://support.myfitnesspal.com/hc/en-us/articles/360032271452-How-to-create-and-find-your-recipes)
documents bulk ingredient entry on iOS and Android.

For **Recipe JSON**, open the downloaded file as text and paste it into a compatible HTML/JSON
importer. [Mealie documents this method](https://mealie.io/documentation/getting-started/features/).
The schema.org Recipe includes title, ingredients, steps, serving yield, total preparation time
and nutrition per serving. It is not a Cronometer backup or Kalorické tabulky file format.
Some importers ignore nutrition fields; review the imported recipe before using it.

## Quantities and nutrition

Import lines use **raw edible grams for all servings**, including pantry stock and oil—not
just quantities to buy. Full text also shows raw shopping/pre-trimming grams. For example,
200 g of raw untrimmed carrot with an edible fraction of 0.61 exports as 122 g of edible raw
carrot. Match that to an edible raw carrot food, not a cooked or whole untrimmed entry.
Known “before trimming/peeling” qualifiers are removed from exported raw-food labels so
they do not contradict the edible quantities. Saved recipe data is not changed.

Nutrition is the saved estimate per serving; full text also shows whole-recipe totals.
Fibre, salt, cooking water and unmeasured condiment nutrition are not invented. Cooking changes
water content and final mass, so no cooked yield or per-100-g cooked nutrition is exported.
Weigh the finished dish yourself if the target app needs a cooked-weight basis.

## API and library

```text
GET /v1/jobs/{job_id}/recipes/{meal_index}/export?format=text&language=cs
```

The index is zero-based. Formats are `text`, `ingredients` and `schema-org`; languages are
`cs` and `en`. The owner-authenticated endpoint returns an attachment with `Cache-Control:
no-store`. Unknown or other users' jobs return 404; pending/non-recipe jobs return 409;
invalid selections or unsupported historical meal data return 422. Capabilities advertise
`recipe_export_formats`; old servers without the field do not show export controls.

Both Python HTTP clients provide `recipe_export(job_id, meal_index, format=..., language=...)`.
Local integrations can call `grocery_agent.recipe_exports.export_recipe(saved_result, index,
format=..., language=...)`, returning `body`, `media_type` and `extension`. An optional frozen
`ingredient_snapshot` provides localized labels; older results use saved line labels, not the
current configuration.

## Český návod

U hotového nebo uloženého receptu otevřete export, vyberte formát a jazyk a text zkopírujte
nebo stáhněte. Pro Kalorické tabulky použijte celý text pro ruční zadání potravin a uložení
vlastního jídla. Přímý import souboru receptu není ověřený. Pro Cronometer či MyFitnessPal
zvolte pouze suroviny a zkontrolujte automaticky přiřazené potraviny.

Množství platí pro **celý recept a jeho počet porcí**, v syrovém jedlém stavu. Pro jednu porci
je vydělte počtem porcí; při zadání celého receptu je znovu nedělte. Výživa na porci je odhad.
Hmotnost hotového jídla, vláknina a výživa koření bez zadaného množství se nevymýšlí.
Export neodečítá zásoby ani nemění původní recept.
