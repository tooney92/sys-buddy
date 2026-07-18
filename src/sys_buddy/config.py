"""Runtime configuration.

A single process runs in one of two modes (SPEC §3):

- ``local``  — loopback, no auth, identity self-declared. The zero-friction on-ramp.
- ``remote`` — bound to 0.0.0.0 behind a tunnel; bearer-token auth, broker-stamped
  identity, enforced state machine.

Mode is the only real switch. Everything downstream reads it off this object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Absolute by default so every repo's CLI invocation hits the *same* broker db,
# no matter which directory it's run from. Override with --db or $SYS_BUDDY_DB.
DEFAULT_DB_PATH = Path(os.environ.get("SYS_BUDDY_DB", "~/.sys-buddy/sys_buddy.db")).expanduser()
DEFAULT_PORT = int(os.environ.get("SYS_BUDDY_PORT", "8787"))


@dataclass
class Config:
    mode: str = "local"  # "local" | "remote"
    db_path: Path = DEFAULT_DB_PATH
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    slack_webhook: str | None = None
    # Public base URL (e.g. the ngrok origin) used to build pairing links.
    # Falls back to http://host:port when unset.
    public_url: str | None = None
    # Optional agent-token lifetime in seconds. None = tokens never expire (default,
    # so a long-running Claude Code session isn't cut off). When set, a paired token
    # expires after this long; the agent can refresh it with the rotate_token tool.
    agent_token_ttl: float | None = None

    @property
    def is_remote(self) -> bool:
        return self.mode == "remote"

    @property
    def base_url(self) -> str:
        if self.public_url:
            return self.public_url.rstrip("/")
        return f"http://{self.host}:{self.port}"


_CONFIG: Config | None = None


def get_config() -> Config:
    """Return the active config, defaulting to a local-mode config if unset.

    Defaulting to local keeps ad-hoc/tests frictionless: nothing has to wire a
    Config before touching the db.
    """
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config()
    return _CONFIG


def set_config(cfg: Config) -> Config:
    global _CONFIG
    _CONFIG = cfg
    return cfg
