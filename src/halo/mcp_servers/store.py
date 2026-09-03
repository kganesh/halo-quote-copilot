"""Loads the synthetic corpus once, indexed for lookup.

The servers stand in for systems of record, so they read the seed data rather
than compute anything clever. If the corpus is missing the error says how to make
it, because that is the first thing anyone hits on a fresh clone.
"""

from __future__ import annotations

import json
from decimal import Decimal
from functools import cache
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parents[3] / "data" / "seed"


class CorpusMissing(RuntimeError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"no seed corpus at {path} — run `make seed` first")


@cache
def _table(name: str, seed_dir: str | None = None) -> tuple[dict, ...]:
    directory = Path(seed_dir) if seed_dir else SEED_DIR
    path = directory / f"{name}.json"
    if not path.exists():
        raise CorpusMissing(path)
    return tuple(json.loads(path.read_text()))


def products(seed_dir: str | None = None) -> tuple[dict, ...]:
    return _table("products", seed_dir)


def price_tiers(seed_dir: str | None = None) -> tuple[dict, ...]:
    return _table("price_tiers", seed_dir)


def margin_policies(seed_dir: str | None = None) -> tuple[dict, ...]:
    return _table("margin_policies", seed_dir)


def suppliers(seed_dir: str | None = None) -> tuple[dict, ...]:
    return _table("suppliers", seed_dir)


def inventory(seed_dir: str | None = None) -> tuple[dict, ...]:
    return _table("inventory", seed_dir)


def capacity(seed_dir: str | None = None) -> tuple[dict, ...]:
    return _table("capacity", seed_dir)


def money(value: object) -> Decimal:
    """Seed JSON stores money as strings so it survives the round trip intact."""
    return Decimal(str(value))
