# Local and budget Codex recipe model benchmark

GPT-6.1 Sol Light is the strongest measured choice for this application's constrained recipes: it passed all six requests, produced coherent cooking steps, and took a median 22.5 seconds. GPT-6 Luna was cheaper and faster at 10.9 seconds, but passed only two of six. The five newly installed local models passed two of thirty requests, both breakfasts. None of those local models is a reliable replacement for the constrained recipe service on this PC under the tested settings.

This report covers the second benchmark round on 3 October 2026, compares it with the [first round](local-model-benchmark.md), and estimates tokens and usage costs. These are small, application-specific experiments, not general model rankings. Application defaults and your installed models were left unchanged. Cloud calls used your existing ChatGPT login, not an API key.

## Test data and controls

Both providers replayed the first round's immutable public grocery snapshot and synthetic pantry cases. The original read-only Kupi collection contains 1,510 accepted offers, reduced to 19 eligible quotes for an eight-ingredient registry. Evaluation remains anchored to 2 October 2026 at 18:47:54 UTC. This avoids changing prices or expiry during the comparison; it is not a claim that these offers are currently fresh. This is one legacy Praha collection, not a complete combined-source catalogue.

| Case | Servings | Minimum protein per serving | Maximum kcal per serving | Maximum cost per serving | Maximum cooking time |
| --- | ---: | ---: | ---: | ---: | ---: |
| High protein dinner | 2 | 70 g | 850 | 100 CZK | 45 min |
| Vegetarian pantry | 2 | 30 g | 750 | 40 CZK | 30 min |
| Oat breakfast | 1 | 12 g | 600 | 25 CZK | 30 min |

The vegetarian case owns 250 g rice and 300 g lentils, with oil available, and excludes meat. Breakfast owns 100 g oats and has oil available; it excludes meat, rice, and lentils. Store limits are two distinct shopping contexts. Each case has a numeric feasibility witness. Models receive eligible ingredient IDs and registry nutrition, but not the offer prices; Decimal evaluation applies nutrition, edible fractions, prices, pantry deductions, and filters afterward. Costs are ingredient usage estimates, not checkout totals.

Each model received two repetitions of all three cases. Models choose ingredients, total raw grams for all servings, title, preparation steps, and cooking time. The production service allows one structural repair, but does not repair a valid draft merely because its nutrition or budget fails. No repairs were needed in this round. First-call application prompt hashes match across all seven models; routing metadata is normalized only in the benchmark prompt.

