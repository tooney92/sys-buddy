"""The HOST CLI list the dashboard shows — and the rule that it is GENERATED.

The owner lost a working session hunting for ``sys-buddy task staging-url … --todo N``,
a command that had shipped in v2.0.0. It worked; it was simply not written down anywhere
they were looking, and a capability nobody can find is indistinguishable from one that
does not exist.

So the Commands panel grew a ``HOST CLI`` section, and the whole point of these specs is
that the section is READ OUT OF ``cli.build_parser`` rather than typed into ``ui.html``.
Every hand-maintained list in this codebase has drifted — ``upto`` fell off the shortcode
cheatsheet, the file-types list drifted twice, the website's releases page sat four
versions stale — so a hand-written command list would have grown exactly the same hole
this section exists to close.

``ui.html`` is one static file with no build step and cannot import Python, so the seam is
the API: ``api._cli_catalog`` serves the walk, the page renders it. What these tests hold:

* the catalog names exactly the commands argparse registers (walked independently here),
* each command's arguments are exactly that subparser's arguments,
* the commands a human is told about actually exist, ``--todo`` included,
* the walk is GENERIC — a command nobody has heard of is picked up by construction,
* ``ui.html`` renders from the wire and hand-types no list of its own,
* and every ``sys-buddy …`` literal that IS in the page names a real command.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from sys_buddy import api, cli

UI = Path(__file__).resolve().parents[1] / "src" / "sys_buddy" / "ui.html"


def _ui() -> str:
    return UI.read_text(encoding="utf-8")


def _registered(parser, path=()) -> dict[str, list[str]]:
    """``{"task staging-url": ["task", "url", "--todo", "--clear"], …}``.

    Walked here from scratch, deliberately: comparing the catalog against itself would
    prove nothing. This is the same tree argparse will use to PARSE a real command line,
    so agreement with it is agreement with what the CLI accepts.
    """
    out: dict[str, list[str]] = {}
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, child in action.choices.items():
            child_path = path + (name,)
            nested = any(
                isinstance(a, argparse._SubParsersAction) for a in child._actions
            )
            if nested:
                out.update(_registered(child, child_path))
                continue
            args = []
            for a in child._actions:
                if isinstance(a, (argparse._HelpAction, argparse._VersionAction)):
                    continue
                args.append(a.option_strings[0] if a.option_strings else (a.metavar or a.dest))
            out[" ".join(child_path)] = args
    return out


def catalog() -> dict:
    return cli.command_catalog()


# --------------------------------------------------------------------------- #
# the catalog IS the parser
# --------------------------------------------------------------------------- #
def test_the_catalog_lists_every_command_argparse_registers():
    listed = [c["name"] for c in catalog()["commands"]]
    assert listed == list(_registered(cli.build_parser())), (
        "the dashboard's HOST CLI list and the CLI itself disagree about which commands "
        "exist — which is the exact failure this list is generated to prevent"
    )


def test_every_commands_arguments_are_that_commands_arguments():
    """A command that is listed with the wrong flags is worse than one that is missing:
    the reader types it and it is refused."""
    registered = _registered(cli.build_parser())
    for cmd in catalog()["commands"]:
        assert [a["name"] for a in cmd["args"]] == registered[cmd["name"]], cmd["name"]


def test_no_command_is_listed_twice():
    names = [c["name"] for c in catalog()["commands"]]
    assert len(names) == len(set(names))


def test_a_parser_with_subcommands_is_a_heading_not_a_command():
    """``sys-buddy task`` on its own is refused (``required=True`` on its subparsers), so
    listing it as something to run would teach an error message."""
    names = {c["name"] for c in catalog()["commands"]}
    assert "task" not in names and "todo" not in names
    assert {c["group"] for c in catalog()["commands"]} >= {"", "task", "todo"}


# --------------------------------------------------------------------------- #
# the commands a human is actually sent looking for
# --------------------------------------------------------------------------- #
# The set named in the brief — the host-side surface a person has to be able to find.
# Not a definition of the CLI (the parser is that); a floor under it.
MUST_LIST = [
    "task create", "task add-seat", "task staging-url", "task roster",
    "task extend-tokens", "todo list", "todo drop", "invite", "host-viewer",
    "join", "revoke-agent", "revoke-viewer", "close",
]


@pytest.mark.parametrize("name", MUST_LIST)
def test_the_host_surface_is_all_there(name):
    assert name in {c["name"] for c in catalog()["commands"]}


def test_staging_url_shows_its_todo_flag_and_says_what_it_does():
    """THE regression. ``--todo`` is what re-points ONE deliverable, and its absence from
    every list a human reads is what cost a session."""
    cmd = {c["name"]: c for c in catalog()["commands"]}["task staging-url"]
    todo = {a["name"]: a for a in cmd["args"]}["--todo"]
    assert todo["kind"] == "option" and not todo["required"]
    assert "--todo" in cmd["usage"] and "[--todo" in cmd["usage"]
    assert todo["help"], "the flag is listed with no explanation of what it is for"


def test_usage_spells_required_and_optional_with_brackets():
    """The bracket convention IS the required/optional fact, so it is produced once, in
    the catalog, rather than re-derived by a renderer that can disagree."""
    cmd = {c["name"]: c for c in catalog()["commands"]}["task create"]
    assert cmd["usage"].startswith("sys-buddy task create <id>")
    assert "--roles <ROLES>" in cmd["usage"]
    assert "[--roles" not in cmd["usage"], "--roles is required and must not read optional"
    assert "[--title <TITLE>]" in cmd["usage"]
    # A `nargs='?'` positional is optional and has to look it.
    url = {c["name"]: c for c in catalog()["commands"]}["task staging-url"]
    assert "[url]" in url["usage"] and "<task>" in url["usage"]


def test_a_store_true_flag_asks_for_no_value():
    extend = {c["name"]: c for c in catalog()["commands"]}["task extend-tokens"]
    assert "[--never]" in extend["usage"]
    assert "--never <" not in extend["usage"]


# --------------------------------------------------------------------------- #
# ...and the walk is generic, which is what "generated" actually means
# --------------------------------------------------------------------------- #
def test_the_walk_picks_up_a_command_it_has_never_heard_of():
    """The property that makes this list drift-proof: nothing in the walker names a
    command. A new subcommand — or a new flag on an old one — is listed because it was
    REGISTERED, not because anybody remembered to add it here."""
    p = argparse.ArgumentParser(prog="sys-buddy")
    sub = p.add_subparsers(dest="command", required=True)
    fresh = sub.add_parser("teleport", help="A command invented in this test")
    fresh.add_argument("where")
    fresh.add_argument("--twice", action="store_true", help="go twice")
    group = sub.add_parser("thing")
    gsub = group.add_subparsers(dest="thing_command", required=True)
    gsub.add_parser("poke", help="poke it")

    walked = {c["name"]: c for c in cli._walk(p)}
    assert set(walked) == {"teleport", "thing poke"}
    assert walked["teleport"]["usage"] == "sys-buddy teleport <where> [--twice]"
    assert walked["teleport"]["help"] == "A command invented in this test"
    assert walked["thing poke"]["group"] == "thing"


def test_argparses_own_furniture_is_not_listed_as_arguments():
    """``--help`` is on every parser and explains nothing about the command it is on."""
    for cmd in catalog()["commands"]:
        names = {a["name"] for a in cmd["args"]}
        assert "--help" not in names and "-h" not in names, cmd["name"]
    # `--db` is real, global, and belongs in its own block rather than on all 17 rows.
    assert "--db" in {a["name"] for a in catalog()["global_options"]}


# --------------------------------------------------------------------------- #
# the API serves it, and the page renders THAT
# --------------------------------------------------------------------------- #
def test_the_api_serves_the_catalog():
    served = api._cli_catalog()
    assert served == cli.command_catalog()
    assert served["commands"], "an empty list would render as 'this broker has no CLI'"


def test_the_page_fetches_the_list_instead_of_carrying_one():
    ui = _ui()
    assert "/api/cli" in ui, "the panel stopped reading the generated list"
    assert "c.commands.forEach" in ui, "the HOST CLI section is no longer rendered from the wire"
    assert "panelSection('Host CLI'" in ui


def test_the_page_hand_types_no_command_list():
    """A second copy of the CLI in ui.html is the failure mode, not a style problem. The
    page may name a command in context (the host's drop line, the staging-url fix), and it
    carries ONE sanctioned worked-example set — ``CLI_EXAMPLES``, a single copy-pasteable
    example per command, keyed by the catalog's own ``cmd.name`` so a renamed command loses
    its example rather than drifting, and ``test_every_command_the_page_does_name_is_real``
    still catches any example that names a command which no longer exists. What the page must
    NOT do is enumerate the CLI a SECOND time, in prose OUTSIDE that map."""
    ui = _ui()
    # Excise the sanctioned example map before counting: its whole job is to name every
    # command once, so it is the one place a full enumeration is intended. The guard below
    # then holds the REST of the page to the old "name at most a few in context" rule.
    body = re.sub(r"var CLI_EXAMPLES=\{.*?\};", "", ui, count=1, flags=re.S)
    assert body != ui, "CLI_EXAMPLES map not found — the excision regex needs updating"
    literals = set(re.findall(r"sys-buddy ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", body))
    known = {c["name"] for c in catalog()["commands"]}
    # Strip the ones that are prose about the product, not a command line.
    literals = {lit for lit in literals if lit.split(" ")[0] in {c.split(" ")[0] for c in known}}
    assert len(literals) <= 3, f"ui.html is enumerating the CLI again: {sorted(literals)}"


def test_every_command_the_page_does_name_is_real():
    """The other half: a contextual line like the host's drop command or the
    missing-target fix must name a command that EXISTS, spelled the way argparse takes
    it. This is what catches `sys-buddy task staging_url` before a human types it."""
    ui = _ui()
    known = {c["name"] for c in catalog()["commands"]}
    heads = {c.split(" ")[0] for c in known}
    for two, one in re.findall(r"sys-buddy ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", ui):
        if two not in heads:
            continue  # prose: "sys-buddy is built for…"
        pair = f"{two} {one}".strip()
        assert pair in known or two in known, f"ui.html names `sys-buddy {pair}`, which is not a command"
