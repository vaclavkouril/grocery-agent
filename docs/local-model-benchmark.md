# Local recipe model benchmark on this PC

For the five additional local models and budget Codex comparison, see the
[second benchmark round](model-benchmark-round-two.md). This report preserves the original run.

All five tested local models fit this PC, but none reliably generated recipes satisfying the application's constraints. Across 30 requests, seven passed the numerical filters, all breakfasts; manual review found incomplete or inconsistent instructions even among those passes. The smallest coding model had the lowest median request time at 70.5 seconds. Gemma 3 1B was the more promising of the two general-purpose models for further experiments, not a production-ready recipe writer.

This report measures the actual grocery-agent machine on 2 and 3 October 2026, using its cached grocery offers and the application's recipe validation and Decimal evaluator. It compares three previously installed coding models with two newly downloaded general-purpose models. No cloud model is included, so this does not establish whether local models beat Codex or another hosted provider.

## Hardware and runtime

The machine has an Intel Core i5-2520M at 2.50 GHz, two physical cores and four logical threads, 7.6 GiB of usable RAM, and 3.8 GiB of swap. Its CPU supports AVX but not AVX2. Ollama 0.35.0 reports CPU-only execution with zero model VRAM for the measured models. These are measurements on an older laptop CPU, not GPU benchmark results.

Normal desktop applications remained running. RAM and swap use changed during the run; Ollama's reported resident model size is not a measured whole-system memory peak. There was about 32 GB of free disk space before the additional model downloads. No benchmark inference calls were issued concurrently and models were unloaded between model groups.

| Installed model tag | Quantization | Downloaded size in MB | Loaded model size in MB | Separate preload time |
| --- | --- | ---: | ---: | ---: |
| `qwen2.5-coder:0.5b` | Q4_K_M | 398 | 484 | 3.38 s |
| `qwen2.5-coder:1.5b` | Q4_K_M | 986 | 1,170 | 6.19 s |
| `codegemma:2b` | Q4_0 | 1,551 | 1,743 | 9.45 s |
| `qwen2.5:1.5b` | Q4_K_M | 986 | 1,170 | 1.84 s |
| `gemma3:1b` | Q4_K_M | 815 | 881 | 2.57 s |

All five models loaded successfully with the 4,096-token context and zero reported VRAM. Sizes above are decimal MB, not GiB. Preload times depend on file caching and are single measurements, not a reliable model-speed ranking. The two general-purpose downloads add approximately 1.80 GB on disk and remain installed; application model defaults were not changed.

