"""Evaluation sets that ship with the package.

Inside `halo` rather than a top-level `evals/` directory because the CLI runs
them: `halo eval` has to import the golden set, and a directory beside the
package is not on the path of an installed console script. The red-team set at
M4 lands here for the same reason.
"""
