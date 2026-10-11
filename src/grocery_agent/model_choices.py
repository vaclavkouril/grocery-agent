"""Explicit recipe model choices backed by the repository's benchmark runs."""

# Prefer the model that passed all six constrained cases. Luna remains selectable
# as the cheaper experiment; being tested does not imply reliable recipe quality.
TESTED_CODEX_MODELS = ("gpt-6.1-sol", "gpt-6-luna")
BENCHMARKED_LOCAL_MODELS = (
    "LiquidAI/lfm2.5-1.2b-instruct:q4_k_m",
    "qwen3.5:0.8b",
    "smollm2:1.7b-instruct-q4_K_M",
    "llama3.2:1b-instruct-q4_K_M",
    "qwen3:1.7b",
    "qwen2.5:1.5b",
    "gemma3:1b",
    "qwen2.5-coder:0.5b",
    "qwen2.5-coder:1.5b",
    "codegemma:2b",
)
