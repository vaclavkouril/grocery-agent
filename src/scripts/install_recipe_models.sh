#!/usr/bin/env bash
set -euo pipefail

# Requires Ollama to be installed and its local service running.
# Only downloads models; does not run benchmarks or change application settings.
ollama pull LiquidAI/lfm2.5-1.2b-instruct:q4_k_m
ollama pull qwen3.5:0.8b
ollama pull smollm2:1.7b-instruct-q4_K_M
ollama pull llama3.2:1b-instruct-q4_K_M
ollama pull qwen3:1.7b
