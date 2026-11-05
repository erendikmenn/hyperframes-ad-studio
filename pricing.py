"""Amazon Bedrock on-demand prices for Claude, USD per 1M tokens (checked 2026-11-03).

`global.` inference profiles bill at Anthropic list price; `us.`/`eu.` (geo / in-region)
profiles bill +10%. Thinking tokens are billed as output tokens.
Cache write = 1.25x input (5 min TTL), cache read = 0.1x input.
"""

import os

# key -> (bedrock model id, input $/1M, output $/1M)
MODELS = {
    "sonnet-4.6": ("global.anthropic.claude-sonnet-4-6", 3.00, 15.00),
    "haiku-4.5": ("global.anthropic.claude-haiku-4-5-20251001-v1:0", 1.00, 5.00),
    # Not enabled on this account yet (403) - listed for the cost comparison.
    "sonnet-5.5": ("global.anthropic.claude-sonnet-5-5", 2.00, 10.00),
    "opus-5.5": ("global.anthropic.claude-opus-5-5", 4.00, 20.00),
    "haiku-5.5": ("global.anthropic.claude-haiku-5-5", 0.10, 0.50),
    # Runs through the signed-in `claude` CLI (the user's subscription), so no per-token bill.
    # A local test route only: a multi-user service belongs on Bedrock.
    "sub-haiku-5.5": ("claude-haiku-5-5", 0.0, 0.0),
    "sub-sonnet-5.5": ("claude-sonnet-5-5", 0.0, 0.0),
}

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "sonnet-4.6")

# Render compute on ECS Fargate (us-east-1, Linux/x86): 4 vCPU + 8 GB task.
FARGATE_VCPU_HOUR = 0.04048
FARGATE_GB_HOUR = 0.004445
RENDER_VCPU, RENDER_GB = 4, 8


def llm_cost(model_key: str, usage: dict) -> float:
    _, p_in, p_out = MODELS[model_key]
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
        + usage.get("cache_read_input_tokens", 0) * p_in * 0.10
        + usage.get("output_tokens", 0) * p_out
    ) / 1_000_000


def render_cost(seconds: float) -> float:
    per_hour = RENDER_VCPU * FARGATE_VCPU_HOUR + RENDER_GB * FARGATE_GB_HOUR
    return per_hour * seconds / 3600
