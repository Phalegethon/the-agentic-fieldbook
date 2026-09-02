"""Tests for the portable TAF context contract."""

from pathlib import Path


# ``unittest discover -s tests`` imports this directory as ``taf_context``.
# Extend that package's lookup path to keep its source sibling importable.
__path__.append(str(Path(__file__).parents[2] / "tools" / "taf-context" / "taf_context"))

# ``unittest discover -s tests`` never imports ``tests/__init__.py`` itself, so
# install the state-home guard from the package every context test imports.
import tests as _tests  # noqa: E402

_tests.install_state_home_guard()
