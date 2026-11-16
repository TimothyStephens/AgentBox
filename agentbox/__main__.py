import sys

if sys.version_info < (3, 11):  # noqa: UP036 -- the check is for an interpreter below the target
    sys.exit("agentbox needs Python 3.11+ (tomllib)")

from .cli import main  # noqa: E402

sys.exit(main())
