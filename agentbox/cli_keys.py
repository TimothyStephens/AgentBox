"""`ab keys`: model tokens and provider API keys (split out of cli.py; the parser scaffolding stays there)."""
import os

from . import adapters, keys, tokens
from .errors import AgentboxError


def cmd_keys_set(s, a):
    if a.provider == "claude":
        raise AgentboxError("Claude Code uses a subscription token: `ab keys rotate claude`")
    keys.set_key(s, a.provider)


def cmd_keys_ls(s, a):
    found = keys.present(s)
    for provider in found:
        known = ", ".join(keys.PROVIDER_DOMAINS.get(provider, [])) or "domain from the model config's baseUrl"
        print(f"{provider:<16} {keys.env_name(provider):<24} {known}")
    tok = s.token_file("claude")
    print(f"{'claude (token)':<16} {'CLAUDE_CODE_OAUTH_TOKEN':<24} "
          f"{'set' if os.path.exists(tok) else 'not set'} ({tok})")


def cmd_keys_rm(s, a):
    keys.remove(s, a.provider)


def cmd_keys_rotate(s, a):
    if a.agent != "claude":
        raise AgentboxError("only `ab keys rotate claude` is supported")
    tokens.rotate(s.token_file(a.agent), a.agent)


def add_verbs(ks, add, ex):
    """The `ab keys` subparsers, built with the CLI's own `add` and `ex` helpers (as screen.add_verbs is)."""
    x = add(ks, "rotate", "replace the Claude subscription token (from `claude setup-token`)",
            ex("ab keys rotate claude"))
    x.add_argument("agent", choices=adapters.NAMES, help="the agent whose subscription token to replace (currently: claude)")
    x.set_defaults(fn=cmd_keys_rotate)
    x = add(ks, "set", "set a provider's API key (pay-per-use: make it dedicated and spend-capped)",
            ex("ab keys set openai"))
    x.add_argument("provider", help="the provider name; its key becomes AB_KEY_<PROVIDER> in sessions "
                                    "(you are prompted for the value, which is never on the command line)")
    x.set_defaults(fn=cmd_keys_set)
    add(ks, "ls", "list providers that have a key (names only, never values)",
        ex("ab keys ls")).set_defaults(fn=cmd_keys_ls)
    x = add(ks, "rm", "remove a provider's key file", ex("ab keys rm openai"))
    x.add_argument("provider", help="the provider whose key file to remove")
    x.set_defaults(fn=cmd_keys_rm)
