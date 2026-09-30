"""Tests for the repo-list type-to-filter: ai_commit_core.filter_typeahead and
ai_commit_core.normalize_typeahead.

filter_typeahead takes the rows in display order and the text typed so far, and
returns the keys of every row whose name contains that text, in the same order.
Matching is case-insensitive and '_' folds to '-' (the keyboard reports the same
key for both). Empty text keeps every row.

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


def filt(text, rows=ROWS):
    return core.filter_typeahead(rows, text)


def test_empty_text_keeps_everything():
    all_keys = [k for k, _ in ROWS]
    check("empty_all", filt("") == all_keys)
    check("none_all", filt(None) == all_keys)


def test_contains_not_prefix():
    # "comm" is mid-word in ai-commit; "stack" is the tail of media-stack.
    check("mid_word", filt("comm") == ["h1"])
    check("suffix", filt("stack") == ["h5"])
    check("single_char_anywhere", filt("v") == ["h6"])


def test_every_match_kept_in_display_order():
    check("ai_prefix_all", filt("ai") == ["h1", "h2", "h3"])
    check("media_both", filt("media") == ["h5", "h6"])
    check("e_many", filt("e") == ["h2", "h4", "h5", "h6"])


def test_more_chars_narrow():
    check("ai_n", filt("ai-n") == ["h2"])
    check("media_v", filt("media-v") == ["h6"])


def test_case_insensitive():
    check("upper_text", filt("MEDIA") == ["h5", "h6"])
    check("mixed_case_row", filt("vault") == ["h6"])


def test_underscore_folds_to_dash():
    check("dash_finds_underscore", filt("ai-t") == ["h3"])
    check("underscore_finds_dash", filt("ai_c") == ["h1"])


def test_parent_prefix_is_searchable():
    check("repo_part", filt("herm") == ["h4"])
    check("parent_part", filt("claude") == ["h4"])
    check("across_slash", filt("code/h") == ["h4"])


def test_no_match_is_empty():
    check("no_match", filt("zzz") == [])


def test_empty_row_list():
    check("empty_rows", core.filter_typeahead([], "a") == [])


def test_normalize():
    check("norm_case", core.normalize_typeahead("Ai-Commit") == "ai-commit")
    check("norm_underscore", core.normalize_typeahead("ai_commit") == "ai-commit")
    check("norm_none", core.normalize_typeahead(None) == "")


def main():
    test_empty_text_keeps_everything()
    test_contains_not_prefix()
    test_every_match_kept_in_display_order()
    test_more_chars_narrow()
    test_case_insensitive()
    test_underscore_folds_to_dash()
    test_parent_prefix_is_searchable()
    test_no_match_is_empty()
    test_empty_row_list()
    test_normalize()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
