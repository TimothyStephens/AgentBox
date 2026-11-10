# Spec: per-project masked paths in agentbox, and masking `data/` for life-updates

> **Status: IMPLEMENTED on 10 Nov 2026 as the `mask` project setting** (`ab project add/set --mask PATH`; `docs/decisions.md`, 10 Nov; `docs/design.md`, "The project's own masks"). The key is `mask`, not `masked_paths`; a bare name masks every file or directory of that name in the grants, `./data` masks that one path. The rest of this file is the spec it was built from and its measurements; §5.1's in-session errno probe is not an automated test yet.

**Status:** implementation spec, for someone with access to the agentbox source.
**Why this file is in this repo:** agentbox's source and per-project config are not
mounted inside an agentbox session, so the agent that wrote this could measure the
sandbox's behaviour but could not change it. This repo was the only writable place.
Move it to agentbox's own docs when you apply it.

---

## 1. Requirement

Add a per-project **`masked_paths`** setting that applies the same
"empty, read-only cover" treatment agentbox already gives secret files
(`.env`, `.env.*`, `*.pem`, `*.key`, `secrets/`) to **arbitrary paths, including
directories**, named per project.

Then add `data/` to life-updates' masks.

Motivation: life-updates keeps its **live** SQLite database at `data/app.db`
inside the repo, and the repo is mounted read-write into agent sessions. So
every agent session has had full read access to the production database.

## 2. What the exposure actually was (measured)

Measured from inside a session on 2026-10-05, uid 1001:

| Probe | Result |
|---|---|
| `stat data/` , `stat data/app.db` | `uid=999 gid=999`, modes `drwxr-xr-x` / `-rw-r--r--` |
| `touch data/.probe` | `Permission denied` |
| `open("data/app.db", "r+b")` | `Permission denied` |
| `sqlite3` read via `?immutable=1` | **succeeded** |

Contents readable: **4,814 items** with real titles, plus the `chat_messages`,
`chat_session_items` and `processed_emails` tables — i.e. real LLM conversation
history and real email UIDs.

