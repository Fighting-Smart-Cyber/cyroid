# Contributing to PROVING GROUND

Thanks for your interest. Please read this before opening a pull request —
this repository works a little differently from most.

## How this repository is updated

`main` is updated in release batches rather than per change, so:

- **Please don't push directly to `main`** — direct pushes are not retained.
  Open a pull request instead.
- Release history is intentionally coarse: one commit per version, not one per
  change.

Issues and pull requests are welcome and are read. Merged work appears here in
the following release.

## Contributor License Agreement

**We require a signed CLA before merging any contribution.**

PROVING GROUND is AGPL-3.0. We also offer commercial licensing, and that
requires us to hold sufficient rights in the whole codebase. A
`Signed-off-by` line (DCO) attests that you had the right to submit the code —
it does **not** grant us the rights needed to relicense it, so it isn't
sufficient on its own.

The CLA bot will comment on your first pull request with a link. It's a
one-time signature that covers all your future contributions.

If you'd rather not sign, we still want to hear from you — open an issue
describing the change and we'll implement it independently.

## Pull requests

1. Open an issue first for anything beyond a small fix, so we can confirm the
   approach before you spend time on it.
2. Branch from `main`.
3. Keep the change focused. One concern per PR.
4. Include tests. The backend uses pytest; the frontend uses Playwright for
   e2e.
5. Sign the CLA when the bot asks.

Merged changes appear in the next release, credited to you.

## Reporting security issues

**Do not open a public issue for a security vulnerability.**

Email security@fightingsmartcyber.com with details and we'll acknowledge
within three business days. PROVING GROUND deploys containers and manipulates
network configuration, so we take isolation-escape and privilege-escalation
reports especially seriously.

## Licence

Contributions are accepted under AGPL-3.0, subject to the CLA above.
