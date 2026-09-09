"""Tests for the repo-list type-ahead matcher: ai_commit_core.find_typeahead_match
and ai_commit_core.normalize_typeahead.

find_typeahead_match takes the rows in display order and the characters typed so
far, and returns the first row the user meant. Matching is case-insensitive, '_'
folds to '-' (the keyboard reports the same key for both), and the search falls
back from a whole-name prefix, to a prefix after the parent-folder prefix used
for duplicate display names, to a substring anywhere in the name.

Run: python tests/test_typeahead.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core as core

_failures = []

# Rows in the order they are rendered in repos_container.
ROWS = [
    ("h1", "ai-commit"),
    ("h2", "ai-news"),
    ("h3", "ai_toolkit"),
    ("h4", "ClaudeCode/hermes"),
    ("h5", "media-stack"),
    ("h6", "Media-Vault"),
]


def check(name, cond):
    if cond:
        print(f"ok  {name}")
    else:
        print(f"FAIL {name}")
        _failures.append(name)


def match(prefix, rows=ROWS):
    return core.find_typeahead_match(rows, prefix)


def test_empty_prefix_matches_nothing():
    check("empty_none", match("") == (None, ""))
    check("none_none", match(None) == (None, ""))


def test_single_char_hits_first_in_display_order():
    check("single_char", match("a")[0] == "h1")
    check("single_char_m", match("m")[0] == "h5")


def test_more_chars_narrow():
    check("ai_n", match("ai-n")[0] == "h2")
    check("media_v", match("media-v")[0] == "h6")


def test_case_insensitive():
    check("upper_prefix", match("MEDIA")[0] == "h5")
    check("mixed_case_row", match("media-va")[0] == "h6")


def test_underscore_folds_to_dash():
    check("dash_finds_underscore", match("ai-t")[0] == "h3")
    check("underscore_finds_dash", match("ai_c")[0] == "h1")


def test_parent_prefixed_name_matches_on_repo_name():
    check("after_slash", match("her")[0] == "h4")
    check("full_prefix_still_works", match("claudecode/h")[0] == "h4")


def test_substring_fallback():
    check("substring", match("stack")[0] == "h5")
    check("substring_mid_word", match("comm")[0] == "h1")


def test_prefix_beats_substring():
    rows = [("a", "stack-viewer"), ("b", "media-stack")]
    # "stack" is a substring of both but a prefix of only the first.
    check("prefix_wins", core.find_typeahead_match(rows, "stack")[0] == "a")
    rows_reversed = [("b", "media-stack"), ("a", "stack-viewer")]
    check("prefix_wins_regardless_of_order",
          core.find_typeahead_match(rows_reversed, "stack")[0] == "a")


def test_name_prefix_beats_after_slash_prefix():
    rows = [("a", "ClaudeCode/hermes"), ("b", "hermes-lcm")]
    check("whole_name_prefix_first",
          core.find_typeahead_match(rows, "her")[0] == "b")


def test_no_match_returns_none():
    check("no_match", match("zzz") == (None, ""))


def test_returns_name_alongside_key():
    check("returns_name", match("ai-n")[1] == "ai-news")


def test_empty_row_list():
    check("empty_rows", core.find_typeahead_match([], "a") == (None, ""))


def test_normalize():
    check("norm_case", core.normalize_typeahead("Ai-Commit") == "ai-commit")
    check("norm_underscore", core.normalize_typeahead("ai_commit") == "ai-commit")
    check("norm_none", core.normalize_typeahead(None) == "")


def main():
    test_empty_prefix_matches_nothing()
    test_single_char_hits_first_in_display_order()
    test_more_chars_narrow()
    test_case_insensitive()
    test_underscore_folds_to_dash()
    test_parent_prefixed_name_matches_on_repo_name()
    test_substring_fallback()
    test_prefix_beats_substring()
    test_name_prefix_beats_after_slash_prefix()
    test_no_match_returns_none()
    test_returns_name_alongside_key()
    test_empty_row_list()
    test_normalize()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