Via the session's **direct filesystem access** this is read-only, because the
directory is owned by uid 999 (the live container's user) and the session runs as
uid 1001. That is incidental protection from host file ownership, not something
agentbox arranged.

**But it is not read-only overall.** Via `/run/agentbox/podman.sock`, a session
can bind the project into a container it starts and run that container as root,
which bypasses both the ownership and any session-namespace mount:

```
$ podman run --rm --user root -v "$PWD":/src:ro --network none IMG python -c \
    "import sqlite3;c=sqlite3.connect('file:/src/data/app.db?immutable=1',uri=True);
     print(c.execute('SELECT COUNT(*) FROM items').fetchone())"
  items readable from inside a container: 4814
  chat_messages readable: 4

$ podman run --rm --user root -v "$PWD":/src --network none IMG sh -c 'touch /src/data/.probe'
  -> data/ WRITABLE as root in container
```

So the live database is **readable and writable** from a session. Integrity was
at risk too, just not by the path you would check first. Both properties need to
be guaranteed by the mask, and §6.2 is the part that actually guarantees them.

## 3. Two corrections to the original framing

### 3.1 `.env` is not an example of the cover you want

The request said "same empty read-only cover as `.env` masks". In a project that
has a `.env.dev`, `.env` is **not** an empty read-only cover:

```
.env      6890 bytes  md5 57228e9ac7628848314aebfd9721f900
.env.dev  6890 bytes  md5 57228e9ac7628848314aebfd9721f900
/dev/mapper/... /home/vmuser/.../life-updates/.env ext4 rw,relatime
```

Byte-identical to `.env.dev`, mounted **rw**, and `open(".env", "a")` succeeds.
That is the documented `.env.dev`-substitution path, not the masking path.

This repo contains no `*.pem`, `*.key` or `secrets/`, so **a session on this
project contains no instance of the empty mask at all.** Nothing below was
derived by observing it. Check §4 against how masking is actually implemented
before relying on it.

### 3.2 The existing read-only directory cover fails with EROFS, not EACCES

agentbox already mounts a directory read-only — `.git/hooks`:

```
/dev/mapper/... /home/vmuser/.../life-updates/.git/hooks ext4 ro,relatime
$ touch .git/hooks/.probe
touch: cannot touch '.git/hooks/.probe': Read-only file system
```

So the mechanism for the read-only half already exists (a `ro` bind mount). The
observable errno was measured against it, **in a session** (not in a container —
see the warning below):

| Operation on a `ro`-bind directory | errno |
|---|---|
| `touch mask/probe` | **EROFS** (30) |
| `mask/sub`.mkdir() | **EROFS** (30) |
| `mask`.rmdir() | **EBUSY** (16) — it is a mount point, not merely read-only |

The §5 tests assert exactly these. If you implement the empty cover with `tmpfs`
rather than a `ro` bind, re-measure: `EROFS` is a property of the mount flag, and
a writable `tmpfs` would give no error at all.

> **The errno depends on where the test runs.** The same three operations inside
> a container the session spawned return **EACCES (13)**, because the session's
> `ro` bind is *not present in the container's mount table* and only file
> ownership applies there. A test that runs in a container therefore asserts the
> wrong thing and, worse, passes for the wrong reason. §5.1 must run in the
> session. This is the same root cause as §6.2.

`.git/hooks` is a read-only *passthrough* (the real hooks are visible), so it is
only evidence about the read-only half, not the empty half.

## 4. Behaviour contract for a masked path

A path listed in `masked_paths` must, inside a session:

1. **Exist.** Tools that `stat` the path must not see ENOENT; masking is a cover,
   not a deletion. A missing path is a worse failure mode than an empty one
   because it changes "no data" into "misconfigured".
2. **Be empty.** A masked directory lists zero entries, including dotfiles. A
   masked file reads as zero bytes.
3. **Be read-only.** Create, write, rename, chmod and delete within it all fail.
   Attempting to create an entry must fail with `EROFS` (§3.2).
4. **Hide the real contents completely.** No path under the mask may expose the
   covered data, including via a dangling symlink, a hardlink, or a
   `..`-traversal out of a nested mount.
5. **Not be escapable from inside the session.** The agent runs rootless and
   cannot mount, so this should follow — but see §6 for the container case, which
   is where it actually needs attention.
6. **Leave siblings untouched.** Masking `data/` must not affect `dev/data/`.
   These are siblings, not nested — verified: the dev stack's bind is
   `./data:/devdata` *relative to `dev/`*, i.e. `dev/data`.
7. **Not affect the host.** The mask is session-local. The live `deploy` stack's
   `../data:/data:U` bind runs on the host, outside any session, and must keep
   seeing the real directory.

### Config surface

Per-project, alongside wherever the existing grants/egress settings live. Shape,
to be adapted to agentbox's own conventions and naming — do not take the key
names below as prescriptive:

```yaml
projects:
  life-updates:
    path: /home/vmuser/repos/Life_Updates/life-updates
    masked_paths:
      - data/            # live SQLite DB; dev/data is separate and stays visible
```

Points worth deciding explicitly, because each one is a place a later reader will
assume the opposite:

- **Relative to the project root**, not absolute, and reject absolute paths and
  any path containing `..` at config-load time rather than at mount time.
- **Trailing slash should not be load-bearing.** Accept `data` and `data/`
  identically; infer directory-vs-file from what is on disk.
- **A masked path that does not exist on the host.** Simplest correct behaviour is
  to create an empty cover anyway, so that adding a mask pre-emptively (before
  the directory exists) does not silently do nothing once it appears.
- **Interaction with the existing secret masks.** `masked_paths` should be
  additive, not a replacement — and if a path matches both, the result must still
  be a mask, not a `.env.dev`-style substitution.
- **Nested masks** (`data/` and `data/logs/`) should either be collapsed to the
  outermost or rejected; silently mounting one over the other is order-dependent.

## 5. Test plan

Two layers. The first is the one that was asked for.

### 5.1 In-session behaviour (the asked-for test)

Runs **in the session** — not in a container it spawns, which asserts the wrong
errno for the wrong reason (§3.2) — on a project configured with
`masked_paths: [data/]`, where the host directory is non-empty before the session
starts. Asserts the §4 contract. Written as pytest for readability; port to
agentbox's framework.

```python
import errno, os, pathlib, pytest

MASK = pathlib.Path("data")          # masked_paths entry
VISIBLE = pathlib.Path("dev/data")   # sibling that must stay visible

def test_masked_directory_exists():
    # A cover, not a deletion: ENOENT would turn "no data" into "misconfigured".
    assert MASK.is_dir()

def test_masked_directory_is_empty():
    # Dotfiles included - os.listdir, not a glob.
    assert os.listdir(MASK) == []

def test_masked_directory_hides_the_real_contents():
    # The specific file this mask exists for.
    assert not (MASK / "app.db").exists()

def test_cannot_create_in_a_masked_directory():
    with pytest.raises(OSError) as exc:
        (MASK / "probe").touch()
    assert exc.value.errno == errno.EROFS   # see spec 3.2

def test_cannot_mkdir_in_a_masked_directory():
    with pytest.raises(OSError) as exc:
        (MASK / "sub").mkdir()
    assert exc.value.errno == errno.EROFS

def test_cannot_rmdir_the_mask_itself():
    with pytest.raises(OSError) as exc:
        MASK.rmdir()
    # EBUSY, not EROFS: the mask is a mount point. Measured, not assumed.
    assert exc.value.errno == errno.EBUSY

def test_a_sibling_path_is_unaffected():
    # dev/data is a sibling of data/, not nested under it.
    VISIBLE.mkdir(parents=True, exist_ok=True)
    probe = VISIBLE / "probe"
    probe.touch()
    assert probe.exists()
    probe.unlink()

def test_no_traversal_out_of_the_mask():
    # Guards against a nested-mount or symlink shape that re-exposes the data.
    assert not (MASK / ".." / "data" / "app.db").exists()
```

Also assert the application-level failure mode, because it is **not** a
permission error and anyone debugging it will expect one. `app/db.py:connect()`
runs `Path(db_path).parent.mkdir(parents=True, exist_ok=True)` before
`sqlite3.connect`, so under a masked `data/` the app fails at `sqlite3.connect`
with `unable to open database file`:

```python
def test_the_app_fails_cleanly_rather_than_misleadingly():
    import sqlite3
    from app.db import connect
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        connect(str(MASK / "app.db"))
```

### 5.2 The bypass test (do not ship the feature without this)

§5.1 passes today against a mask that is trivially bypassable, so it is not on
its own evidence the feature works. This asserts §6.2. It needs the podman
socket, so it belongs wherever agentbox's container-gate tests live.

```python
def test_a_container_bind_cannot_reach_a_masked_path():
    # The project root is an allowed bind source, and data/ is inside it.
    # Without a gate-level overlap check this read succeeds, defeating the mask.
    result = run_in_session(
        "podman run --rm --user root -v $PWD:/src:ro --network none "
        "IMG sh -c 'cat /src/data/app.db | head -c 16'"
    )
    assert result.returncode != 0
    assert "masked" in result.stderr.lower()

def test_a_container_bind_of_a_masked_path_directly_is_rejected():
    result = run_in_session("podman run --rm -v $PWD/data:/d --network none IMG true")
    assert result.returncode != 0

@pytest.mark.parametrize("src", ["./data", "data/../data", "$PWD/./data/"])
def test_overlap_check_is_canonicalizing(src):
    # Equivalent spellings of the same path must all be rejected.
    assert run_in_session(f"podman run --rm -v {src}:/d --network none IMG true").returncode != 0

def test_a_sibling_bind_is_still_allowed():
    # dev/data is not masked; this project's own dev stack binds it.
    assert run_in_session(
        "podman run --rm -v $PWD/dev/data:/d --network none IMG true"
    ).returncode == 0
```

### 5.3 Host-side verification (worth having, outside the asked scope)

The in-session tests cannot prove the mask was *effective*, only that the session
sees an empty directory — an identical result if the config silently did nothing
and the host directory happened to be empty. So, host-side:

- Seed the host directory with a known sentinel file before starting the session.
- Assert the in-session listing is empty **while that sentinel exists on the host**.
- After the session ends, assert the sentinel is still present and unmodified
  (mtime and checksum), i.e. the mask did not pass writes through or clobber.

Without the sentinel, 5.1 passes vacuously against a broken implementation.

## 6. Two gaps this should probably also close

### 6.1 `.git/config` is writable, contrary to the sandbox's own documentation

The session CLAUDE.md states "`.git/hooks` and `.git/config` are read-only."
`.git/hooks` is. `.git/config` is not:

```
$ grep -c 'git/config' /proc/mounts
0
$ stat -c '%n uid=%u mode=%A' .git/config
.git/config uid=1001 mode=-rw-r--r--
$ printf '' >> .git/config   # succeeds
```

It is owned by the session user with mode 644 and no mount entry. This matters
beyond the documentation being wrong: `.git/config` can set `core.hooksPath`,
which redirects hook lookup away from the read-only `.git/hooks` to any writable
directory — so the `.git/hooks` protection is bypassable by editing a file the
same sentence claims is protected. It can also set `credential.helper` and
rewrite remote URLs.

`masked_paths` is the wrong tool for this one (the config needs to stay readable
for git to work), but it is the same review. Either make it genuinely read-only
or correct the documentation.

### 6.2 CONFIRMED BLOCKER: session mounts do not reach spawned containers

**A `masked_paths` implementation built only as a session-namespace mount will be
fully bypassable, and will not close the exposure it was added for.**

A session can bind project directories into containers it starts via
`/run/agentbox/podman.sock`; the gate allows "binds only inside the project's own
directories", and the project root qualifies. Measured against the existing
`ro`-bind cover on `.git/hooks`:

```
$ podman run --rm --user root -v "$PWD":/src --network none IMG sh -c '
    grep "git/hooks" /proc/mounts || echo "session ro-bind NOT present in container"
    touch /src/.git/hooks/.probe'

  -> session ro-bind NOT present in container mount table
  -> WRITE SUCCEEDED through a container bind
```

The session's read-only cover is simply absent inside the container, and a root
container writes through it. The same applies to the live database (§2): read and
write, both confirmed. So:

- The mask must be enforced **at the podman gate**, not only in the session's
  mount namespace. Reject any `-v`/`--mount` whose source resolves to, or
  contains, a masked path — resolving symlinks first, and comparing canonical
  paths so `./data`, `data/../data` and an absolute path are all caught.
- Rejecting the **project root** as a bind source is not acceptable as a blunt
  fix here: this project's own dev stack legitimately binds `dev/`
  subdirectories into containers (`dev/compose.dev.yaml`). The check needs to be
  overlap-based, and must still allow a sibling like `dev/data`.
- Re-applying the empty cover inside spawned containers is the alternative, but
  it is strictly harder to get right (it has to hold for `podman build` and
  compose-created containers too), and a rejected bind fails loudly whereas a
  missed cover fails silently. Prefer rejection.
- `--user root` is worth noting independently: it defeats the uid-based
  protection that currently, incidentally, keeps `data/app.db` read-only.

## 7. Compatibility check for life-updates

Masking `data/` is safe for this project. Everything that references it:

| Reference | Effect of the mask |
|---|---|
| `deploy/compose.yaml` ×2: `../data:/data:U` | **None** — runs on the host, outside any session. |
| `app/config.py`: `db_path = "/data/app.db"` | **None** — a *container* path, not the repo directory. |
| `dev/compose.dev.yaml`: `./data:/devdata` | **None** — resolves to `dev/data`, a sibling. |
| `tests/` | **None** — the suite uses `:memory:` and tempfiles; verified 280 passed with no access to `data/`. |

No code reads the repo-level `data/` directory. The only loss is the agent's
ability to read the live database, which is the point.

---

### Appendix: reproducing the measurements

All figures above came from inside an agentbox session on life-updates:

```sh
stat -c '%n uid=%u gid=%g mode=%A' data data/app.db
touch data/.probe                                    # expect: Permission denied
python3 -c "open('data/app.db','r+b')"               # expect: Permission denied
python3 -c "import sqlite3;c=sqlite3.connect('file:data/app.db?immutable=1',uri=True);print(c.execute('SELECT COUNT(*) FROM items').fetchone())"
grep -E 'agentbox|life-updates' /proc/mounts         # shows which covers are mounts
md5sum .env .env.dev                                 # equal when .env.dev exists
touch .git/hooks/.probe                              # expect: Read-only file system
```

Container-bypass probes (§6.2), from the same session:

```sh
# session's ro cover is absent inside a container it spawns, and root writes through
podman run --rm --user root -v "$PWD":/src --network none IMG sh -c '
  grep "git/hooks" /proc/mounts || echo "ro-bind NOT in container"
  touch /src/.git/hooks/.probe && echo WROTE && rm -f /src/.git/hooks/.probe'

# the live DB, read and written from a container
podman run --rm --user root -v "$PWD":/src:ro --network none IMG python -c \
  "import sqlite3;print(sqlite3.connect('file:/src/data/app.db?immutable=1',uri=True).execute('SELECT COUNT(*) FROM items').fetchone())"
podman run --rm --user root -v "$PWD":/src --network none IMG sh -c 'touch /src/data/.probe && rm -f /src/data/.probe'
```

All probe files created while measuring were removed; `git status` was clean
afterwards and `md5sum .env` was unchanged.
