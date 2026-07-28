# Connect your agent — build spec

How a buddy connects **their** agent to a sys-buddy broker, for every agent we support.
This is the spec the join page (`src/sys_buddy/join.html`) and the desktop app
(`src/sys_buddy/gui_app.html`) render from.

**Status:** Claude is LOCKED (verified end-to-end 2026-07-28). Cursor, Gemini CLI and
"Other" are drafted but NOT yet verified. Nothing here is built yet.

---

## The two facts

Every client needs the same two things and nothing else:

| | |
|---|---|
| **URL** | `<origin>/mcp` |
| **Header** | `Authorization: Bearer <agent_token>` |

`middleware.py:69` reads that header and nothing else — no OAuth, no query param, no
cookie. Everything below is just per-client packaging of those two facts.

## Principles

1. **Ask which AGENT, not which mechanism.** "Do you use a CLI?" asks the user to
   classify our implementation detail. They know they use Cursor.
2. **Some agents live inside editors.** The thing consuming MCP in VS Code is GitHub
   Copilot agent mode, not VS Code. Label the agent, not the app.
3. **Never ship an unverified snippet as if it were tested.** Field names differ in ways
   that look cosmetic and fail confusingly. Unverified clients go under "Other" with the
   raw facts.
4. **The choice is per-person, stored nowhere.** Your buddy can be on Cursor while you're
   on Claude Code; the broker neither knows nor cares. It must never become task state —
   that would invent a compatibility matrix we'd have to maintain.
5. **Always include a verification step.** Every failure we found was silent.

---

## Step ① — the picker

```
Which agent are you using?
┌────────┐ ┌────────┐ ┌────────────┐ ┌───────┐
│ Claude │ │ Cursor │ │ Gemini CLI │ │ Other │
└────────┘ └────────┘ └────────────┘ └───────┘
  default
```

Claude is default-selected: zero clicks for today's audience. Only Claude gets a
sub-step — the others have exactly one way in, so a second click would be friction with
no information.

---

## Claude — LOCKED ✅

Verified 2026-07-28 against a real broker on `:9292`, desktop app, no terminal.

```
Where do you use Claude?
┌───────────────────┐ ┌────────────────────┐
│ In the terminal   │ │ In the desktop app │
└───────────────────┘ └────────────────────┘
```

### Claude · terminal

```
claude mcp remove sys-buddy
claude mcp add --scope local --transport http sys-buddy <url> \
  --header "Authorization: Bearer <token>"
```

Copy button. Then:

> Run it **in the folder you'll open your agent in**, then restart your session.

**`--scope local` is deliberate.** The token is a seat on ONE task, so the connection
belongs to the folder that task is worked from — and multiple tasks in multiple folders
then work at once. Global (`user`) scope was tried and reverted: pairing again replaces
the single global entry, capping you at one active task per machine, and it leaves one
task's bearer token in every project you open. The REMOVE half passes no scope, so it
clears the entry wherever it lives (including entries the short-lived user-scope version
created).

**The failure this causes is real** — run it in the wrong directory and you get no tools,
with `claude mcp list` reporting nothing from there, which reads as the add having
failed. It cost a pairing session. The answer is saying WHERE to run it plus the
verification step, not making the entry global.

### Claude · desktop app

No terminal. Claude writes the file itself.

**1 — paste into the chat** (copy button):

```
Add an MCP server called "sys-buddy" to this project's .mcp.json.

If .mcp.json already exists, KEEP every server already in it and add this one
alongside them — do not replace the file or remove anything.
If it doesn't exist, create it with just this entry.

The entry to add under "mcpServers":

"sys-buddy": {
  "type": "http",
  "url": "<url>",
  "headers": { "Authorization": "Bearer <token>" }
}

Then show me the resulting file so I can confirm nothing was lost.
```

**2 — start a NEW conversation.** Not a restart; a new conversation is enough.

**3 — verify:** ask *"what sys-buddy tools do you have?"* Expect `rules`,
`readiness_check`, `send_message`, …

Then:

> Nothing? The file must be at the root of the project you have open in Claude — not your
> home folder.

> ⓘ You won't find sys-buddy in the Directory. That's a catalogue of published
> connectors; this is your own broker.

