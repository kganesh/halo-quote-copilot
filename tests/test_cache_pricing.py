"""Cache tokens are billed at their own rates and counted toward the budget.

Every expected figure here was read from the account's Bedrock rate card, not
derived from the first-party price list:

    USE1_InputTokenCount              3.30
    USE1_CacheReadInputTokenCount     0.33   a tenth
    USE1_CacheWriteInputTokenCount    4.125  a quarter more
    USE1_CacheWrite1hInputTokenCount  6.60   twice
"""

from decimal import Decimal

from halo.platform.bedrock import TokenCounts, counts_from, estimate_usd, estimate_usd_for
from halo.platform.budget import Budget, BudgetExceeded, BudgetTracker, Usage

MILLION = 1_000_000
REGIONAL = "us.anthropic.claude-sonnet-4-6"
GLOBAL = "global.anthropic.claude-sonnet-4-6"


class TestRatesMatchTheRateCard:
    def test_a_cache_read_costs_a_tenth_of_a_fresh_input_token(self):
        assert estimate_usd(REGIONAL, MILLION, 0) == Decimal("3.300000")
        assert estimate_usd(REGIONAL, 0, 0, cache_read_tokens=MILLION) == Decimal("0.330000")

    def test_a_five_minute_cache_write_costs_a_quarter_more(self):
        assert estimate_usd(REGIONAL, 0, 0, cache_write_5m_tokens=MILLION) == Decimal("4.125000")

    def test_a_one_hour_cache_write_costs_twice_as_much(self):
        assert estimate_usd(REGIONAL, 0, 0, cache_write_1h_tokens=MILLION) == Decimal("6.600000")

    def test_the_global_discount_applies_to_cache_rates_too(self):
        """The multipliers are the same on both profiles; the base rate differs.
        Global input is 3.00, so a global cache read is 0.30, not 0.33."""
        assert estimate_usd(GLOBAL, MILLION, 0) == Decimal("3.000000")
        assert estimate_usd(GLOBAL, 0, 0, cache_read_tokens=MILLION) == Decimal("0.300000")

    def test_the_categories_add_up(self):
        combined = estimate_usd(
            REGIONAL, 1000, 500, cache_read_tokens=8000, cache_write_5m_tokens=2000
        )
        separate = (
            estimate_usd(REGIONAL, 1000, 0)
            + estimate_usd(REGIONAL, 0, 500)
            + estimate_usd(REGIONAL, 0, 0, cache_read_tokens=8000)
            + estimate_usd(REGIONAL, 0, 0, cache_write_5m_tokens=2000)
        )
        assert combined == separate

    def test_an_unpriced_model_still_costs_nothing_rather_than_raising(self):
        assert estimate_usd("anthropic.claude-sonnet-5", 1000, 1000, cache_read_tokens=1000) == (
            Decimal("0.00")
        )


class _Usage:
    """Stands in for the SDK usage object."""

    def __init__(self, **fields):
        self.input_tokens = fields.get("input_tokens", 0)
        self.output_tokens = fields.get("output_tokens", 0)
        self.cache_read_input_tokens = fields.get("cache_read_input_tokens", 0)
        self.cache_creation_input_tokens = fields.get("cache_creation_input_tokens", 0)
        self.cache_creation = fields.get("cache_creation")


class _Breakdown:
    def __init__(self, five_minute=0, one_hour=0):
        self.ephemeral_5m_input_tokens = five_minute
        self.ephemeral_1h_input_tokens = one_hour


class TestReadingTheSdkUsage:
    def test_the_ttl_breakdown_is_used_when_present(self):
        """The two caches are priced differently, so the split matters."""
        counts = counts_from(
            _Usage(
                input_tokens=100,
                output_tokens=50,
                cache_read_input_tokens=900,
                cache_creation=_Breakdown(five_minute=200, one_hour=300),
            )
        )
        assert counts.cache_write_5m_tokens == 200
        assert counts.cache_write_1h_tokens == 300
        assert counts.cache_write_tokens == 500

    def test_without_a_breakdown_the_total_is_billed_at_the_cheaper_rate(self):
        """A missing breakdown should under-report, not stop a run for spend it
        did not incur. Five minutes is both the default TTL and the cheaper one."""
        counts = counts_from(_Usage(cache_creation_input_tokens=400))
        assert counts.cache_write_5m_tokens == 400
        assert counts.cache_write_1h_tokens == 0

    def test_an_uncached_call_reads_as_zeros_not_as_missing(self):
        counts = counts_from(_Usage(input_tokens=1200, output_tokens=300))
        assert counts.cache_read_tokens == 0
        assert counts.cache_write_tokens == 0
        assert estimate_usd_for(REGIONAL, counts) == estimate_usd(REGIONAL, 1200, 300)


class TestCacheTokensCountAgainstTheBudget:
    def _tracker(self, max_tokens: int) -> BudgetTracker:
        return BudgetTracker(
            Budget(
                wall_clock_seconds=60,
                max_tokens=max_tokens,
                max_tool_calls=0,
                max_usd=Decimal("10"),
            ),
            now=lambda: 0.0,
        )

    def test_total_tokens_includes_cache(self):
        """The API reports cache tokens separately from input_tokens, not inside
        it. Leaving them out lets a cached run pass a limit it has exceeded."""
        usage = Usage(
            input_tokens=100, output_tokens=50, cache_read_tokens=8000, cache_write_tokens=2000
        )
        assert usage.total_tokens == 10_150

    def test_a_cached_run_can_exhaust_the_token_budget(self):
        tracker = self._tracker(max_tokens=5_000)
        tracker.record_model_call(
            100, 50, Decimal("0.01"), cache_read_tokens=9_000, cache_write_tokens=0
        )
        try:
            tracker.check()
        except BudgetExceeded as exc:
            assert exc.dimension == "max_tokens"
        else:
            raise AssertionError("cache tokens were not counted toward max_tokens")

    def test_the_cache_hit_rate_is_reported(self):
        usage = Usage(input_tokens=1_000, cache_read_tokens=9_000)
        assert usage.cache_hit_rate == 0.9

    def test_the_hit_rate_is_zero_when_nothing_has_been_billed(self):
        assert Usage().cache_hit_rate == 0.0


def test_token_counts_sum_every_category():
    counts = TokenCounts(
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_5m_tokens=40,
        cache_write_1h_tokens=50,
    )
    assert counts.total_tokens == 150
