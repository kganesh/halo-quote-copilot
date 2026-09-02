import json
from pathlib import Path

import pytest

from halo.seed.generate import generate


@pytest.fixture(scope="session")
def corpus(tmp_path_factory) -> Path:
    """One generated corpus for the whole session, in a temp directory."""
    out = tmp_path_factory.mktemp("seed")
    generate(out)
    return out


def load(corpus: Path, name: str) -> list[dict]:
    return json.loads((corpus / f"{name}.json").read_text())
