Independent re-review gating the first REAL project. Defensive audit for the author — do not edit files or
start containers. This is the sign-off CLAUDE.md requires before any real repo (life-updates) runs in a
session, so the bar is "would I trust this with my real `.env` and a live database next to the grant?"

Range: everything since the last security audit, base commit a02fbdd (docs/reviews/2026-10-06-final.md) to
HEAD — ~850 lines of agentbox/ code plus tests and docs. For the diff-based `/code-review ultra`, that is
`/code-review ultra a02fbdd`; ultra runs its own recipe over the diff and does not read this file, so this
brief is the scope-of-record and the checklist for reading ultra's output (and the prompt for a human or a
reading-agent reviewer who audits the whole files rather than the diff).

## Threat model (from docs/design.md, top; decisions.md 6 Oct)

In scope: containing an ACCIDENT (a stray `rm -rf`, an over-eager fix) and stopping CROSS-PROJECT movement.
Out of scope: a deliberately malicious or prompt-injected agent breaking out, and deferred execution (code
the agent writes that the user later runs). Rank findings against the in-scope model; note out-of-scope ones
separately, don't inflate them.

## Read first

docs/design.md (esp. the Mounts/covers and Presets sections, and the bind-refusal rules at design.md:315),
docs/decisions.md, CLAUDE.md, and the prior reviews — above all docs/reviews/2026-10-06-final.md, whose
20-row coverage table is the accepted baseline. The security model is two walls: (1) the grant should hold
only CODE — grantcheck (Track 1) refuses a grant carrying secrets or live data; (2) masks/covers are
defense-in-depth behind it. The real question is always the SEAM: for a shape grantcheck misses, does a cover
still contain it, or is it exposed — including from a container the agent spawns through the gate?

## Scope — the two walls, and the files that changed under them

The core walls (audit even where unchanged — this is the gate before real data):

- **agentbox/grantcheck.py** (200 lines, unchanged since 2026-10-06) — the primary wall. Property: a grant
  carrying a readable secret or live data does not start a session silently; anything it misses must be caught
  by a cover or be genuinely harmless. Re-confirm the 20-shape table still holds; report a row only if it is
  newly wrong, newly exploitable, or a shape the table omits.
- **agentbox/mounts.py** (291 lines, unchanged) — the masks/covers set. Property: every secret-shaped name and
  the covered git/compose paths are masked/empty/read-only in the agent's own namespace; `covered_paths()` is
  the single source the gate and the pre-session backup both derive from.
- **agentbox/gate.py** (2552 lines) — the podman wall. Property: a bind/exec/build/create a spawned container
  asks for is allowed only if it is this session's own, user-owned, and reaches no covered path; the client
  never chooses a namespace or a foreign bind source.

The changed code since the base, in rough risk order (verify each still upholds the property above):

- **agentbox/session.py** (+122) — the seam that wires it together: `covered_paths()` now the single source,
  `_backup_grants` excludes exactly the covered set, `_write_scan_state`, lockdown ephemeral state. Confirm the
  backup can never capture a masked secret and the three consumers can't drift.
- **agentbox/gate.py** (+46) — `_refuse_if_foreign` (gate.py:585), the enforced half of audit-6-Oct H1: a bind
  source owned by another uid is refused (under keep-id the agent's files are user-owned, so a foreign uid is
  bytes a rootless container wrote — a live stack's DB). Verify it runs on EVERY spawned-container bind source
  (compose binds and `--mount` forms included), uses `lstat` so a symlink can't launder past it, the
  missing-source early return can't slip a TOCTOU bind, and it composes with `covers_intact`/`covers_overlap`.
- **agentbox/sockguard.py** (+41) — the podman-socket filter (label scoping of list/inspect/logs, bind
  allowlist). Property: no cross-project container is visible or reachable; exec stays gate-scoped.
- **agentbox/initproj.py** (+372, NEW) — `ab init` scaffolding. Property it CLAIMS: the scaffold is
  grantcheck-clean by construction (0600 gitignored `.env`, committed fake `.env.dev`, live data in a named
  volume, no `./data` bind). Check the written files actually pass grantcheck and that no template writes a
  real secret into a tracked file.
- **agentbox/preview.py** (+8) — publishes a dev service on 127.0.0.1. Property: loopback only, this session's
  own dev containers only, torn down on kill — no 0.0.0.0 bind, no other session's service.
- **agentbox/cli.py** (+315) — mostly ergonomics (the `ab session` grouping, arg docs), but `project add`
  validation (overlap, grant exposure, lockdown ceiling) and `accept-grant-exposure`/`reset-state` are
  security-relevant: confirm they can't register an overlapping or exposed grant without the explicit ack.
- **adapters/claude.py** (+38), **backup.py** (+16), **config.py** (+9), **scan.py**, **selftest/__init__.py**,
  **builtrec.py** — supporting; skim for anything that weakens a property above (e.g. the scan no longer
  flagging a file the agent writes at startup must not also stop flagging a real secret).

## Known-accepted, do NOT re-litigate (unless newly exploitable)

The out-of-scope model limits (malicious agent, deferred execution), the subscription-token-readable-in-session
accepted risk (decisions.md 29 Sep), and the coverage-table shapes already recorded as accepted under the
threat model. Flag one of these only if the current code makes it WORSE than the review recorded, or if a new
path reaches it.

## Output

A coverage/seam table (shape | caught by grantcheck? | caught by a cover? | exposed, and from where?), then
findings ranked high/medium/low — each with the concrete failing shape, whether it bites in the agent's own
namespace or only from a gate-spawned container, the fix, and the test that would pin it. Settle anything that
needs podman behaviour with a command the author can run, marked as such.
