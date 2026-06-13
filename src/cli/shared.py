"""Shared CLI helpers used by run/eval without importing the full run command."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from src.cli.config import ProjectConfig


def load_and_split(cfg: ProjectConfig):
    from src.api.data_utils import stratified_split

    data = pd.read_csv(cfg.dataset_path)

    renames: dict[str, str] = {}
    if cfg.dataset.question_column != "question":
        renames[cfg.dataset.question_column] = "question"
    if cfg.dataset.ground_truth_column != "ground_truth":
        renames[cfg.dataset.ground_truth_column] = "ground_truth"
    if renames:
        data.rename(columns=renames, inplace=True)

    if cfg.dataset.category_column and cfg.dataset.category_column in data.columns:
        if cfg.dataset.category_column != "category":
            data.rename(columns={cfg.dataset.category_column: "category"}, inplace=True)
    elif "category" not in data.columns:
        data["category"] = "default"

    return stratified_split(
        data,
        train_ratio=cfg.dataset.train_ratio,
        val_ratio=cfg.dataset.val_ratio,
    )


def infer_provider(model: str) -> str:
    """Infer the LLM provider from the configured model name."""
    normalized = model.strip()
    if normalized.startswith("openrouter/"):
        return "openrouter"
    if normalized.startswith("anthropic/"):
        return "anthropic"
    if normalized.startswith("openai/"):
        return "openai"
    if normalized.startswith("google/"):
        return "google"
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if model.startswith("gemini"):
        return "google"
    return "anthropic"


def _normalize_provider_model(provider: str, model: str) -> str:
    """Strip provider prefixes that the downstream SDKs do not expect."""
    normalized = model.strip()

    if provider == "openrouter" and normalized.startswith("openrouter/"):
        return normalized[len("openrouter/") :]
    if provider == "anthropic" and normalized.startswith("anthropic/"):
        return normalized[len("anthropic/") :]
    if provider == "openai" and normalized.startswith("openai/"):
        return normalized[len("openai/") :]
    if provider == "google" and normalized.startswith("google/"):
        return normalized[len("google/") :]

    return normalized


async def call_llm(provider: str, model: str, prompt: str) -> str:
    """Call the requested LLM provider and return the raw text response."""
    normalized_model = _normalize_provider_model(provider, model)

    if provider == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model=normalized_model,
            max_tokens=16,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    if provider == "openai":
        try:
            import openai
        except ImportError as exc:
            raise RuntimeError(
                "openai package not installed. Run: uv add openai"
            ) from exc

        client = openai.AsyncOpenAI()
        response = await client.chat.completions.create(
            model=normalized_model,
            max_tokens=16,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content

    if provider == "openrouter":
        try:
            import openai
        except ImportError as exc:
            raise RuntimeError(
                "openai package not installed. Run: uv add openai"
            ) from exc

        api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("LLM_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OpenRouter API key not configured. Set OPENROUTER_API_KEY or LLM_API_KEY."
            )

        default_headers: dict[str, str] = {}
        if referer := os.environ.get("OPENROUTER_HTTP_REFERER"):
            default_headers["HTTP-Referer"] = referer
        if title := (os.environ.get("OPENROUTER_APP_TITLE") or os.environ.get("OPENROUTER_TITLE")):
            default_headers["X-OpenRouter-Title"] = title

        client = openai.AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            default_headers=default_headers or None,
        )
        response = await client.chat.completions.create(
            model=normalized_model,
            max_tokens=16,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content

    if provider == "google":
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "google-genai package not installed. Run: uv add google-genai"
            ) from exc

        client = genai.Client()
        response = await client.aio.models.generate_content(model=normalized_model, contents=prompt)
        return response.text

    raise ValueError(f"Unknown provider: {provider}")

'''
make_scorer 在src/cli/shared.py 里，是一个工厂函数：读 load_config()得到的 ProjectConfig，根据[scorer]配置返回
一个可调用的打分函数。

'''
def make_scorer(cfg: ProjectConfig):
    from src.loop.runner import _score_multi_tolerance
    
    #四种实现模式
    #.去空格、转小写后 完全相等 -> 1.0,否则 0.0
    #.适合选择题、固定格式答案
    if cfg.scorer.type == "exact":

        def exact(question: str, predicted: str, ground_truth: str) -> float:
            return (
                1.0
                if str(predicted).strip().lower()
                == str(ground_truth).strip().lower()
                else 0.0
            )

        return exact
    # 底层score_answer 做模糊匹配:文本包含、数字容差、多数字、单位等
    # 5档容差各打 0/1,再加权平均 成一个 0~1的连续分数。
    # 更严的容差权重更高（exact档权重约1.0,10%容差约0.33）
    # 所以默认socrer往往不是纯 0/1,例如[接近正确]可能得到0.5~0.8。
    if cfg.scorer.type == "multi_tolerance":
        return _score_multi_tolerance

    #外层是 同步函数，内部用 run_until_comlete 调异步 LLM.在asyncio.run(loop.run())里嵌套调用时，
    #某些环境下可能有问题;eval 单独跑一般没问题。
    if cfg.scorer.type == "llm":
        rubric = cfg.scorer.rubric or "Award 1.0 if correct, 0.0 if wrong."
        model = cfg.scorer.model or "claude-sonnet-4-6"
        provider = cfg.scorer.provider or infer_provider(model)

        async def llm_score(question: str, predicted: str, ground_truth: str) -> float:
            prompt = (
                f"Question: {question}\n"
                f"Expected: {ground_truth}\n"
                f"Got: {predicted}\n\n"
                f"Rubric: {rubric}\n\n"
                "Reply with only a number between 0.0 and 1.0."
            )
            try:
                text = await call_llm(provider, model, prompt)
                return float(text.strip())
            except (ValueError, Exception):
                return 0.0

        def llm_scorer(question: str, predicted: str, ground_truth: str) -> float:
            return asyncio.get_event_loop().run_until_complete(
                llm_score(question, predicted, ground_truth)
            )

        return llm_scorer
    #自定义脚本
    if cfg.scorer.type == "script":
        import shlex
        import subprocess

        def script_scorer(question: str, predicted: str, ground_truth: str) -> float:
            cmd = cfg.scorer.command.format(predicted=predicted, expected=ground_truth)
            result = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
            try:
                return float(result.stdout.strip())
            except ValueError:
                return 0.0

        return script_scorer

    return _score_multi_tolerance
