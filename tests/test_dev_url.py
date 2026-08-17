"""The LOCAL dev URL, and the same-machine credential carve-out.

`dev_url` is the deliberately-lenient sibling of `staging_url`: it names where the app runs
during development (`http://localhost:3000`), so localhost / http / a bare host are all fine —
that is the entire point. It is host-owned configuration, never a signed/fetchable target, so
it is shown to every viewer and never withheld.

The carve-out relaxes rule 6 (never message a credential) ONLY on a same-machine / local task,
and only for seed/test/fixture credentials — because there the thread never leaves the box.
"""

from __future__ import annotations

from sys_buddy import admin, api, state
from sys_buddy.rules import RULES_OF_ENGAGEMENT, rules_text
from tests.test_state import _agents, _lock_all, _valid_spec

_MARK = "LOCAL / SAME-MACHINE TASK"


def test_get_contract_carries_dev_url_proposed_and_locked(conn):
    """The agent-facing path: get_contract hands dev_url to agents at BOTH stages — it is
    the local target, not the signed one, so unlike staging_url it is never withheld."""
    ag = _agents(conn, roles=("backend", "frontend"))          # seeds task "signin" + todo #1
    admin.set_dev_url("signin", "http://localhost:3000")
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)

    proposed = state.get_contract(conn, "signin", 1)
    assert proposed["status"] == "proposed"
    assert proposed["staging_url"] is None                     # withheld pre-lock, as always
    assert proposed["dev_url"] == "http://localhost:3000"      # dev_url is NOT withheld

    _lock_all(conn, ag, version=1)
    locked = state.get_contract(conn, "signin", 1)
    assert locked["locked"] is True
    assert locked["dev_url"] == "http://localhost:3000"


def test_create_task_stores_dev_url(conn):
    admin.create_task(
        "dev-task", title="Dev", roles=["backend", "frontend"],
        dev_url="http://localhost:3000",
    )
    assert admin.get_dev_url("dev-task") == "http://localhost:3000"


def test_set_dev_url_accepts_localhost_http_bare_host_and_clears(conn):
    admin.create_task("dev-task", title="Dev", roles=["backend", "frontend"])
    # No https/SSRF gate here — that is what makes it useful for local work.
    for u in ("http://localhost:3000", "localhost:8080", "http://127.0.0.1:5173"):
        r = admin.set_dev_url("dev-task", u)
        assert r["dev_url"] == u
        assert admin.get_dev_url("dev-task") == u
    r = admin.set_dev_url("dev-task", None)          # clears
    assert r["dev_url"] is None
    assert admin.get_dev_url("dev-task") is None


def test_dev_url_shows_to_every_viewer_host_or_not(conn):
    admin.create_task(
        "dev-task", title="Dev", roles=["backend", "frontend"],
        dev_url="http://localhost:3000",
    )
    # Unlike staging_url (host-only in the detail), the local URL is not sensitive.
    for is_host in (True, False):
        detail = api._task_detail(conn, "dev-task", is_host=is_host)
        assert detail["dev_url"] == "http://localhost:3000"


def test_state_task_dev_url_reads_it(conn):
    admin.create_task(
        "dev-task", title="Dev", roles=["backend", "frontend"],
        dev_url="http://localhost:3000",
    )
    assert state._task_dev_url(conn, "dev-task") == "http://localhost:3000"


# --------------------------------------------------------------------------- #
# the same-machine credential carve-out (scoped rule-6 exception)
# --------------------------------------------------------------------------- #
def test_strict_rules_have_no_carve_out(conn):
    strict = rules_text(same_machine=False, is_remote=True)
    assert _MARK not in strict
    assert strict == RULES_OF_ENGAGEMENT   # the bare constant is the strict (remote) charter


def test_same_machine_or_local_unlocks_the_carve_out(conn):
    assert _MARK in rules_text(same_machine=True, is_remote=True)    # declared same-machine
    assert _MARK in rules_text(same_machine=False, is_remote=False)  # loopback local broker


def test_carve_out_is_scoped_and_leaves_rule_6_standing(conn):
    note = rules_text(same_machine=True)
    assert "SEED / TEST / FIXTURE" in note          # only throwaway dev creds
    assert "never on a remote task" in note          # and never off-box
    # The carve-out is an exception, not a deletion — rule 6 is still right there.
    assert "NEVER put credentials in a message" in note
