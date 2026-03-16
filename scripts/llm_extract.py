#!/usr/bin/env python3
"""LLM extraction client. Wraps the Anthropic API with retry, cost tracking, and JSON parsing."""

import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def load_config():
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


class LLMClient:
    """Wrapper around the Anthropic Claude API with retry, cost tracking, and JSON output."""

    # Approximate pricing per 1M tokens (as of early 2025)
    PRICING = {
        "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
        "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    }

    def __init__(self, config: dict = None):
        import anthropic

        self.config = config or load_config()
        llm_config = self.config.get("llm", {})
        # v4: per-agent model config (with fallback to legacy keys)
        self.agent_models = {
            "agent1": llm_config.get("agent1_model", llm_config.get("text_model", "claude-sonnet-4-20250514")),
            "agent2": llm_config.get("agent2_model", llm_config.get("text_model", "claude-sonnet-4-20250514")),
            "agent3": llm_config.get("agent3_model", llm_config.get("vision_model", "claude-opus-4-20250514")),
            "agent4": llm_config.get("agent4_model", llm_config.get("text_model", "claude-sonnet-4-20250514")),
        }
        self.text_model = self.agent_models["agent1"]
        self.vision_model = self.agent_models["agent3"]
        self.max_tokens = llm_config.get("max_tokens", 8192)
        self.temperature = llm_config.get("temperature", 0.0)
        self.max_retries = llm_config.get("max_retries", 3)
        self.retry_delay = llm_config.get("retry_delay_seconds", 5)

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in .env file")

        self.client = anthropic.Anthropic(api_key=api_key)

        # Cost tracking
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.call_count = 0
        self.costs_by_model = {}

    def get_model(self, agent: str) -> str:
        """Get the model name for a specific agent (agent1, agent2, agent3, agent4)."""
        return self.agent_models.get(agent, self.text_model)

    def extract_json(self, prompt: str, model: str = None, system: str = None) -> dict:
        """Send a prompt and parse the response as JSON.

        Args:
            prompt: The user prompt (should request JSON output)
            model: Model to use (defaults to text_model)
            system: Optional system prompt

        Returns:
            Parsed JSON dict from the response
        """
        model = model or self.text_model
        response_text = self._call_api(prompt, model=model, system=system)
        return self._parse_json(response_text)

    def extract_json_with_image(self, prompt: str, image_path: str,
                                 model: str = None, system: str = None) -> dict:
        """Send a prompt with an image and parse the response as JSON.

        Args:
            prompt: The user prompt
            image_path: Path to the image file
            model: Model to use (defaults to vision_model)
            system: Optional system prompt

        Returns:
            Parsed JSON dict from the response
        """
        model = model or self.vision_model
        image_data = self._encode_image(image_path)
        media_type = self._get_media_type(image_path)

        messages = [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data,
                    },
                },
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }]

        response_text = self._call_api_messages(messages, model=model, system=system)
        return self._parse_json(response_text)

    def _call_api(self, prompt: str, model: str, system: str = None) -> str:
        """Call the Anthropic API with retry logic."""
        messages = [{"role": "user", "content": prompt}]
        return self._call_api_messages(messages, model=model, system=system)

    MAX_CONTINUATIONS = 3  # Cap continuation attempts to avoid infinite loops

    def _call_api_messages(self, messages: list, model: str, system: str = None) -> str:
        """Call the API with structured messages and retry logic."""
        for attempt in range(self.max_retries):
            try:
                kwargs = {
                    "model": model,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "messages": messages,
                }
                if system:
                    kwargs["system"] = system

                with self.client.messages.stream(**kwargs) as stream:
                    response = stream.get_final_message()
                self._track_usage(model, response.usage)
                result_text = response.content[0].text

                # Handle truncation: continue generating if max_tokens hit
                continuations = 0
                while response.stop_reason == "max_tokens" and continuations < self.MAX_CONTINUATIONS:
                    continuations += 1
                    print(f"  ⚠ Response truncated at max_tokens, requesting continuation ({continuations}/{self.MAX_CONTINUATIONS})...")
                    # Strip any trailing code fence from the truncated response
                    result_text = re.sub(r'\n?```\s*$', '', result_text)
                    continuation_messages = messages + [
                        {"role": "assistant", "content": result_text},
                        {"role": "user", "content": "Continue exactly from where you left off. Do not repeat any text."},
                    ]
                    cont_kwargs = {
                        "model": model,
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                        "messages": continuation_messages,
                    }
                    if system:
                        cont_kwargs["system"] = system

                    with self.client.messages.stream(**cont_kwargs) as stream:
                        response = stream.get_final_message()
                    self._track_usage(model, response.usage)
                    # Strip code fences the model may wrap the continuation in
                    continuation_text = response.content[0].text
                    continuation_text = re.sub(r'^```(?:json)?\s*\n?', '', continuation_text)
                    continuation_text = re.sub(r'\n?```\s*$', '', continuation_text)
                    result_text += continuation_text

                if response.stop_reason == "max_tokens":
                    print(f"  ⚠ Response still truncated after {self.MAX_CONTINUATIONS} continuations — output may be incomplete")

                return result_text

            except Exception as e:
                error_str = str(e)
                if "rate_limit" in error_str.lower() or "overloaded" in error_str.lower():
                    wait = self.retry_delay * (2 ** attempt)
                    print(f"  ⚠ Rate limited, waiting {wait}s (attempt {attempt + 1}/{self.max_retries})")
                    time.sleep(wait)
                elif attempt < self.max_retries - 1:
                    print(f"  ⚠ API error: {e}. Retrying ({attempt + 1}/{self.max_retries})...")
                    time.sleep(self.retry_delay)
                else:
                    raise

        raise RuntimeError(f"Failed after {self.max_retries} retries")

    def _track_usage(self, model: str, usage):
        """Track token usage and estimated cost."""
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens

        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.call_count += 1

        if model not in self.costs_by_model:
            self.costs_by_model[model] = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        self.costs_by_model[model]["input_tokens"] += input_tokens
        self.costs_by_model[model]["output_tokens"] += output_tokens
        self.costs_by_model[model]["calls"] += 1

    def _parse_json(self, text: str) -> dict:
        """Parse JSON from LLM response, handling markdown code blocks."""
        # Strip ALL code fence markers unconditionally
        # (handles both single-block and stitched multi-block continuations)
        text = re.sub(r'```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```', '', text)

        # Try direct parse
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        # Try finding JSON object in text
        brace_start = text.find('{')
        brace_end = text.rfind('}')
        if brace_start != -1 and brace_end != -1:
            try:
                return json.loads(text[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass

        # Try finding JSON array
        bracket_start = text.find('[')
        bracket_end = text.rfind(']')
        if bracket_start != -1 and bracket_end != -1:
            try:
                return json.loads(text[bracket_start:bracket_end + 1])
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not parse JSON from LLM response:\n{text[:500]}...")

    def _encode_image(self, image_path: str) -> str:
        """Read and base64-encode an image file."""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def _get_media_type(self, image_path: str) -> str:
        """Get MIME type from file extension."""
        ext = Path(image_path).suffix.lower()
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(ext, "image/png")

    def _get_pricing(self, model: str) -> dict:
        """Look up pricing for a model, falling back to prefix match."""
        if model in self.PRICING:
            return self.PRICING[model]
        for key in self.PRICING:
            if model.startswith(key) or key.startswith(model):
                return self.PRICING[key]
        return {"input": 3.0, "output": 15.0}

    def get_cost_summary(self) -> dict:
        """Get a summary of API usage and estimated costs."""
        total_cost = 0.0
        model_costs = {}

        for model, usage in self.costs_by_model.items():
            pricing = self._get_pricing(model)
            input_cost = (usage["input_tokens"] / 1_000_000) * pricing["input"]
            output_cost = (usage["output_tokens"] / 1_000_000) * pricing["output"]
            model_cost = input_cost + output_cost
            total_cost += model_cost
            model_costs[model] = {
                "calls": usage["calls"],
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "estimated_cost_usd": round(model_cost, 4),
            }

        return {
            "total_calls": self.call_count,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_estimated_cost_usd": round(total_cost, 4),
            "by_model": model_costs,
        }

    def print_cost_summary(self):
        """Print a formatted cost summary."""
        summary = self.get_cost_summary()
        print(f"\n💰 API Cost Summary:")
        print(f"  Total calls: {summary['total_calls']}")
        print(f"  Total tokens: {summary['total_input_tokens']:,} in / {summary['total_output_tokens']:,} out")
        print(f"  Estimated cost: ${summary['total_estimated_cost_usd']:.4f}")
        for model, data in summary["by_model"].items():
            print(f"  {model}: {data['calls']} calls, ${data['estimated_cost_usd']:.4f}")


def load_prompt(prompt_name: str) -> str:
    """Load a prompt template from the prompts/ directory."""
    prompt_path = ROOT_DIR / "prompts" / f"{prompt_name}.txt"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt not found: {prompt_path}")
    return prompt_path.read_text()
