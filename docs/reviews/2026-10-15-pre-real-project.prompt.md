Independent re-review gating the first REAL project. Defensive audit for the author — do not edit files or
start containers. This is the sign-off CLAUDE.md requires before any real repo (life-updates) runs in a
session, so the bar is "would I trust this with my real `.env` and a live database next to the grant?"

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

## Scope — three files, each with the property it must uphold

- **agentbox/grantcheck.py** (200 lines, unchanged since the 2026-10-06 review) — the primary wall. Property:
  a grant that carries a readable secret or live data does not start a session silently; anything it misses
  must be caught by a cover or be genuinely harmless. Re-confirm the 20-shape table still holds; only report a
  row if it is newly wrong or newly exploitable, or a shape the table omits.
- **agentbox/mounts.py** (291 lines, unchanged since the review) — the masks/covers set. Property: every
  secret-shaped name and the covered git/compose paths are masked/empty/read-only in the agent's own
  namespace; `covered_paths()` is the single source the gate and the pre-session backup both derive from, so a
  cover cannot be enforced in one place and missed in another.
- **agentbox/gate.py** (2552 lines) — the podman wall. Property: a bind, exec, build or create a spawned
  container asks for is only allowed if it is this session's own, user-owned, and reaches no covered path;
  the client never gets to choose a namespace or a foreign bind source. This is the ONLY file changed since
  the last review (see delta below) — spend the most new effort here.

session.py is supporting context (it wires grantcheck, the covers and the backup together); read
`_prepare_reserved`, `covered_paths`, `_backup_grants` and `_write_scan_state`, but the audit's weight is on
the three files above.

## The delta since the last review (base a02fbdd)

Only gate.py changed: +35/-11 across 0f25eb9 and 57f94da. The substantive addition is `_refuse_if_foreign`
(gate.py:585), the enforced half of audit-6-Oct H1: a bind source owned by another uid is refused, because
under keep-id the agent's own files are user-owned, so a foreign uid means bytes a rootless container wrote
(a live stack's database). Verify specifically:
- it is called on EVERY path that becomes a spawned-container bind source (not just the first), before the
  bind is accepted — trace each call site, including compose-provided binds and `--mount` forms;
- `os.lstat` (not `stat`) so a symlink's own ownership is what's checked, and a symlink in the grant pointing
  at a foreign-owned or covered target cannot launder past it;
- the missing-source early return can't be used to slip a bind that later resolves to a foreign path (TOCTOU
  between the check and podman's own resolution);
- it composes correctly with `covers_intact()` / `covers_overlap()` in `_refuse_if_covered` — a path that is
  both foreign and covered is refused either way, and re-checking covers mid-session still holds.

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
