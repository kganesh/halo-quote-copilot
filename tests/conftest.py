import json
from pathlib import Path

import pytest

from halo.seed.generate import generate


def pytest_sessionstart(session):
    """Stop before the suite runs if the corpus is not there.

    The tool servers read the generated seed, and without it 55 tests fail with
    `CorpusMissing` while four more surface as `coroutine raised StopIteration`
    — the control model calling `next()` over an audit that is empty because
    every tool returned an error. That is one missing directory wearing fifty-nine
    different masks, and CI wore them for four milestones.

    One message, naming the command, before any of it.
    """
    from halo.mcp_servers.store import SEED_DIR

    if not (SEED_DIR / "products.json").exists():
        pytest.exit(
            f"no seed corpus at {SEED_DIR} — run `make seed` first.\n"
            "The corpus is generated from a fixed seed and deliberately not "
            "committed; the generator is the source of truth.",
            returncode=1,
        )


@pytest.fixture(scope="session")
def corpus(tmp_path_factory) -> Path:
    """One generated corpus for the whole session, in a temp directory."""
    out = tmp_path_factory.mktemp("seed")
    generate(out)
    return out


def load(corpus: Path, name: str) -> list[dict]:
    return json.loads((corpus / f"{name}.json").read_text())
