# Gate error-path tests (TODO §4.3), 17 Nov

**How.** Two fresh agents, each given TODO §4.3's rows and told to pin the current fail-closed behaviour in a
new test file, to mark anything that does not fail closed `expectedFailure` and report it rather than fix it,
and to check for existing coverage first. The fixer verified each finding and fixed it; each fix's test fails
on the code before it.

| Rows | File | Tests |
|---|---|---|
| E5, E6, E11, E12 | `tests/unit/test_gate_error_paths.py` | 22 |
| E9, E10 | `tests/unit/test_gate_http_build_errors.py` | 31 |

Skipped as already covered (`test_review_step5.py`): Transfer-Encoding twice, an oversized chunk on a plain
body, bytes after a body, a dockerfile outside the context, tar member spellings.

## Findings, all fixed

| # | Sev | Finding | Fix |
|---|---|---|---|
| G-1 | medium | `# escape=` was read from any of the first five lines; buildkit and buildah honour it only in the leading run of directive lines (after leading whitespace and a BOM). `FROM alpine` / `` # escape=` `` / ``RUN true ` `` / `FROM localhost/live-app` hid the second FROM from the image check while buildah built from it; an indented or BOM-prefixed directive the gate ignored did the same in reverse. A BOM before the first `FROM` also hid it. | `gate_dockerfile.parser_escape` reads directives as buildkit does: the leading run of `# syntax/escape/check=` lines, ended by anything else; a repeated or invalid escape is refused; a leading BOM is dropped |
| G-2 | low | `ignoreIfExists` on network create was refused only as `1`/`true`; podman also reads `t`, `T`, `TRUE`, `on` as true. The post-create check still refused a foreign network reported as created. | anything but a plain false (`""`, `0`, `f`, `false`) is refused |
| G-3 | low | `spool_body` held a whole build-context chunk in memory before the 8 GiB limit applied, and grew its buffer by concatenation (quadratic in a large chunk). | the chunk's declared size is checked first and its payload streamed to the spool file |
