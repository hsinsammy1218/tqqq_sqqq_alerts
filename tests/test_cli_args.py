from __future__ import annotations

import argparse
import re

from cli_args import build_parser


def test_build_parser_constructs() -> None:
    """Python 3.14+ validates argparse help as %-format strings; bare % must be %%."""
    parser = build_parser()
    assert isinstance(parser, argparse.ArgumentParser)


def test_help_strings_escape_percent() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "~19%" in help_text
    assert "≥75%" in help_text
    # No leftover argparse format artifacts from bad escaping.
    assert "%(" not in help_text or all(
        tok.startswith("%(") and ")s" in tok for tok in re.findall(r"%\([^)]*\)s?", help_text)
    )


def test_no_bare_percent_in_action_help() -> None:
    """Every help= string that contains % should use %% so 3.14 argparse accepts it."""
    parser = build_parser()
    for action in parser._actions:
        help_str = action.help
        if not help_str or "%" not in help_str:
            continue
        # Collapse %% pairs; any remaining lone % is unsafe for argparse formatting.
        collapsed = help_str.replace("%%", "")
        assert "%" not in collapsed, f"bare % in help for {action.option_strings}: {help_str!r}"
