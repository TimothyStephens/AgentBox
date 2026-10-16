I checked all ten findings against the code and reproduced the important ones in the scratchpad. Six are real and worth fixing before the real project. Two are real but are design
  decisions for you. One doesn't apply on this host, and one is a small cleanup.

  Fix now

  1. Claude state-dir symlinks (security). At each start the launcher reads and writes files in the Claude state dir, which the agent can write to. Those reads and writes follow
     symlinks the agent leaves there (agentbox/adapters/claude.py:43, :87, :110). That lets a session get the next session start to overwrite or copy out files you own on the host. I
     reproduced all of the cases with stand-in files.
     - The reviewer listed CLAUDE.md and settings.json.tmp. _mark_onboarded (.claude.json) has the same flaw and runs at every Claude start.
     - Lockdown isn't affected, because its state is new each session.
     - The fix is to copy the OMP adapter, which already does this correctly (agentbox/adapters/omp.py:326): remove any existing link first, open with O_NOFOLLOW/O_EXCL, and add a unit
       test for each path.
  2. Sandbox notes pile up in CLAUDE.md. I reproduced this: two sessions leave two copies. The fix is to wrap the injected block in markers and strip the old block before adding the new
     one.
  3. ab run claude web --shell doesn't work, though the help text shows it. I confirmed with the parser that any flag after the positional names goes to the agent. ab run --agent claude
     web is rejected too. Making the two positionals optional ahead of the passthrough arguments fixes both cases, and the help example needs correcting (agentbox/cli.py:463).
  4. idle_seconds is wrong for lockdown sessions (agentbox/session.py:1270). It always looks at the persistent state dir. ab session ls can then mark a busy lockdown session as idle and
     suggest killing it. The session metadata already holds the session name, so it can check the per-session dir instead.
  5. Backup excludes aren't escaped (agentbox/backup.py:21). I reproduced this: an exclude for prod[1].key lets the real file into the backup and leaves out prod1.key instead. Escaping
     [, *, ? and \ fixes it, for both kinds of exclude.
  6. ab project rm web web crashes with a KeyError traceback. Removing duplicate names first fixes it.

  Your call

  7. The gate refuses binds owned by another uid (agentbox/gate.py:585). The reviewer is right about how it happens. A dev database container takes ownership of its data dir, and from
     then on any podman-compose up after a down is refused for that service. Restarting a stopped container still works. Neither the pod template nor the tickerlab trial runs into this;
     a real project with a database in compose.dev.yaml would. One option is to document that dev databases use a named volume created in the session, which the gate already allows.
     The other is a per-project list of dev data paths exempt from the check.
  8. Backups leave out git config, hooks and compose files. The reviewer is right, but this was deliberate: 1927027 merged the backup excludes with the gate's covered paths. The agent
     can't damage those files anyway, since they're read-only in the session. It only causes harm if someone restores with rsync --delete, and no doc describes how to restore. I'd keep
     it, since git config can hold credentials, and document that restoring means copying the snapshot over the project without --delete.

  No change needed

  9. Makefile date -d. GNU date on this host parses podman's timestamp format; I tested it. macOS isn't a target.
  10. Two copies of the git identity lookup (agentbox/initproj.py:274 and agentbox/session.py:197). They really do differ, but this is cleanup and can wait.

  The current agentbox-claude image was built on 2 Oct and has no CLI-version label. So make claude-image hasn't been run since the label was added to the build. Rebuild it before the
  real project.

  Should I fix 1–6 now, with tests, committing after each green step? Items 7 and 8 would wait for your decision, and I'd record both in docs/decisions.md.

---

## Response (2026-10-15)

All ten triaged and resolved before the real project.

| # | Finding | Resolution | Commit / test |
|---|---|---|---|
| 1 | Claude state-dir reads/writes follow agent-left symlinks | **Fixed** — drop any link first, write via `O_EXCL\|O_NOFOLLOW` (mirrors OMP `_prepare_file`); covers CLAUDE.md, settings.json(.tmp), .claude.json(.tmp) | `0e869fe` · `test_claude_state.ClaudeStateSymlinks` |
| 2 | Sandbox preamble stacks up in CLAUDE.md | **Fixed** — wrap in markers, strip the old block before writing | `0e869fe` · `test_claude_state.ClaudeSandboxBlock`, `test_session` |
| 3 | `ab run … --shell` / `--agent X project` mis-parsed | **Fixed** — positionals as `nargs=*`, `--` passthrough split in `main()` | `25b06d7` · `test_cli.RunParsing` |
| 4 | `idle_seconds` wrong for lockdown | **Fixed** — also read the ephemeral per-session dir | `25b06d7` · `test_cli.IdleTest.test_idle_seconds_uses_ephemeral_dir_for_lockdown` |
| 5 | Backup excludes not escaped (glob metacharacters) | **Fixed** — escape `\ * ? [ ]` in both exclude kinds | `fc93853` · `test_backup_tokens.BackupExcludeEscaping` |
| 6 | `ab project rm web web` KeyErrors | **Fixed** — de-duplicate names | `25b06d7` · `test_cli.ProjectRmTest.test_rm_deduplicates_repeated_names` |
| 7 | Foreign-uid bind refusal blocks dev DB re-`up` | **Kept strict, documented** — dev DBs use a session-created named volume | `f86418b` · decisions.md + design.md |
| 8 | Backups exclude covered git/compose files | **Kept, documented restore** — `.git/config` can hold creds; restore without `--delete` | `f86418b` · decisions.md + design.md |
| 9 | Makefile `date -d` (macOS) | **No change** — GNU date on this host parses it; macOS not a target | — |
| 10 | Duplicated git-identity lookup | **Deferred** — cleanup, noted in TECH-DEBT | TECH-DEBT.md |

Also: the agentbox-claude image predates the CLI-version label, so `ab selftest` shows one `SKIP`; rebuild with `make claude-image` before the real project.
