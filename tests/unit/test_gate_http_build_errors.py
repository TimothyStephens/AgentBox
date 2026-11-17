"""docs/TODO.md §4.3, rows E9 and E10: the gate's HTTP framing and build-context error paths. Each test pins
the fail-closed answer: the request is refused with an error status, nothing reaches podman, the build is not
started. Already covered elsewhere and not repeated here (test_review_step5.py): Transfer-Encoding twice
(Item10RequestHead), one oversized chunk on a plain body (Item43ClientLimits), bytes after a body
(Framing), a dockerfile outside the context (Images.test_build), member-name spellings
(BuildContextMemberNames)."""
import io
import os
import shutil
import socket
import tarfile
import tempfile
import threading
import unittest
import unittest.mock

from agentbox import gate, gate_http
from tests.unit import test_review_step5 as s5      # the module, not its TestCases: importing those reruns them

BUILD = "/v5/libpod/build?t=localhost/abt-x:1"
CREATE = "/v5/libpod/containers/create"


def watched_upstream(test):
    """A fake podman socket that only records whether the gate ever connected to it."""
    def serve(conn, result):
        result["connected"] = True
        conn.settimeout(1.0)
        try:
            result["got"] = conn.recv(65536)
        except OSError:
            result["got"] = b""
    return s5.Upstream(test, serve)


def exchange(test, g, data, up=None, shut=True):
    """Send `data` to the gate's handler, half-close (unless `shut` is false), and return everything it
    answers. The handler must have finished within 10 s."""
    a, t = s5.run_handler(test, g, up)
    a.sendall(data)
    if shut:
        a.shutdown(socket.SHUT_WR)
    t.join(10)
    test.assertFalse(t.is_alive(), "the handler is still waiting")
    a.settimeout(5)
    reply = b""
    while True:
        chunk = a.recv(65536)
        if not chunk:
            return reply
        reply += chunk


class GateCase(unittest.TestCase):
    def assertRefused(self, reply, *words, status=b"403"):
        self.assertTrue(reply.startswith(b"HTTP/1.1 " + status + b" "), reply[:160])
        self.assertIn(b"Connection: close", reply)
        for w in words:
            self.assertIn(w.encode(), reply)

    def assertUpstreamUntouched(self, up):
        up.thread.join(0.5)
        self.assertNotIn("connected", up.result, "the gate connected to podman")


# ---- E9: request framing --------------------------------------------------------------------------------------

class AmbiguousFraming(GateCase):
    def test_content_length_with_transfer_encoding_refused_on_any_endpoint(self):
        body = b"5\r\nhello\r\n0\r\n\r\n"
        for path in (CREATE, BUILD):
            for hdrs in (b"Content-Length: 5\r\nTransfer-Encoding: chunked\r\n",
                         b"Transfer-Encoding: chunked\r\nContent-Length: 5\r\n",
                         b"content-length: 18\r\ntransfer-encoding: CHUNKED\r\n"):
                with self.subTest(path=path, hdrs=hdrs):
                    up = watched_upstream(self)
                    reply = exchange(self, s5.Fake(), f"POST {path} HTTP/1.1\r\nHost: d\r\n".encode() + hdrs
                                     + b"\r\n" + body, up)
                    self.assertRefused(reply, "ambiguous request framing")
                    self.assertUpstreamUntouched(up)

    def test_two_content_lengths_refused_even_when_equal(self):
        for path in (CREATE, BUILD):
            with self.subTest(path=path):
                up = watched_upstream(self)
                reply = exchange(self, s5.Fake(), f"POST {path} HTTP/1.1\r\nHost: d\r\n".encode()
                                 + b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}", up)
                self.assertRefused(reply, "ambiguous request framing")
                self.assertUpstreamUntouched(up)

    def test_an_unsupported_transfer_encoding_refused(self):
        for te in ("gzip", "gzip, chunked", "chunked, gzip", "identity", "compress", "chunked;x=1"):
            for path in (CREATE, BUILD):
                with self.subTest(te=te, path=path):
                    up = watched_upstream(self)
                    reply = exchange(self, s5.Fake(), f"POST {path} HTTP/1.1\r\nHost: d\r\n"
                                     f"Transfer-Encoding: {te}\r\n\r\n2\r\n{{}}\r\n0\r\n\r\n".encode(), up)
                    self.assertRefused(reply, "is not supported")
                    self.assertUpstreamUntouched(up)


