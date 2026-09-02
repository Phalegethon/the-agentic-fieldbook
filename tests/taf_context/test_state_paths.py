"""Tests for resolving the user-local TAF state root."""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest import mock

from taf_context.state_paths import StateError, StatePaths, resolve_state_paths


class StatePathTests(unittest.TestCase):
    def test_resolves_exact_platform_roots_without_global_state(self) -> None:
        cases = (
            ("darwin", {}, Path("/Users/test"), Path("/Users/test/Library/Application Support/TAF/context")),
            ("linux", {"XDG_STATE_HOME": "/state"}, Path("/home/test"), Path("/state/taf/context")),
            ("linux", {}, Path("/home/test"), Path("/home/test/.local/state/taf/context")),
            ("win32", {"LOCALAPPDATA": "C:/Users/test/AppData/Local"}, Path("C:/Users/test"), Path("C:/Users/test/AppData/Local/TAF/context")),
            ("linux", {"TAF_STATE_HOME": "/exact/override", "XDG_STATE_HOME": "/ignored"}, Path("/ignored"), Path("/exact/override")),
        )
        with mock.patch.dict(os.environ, {"TAF_STATE_HOME": "/global-must-not-leak"}, clear=True):
            for platform_name, environment, home, expected in cases:
                with self.subTest(platform_name=platform_name, environment=environment):
                    paths = resolve_state_paths(environment, platform_name, home)
                    self.assertEqual(paths, StatePaths(expected))

    def test_windows_without_local_app_data_fails_with_a_stable_code(self) -> None:
        with self.assertRaises(StateError) as caught:
            resolve_state_paths({}, "win32", Path("C:/Users/test"))
        self.assertEqual(caught.exception.code, "state-home-unavailable")

    def test_explicit_empty_override_is_not_redirected_to_a_fallback(self) -> None:
        with self.assertRaises(StateError) as caught:
            resolve_state_paths(
                {"TAF_STATE_HOME": "", "XDG_STATE_HOME": "/must-not-be-used"},
                "linux",
                Path("/must-not-be-used"),
            )
        self.assertEqual(caught.exception.code, "state-home-unavailable")

    def test_unknown_platform_fails_with_a_stable_code(self) -> None:
        with self.assertRaises(StateError) as caught:
            resolve_state_paths({}, "plan9", Path("/home/test"))
        self.assertEqual(caught.exception.code, "state-home-unavailable")


if __name__ == "__main__":
    unittest.main()