> ⚠ This file holds your access token. Don't commit it.

### Why every line above is what it is — all four were wrong before testing

| we assumed | reality |
|---|---|
| "Save this as `.mcp.json`" | would CLOBBER existing servers; must merge |
| "Fully quit the app" | a new conversation is enough |
| "Reconnect with `/mcp`" | desktop says *"MCP controls aren't available right now"* — CLI-only advice |
| user would find us in the Directory | it's a published-connector catalogue; never lists self-hosted |

Also confirmed: `claude mcp add` fails in the desktop app because the `claude` binary
isn't on its PATH — the buddy in the session that prompted all this had never heard of
the CLI and ended up installing it just to join.

---

## Cursor — DRAFT, not verified

Same JSON as the desktop-app path, saved to `~/.cursor/mcp.json` (global) or the
project's `.cursor/mcp.json`. Key is `mcpServers`, URL field is `url`.

Cursor is installed on the owner's machine, so this IS verifiable — do it before shipping.

⚠ Known conflict to preserve: if a server answers RFC 9728 OAuth discovery, Cursor
**ignores configured headers** and starts OAuth instead (maintainer-confirmed, unfixed).
sys-buddy is immune today — verified: `/.well-known/oauth-protected-resource` and
`/.well-known/oauth-authorization-server` both 404. This breaks the moment anyone adds
OAuth.

## Gemini CLI — DRAFT, not verified

```
gemini mcp add -t http sys-buddy <url> -H "Authorization: Bearer <token>"
```

Confirmed from `gemini mcp add --help` on the owner's machine: `-t/--transport http` and
`-H/--header` exist, and the help text's own example is an `Authorization: Bearer`.
Not yet run end-to-end. `gemini` is installed, so verify before shipping.

⚠ In Gemini's config file, `httpUrl` selects streamable HTTP and plain `url` means **SSE**
— a snippet copied from another client silently picks the wrong transport.

## Other — DRAFT

The honest escape hatch for everything we cannot verify. Two copy buttons (URL, header),
the generic `mcpServers` block, and a note that field names differ:

- top-level key: `mcpServers` (Cursor, Windsurf, Gemini) · `servers` (VS Code) ·
  `context_servers` (Zed)
- URL field: `url` (Cursor, VS Code, Zed) · `serverUrl` (Windsurf) · `httpUrl` (Gemini)
- VS Code **requires** `"type": "http"`; others infer it

These are per-client literals, not a shared schema with cosmetic differences. A single
template would generate configs that fail confusingly.

---

## Not shipping, and why

| client | blocker |
|---|---|
| **Claude Desktop Connectors** | `static_headers` is a gated BETA (contact Anthropic); config-file remote entries are silently deleted; open bug where the header is ignored and a bogus OAuth flow starts. Different system from Claude Code in the same app. |
| **Perplexity** | IS an MCP client (the sceptical hypothesis was wrong), but auth is a fixed menu — None / API Key / OAuth — with no free-form header field, and the API-key wire format is undocumented. Its own doc still says "coming soon". Needs an empirical test. |
| **Codex CLI** | `codex mcp add` is stdio-only (`-- <command>`); remote needs hand-edited `~/.codex/config.toml`. Not installed locally, so unverifiable. Best secret hygiene of any client though (`bearer_token_env_var`), so worth doing properly later. |
| **VS Code / Copilot** | Needs a Copilot subscription; `code` not on PATH here. Also a live contradiction: VS Code's docs say the key is `servers`, Perplexity's docs say `mcpServers`. Testable — VS Code is installed. |
| **Zed, Windsurf** | Not installed; Windsurf is mid-rebrand to "Devin Desktop" and steering toward OAuth. |

## Cheap enablers worth doing regardless

- **Accept `X-API-Key` as well as `Authorization: Bearer`** — ~5 lines in
  `middleware.py:69`, same token/resolver/rate-limiter. It's on Claude's connector
  allowlist and the likeliest Perplexity wire format.
- **Surface auth failures.** A wrong token currently yields `Invalid request parameters`
  with an empty `data` field; the real message from `middleware.py:137` never reaches the
  client. Every setup doc we write will generate support questions this one fix answers.
  (Reported by research; not independently reproduced — needs a repro first.)