class SizeLimits(GateCase):
    def test_a_head_over_the_limit_refused(self):
        """The real MAX_HEAD (1 MiB): a head that never ends is refused once it passes the limit."""
        up = watched_upstream(self)
        head = b"GET /v5/libpod/_ping HTTP/1.1\r\nX-Pad: "
        reply = exchange(self, s5.Fake(), head + b"a" * (gate_http.MAX_HEAD + 1 - len(head)), up, shut=False)
        self.assertRefused(reply, "request head too large")
        self.assertUpstreamUntouched(up)

    def test_a_body_over_the_limit_refused_from_its_content_length_alone(self):
        """No body byte is sent and the client does not close: the header alone is enough to refuse."""
        up = watched_upstream(self)
        reply = exchange(self, s5.Fake(), f"POST {CREATE} HTTP/1.1\r\nHost: d\r\n"
                         f"Content-Length: {gate_http.MAX_BODY + 1}\r\n\r\n".encode(), up, shut=False)
        self.assertRefused(reply, "request body too large")
        self.assertUpstreamUntouched(up)

    def test_a_chunked_body_over_the_limit_in_small_chunks_refused(self):
        """MAX_BODY patched to 8 bytes; two 5-byte chunks pass it only together."""
        up = watched_upstream(self)
        with unittest.mock.patch.object(gate_http, "MAX_BODY", 8):
            reply = exchange(self, s5.Fake(), f"POST {CREATE} HTTP/1.1\r\nHost: d\r\nTransfer-Encoding: chunked"
                             "\r\n\r\n5\r\n{\"a\":\r\n5\r\n 1}  \r\n0\r\n\r\n".encode(), up)
        self.assertRefused(reply, "request body too large")
        self.assertUpstreamUntouched(up)

    def test_spool_body_refuses_a_context_over_its_limit(self):
        """spool_body's `limit` (MAX_BUILD_BODY, 8 GiB, by default) passed as 4 bytes, both framings."""
        with self.assertRaises(gate.Deny) as e:
            gate.spool_body(s5.FakeSock([b"de"]), [("Content-Length", "5")], b"abc", io.BytesIO(), limit=4)
        self.assertIn("too large", str(e.exception))
        with self.assertRaises(gate.Deny) as e:
            gate.spool_body(s5.FakeSock([b"2\r\nde\r\n0\r\n\r\n"]), [("Transfer-Encoding", "chunked")],
                            b"3\r\nabc\r\n", io.BytesIO(), limit=4)
        self.assertIn("too large", str(e.exception))
        out = io.BytesIO()
        self.assertEqual(gate.spool_body(s5.FakeSock([]), [("Content-Length", "4")], b"abcd", out, limit=4), 4)

    def test_the_build_limit_is_8_gib(self):
        self.assertEqual(gate_http.MAX_BUILD_BODY, 8 << 30)
        self.assertEqual(gate.spool_body.__defaults__, (gate_http.MAX_BUILD_BODY,))

    def test_a_build_context_over_the_limit_refused_through_the_handler(self):
        """The handler's limit is spool_body's default; patched to 16 bytes, a 32-byte context is refused before
        the Containerfile is read or podman is reached."""
        up = watched_upstream(self)
        checked = []
        with unittest.mock.patch.object(gate_http.spool_body, "__defaults__", (16,)), \
                unittest.mock.patch.object(gate.Gate, "check_build_images", lambda *a: checked.append(a)):
            reply = exchange(self, s5.Fake(), f"POST {BUILD} HTTP/1.1\r\nHost: d\r\nContent-Length: 32\r\n\r\n"
                             .encode() + b"\0" * 32, up)
        self.assertRefused(reply, "the build context is too large")
        self.assertEqual(checked, [])
        self.assertUpstreamUntouched(up)

    def test_a_build_declaring_over_8_gib_that_ends_early_is_refused(self):
        """Content-Length: 8 GiB + 1 with a few bytes, then the client closes. The gate does not refuse on the
        header (it spools up to the limit, as for a real large context); the short body is refused."""
        up = watched_upstream(self)
        checked = []
        with unittest.mock.patch.object(gate.Gate, "check_build_images", lambda *a: checked.append(a)):
            reply = exchange(self, s5.Fake(), f"POST {BUILD} HTTP/1.1\r\nHost: d\r\n"
                             f"Content-Length: {gate_http.MAX_BUILD_BODY + 1}\r\n\r\n".encode() + b"\0" * 1024, up)
        self.assertRefused(reply, "connection closed mid-body")
        self.assertEqual(checked, [])
        self.assertUpstreamUntouched(up)

    # Found while writing these tests (17 Nov): spool_body buffered a whole chunk before the limit applied, so a
    # chunk declared at 8 GiB could take 8 GiB of memory first. It now refuses on the declared size and streams.
    def test_an_oversized_build_chunk_is_refused_before_it_is_read(self):
        sock = s5.FakeSock([b"x" * 250] * 4 + [b"\r\n0\r\n\r\n"])      # a whole, well-formed 1000-byte chunk
        with self.assertRaises(gate.Deny):
            gate.spool_body(sock, [("Transfer-Encoding", "chunked")], b"%x\r\n" % 1000, io.BytesIO(), limit=100)
        self.assertEqual(len(sock.chunks), 5, "the chunk was read before its size was checked")


    def test_a_chunked_context_split_across_reads_is_spooled_exactly(self):
        payload = bytes(range(256)) * 40
        wire = b"%x\r\n" % len(payload) + payload + b"\r\n" + b"3;ext=1\r\nabc\r\n0\r\nX-T: 1\r\n\r\n"
        pieces = [wire[i:i + 7] for i in range(0, len(wire), 7)]
        out = io.BytesIO()
        n = gate.spool_body(s5.FakeSock(pieces[1:]), [("Transfer-Encoding", "chunked")], pieces[0], out)
        self.assertEqual(out.getvalue(), payload + b"abc")
        self.assertEqual(n, len(payload) + 3)

    def test_a_chunk_without_its_crlf_is_refused(self):
        with self.assertRaises(ValueError):
            gate.spool_body(s5.FakeSock([b"abcXY0\r\n\r\n"]), [("Transfer-Encoding", "chunked")], b"3\r\n",
                            io.BytesIO())