CodeGemma's tag says `2b`, while this installed GGUF's metadata reports a rounded parameter size of `3B`; the report uses the exact installed tag rather than silently renaming it. Its advertised 2B variant is intended for code completion, not the separate 7B instruction model. The additional models use the general-purpose [Qwen 2.5 1.5B](https://ollama.com/library/qwen2.5:1.5b) and text-only [Gemma 3 1B](https://ollama.com/library/gemma3:1b) variants. See the [CodeGemma model documentation](https://ollama.com/library/codegemma).

## Grocery data and test cases

The read-only input is `data/grocery.db`, using the complete Kupi collection finished on 1 October 2026 at 12:54:19 UTC. It contains 1,510 accepted offers. Eligibility filters leave 19 quotes matching the configured ingredient registry, retailer allowlist, validity dates, loyalty policy, mass-price requirements, and acquisition scope `kupi:locality:praha`. The collection uses legacy coverage: this experiment does not establish a complete combined-source catalogue.

The eight configured ingredients are chicken, turkey, lentils, rice, carrot, onion, oil, and oats. Oats are supplied as synthetic pantry stock in the breakfast case; oil can use the configured pantry estimate. Model contexts contain eligible ingredient registry records and nutrition, not all 1,510 offers or actual quote prices. Prices and pantry deductions are applied afterward by the deterministic evaluator. Pantry quantities below are test inputs, not the user's personal stock.

| Case | Servings | Protein minimum per serving | Calories maximum per serving | Usage cost maximum per serving | Time limit | Pantry and exclusions |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| High protein dinner | 2 | 70 g | 850 kcal | 100 CZK | 45 min | No pantry stock |
| Vegetarian pantry | 2 | 30 g | 750 kcal | 40 CZK | 30 min | Rice 250 g, lentils 300 g, oil available; exclude chicken and turkey |
| Oat breakfast | 1 | 12 g | 600 kcal | 25 CZK | 30 min | Oats 100 g, oil available; exclude chicken, turkey, rice and lentils |

All cases permit at most two distinct shopping contexts. The vegetarian case prioritizes using owned rice and lentils. Every case has a deterministic witness that passes the numeric filters on the pinned quotes before inference starts; these witnesses establish numeric feasibility, not taste or cooking quality. Costs are consumed-ingredient estimates after pantry deductions, not supermarket checkout totals. Nutrition comes from registry estimates and edible fractions, not the model.

## Measurement method

Each model receives the same three cases in the same order, repeated with seeds 17 and 18. The harness uses the application's prompt, JSON schema, structural validation, one allowed structural repair attempt, and deterministic recipe service. Model-routing metadata is removed from the prompt so its hash is identical across models for each case. Models choose titles, ingredient IDs, total raw grams for all servings, steps, and cooking minutes. General-purpose model runs replay the exact recorded cases and evaluation date, with snapshot and case equality checked before comparison.

Controlled Ollama options are temperature 0.2, context 4,096 tokens, maximum generation 512 tokens, and two CPU inference threads. The timeout is 240 seconds per provider call. These decoding controls are benchmark settings, not unchanged application defaults; the application's configured provider timeout defaults to 120 seconds. Initial model loading is measured separately. Recipe request time includes provider calls, any structural repair, and deterministic evaluation, but excludes that separate preload and model download.

The service does not repair a structurally valid recipe merely because its quantities fail nutrition or budget filters. A valid JSON draft, a numerically accepted recipe, and useful cooking instructions are therefore separate outcomes. Schema-constrained ingredient IDs make structural compliance easier and do not demonstrate unrestricted reasoning ability.

Ollama reports prompt and generation durations in nanoseconds. Generation throughput is total returned output tokens divided by total returned generation duration, excluding prompt evaluation and model loading. Calls without a response have no usable token counters and are excluded from that throughput calculation; returned invalid drafts still count as generated output. Different tokenizers also mean tokens per second are not a direct measure of equivalent semantic work. Timeout times are censored observations: they show that a response did not arrive before the limit, not how long eventual completion would have taken. See the [Ollama generate API documentation](https://docs.ollama.com/api/generate).

Prompt-cache reuse, different output lengths, background desktop load, and fixed model order can affect timings. Six requests per model are exploratory evidence, not a stable performance guarantee. No meals were cooked or taste-tested, and this is not a nutritional or food-safety certification.

### Host suspension during the general model run

Kernel logs record a deep suspend from 3 October 00:56:04 to 10:05:42 Europe/Prague during the first general-purpose Qwen vegetarian request. The provider call started at 2 October 22:54:01 UTC and returned at 3 October 08:06:52 UTC. Its monotonic elapsed time was 194.6 seconds, while calendar elapsed time was about 9 hours 12 minutes 51 seconds. Linux's monotonic clock used by the harness excludes host suspension; this is not nine hours of continuous inference.

The timing table reports monotonic request elapsed time, not CPU time or calendar time across suspension. The interrupted sample remains visible in raw evidence and quality counts. Excluding it, general-purpose Qwen's five uninterrupted requests have a median of 135.9 seconds, compared with 136.2 seconds across all six. The UTC gap and cooling/background-load changes limit latency comparisons; no claim of a tightly controlled laboratory benchmark is made.

## Results

The coding-model run finished on 2 October 2026 between 18:47:54 and 19:24:58 UTC. The general-purpose run started at 22:49:58 UTC, already 3 October in Europe/Prague, and finished on 3 October at 08:24:57 UTC after the documented overnight suspension. Downloads completed before inference and are excluded from recipe timings. The figures below include all six requests per model, including rejected drafts, repairs, and timeouts; they are not successful-response-only medians.

| Model | Final valid drafts | Numeric passes | Provider failures | Median request | Request range | Returned generation tokens per second |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `qwen2.5-coder:0.5b` | 6/6 | 1/6 | None | 70.5 s | 36.2–128.8 s | 4.94 |
| `qwen2.5-coder:1.5b` | 4/6 | 1/6 | 2 timeouts | 151.2 s | 78.1–240.1 s | 2.47 |
| `codegemma:2b` | 4/6 | 2/6 | 2 timeouts | 111.1 s | 22.1–240.1 s | 2.19 |
| `qwen2.5:1.5b` | 5/6 | 1/6 | 1 timeout | 136.2 s | 74.9–240.1 s | 2.52 |
| `gemma3:1b` | 4/6 | 2/6 | 2 validation failures | 102.7 s | 33.1–209.2 s | 4.85 |

Final valid drafts means parseable, structurally valid output with unique eligible ingredient IDs after the permitted repair; it does not mean feasible quantities or good preparation steps. Request times are monotonic elapsed times as explained above.

There were 33 provider calls for the 30 requests: three extra calls were repairs. Totals were seven numerical passes, 16 structurally valid but infeasible recipes, five provider timeouts, and two exhausted validation repairs. Summed monotonic recipe-request time was 62.0 minutes, excluding downloads, separate model preloads, checkpoint writes, and suspension. All five models were unloaded after their groups, leaving the local service idle.

The 0.5B model needed one repair: its first breakfast draft repeated ingredient entries until it hit the 512-token generation limit and returned incomplete JSON. The repair succeeded. Other coding-model requests had no repair call. All four coding-model provider failures and general-purpose Qwen's one failure were 240-second timeouts without returned drafts or token counters. Gemma's two vegetarian requests each returned duplicate ingredient entries again after repair, causing explicit validation failures rather than timeouts.

### Results by recipe case

Each cell below gives first-seed and second-seed outcomes and seconds, in that order. “Pass” means numerical acceptance only; “reject” means a structurally valid but infeasible recipe.

| Model | High protein dinner | Vegetarian pantry | Oat breakfast |
| --- | --- | --- | --- |
| `qwen2.5-coder:0.5b` | Reject 126.6 / reject 36.2 | Reject 76.7 / reject 48.7 | Reject 64.2 / pass 128.8 |
| `qwen2.5-coder:1.5b` | Timeout 240.1 / reject 136.6 | Timeout 240.1 / reject 113.4 | Reject 165.7 / pass 78.1 |
| `codegemma:2b` | Timeout 240.1 / reject 38.0 | Timeout 240.1 / reject 23.9 | Pass 184.3 / pass 22.1 |
| `qwen2.5:1.5b` | Timeout 240.1 / reject 135.9 | Reject 194.6 with suspension / reject 74.9 | Reject 136.5 / pass 80.0 |
| `gemma3:1b` | Reject 158.5 / reject 48.4 | Validation failure 209.2 / validation failure 118.0 | Pass 87.3 / pass 33.1 |

No model passed either dinner or vegetarian case, despite the numeric feasibility witnesses. All seven accepted recipes were breakfasts dominated by owned oats. General-purpose models did not solve the harder quantity constraints in this experiment.

Of the seven numerical passes, five finished within 120 seconds overall. Six had every individual provider call within 120 seconds: the 0.5B model's 128.8-second repaired breakfast used calls of 95.5 and 33.3 seconds, so its overall time must not be mistaken for a per-call timeout. CodeGemma's 184.3-second breakfast had a single call exceeding 120 seconds. These are observations under the controlled benchmark settings, not a prediction for unchanged production decoding defaults.

## Cooking quality observations

Numerical acceptance does not imply a useful recipe. These are manual observations of returned text, not a blind rating or cooked-meal evaluation:

- The 0.5B model's accepted breakfast has 100 g oats and 100 g carrot, computed as 13.71 g protein, 374.96 kcal, and 1.19 CZK per serving. Its steps add unlisted olive oil and toast or bread. Those extras are outside the calculated ingredients and cost.
- The 1.5B coding model's accepted breakfast specifies oats, carrot, 1 g onion, and 1 g oil, computed as 13.72 g protein, 384.30 kcal, and 1.20 CZK. It says to pour pancake batter but gives no liquid or batter-making preparation. Its rejected first breakfast used 200 g oil and calculated 2,171.32 kcal.
- CodeGemma's first accepted breakfast lists carrot and oats but only says to put oats in an oven and stir them, without a temperature or carrot preparation. Its second accepted breakfast lists only oats but is titled “Carrot” and instructs the cook to cut carrots. Fast numerical passes therefore do not demonstrate instruction consistency.
- A 0.5B dinner draft allocated only 2 g each of chicken, lentils, carrot, and onion for two servings, yielding 0.49 g protein per serving against the requested 70 g. The 1.5B coding model similarly returned gram quantities near one in its second dinner.
- The 1.5B coding model called its second vegetarian recipe “Spaghetti Carbonara” despite listing rice, lentils, carrot, oil, and onion, and introduced unlisted Parmesan and lemon in its steps. Its calculated 940.37 kcal exceeded the 750 kcal limit.
- General-purpose Qwen's first vegetarian draft used 1,000 g dry rice, 200 g dry lentils, and 100 g oil for two servings, calculating 2,517 kcal per serving against the 750 kcal limit. It declared 30 minutes while its steps specified 45 minutes of simmering followed by five minutes of resting. Its accepted breakfast used 100 g oats, 1 g carrot, 1 g onion, and 10 g oil; it described pouring a mixture into a baking pan without specifying a liquid or preparation that makes it pourable.
- Gemma's first accepted breakfast calculated 13.10 g protein and 361.98 kcal from oats and 1 g oil, then added unlisted carrot in its steps. Its second accepted breakfast included carrot and onion and calculated 14.33 g protein, 406.26 kcal, and 2.39 CZK, but only said to cook the oats until soft, without liquid amounts or a microwave/stovetop procedure. Its vegetarian drafts duplicated rice and lentils; the first also introduced excluded chicken in the instructions.

These examples expose a service limitation as well as model weaknesses: structured validation checks ingredient lines, while free-text steps can introduce extra foods or fail to prepare listed ingredients. The numerical evaluator correctly rejects infeasible quantities, but it does not validate every cooking instruction.

## Measured bottleneck

Across the 18 coding-model requests, whole-request time minus measured provider-call time had a median of 0.0092 seconds and a maximum of 0.0143 seconds. Most latency is inside the model provider, not Python pricing or nutrition calculations. This overhead measure includes service validation and evaluation around the calls, but not separate model preloading or the later diagnostic reevaluation.

The 0.5B model's first calls processed 1,995 prompt tokens for dinner, 1,528 for the vegetarian case, and 1,191 for breakfast. Prompt evaluation alone took 55.75, 41.09, and 30.73 seconds respectively. An inferred optimization priority is to shorten the model-facing context by removing retailer-matching regexes and other registry fields unused in recipe writing, while preserving nutrition and gram units. That optimization was not applied or timed in this comparison. NumPy changes to the deterministic evaluator would not address the measured minutes of inference delay; keep exact Decimal calculations for money and pantry arithmetic.

## Recommendation

For further small-model experiments on this machine, Gemma 3 1B is the more promising general-purpose candidate: it used about 881 MB of loaded model memory, numerically passed two of six cases versus Qwen's one, and generated returned tokens at about 4.85 tokens per second versus 2.52. These are exploratory differences from six requests, not proof of universal superiority. It still failed both vegetarian requests after repair and produced incomplete instructions, so it should not become an unattended recipe-writing default on this evidence.

The 0.5B coder is the smallest and had the fastest median across all outcomes, but its one numerical pass and frequent quantity/instruction mistakes make it a poor quality choice. CodeGemma tied Gemma's numerical pass count, yet its accepted outputs included missing or contradictory ingredient preparation; its completion-model behavior makes those passes especially misleading. The two 1.5B Qwen variants were slower without a reliable constraint-following gain.

Before choosing larger or slower models, improve the model-facing context and validation: make total raw grams and servings explicit with a worked quantity example, supply relevant price information if budget reasoning is expected, consider feedback from numeric rejections, and check ingredients and stated cooking time against preparation steps. Procedural water can be described without treating it as an unpriced extra food. These are proposed next experiments, not changes measured here. Continue deterministic rejection and human review; do not treat schema-valid JSON as a finished recipe.

## Evidence and verification

The primary measured evidence is [coding model results](../reports/benchmarks/2026-10-02-local-recipes/results.json) and [general-purpose model results](../reports/benchmarks/2026-10-03-general-recipes/results.json). Both contain the same pinned cases, quote snapshot, and historical evaluation time. First-call prompt hashes also match across all five models for each case. Full model digests, not just mutable tags, are recorded in those files. The benchmark code is [benchmark_recipes.py](../src/grocery_agent/apps/benchmark_recipes.py), with [offline safeguards](../tests/test_recipe_benchmark.py).

All ten focused safeguards passed, covering feasible witnesses, database preservation, model-independent prompts, raw metrics, invalid output, transport failures, timeout evidence, aggregation, historical replay, and calendar-versus-monotonic time. Lint and type checks passed. The final full offline suite passed 1,039 tests with six live/browser tests deselected and one upstream Starlette/httpx deprecation warning. Live model behavior above was measured separately, not mocked. Browser, retailer refresh, mailbox, and chat acceptance are outside this benchmark.

The harness now records a provider-call finish timestamp and calendar elapsed time separately from monotonic elapsed time for future runs, with a simulated one-hour clock-gap test. Those additional fields were added after the measured run started; the interrupted sample above is identified using its existing start timestamp, Ollama response timestamp, and kernel suspend logs, not retroactively fabricated calendar fields.

## Reproduce the benchmark

From the repository root, with an idle local Ollama service and already-installed model tags, replay the saved historical inputs:

```sh
.venv/bin/python -m grocery_agent.apps.benchmark_recipes \
  --models qwen2.5-coder:0.5b qwen2.5-coder:1.5b codegemma:2b \
  --inputs-from reports/benchmarks/2026-10-02-local-recipes/results.json \
  --repeats 2 --timeout 240 \
  --output reports/benchmarks/my-coding-model-run

.venv/bin/python -m grocery_agent.apps.benchmark_recipes \
  --models qwen2.5:1.5b gemma3:1b \
  --inputs-from reports/benchmarks/my-coding-model-run/results.json \
  --repeats 2 --timeout 240 \
  --output reports/benchmarks/my-general-model-run
```

Output directories must not already exist. The harness rejects non-local API URLs, missing models, an already-loaded model, and a stale grocery snapshot when reading the database. `--inputs-from` instead replays the exact cases and evaluation date from previous evidence without reading the database or live ingredient configuration; this is historical evaluation, not a freshness claim for today's shopping. It never scrapes or migrates the grocery database. The output records pinned quotes, effective requests, numeric feasibility witnesses, model digests, prompts and their hashes, raw responses, timing counters, preload metadata, structural repair calls, and evaluator results. Offline checks are in `tests/test_recipe_benchmark.py`.

Omit `--inputs-from` to benchmark a fresh cache instead, optionally selecting `--database` and `--catalog`; the built-in cases depend on the eight ingredient IDs described above. Keep the PC awake for an uninterrupted timing comparison. Compare recorded model digests and options before treating later mutable-tag runs as identical experiments.