Ollama settings were temperature 0.2, seeds 17 and 18, context 4,096 tokens, maximum generation 512 tokens, two inference threads, and a 240-second timeout per call. Thinking was explicitly disabled. Qwen 3 and Qwen 3.5 support this control; no returned response contained a thinking trace. The results therefore do not establish performance with thinking enabled. See [Ollama thinking controls](https://docs.ollama.com/capabilities/thinking).

Codex used CLI 0.159.3, Standard speed and low reasoning effort, temporary working directories, a read-only sandbox, schema output, ephemeral sessions, ignored user configuration and rules, and disabled shell and web tools. Recorded items were final agent messages only. Codex offers no equivalent generation cap in the used CLI command: the local 512-token cap is not imposed on Codex. Codex's system context and tokenizers differ from Ollama's despite identical application prompts. Usage comes from `turn.completed` JSON events, as described in [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

## Local hardware and resource use

The PC has an Intel Core i5-2520M at 2.50 GHz, two cores and four logical threads, 7.6 GiB usable RAM, and 3.8 GiB swap. It supports AVX but not AVX2. Ollama 0.35.0 reports CPU-only inference and zero VRAM for every tested model. Resident sizes below are Ollama's model allocation estimates, not measured whole-process or whole-system peak RAM.

| Model tag | Quantization | Disk MB | Resident MB | Separate preload |
| --- | --- | ---: | ---: | ---: |
| `LiquidAI/lfm2.5-1.2b-instruct:q4_k_m` | Q4_K_M | 731 | 846 | 4.57 s |
| `qwen3.5:0.8b` | Q8_0 | 1,036 | 1,090 | 11.80 s |
| `smollm2:1.7b-instruct-q4_K_M` | Q4_K_M | 1,056 | 1,931 | 7.45 s |
| `llama3.2:1b-instruct-q4_K_M` | Q4_K_M | 808 | 1,006 | 6.09 s |
| `qwen3:1.7b` | Q4_K_M | 1,359 | 1,882 | 8.68 s |

Sizes use decimal MB. All five models fit and loaded successfully. Their combined disk size is approximately 4.99 GB. Only one model was loaded at a time. Cloud inference does not need local model weights or comparable inference RAM, but its CLI still consumes ordinary process resources; CLI peak RAM, laptop energy use, and cloud hardware were not measured.

Local inference ran from 12:22 to 13:26 UTC; recipe requests totaled 63.8 minutes, excluding preloads. Cloud recipe requests ran later, from 16:13 to 16:15 UTC, and totaled 3.16 minutes. These intervals exclude report preparation and quota checks. No provider call in this round had a significant calendar-versus-monotonic gap, unlike the documented suspension in round one. Request elapsed time is not CPU time.

## Results for the new models

| Model | Valid drafts | Numeric passes | Provider failures | Median request | Request range | Generation tokens per second |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| LiquidAI LFM 2.5 1.2B | 6/6 | 0/6 | 0 | 75.5 s | 20.0–156.6 s | 5.52 |
| Qwen 3.5 0.8B | 6/6 | 0/6 | 0 | 67.1 s | 22.5–100.3 s | 5.81 |
| SmolLM2 1.7B instruct | 2/6 | 1/6 | 4 timeouts | 240.1 s | 59.8–240.1 s | 1.77 |
| Llama 3.2 1B instruct | 6/6 | 1/6 | 0 | 102.5 s | 51.8–193.5 s | 3.11 |
| Qwen 3 1.7B | 3/6 | 0/6 | 3 timeouts | 209.6 s | 104.0–240.1 s | 1.64 |
| GPT-6 Luna Light | 6/6 | 2/6 | 0 | 10.9 s | 7.4–12.7 s | Not exposed |
| GPT-6.1 Sol Light | 6/6 | 6/6 | 0 | 22.5 s | 14.2–25.3 s | Not exposed |

Medians include failures and rejected recipes. Generation throughput uses returned Ollama token counters divided by returned generation duration; timeouts lack counters and are excluded from throughput. Codex exposes end-to-end time, not a comparable isolated generation duration. Do not interpret the legacy Ollama-only zero token fields in the original cloud `summary` as no cloud usage: authoritative cloud counters are in `token_usage` and each call's `usage_events`. The harness now also populates the shared output-token summary for future runs.

Prompt evaluation and cache effects are substantial. LFM's dinner took 156.6 seconds on the first repetition and 50.7 seconds on the second; Qwen 3.5's dinner took 100.3 and 61.9 seconds. The fixed case order, prompt-cache reuse, varying output length, and desktop background load limit speed comparisons. Timeouts are censored observations, not measured eventual completion times. Six requests per model cannot establish stable success probabilities.

### Comparison with the earlier local models

These are historical results on the same pinned cases, not fresh reruns of the older five models. The first round used the same decoding limits but did not explicitly set thinking controls; its general-purpose Qwen run also included the documented host suspension.

| Earlier model | Numeric passes | Median request | Provider failures |
| --- | ---: | ---: | ---: |
| `qwen2.5-coder:0.5b` | 1/6 | 70.5 s | 0 |
| `qwen2.5-coder:1.5b` | 1/6 | 151.2 s | 2 timeouts |
| `codegemma:2b` | 2/6 | 111.1 s | 2 timeouts |
| `qwen2.5:1.5b` | 1/6 | 136.2 s | 1 timeout |
| `gemma3:1b` | 2/6 | 102.7 s | 2 validation failures |

Every earlier numeric pass was a breakfast; manual review found incomplete or inconsistent cooking instructions even among those passes. No new local model improved on that pattern. Across both local rounds, nine of sixty requests passed numeric filters. The seven cloud/local models freshly measured in round two passed ten of forty-two requests.

## Recipe quality and numeric failures

These observations are manual desk review of the recorded drafts, not taste tests or a food-safety certification. Numeric validity does not inspect ingredient mentions inside prose, verify cooking time, or establish that the meal is pleasant.

| Model | Main observed weaknesses |
| --- | --- |
| LFM 2.5 | Dinner protein too low; uses all pantry rice and lentils and exceeds calories; oats mixed with 30 g oil are called porridge without water or heating. |
| Qwen 3.5 | Dinner contains just 1.5 g chicken; steps add ingredients absent from the quantity list; excessive oil; one vegetarian draft has incoherent text and another describes 45 minutes of simmering despite a 30-minute field. |
| SmolLM2 | Four timeouts. Its passing oat and carrot salad instructs cooking oats according to the package, then mixing in grated carrot: basic but coherent. This single pass does not offset poor responsiveness. |
| Llama 3.2 | Pantry quantities exceed calories; treats onion and carrot as 1 g rather than sensible portions; steps omit listed rice or add unlisted eggs. Its passing breakfast is toasted oats with roasted vegetables, less complete than a well-specified recipe. |
| Qwen 3 | Three timeouts; returned dinners/pantry recipes use excessive quantities and oil. One dinner asks to stir-fry dry rice with no hydration; breakfast cooks oats and 100 g oil into a paste. |
| Luna | Much more coherent prose than the local drafts, but both dinners miss protein, one vegetarian draft exceeds calories, and one breakfast misses protein. Its passing pancake specifies only a little water; texture and binding remain untested. |
| Sol Light | All six drafts satisfy numeric constraints. Steps describe raw/dry weights, hydration, dividing servings, and consistent ingredients. Both chicken drafts specify a center temperature. Meals remain simple and repetitive because the registry is narrow. |

The two main dishes per model reveal the difference better than JSON compliance. Sol produced chicken dinners with 84.45 and 76.63 g protein per serving; Luna produced 47.50 and 61.70 g against the 70 g requirement. Luna's failed vegetarian case has 982.07 kcal against a 750 kcal ceiling. LFM vegetarian drafts reach 930.7 and 1,357.25 kcal; Qwen 3's returned dinner reaches 2,209.32 kcal per serving. The deterministic filter correctly prevents these drafts from being offered as feasible recipes.

Sol's accepted costs range from 0 to 39.70 CZK per serving after pantry deductions. Its two vegetarian recipes use 260/120 g and 300/100 g lentils/rice in total, remaining within pantry stock. Its breakfasts use 100 g oats rather than exceeding the 100 g stock. The full per-line quotes and calculations are retained in the raw results.

## Why these Codex options were selected

Your installed CLI's account model catalogue listed both `gpt-6-luna` and `gpt-6.1-sol`, and all twelve live calls succeeded. Luna is the documented efficient model for focused high-volume work. Sol is a lower-cost alternative to Astra; low reasoning effort is the Light configuration here. The newer 6.1 Sol has a lower cached-input credit rate than 6 Sol, while their standard input/output credit rates match. These are budget choices available to this account, not a claim that all OpenAI API models are accessible through Codex. See [current Codex models](https://learn.chatgpt.com/docs/models).

## Measured Codex token consumption

| Metric | Luna | Sol Light |
| --- | ---: | ---: |
| Requests and provider calls | 6 and 6 | 6 and 6 |
| Total input tokens | 72,538 | 76,686 |
| Cached input tokens | 0 | 0 |
| Total output tokens | 1,118 | 1,760 |
| Reasoning output tokens within output | 0 | 484 |
| Mean input tokens per attempted recipe | 12,090 | 12,781 |
| Mean output tokens per attempted recipe | 186 | 293 |
| Observed input range per call | 11,592–13,290 | 12,282–13,274 |
| Numeric passes | 2 | 6 |

Output totals already include reasoning output; do not add reasoning a second time. No cache hits or cache writes were reported. These counters include Codex's agent context, not just the roughly 1,100–2,100-token application prompt measured with local tokenizers. That overhead makes this CLI route more token-intensive than a hypothetical minimal API recipe prompt. We did not measure that API alternative, and tokenizers differ.

Across the twelve benchmark calls, total measured cloud usage was 149,224 input and 2,878 output tokens, with 484 reasoning tokens included in output. Report-writing and this interactive coding session consume additional account usage and are not included in those totals.

### Weekly token estimates

The following estimates multiply observed mean usage by the number of attempted recipe requests, assuming the same small ingredient context, no tools, low reasoning, no cache hits, and no additional repair. They are not estimates for large weekly meal plans or a guarantee of future consumption.

| Requests per week | Luna input | Luna output | Sol input | Sol output including reasoning |
| --- | ---: | ---: | ---: | ---: |
| 7 | 84,628 | 1,304 | 89,467 | 2,053 |
| 21 | 253,883 | 3,913 | 268,401 | 6,160 |
| 100 | 1,208,967 | 18,633 | 1,278,100 | 29,333 |

Failures still consume tokens. Based only on this tiny sample, Luna required three attempted requests per numeric pass, so seven accepted recipes would correspond to about 21 attempts, not seven. This is a rough sample-based planning estimate, not a proven retry success rate. Sol had one attempt per pass here. A structural repair can roughly double a request's input demand; higher reasoning or tool calls may increase it further. Larger catalogues need separate measurements.

## Weekly allowance and cost estimates

The documented read-only account quota endpoint returned a 10,080-minute window, meaning seven days. At 16:25:44 UTC on 3 October, it reported 8% used, approximately 92% remaining, and a reset at 00:25:27 Europe/Prague on 10 October. The internal plan label was `prolite`; no billing identity was fetched. This is a point-in-time account reading, not a public subscription-plan inference. See the [app-server quota protocol](https://learn.chatgpt.com/docs/app-server).

The pre-run and immediate post-run quota reads failed because the initial command used an unsupported app-server flag. That reader was corrected and verified afterward without further model calls. Therefore there is no valid before/after benchmark quota delta. The 8% includes other account activity; it is not the consumption of these twelve calls.

OpenAI does not publish a fixed tokens-per-week conversion for included subscription usage. Model, reasoning, tools, context, and caching affect the allowance; credit prices alone do not determine subscription consumption. An exact percentage per recipe, or number of recipes left this week, cannot be calculated from the reported 92% and our token counters. Check the usage dashboard when limits matter. The estimates below are explicitly for paid-credit or API-equivalent costs, not percentages of included weekly allowance. [Subscription and credit pricing](https://learn.chatgpt.com/docs/pricing).

Standard paid-credit rates per million tokens are Luna 2.5 input, 0.25 cached input, and 12.5 output credits; Sol 50 input, 2.5 cached input, and 250 output credits. Applying these to the measured uncached tokens gives approximately 0.03255 credits per Luna attempt and 0.71238 per Sol attempt. Sol costs about 21.9 times more per attempt on this credit-rate calculation, but delivered six passes instead of two. These are calculated equivalents; no paid-credit debit was measured.

| Attempted recipes | Luna credit equivalent | Sol credit equivalent | Luna API equivalent USD | Sol API equivalent USD |
| --- | ---: | ---: | ---: | ---: |
| One average request | 0.0326 | 0.7124 | $0.00130 | $0.02850 |
| Six benchmark requests | 0.1953 | 4.2743 | $0.00781 | $0.17097 |
| 7 per week | 0.2279 | 4.9867 | $0.00911 | $0.19947 |
| 21 per week | 0.6836 | 14.9601 | $0.02734 | $0.59840 |
| 100 per week | 3.2553 | 71.2383 | $0.13021 | $2.84953 |

API equivalents use published Standard rates of Luna $0.10 input and $0.50 output per million tokens, and Sol $2 input and $10 output. These estimates apply to the measured Codex token counts; they are not an invoice and not a measured API benchmark. There were no reported cache writes, regional premiums, tools, or Fast-mode charges in this calculation. [Luna API pricing](https://developers.openai.com/api/docs/models/gpt-6-luna), [Sol API pricing](https://developers.openai.com/api/docs/models/gpt-6.1-sol).

For a separately chosen paid-credit budget of B credits, these measurements suggest about B / 0.03255 Luna attempts or B / 0.71238 Sol attempts. For example, a hypothetical ten-credit allocation covers about 307 Luna or 14 Sol attempts. That hypothetical allocation is not your subscription's weekly allowance. Future cache hits could lower cost, but this run provides no measured warm-cache savings.

Local Ollama requests consume no Codex tokens or weekly allowance. Their costs here are local CPU time, RAM, disk, and unmeasured electricity. Running the benchmark and writing this report with Codex is separate cloud activity even when the recipes themselves are generated locally.

## Recommended choice for this PC

Use Sol Light when constraint compliance and useful instructions matter more than minimizing cloud usage. It was approximately three times faster than the fastest new local model's overall median and passed every case. Keep deterministic validation: six passes do not prove universal reliability.

Use Luna only for lower-stakes drafts with validation and an explicit retry or fallback budget. It was about twice as fast as Sol and much cheaper on the paid-credit rate card, but missed numeric requirements in four requests. A Luna-first/Sol-fallback workflow could be useful, but was not benchmarked and is not enabled by this change.

For fully offline use, keep fixed recipes or generate prose for a deterministically selected feasible basket instead of trusting these small models to choose grams unaided. Qwen 3.5 was responsive and structurally compliant, but its food quantities and prose failed this task. SmolLM2's one coherent breakfast does not justify its four-minute timeout rate. Thinking-enabled local runs or larger models remain possible follow-up experiments, not hidden conclusions from these results.

## Evidence and reproduction

Raw results retain exact model tags/digests, prompts/hashes, per-call timings, returned recipes, usage, errors, and deterministic diagnostic reports:

- [New local model evidence](../reports/benchmarks/2026-10-03-new-local-recipes/results.json)
- [Budget Codex evidence](../reports/benchmarks/2026-10-03-budget-codex/results.json)
- [Read-only quota follow-up and CLI version](../reports/benchmarks/2026-10-03-quota-followup/results.json)
- [Original pinned cases](../reports/benchmarks/2026-10-02-local-recipes/results.json)

Runtime code is under `src/grocery_agent/apps/`. Use new output directories; the harness refuses to overwrite existing evidence. Models must already be installed and Ollama must be idle. Re-running the cloud command consumes subscription usage.

```sh
.venv/bin/python -m grocery_agent.apps.benchmark_recipes \
  --models LiquidAI/lfm2.5-1.2b-instruct:q4_k_m qwen3.5:0.8b \
    smollm2:1.7b-instruct-q4_K_M llama3.2:1b-instruct-q4_K_M qwen3:1.7b \
  --inputs-from reports/benchmarks/2026-10-02-local-recipes/results.json \
  --output reports/benchmarks/new-local-replay --no-think --repeats 2 --timeout 240

.venv/bin/python -m grocery_agent.apps.benchmark_codex \
  --models gpt-6-luna gpt-6.1-sol \
  --inputs-from reports/benchmarks/2026-10-02-local-recipes/results.json \
  --output reports/benchmarks/new-codex-replay --repeats 2

.venv/bin/python -m grocery_agent.apps.benchmark_codex \
  --quota-only --output reports/benchmarks/new-quota-reading
```

The quota-only command generates no recipes. It saves only quota windows and the CLI version, without credentials, account identity, or credit balances. Tests cover prompt parity, explicit thinking settings, process isolation, token accounting including repairs and missing usage, malformed responses, timeouts, and quota response handling. No production recipe prompts or provider defaults were changed to improve the benchmark score.

Final verification passed: 1,054 offline tests, with two live tests deselected; Ruff lint and formatting; and mypy across 128 source files. The existing Starlette/httpx deprecation warning remains. An additional evidence audit confirmed identical pinned snapshots and cases, all 42 recorded requests, token-summary consistency, the repair limit, and tool-free Codex execution.