# ---- E10: build-context errors --------------------------------------------------------------------------------

def write_tar(test, entries):
    """A build context: entries are (name, bytes) files or (name, TarInfo-type, linkname) others."""
    d = tempfile.mkdtemp(prefix="abt-unit-")
    test.addCleanup(shutil.rmtree, d)
    path = os.path.join(d, "ctx.tar")
    with tarfile.open(path, "w") as t:
        for e in entries:
            info = tarfile.TarInfo(e[0])
            if len(e) == 2:
                info.size = len(e[1])
                t.addfile(info, io.BytesIO(e[1]))
            else:
                info.type, info.linkname = e[1], e[2]
                t.addfile(info)
    return path


def raw_file(test, data):
    d = tempfile.mkdtemp(prefix="abt-unit-")
    test.addCleanup(shutil.rmtree, d)
    path = os.path.join(d, "ctx.tar")
    with open(path, "wb") as f:
        f.write(data)
    return path


class BuildContextErrors(GateCase):
    MINE, OLD, FORGED = s5.Images.MINE, s5.Images.OLD, s5.Images.FORGED
    gate = s5.Images.gate                    # the fixture only (localhost/live-app is a foreign image)
    Q = "/v5/libpod/build?dockerfile=%5B%22Containerfile%22%5D"

    def refused(self, path, target, *words):
        with self.assertRaises(gate.Deny) as e:
            self.gate().check_build_images(path, target)
        for w in words:
            self.assertIn(w, str(e.exception))

    def test_an_unreadable_dockerfile_parameter_refused(self):
        ctx = write_tar(self, [("Containerfile", b"FROM busybox\n")])
        for value, why in (("%5B", "not readable"),                         # [
                           ("%5B%22Containerfile%22", "not readable"),       # ["Containerfile"
                           ("%5B1%5D", "must be inside"),                    # [1]
                           ("%5Bnull%5D", "must be inside"),                 # [null]
                           ("%5B%5B%22Containerfile%22%5D%5D", "must be inside"),   # [["Containerfile"]]
                           ("%7B%22a%22%3A1%7D", "is not a file")):          # {"a":1}, taken as a name
            with self.subTest(value=value):
                self.refused(ctx, f"/v5/libpod/build?dockerfile={value}", why)

    def test_a_context_without_a_containerfile_refused(self):
        for entries in ([("app/x", b"1")], [], [("sub/Containerfile", b"FROM busybox\n")],
                        [("containerfile", b"FROM busybox\n")]):
            with self.subTest(entries=entries):
                self.refused(write_tar(self, entries), "/v5/libpod/build", "no Containerfile or Dockerfile")

    def test_a_named_dockerfile_missing_from_the_context_refused(self):
        self.refused(write_tar(self, [("Containerfile", b"FROM busybox\n")]),
                     "/v5/libpod/build?dockerfile=%5B%22Other%22%5D", "is not a file in the build context")

    def test_a_containerfile_that_is_not_a_regular_file_refused(self):
        real = ("real", b"FROM localhost/live-app:latest\n")
        for entries in ([("Containerfile", tarfile.DIRTYPE, "")],
                        [real, ("Containerfile", tarfile.SYMTYPE, "real")],
                        [real, ("Containerfile", tarfile.LNKTYPE, "real")],
                        [("Containerfile", tarfile.FIFOTYPE, "")]):
            for target in (self.Q, "/v5/libpod/build"):
                with self.subTest(kind=entries[-1][1], target=target):
                    path = write_tar(self, entries)
                    if target == self.Q:
                        self.refused(path, target, "is not a file in the build context")
                    else:                                 # no dockerfile named: a non-file is not picked either
                        self.refused(path, target)

    def test_a_context_that_is_not_a_tar_refused(self):
        with open(write_tar(self, [("Containerfile", b"FROM busybox\n" + b"#" * 2000)]), "rb") as f:
            good = f.read()
        for name, data in (("empty", b""), ("text", b"FROM busybox\n"), ("random", os.urandom(4096)),
                           ("cut in the first header", good[:100]), ("cut in the data", good[:1024]),
                           ("bad gzip", b"\x1f\x8b\x08\x00" + b"\xff" * 64)):
            with self.subTest(name):
                self.refused(raw_file(self, data), self.Q, "not a readable tar")

    def test_a_bad_context_through_the_handler_answers_403_and_never_reaches_podman(self):
        up = watched_upstream(self)
        body = b"this is not a tar archive"
        reply = exchange(self, self.gate(), f"POST {BUILD} HTTP/1.1\r\nHost: d\r\n"
                         f"Content-Length: {len(body)}\r\n\r\n".encode() + body, up)
        self.assertRefused(reply, "not a readable tar")
        self.assertUpstreamUntouched(up)

    def test_a_from_without_an_image_refused(self):
        for cf in ("FROM\n", "FROM \t\n", "FROM --platform=linux/amd64\n", "FROM alpine\nRUN true\nfrom\n"):
            with self.subTest(cf=cf):
                with self.assertRaises(gate.Deny) as e:
                    gate.dockerfile_images(cf)
                self.assertIn("FROM without an image", str(e.exception))
        self.refused(write_tar(self, [("Containerfile", b"FROM\n")]), self.Q, "FROM without an image")


