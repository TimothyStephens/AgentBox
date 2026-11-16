"""What every `ab` noun module shares (cli.py and the cli_<noun>.py modules split out of it), kept in a leaf so
none of them imports cli.py back."""
import sys


def warn(msg):
    print(msg, file=sys.stderr)
