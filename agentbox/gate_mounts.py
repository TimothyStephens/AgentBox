"""Reading /proc/<pid>/mountinfo: what the runtime actually mounted for a container the gate is about to
start. Split out of gate.py; gate.py re-exports every name here."""
import re


def parse_mountinfo_opts(text):
    """{mount point: (major:minor, root within that filesystem, [per-mount options])}."""
    def unescape(s):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 6:
            out[unescape(f[4])] = (f[2], unescape(f[3]), f[5].split(","))
    return out


def parse_mountinfo(text):
    """[(mount point, 'major:minor', root within that filesystem)] from a mountinfo file."""
    def unescape(s):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 5:
            out.append((unescape(f[4]), f[2], unescape(f[3])))
    return out
