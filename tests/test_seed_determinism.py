"""Two runs must be byte-identical, or an eval regression is indistinguishable
from a reshuffled catalogue."""

import filecmp

from halo.seed.generate import generate


def test_two_runs_produce_identical_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate(a)
    generate(b)

    names = sorted(p.name for p in a.rglob("*") if p.is_file())
    assert names == sorted(p.name for p in b.rglob("*") if p.is_file())

    match, mismatch, errors = filecmp.cmpfiles(
        a, b, [p.name for p in a.glob("*.json")], shallow=False
    )
    assert not mismatch and not errors
