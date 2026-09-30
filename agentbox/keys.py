"""Provider API keys: one 0600 file per provider under <config>/keys/, set with `ab keys set <provider>`.

A key enters a session exactly like the Claude token: the file is bind-mounted read-only under
/run/agentbox/keys/ and the session wrapper exports it as AB_KEY_<PROVIDER> just before exec, so it is never
on a command line or in podman's container config. The generated model config names the variable, not the
value. A provider whose domain is not known here must give a `baseUrl` in the model config.
"""
import getpass
import os
import re
import sys

from .errors import AgentboxError
from .tokens import check_token_file

PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
CONTAINER_DIR = "/run/agentbox/keys"

# API domains per provider name, allowed through the proxy in every egress mode when the provider has a key.
# A provider's `baseUrl` host is allowed too (docs/decisions.md lists these).
PROVIDER_DOMAINS = {
    "anthropic": ["api.anthropic.com"],
    "openai": ["api.openai.com"],
    "gemini": ["generativelanguage.googleapis.com"],
    "google": ["generativelanguage.googleapis.com"],
    "openrouter": ["openrouter.ai"],
    "mistral": ["api.mistral.ai"],
    "groq": ["api.groq.com"],
    "deepseek": ["api.deepseek.com"],
    "xai": ["api.x.ai"],
}


def check_provider(name):
    if not PROVIDER.match(name or ""):
        raise AgentboxError(f"provider name {name!r}: use lower-case letters, digits, '-' or '_' (max 40)")
    return name


def keys_dir(settings):
    return os.path.expanduser(settings.values.get("keys_dir") or "") or os.path.join(settings.config_dir, "keys")


def key_file(settings, provider):
    return os.path.join(keys_dir(settings), check_provider(provider))


def env_name(provider):
    return "AB_KEY_" + check_provider(provider).upper().replace("-", "_")


def present(settings):
    """{provider: path} for every key file. A file that fails the checks (mode, owner, symlink, empty) is an
    error, not silently skipped: a key you set but that a session ignores is worse than a refusal."""
    d = keys_dir(settings)
    if not os.path.isdir(d):
        return {}
    out = {}
    for fn in sorted(os.listdir(d)):
        if fn.startswith(".") or fn.endswith(".new"):
            continue
        if not PROVIDER.match(fn):
            raise AgentboxError(f"{os.path.join(d, fn)}: not a provider name; remove it or rename it")
        out[fn] = check_token_file(os.path.join(d, fn))
    return out


WARNING = (
    "Pay-per-use API keys can run up a bill. Use a key made only for agentbox, with a spending cap set at the\n"
    "provider, so a runaway or misdirected agent is bounded by the cap and the key can be revoked alone.\n"
    "The agent can read this key inside its sandbox (there is no key gateway yet).")


def set_key(settings, provider, read=getpass.getpass, out=None):
    out = out or sys.stderr
    path = key_file(settings, provider)
    print(f"Set the API key for provider {provider!r}.\n\n{WARNING}\n", file=out)
    key = read("key (input is hidden): ").strip()
    if not key or any(c.isspace() for c in key):
        raise AgentboxError("no key given (or it contains whitespace); nothing changed")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(path), 0o700)
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    print(f"saved {path}; new sessions use it, running sessions keep the old one until restarted", file=out)
    if provider not in PROVIDER_DOMAINS:
        print(f"note: {provider!r} has no known API domain; its entry in the model config needs a baseUrl",
              file=out)


def remove(settings, provider, out=None):
    out = out or sys.stderr
    path = key_file(settings, provider)
    if not os.path.lexists(path):
        raise AgentboxError(f"no key for {provider}")
    os.unlink(path)
    print(f"removed {path}; revoke the key at the provider too", file=out)
