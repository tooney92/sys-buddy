# Design bundle — read SPEC.md §12 first

This is the Claude Design handoff for the sys-buddy dashboard.

`project/Sys-Buddy Dashboard.dc.html` is the **visual source of truth** — but it is a
**prototype, not production code**. It uses a custom `<x-dc>` / `<sc-if>` / `<sc-for>`
template runtime (`project/support.js`) with hardcoded mock data.

**Rebuild it as single-file vanilla HTML/CSS/JS** served by FastMCP at `/ui`, fetching
the real `/api` routes (SPEC §11). Match the visual output pixel-for-pixel; do not copy
the prototype's internal structure.

## Two security corrections to the prototype

1. **The Host/Buddy segmented toggle must not be clickable.** It's a demo switch here.
   In production, viewer mode comes from *which token you hold* — a buddy clicking
   "Host" to reveal all tasks would be privilege escalation. Render a static badge.

2. **Buddy task filtering is server-side.** The prototype does
   `listTasks.filter(t => t.id==='signin')` in the client. Real scoping lives in
   `/api/tasks`. Client-side filtering is decoration, not enforcement.

Design tokens (exact hex values), screen breakdowns, and the flourishes worth keeping
are all listed in SPEC.md §12.
