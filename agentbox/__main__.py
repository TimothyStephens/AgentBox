import sys

if sys.version_info < (3, 11):
    sys.exit("agentbox needs Python 3.11+ (tomllib)")

from .cli import main  # noqa: E402

sys.exit(main())
