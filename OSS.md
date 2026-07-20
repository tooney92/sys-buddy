# Open-source governance

How sys-buddy is licensed and how changes reach `main`. This is a reference for
the maintainer and a heads-up for contributors.

## License
- **MIT** (see `LICENSE`). Anyone may use, modify, and distribute with attribution.

## Repository
- Public: `github.com/tooney92/sys-buddy`
- Maintainer / sole merger: [@tooney92](https://github.com/tooney92)

## Branch protection on `main`
`main` is protected — the settings, and why:

| Rule | Effect |
|------|--------|
| Require a pull request before merging | No direct pushes; every change lands via PR. |
| Require Code Owner review (`CODEOWNERS` = `* @tooney92`) | Every PR needs the owner's approval — nothing merges without sign-off. |
| Dismiss stale approvals on new pushes | A re-pushed PR must be re-approved. |
| Block force-pushes and branch deletion | History can't be rewritten or the branch removed. |
| Require conversation resolution | Open review threads must be resolved before merge. |
| `enforce_admins = false` | The owner can bypass the review requirement to merge their **own** PRs (you can't approve your own PR) and can push directly in a pinch. |

## Who can merge
- **The owner is the only merger.** Contributors work from **forks**: they can open
  PRs but cannot merge — merging requires push access to `main`, which only the owner has.
- Do **not** add outside contributors as *Write* collaborators. On a user-owned repo,
  GitHub cannot restrict *who* clicks Merge (that control is organization-only), so a
  Write collaborator could merge an already-approved PR. The fork model avoids this and
  keeps the owner as sole merger.

## Contributing (summary)
See `CONTRIBUTING.md`. In short: fork → branch → `uv run pytest -q` (green) → open a PR
against `main` with a clear description. The owner reviews and merges.

## If you later want Write-collaborators *and* guaranteed sole-merge
Move the repo into a **free GitHub Organization**, then add a "Restrict who can push to
`main`" rule listing only the owner. That org-only control locks the merge button itself
while still letting collaborators have Write access for branches.

## Operating the protection (maintainer)
Inspect the current rules:
```bash
gh api repos/tooney92/sys-buddy/branches/main/protection
```
Re-apply / edit (example body — the one currently in effect):
```bash
gh api -X PUT repos/tooney92/sys-buddy/branches/main/protection \
  -H "Accept: application/vnd.github+json" --input - <<'JSON'
{
  "required_status_checks": null,
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "require_code_owner_reviews": true,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
```
> `restrictions` must be `null` on a user-owned repo — it's the org-only "restrict who
> can push" control and the API rejects a non-null value here.
