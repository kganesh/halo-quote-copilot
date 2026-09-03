"""Evaluation sets that ship with the package.

These live inside `halo` rather than in a top-level `evals/` directory because
the CLI runs them. `halo eval` imports the golden set, and a directory next to
the package is not on the import path of an installed console script. The M4
red-team set will go here for the same reason.
"""
