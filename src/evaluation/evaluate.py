import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from tqdm.asyncio import tqdm_asyncio

from src.harness import Agent, AgentTrace
from src.harness.sdk_config import get_sdk

if TYPE_CHECKING:
    from src.cache import RunCache

T = TypeVar("T")


def _extract_model(options) -> str:
    """Best-effort model id extraction from either dict or ClaudeAgentOptions."""
    if isinstance(options, dict):
        return str(options.get("model", "") or "")
    return str(getattr(options, "model", "") or "")


@dataclass
class EvalResult(Generic[T]):
    """Result of evaluating a single question."""
    question: str
    ground_truth: str
    trace: AgentTrace[T] | None

'''
evaluate_agent_paralle 是 EvoSkill里批量、并发跑agent答案的函数；_ensure_base_program、子代_evaluate都通过
它执行。cache是可选的RunCache，用来避免在相同题目+相同agent环境下重复调LLM。
'''
async def evaluate_agent_parallel(agent: Agent[T],items: list[tuple[str, str]],max_concurrent: int = 2,*,
    cache: "RunCache | None" = None,) -> list[EvalResult[T]]:
    """
    Run agent on multiple questions in parallel.

    Args:
        agent: The agent to evaluate
        items: List of (question, ground_truth) tuples
        max_concurrent: Max concurrent agent runs (default 2)
        cache: Optional RunCache for caching results (keys on git tree hash)

    Returns:
        List of EvalResult containing question, ground_truth, and trace
    """
    semaphore = asyncio.Semaphore(max_concurrent) #最大并发数

    async def run_one(question: str, ground_truth: str) -> EvalResult[T]:
        async with semaphore:
            try:
                async with asyncio.timeout(1020):  # 17-minute hard limit per eval
                    # Check cache first
                    trace = None
                    sdk = get_sdk()
                    model = _extract_model(agent._get_options())
                    if cache is not None:
                        trace = cache.get(  #先查cache，再agent.run:命中就不花钱、不调模型。
                            question,
                            agent.response_model,
                            sdk=sdk,
                            model=model,
                        )

                    # Cache miss - run agent
                    if trace is None:
                        trace = await agent.run(question)
                        # Store in cache
                        if cache is not None:
                            cache.set(question, trace, sdk=sdk, model=model)

            except asyncio.TimeoutError:
                print(f"Eval timed out (17min) for: {question[:50]}...")
                trace = None
            except Exception as e:
                print(f"Failed on question: {question[:50]}... Error: {e}")
                trace = None
            return EvalResult(question=question, ground_truth=ground_truth, trace=trace)

    tasks = [run_one(q, gt) for q, gt in items]
    results = await tqdm_asyncio.gather(*tasks, desc="Evaluating") #并行跑，tqdm显示进度。
    return results
