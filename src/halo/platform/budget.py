"""Design rule 05: budgets are enforced by the harness, not requested in a prompt.

Asking a model to be brief is not a ceiling. This is — the loop checks before
every step and stops with a reason, so an over-budget run produces a clean
escalation rather than a truncated answer.
"""

import time
from decimal import Decimal

from pydantic import BaseModel, Field


class Budget(BaseModel):
    """Ceilings for one agent run. All four are hard stops."""

    wall_clock_seconds: float = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_tool_calls: int = Field(ge=0)
    max_usd: Decimal = Field(gt=0)


class Usage(BaseModel):
    """What a run has spent so far."""

    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    usd: Decimal = Decimal("0.00")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class BudgetExceeded(Exception):
    """Which ceiling was hit, so the escalation reason can say so."""

    def __init__(self, dimension: str, limit: object, spent: object) -> None:
        super().__init__(f"budget exceeded on {dimension}: spent {spent}, limit {limit}")
        self.dimension = dimension


class BudgetTracker:
    """Wraps a budget with a clock. One tracker per agent run."""

    def __init__(self, budget: Budget, now: callable = time.monotonic) -> None:
        self._budget = budget
        self._now = now
        self._started = now()
        self.usage = Usage()

    @property
    def elapsed_seconds(self) -> float:
        return self._now() - self._started

    def check(self) -> None:
        """Raise if any ceiling is already crossed. Call before each step."""
        if self.elapsed_seconds > self._budget.wall_clock_seconds:
            raise BudgetExceeded(
                "wall_clock_seconds",
                self._budget.wall_clock_seconds,
                round(self.elapsed_seconds, 2),
            )
        if self.usage.total_tokens > self._budget.max_tokens:
            raise BudgetExceeded("max_tokens", self._budget.max_tokens, self.usage.total_tokens)
        if self.usage.tool_calls > self._budget.max_tool_calls:
            raise BudgetExceeded(
                "max_tool_calls", self._budget.max_tool_calls, self.usage.tool_calls
            )
        if self.usage.usd > self._budget.max_usd:
            raise BudgetExceeded("max_usd", self._budget.max_usd, self.usage.usd)

    def record_model_call(self, input_tokens: int, output_tokens: int, usd: Decimal) -> None:
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        self.usage.usd += usd

    def record_tool_call(self) -> None:
        self.usage.tool_calls += 1