class EscapeDirective(GateCase):
    """`# escape=` changes the line-continuation character, and so which lines are instructions. buildkit
    (frontend/dockerfile/parser/directives.go, DirectiveParser.ParseLine) and buildah's imagebuilder
    (dockerfile/parser/parser.go, possibleParserDirective) honour it only in the leading run of directive lines,
    after trimming leading whitespace (and a BOM); the first other line, blank line, comment or instruction
    ends that run."""

    LIVE = [("FROM", "alpine"), ("FROM", "localhost/live-app")]
    MINE, OLD, FORGED = s5.Images.MINE, s5.Images.OLD, s5.Images.FORGED
    gate = s5.Images.gate

    def test_a_leading_escape_directive_is_honoured(self):
        tick = "# escape=`\nFROM alpine\nRUN true \\\nFROM localhost/live-app\n"      # `\` no longer continues
        self.assertEqual(gate.dockerfile_images(tick), self.LIVE)
        self.assertEqual(gate.dockerfile_images("# syntax=docker/dockerfile:1\n" + tick), self.LIVE)
        self.assertEqual(gate.dockerfile_images("#ESCAPE = `\r\nFROM alpine\r\nRUN true \\\r\nFROM localhost/live-app\r\n"),
                         self.LIVE)
        # and the backtick does continue: the FROM line is RUN's argument, for buildkit as for the gate
        self.assertEqual(gate.dockerfile_images("# escape=`\nFROM alpine\nRUN true `\nFROM localhost/x\n"),
                         [("FROM", "alpine")])

    def test_an_image_after_a_backslash_under_escape_backtick_is_checked_in_a_build(self):
        g = self.gate()
        ctx = write_tar(self, [("Containerfile", b"# escape=`\nFROM alpine\nRUN true \\\nFROM localhost/live-app\n")])
        with self.assertRaises(gate.Deny) as e:
            g.check_build_images(ctx, BuildContextErrors.Q)
        self.assertIn("live-app", str(e.exception))

    # Found while writing these tests (17 Nov): the gate took `# escape=` from any of the first five lines, while
    # buildkit/buildah honour it only in the leading run of directive lines (after leading whitespace and a BOM).
    # A misplaced directive made "`" a continuation for the gate only, hiding the next FROM. Fixed in
    # gate_dockerfile.parser_escape.
    def test_an_escape_directive_after_an_instruction_is_ignored(self):
        self.assertEqual(gate.dockerfile_images("FROM alpine\n# escape=`\nRUN true `\nFROM localhost/live-app\n"),
                         self.LIVE)

    def test_an_escape_directive_after_a_plain_comment_is_ignored(self):
        self.assertEqual(gate.dockerfile_images("# hello\n# escape=`\nFROM alpine\nRUN true `\n"
                                                "FROM localhost/live-app\n"), self.LIVE)

    def test_an_escape_directive_after_a_blank_line_is_ignored(self):
        self.assertEqual(gate.dockerfile_images("\n# escape=`\nFROM alpine\nRUN true `\nFROM localhost/live-app\n"),
                         self.LIVE)

    # The other direction: buildkit trims leading whitespace and a BOM before it looks for a directive.
    def test_an_indented_escape_directive_is_honoured(self):
        self.assertEqual(gate.dockerfile_images("  # escape=`\nFROM alpine\nRUN true \\\nFROM localhost/live-app\n"),
                         self.LIVE)

    def test_an_escape_directive_after_a_bom_is_honoured(self):
        self.assertEqual(gate.dockerfile_images("\ufeff# escape=`\nFROM alpine\nRUN true \\\n"
                                                "FROM localhost/live-app\n"), self.LIVE)


    def test_an_unknown_directive_ends_the_directives(self):
        self.assertEqual(gate.dockerfile_images("# foo=bar\n# escape=`\nFROM alpine\nRUN true `\n"
                                                "FROM localhost/live-app\n"), self.LIVE)

    def test_a_syntax_directive_before_escape_keeps_the_run_going(self):
        self.assertEqual(gate.dockerfile_images("# syntax=docker/dockerfile:1\n# escape=`\nFROM alpine\n"
                                                "RUN true \\\nFROM localhost/live-app\n"), self.LIVE)

    def test_a_repeated_or_invalid_escape_is_refused(self):
        for text in ("# escape=`\n# escape=`\nFROM alpine\n", "# escape=x\nFROM alpine\n"):
            with self.subTest(text=text), self.assertRaises(gate.Deny):
                gate.dockerfile_images(text)

    def test_a_bom_does_not_hide_the_first_from(self):
        self.assertEqual(gate.dockerfile_images("\ufeffFROM localhost/live-app\n"),
                         [("FROM", "localhost/live-app")])


if __name__ == "__main__":
    unittest.main()
