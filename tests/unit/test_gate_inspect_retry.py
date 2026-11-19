"""A podman 500 on a container lookup is retried before the gate gives up (19 Nov itest, test_step6_covers RODATA):
`podman run --rm` removes the container in the service while the client also sends DELETE; the gate's ownership
lookup for that DELETE hit podman mid-removal, got a 500, and the client printed the gate's error over the
container's output. A lookup that settles to 404 is "no such container", which podman ignores for --rm; one that
stays 500 still fails closed."""
import unittest
from unittest import mock

from agentbox import gate
from tests.unit.test_review_step5 import Fake, container, decide


def answers(*replies):
    seq = list(replies)
    calls = []

    def raw_json(path):
        calls.append(path)
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return raw_json, calls


@mock.patch.object(gate, "INSPECT_RETRY_DELAY", 0)
class InspectRetry(unittest.TestCase):
    def test_a_500_that_settles_to_404_is_no_such_container(self):
        g = Fake()
        g.raw_json, calls = answers((500, None), (404, None))
        with self.assertRaises(gate.Deny) as e:
            decide(g, "DELETE", "/v5/libpod/containers/abc?force=true")
        self.assertNotIsInstance(e.exception, gate.UpstreamError)
        self.assertEqual(e.exception.status, 404)
        self.assertIn("no such container", str(e.exception))
        self.assertEqual(len(calls), 2)

    def test_a_500_that_settles_to_the_object_is_used(self):
        g = Fake()
        doc = container("c1")
        g.raw_json, _calls = answers((500, None), (200, doc))
        self.assertEqual(g.inspect("containers", "c1"), doc)

    def test_a_500_that_persists_still_fails_closed(self):
        g = Fake()
        g.raw_json, calls = answers((500, None))
        with self.assertRaises(gate.UpstreamError) as e:
            decide(g, "POST", "/v5/libpod/containers/x/stop")
        self.assertEqual(e.exception.status, 502)
        self.assertEqual(len(calls), gate.INSPECT_TRIES)


if __name__ == "__main__":
    unittest.main()
