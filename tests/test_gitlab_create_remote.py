"""Create-Remote on GitLab via push-to-create, prefilled from existing remotes.

GitLab creates a (private) project on the first push to a path that does not
exist yet, authenticated by whatever credential helper already serves the
user's other GitLab repos (Git Credential Manager). So "create a GitLab remote"
is just `git remote add origin <url>` + `git push -u origin HEAD` -- the hard
part is knowing <url>. The popup prefills host / namespace / project from the
origin remotes of repos ai-commit already watches.

The worker tests run real git against a real local bare repo (no mocked git):
the success path pushes into it; the failure path points at a path that does
not exist and must leave the repo with NO origin, so Create-Remote stays.

Run: python tests/test_gitlab_create_remote.py
"""
import importlib.util
import os
import queue
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core as core

os.environ["_AI_COMMIT_GUI_CHILD"] = "1"

_spec = importlib.util.spec_from_file_location(
    "acg", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ai-commit-gui.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

_failures = []


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


# --------------------------------------------------------------------------
# Pure URL parsing
# --------------------------------------------------------------------------

def test_parse_https():
    p = core.parse_gitlab_remote("https://git.delta.com/dctm/dxacontainerize.git")
    check("https_prefix", p and p["prefix"] == "https://git.delta.com/")
    check("https_namespace", p and p["namespace"] == "dctm")
    check("https_project", p and p["project"] == "dxacontainerize")
    # No .git suffix is just as valid a remote.
    p = core.parse_gitlab_remote("https://git.delta.com/dctm/dxacontainerize")
    check("https_no_dot_git", p and p["project"] == "dxacontainerize")


def test_parse_nested_groups():
    p = core.parse_gitlab_remote("https://gitlab.com/grp/sub/team/proj.git")
    check("nested_namespace", p and p["namespace"] == "grp/sub/team")
    check("nested_project", p and p["project"] == "proj")


def test_parse_strips_credentials():
    p = core.parse_gitlab_remote("https://oauth2:glpat-SECRET@git.delta.com/dctm/x.git")
    check("token_stripped_from_prefix",
          p and p["prefix"] == "https://git.delta.com/" and "SECRET" not in str(p))
    p = core.parse_gitlab_remote("https://user@git.delta.com/dctm/x.git")
    check("user_stripped_from_prefix", p and p["prefix"] == "https://git.delta.com/")


def test_parse_port():
    p = core.parse_gitlab_remote("https://git.example.com:8443/team/x.git")
    check("https_port_kept", p and p["prefix"] == "https://git.example.com:8443/")


def test_parse_scp_ssh():
    p = core.parse_gitlab_remote("git@git.delta.com:dctm/dxacontainerize.git")
    check("scp_prefix", p and p["prefix"] == "git@git.delta.com:")
    check("scp_namespace", p and p["namespace"] == "dctm")
    check("scp_project", p and p["project"] == "dxacontainerize")


def test_parse_ssh_scheme():
    p = core.parse_gitlab_remote("ssh://git@git.example.com:2222/grp/sub/x.git")
    check("ssh_scheme_prefix", p and p["prefix"] == "ssh://git@git.example.com:2222/")
    check("ssh_scheme_namespace", p and p["namespace"] == "grp/sub")


def test_parse_rejects():
    check("github_https_rejected",
          core.parse_gitlab_remote("https://github.com/hankel-ai/ai-commit.git") is None)
    check("github_ssh_rejected",
          core.parse_gitlab_remote("git@github.com:hankel-ai/ai-commit.git") is None)
    check("github_case_rejected",
          core.parse_gitlab_remote("https://GitHub.com/o/r") is None)
    # A project directly under the host has no namespace to push into.
    check("no_namespace_rejected",
          core.parse_gitlab_remote("https://git.delta.com/x.git") is None)
    check("empty_rejected", core.parse_gitlab_remote("") is None)
    check("local_path_rejected", core.parse_gitlab_remote("C:/repos/bare.git") is None)
    check("file_url_rejected", core.parse_gitlab_remote("file:///C:/repos/bare.git") is None)


# --------------------------------------------------------------------------
# Ranking candidates
# --------------------------------------------------------------------------

def test_candidates_ranked():
    urls = [
        "https://git.delta.com/dctm/a.git",
        "https://github.com/hankel-ai/b.git",
        "https://git.delta.com/other/c.git",
        "https://git.delta.com/dctm/d.git",
        "git@gitlab.com:me/e.git",
        "https://oauth2:tok@git.delta.com/dctm/f.git",
        "",
    ]
    c = core.gitlab_remote_candidates(urls)
    check("candidates_hosts_ranked",
          [h["prefix"] for h in c] == ["https://git.delta.com/", "git@gitlab.com:"])
    check("candidates_namespaces_ranked",
          c and c[0]["namespaces"] == ["dctm", "other"])
    check("candidates_empty", core.gitlab_remote_candidates([]) == [])
    check("candidates_github_only",
          core.gitlab_remote_candidates(["https://github.com/o/r"]) == [])


# --------------------------------------------------------------------------
# Building / validating
# --------------------------------------------------------------------------

def test_build_url():
    check("build_https",
          core.build_gitlab_remote_url("https://git.delta.com/", "dctm", "newproj")
          == "https://git.delta.com/dctm/newproj.git")
    check("build_scp",
          core.build_gitlab_remote_url("git@git.delta.com:", "grp/sub/", "p")
          == "git@git.delta.com:grp/sub/p.git")
    check("build_trims",
          core.build_gitlab_remote_url(" https://h/ ", " /ns/ ", " p ")
          == "https://h/ns/p.git")


def test_validate():
    ok = core.validate_gitlab_target
    check("valid", ok("https://h/", "dctm", "my-proj_1.x") == "")
    check("valid_nested", ok("https://h/", "grp/sub", "p") == "")
    check("no_host", ok("", "dctm", "p") != "")
    check("no_namespace", ok("https://h/", "", "p") != "")
    check("no_project", ok("https://h/", "dctm", "") != "")
    check("space_in_project", ok("https://h/", "dctm", "my proj") != "")
    check("leading_dash", ok("https://h/", "dctm", "-p") != "")
    check("dot_git_suffix", ok("https://h/", "dctm", "p.git") != "")
    check("bad_namespace_segment", ok("https://h/", "grp//sub", "p") != "")


def test_suggest_project_name():
    s = core.suggest_gitlab_project_name
    check("suggest_keeps_valid", s("dxacontainerize") == "dxacontainerize")
    check("suggest_spaces", s("My Cool Repo") == "My-Cool-Repo")
    check("suggest_strips_edges", s("-weird name.") == "weird-name")


# --------------------------------------------------------------------------
# Worker: real git against a real local bare repo
# --------------------------------------------------------------------------

def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _make_repo(root):
    work = Path(root) / "work"
    work.mkdir()
    _git(["init", "-b", "main"], work)
    _git(["config", "user.email", "t@example.com"], work)
    _git(["config", "user.name", "t"], work)
    (work / "a.txt").write_text("hi\n")
    _git(["add", "a.txt"], work)
    _git(["commit", "-m", "init"], work)
    return work


def _run_worker(work, url):
    q = queue.Queue()
    m.ui_queue = q
    rs = SimpleNamespace(path=work)
    m.app = m.AppState(repos={str(work): rs}, non_git_folders={})
    m.bg_create_gitlab_remote(str(work), url)
    return q.get_nowait()


def test_worker_success():
    root = tempfile.mkdtemp()
    try:
        work = _make_repo(root)
        bare = Path(root) / "remote.git"
        _git(["init", "--bare", str(bare)], root)
        msg = _run_worker(work, bare.as_posix())
        check("success_message", msg[0] == "create_remote_result" and msg[2] is True)
        origin = _git(["remote", "get-url", "origin"], work).stdout.strip()
        check("success_origin_set", origin == bare.as_posix())
        upstream = _git(["rev-parse", "--abbrev-ref", "@{u}"], work).stdout.strip()
        check("success_upstream_tracked", upstream == "origin/main")
        pushed = _git(["rev-parse", "main"], bare).stdout.strip()
        local = _git(["rev-parse", "HEAD"], work).stdout.strip()
        check("success_commit_pushed", pushed and pushed == local)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_worker_failure_rolls_back():
    root = tempfile.mkdtemp()
    try:
        work = _make_repo(root)
        missing = (Path(root) / "does-not-exist.git").as_posix()
        msg = _run_worker(work, missing)
        check("failure_message", msg[0] == "create_remote_result" and msg[2] is False)
        check("failure_detail_nonempty", bool(msg[3]))
        remotes = _git(["remote"], work).stdout.strip()
        check("failure_origin_removed", remotes == "")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_worker_existing_origin_untouched():
    """If origin already exists, don't clobber it -- and don't remove it."""
    root = tempfile.mkdtemp()
    try:
        work = _make_repo(root)
        _git(["remote", "add", "origin", "https://git.delta.com/dctm/keep.git"], work)
        msg = _run_worker(work, (Path(root) / "x.git").as_posix())
        check("existing_origin_fails", msg[2] is False)
        origin = _git(["remote", "get-url", "origin"], work).stdout.strip()
        check("existing_origin_kept", origin == "https://git.delta.com/dctm/keep.git")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# Popup render (real Dear PyGui)
# --------------------------------------------------------------------------

def _popup_items(dpg):
    texts, buttons, combos, inputs, radios = [], [], {}, {}, {}
    for item in dpg.get_all_items():
        if not dpg.does_item_exist(item):
            continue
        kind = dpg.get_item_type(item)
        if kind == "mvAppItemType::mvText":
            texts.append(dpg.get_value(item) or "")
        elif kind == "mvAppItemType::mvButton":
            buttons.append(dpg.get_item_label(item))
        elif kind == "mvAppItemType::mvCombo":
            combos[item] = (dpg.get_item_configuration(item).get("items"),
                            dpg.get_value(item))
        elif kind == "mvAppItemType::mvInputText":
            inputs[item] = dpg.get_value(item)
        elif kind == "mvAppItemType::mvRadioButton":
            radios[item] = (dpg.get_item_configuration(item).get("items"),
                            dpg.get_value(item))
    return texts, buttons, combos, inputs, radios


def test_popup_prefills_gitlab():
    import dearpygui.dearpygui as dpg
    fresh = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(fresh)
    dpg.create_context()
    try:
        r1 = "C:/repos/My New Repo"
        rs = SimpleNamespace(path=Path(r1), status_tag=None)
        fresh.app = fresh.AppState(repos={r1: rs}, non_git_folders={})
        cands = core.gitlab_remote_candidates([
            "https://git.delta.com/dctm/a.git",
            "https://git.delta.com/dctm/b.git",
            "https://git.delta.com/other/c.git",
        ])
        # Work-computer case: GitLab remotes exist, gh has no accounts.
        fresh._show_create_remote_popup(r1, [], "", gitlab_candidates=cands)
        texts, buttons, combos, inputs, radios = _popup_items(dpg)
        blob = "\n".join(texts)
        check("popup_provider_radio",
              any(v[0] == ["GitHub", "GitLab"] for v in radios.values()))
        check("popup_defaults_gitlab",
              any(v[1] == "GitLab" for v in radios.values()))
        check("popup_host_prefilled",
              any(v[1] == "https://git.delta.com/" for v in combos.values()))
        check("popup_namespace_prefilled",
              any(v == (["dctm", "other"], "dctm") for v in combos.values()))
        check("popup_project_prefilled", "My-New-Repo" in inputs.values())
        check("popup_preview_url",
              "https://git.delta.com/dctm/My-New-Repo.git" in blob)
        check("popup_says_private", "private" in blob.lower())
        check("popup_has_create", "Create" in buttons)
    finally:
        dpg.destroy_context()


def test_popup_defaults_github_when_accounts():
    import dearpygui.dearpygui as dpg
    fresh = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(fresh)
    dpg.create_context()
    try:
        r1 = "C:/repos/x"
        rs = SimpleNamespace(path=Path(r1), status_tag=None)
        fresh.app = fresh.AppState(repos={r1: rs}, non_git_folders={})
        cands = core.gitlab_remote_candidates(["https://git.delta.com/dctm/a.git"])
        fresh._show_create_remote_popup(r1, ["hankel-ai"], "hankel-ai",
                                        gitlab_candidates=cands)
        _, _, _, _, radios = _popup_items(dpg)
        check("popup_defaults_github",
              any(v == (["GitHub", "GitLab"], "GitHub") for v in radios.values()))
    finally:
        dpg.destroy_context()


def test_popup_github_only_unchanged():
    """No GitLab remotes anywhere: no provider radio, same GitHub popup."""
    import dearpygui.dearpygui as dpg
    fresh = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(fresh)
    dpg.create_context()
    try:
        r1 = "C:/repos/x"
        rs = SimpleNamespace(path=Path(r1), status_tag=None)
        fresh.app = fresh.AppState(repos={r1: rs}, non_git_folders={})
        fresh._show_create_remote_popup(r1, ["hankel-ai"], "hankel-ai")
        _, buttons, _, _, radios = _popup_items(dpg)
        check("github_only_no_provider_radio",
              not any(v[0] == ["GitHub", "GitLab"] for v in radios.values()))
        check("github_only_has_create", "Create" in buttons)
    finally:
        dpg.destroy_context()


def _find(dpg, kind, pred):
    for item in dpg.get_all_items():
        if dpg.does_item_exist(item) and dpg.get_item_type(item) == kind and pred(item):
            return item
    return None


def _fire(dpg, item, value):
    """Set a widget's value and run its callback, as a user edit would."""
    dpg.set_value(item, value)
    cb = dpg.get_item_callback(item)
    if cb:
        cb(item, value, dpg.get_item_user_data(item))


def test_popup_interactions():
    import dearpygui.dearpygui as dpg
    fresh = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(fresh)
    dpg.create_context()
    try:
        r1 = "C:/repos/newproj"
        with dpg.window():
            status = dpg.add_text("")
        rs = SimpleNamespace(path=Path(r1), status_tag=status)
        fresh.app = fresh.AppState(repos={r1: rs}, non_git_folders={})
        submitted = []
        fresh.executor = SimpleNamespace(
            submit=lambda fn, *a: submitted.append((fn.__name__,) + a))
        cands = core.gitlab_remote_candidates([
            "https://git.delta.com/dctm/a.git",
            "git@gitlab.com:me/b.git",
        ])
        fresh._show_create_remote_popup(r1, [], "", gitlab_candidates=cands)

        create = _find(dpg, "mvAppItemType::mvButton",
                       lambda i: dpg.get_item_label(i) == "Create")
        provider = _find(dpg, "mvAppItemType::mvRadioButton",
                         lambda i: dpg.get_item_configuration(i).get("items")
                         == ["GitHub", "GitLab"])
        project = _find(dpg, "mvAppItemType::mvInputText",
                        lambda i: dpg.get_value(i) == "newproj")
        ns_input = _find(dpg, "mvAppItemType::mvInputText",
                         lambda i: dpg.get_value(i) == "dctm")
        host_pick = _find(dpg, "mvAppItemType::mvCombo",
                          lambda i: "git@gitlab.com:" in
                          (dpg.get_item_configuration(i).get("items") or []))
        check("interact_create_enabled_valid",
              dpg.get_item_configuration(create)["enabled"])

        _fire(dpg, project, "bad name")
        check("interact_invalid_disables_create",
              not dpg.get_item_configuration(create)["enabled"])
        _fire(dpg, project, "newproj")

        # Switching host swaps the namespace list and value.
        _fire(dpg, host_pick, "git@gitlab.com:")
        check("interact_host_switch_namespace", dpg.get_value(ns_input) == "me")

        # GitHub with no gh accounts: Create must be disabled.
        _fire(dpg, provider, "GitHub")
        check("interact_github_no_accounts_disabled",
              not dpg.get_item_configuration(create)["enabled"])
        _fire(dpg, provider, "GitLab")

        cb = dpg.get_item_callback(create)
        cb(create, None, dpg.get_item_user_data(create))
        check("interact_confirm_submits_gitlab",
              submitted == [("bg_create_gitlab_remote", r1,
                             "git@gitlab.com:me/newproj.git")])
        check("interact_status_names_url",
              "git@gitlab.com:me/newproj.git" in dpg.get_value(status))
    finally:
        dpg.destroy_context()


def main():
    test_parse_https()
    test_parse_nested_groups()
    test_parse_strips_credentials()
    test_parse_port()
    test_parse_scp_ssh()
    test_parse_ssh_scheme()
    test_parse_rejects()
    test_candidates_ranked()
    test_build_url()
    test_validate()
    test_suggest_project_name()
    test_worker_success()
    test_worker_failure_rolls_back()
    test_worker_existing_origin_untouched()
    test_popup_prefills_gitlab()
    test_popup_defaults_github_when_accounts()
    test_popup_github_only_unchanged()
    test_popup_interactions()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
