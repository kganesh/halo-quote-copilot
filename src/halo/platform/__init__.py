"""Platform contracts shared by every agent.

These are deliberately pure data with no model calls, so the rules that matter
most — identity, budgets, and the shape of an answer — are unit-testable on their
own.
"""
