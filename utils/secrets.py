"""Recognising a secret in free text, and taking it back out.

The patterns lived in `memory/facts.py`, which used them to refuse to *store* a
fact containing a credential. Cards need the same recognition for a different
purpose: a card can now carry work north filled in, those values are written to
SQLite and audit-logged permanently, and a filled form is exactly where an API
key or a password ends up.

Refusing is not an option there - the card is the thing the user has to read
before deciding - so this module also redacts, leaving the field in place with
its value removed.
"""

from __future__ import annotations

import re

# Secret detection patterns.
SECRET_RE = re.compile(
    r"""(?ix)
    (?:^|[\s\W])
    (?:
        (?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|bearer[_-]?token)
        |(?:password|passwd|pwd)
        |(?:private[_-]?key|ssh[_-]?key)
        |(?:aws[_-]?access[_-]?key|aws[_-]?secret[_-]?key)
        |(?:github[_-]?token|gh[_-]?token|ghp_)
        |(?:slack[_-]?token|xox[baprs]-)
        |(?:stripe[_-]?key|sk_live_|pk_live_)
        |(?:jwt[_-]?token|eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*)
        |(?:credit[_-]?card|cc[_-]?num)
        |(?:seed[_-]?phrase|mnemonic)
    )
    [\s:=]+
    [A-Za-z0-9_\-+/=]{8,}
    """,
)

CC_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

# Credentials that are recognisable on their own, with no `key = ` in front of
# them. SECRET_RE needs a label followed by a value, so a bare `ghp_...` pasted
# into a field slipped straight through it - which for a card is a live token in
# the audit log.
TOKEN_RE = re.compile(
    r"""(?x)
    \b(?:
        # Prefixed tokens: a known marker followed by the secret itself.
        (?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_     # GitHub
          |sk-|sk_live_|sk_test_|pk_live_           # OpenAI, Stripe
          |xox[baprs]-                              # Slack
        )[A-Za-z0-9_\-]{8,}
        # Self-contained shapes: the whole thing is the identifier.
        |AKIA[0-9A-Z]{16}                           # AWS access key id
        |AIza[0-9A-Za-z_\-]{35}                     # Google API key
    )
    """
)

# Shapes that commonly enter logs through HTTP client exceptions. These need
# dedicated handling because the credential is embedded in transport syntax,
# not introduced by a human-readable ``api_key =`` label.
_TELEGRAM_BOT_URL_RE = re.compile(r"(?i)(https?://api\.telegram\.org/bot)[^/\s?\"']+")
_URL_CREDENTIAL_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{6,}")
_AUTH_HEADER_RE = re.compile(
    r"(?ix)(\b(?:authorization|proxy[-_]?authorization)\b[\"']?\s*[:=]\s*)"
    r"(?:(?:bearer|basic)\s+)?(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)"
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?ix)([?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|pwd|secret|client[_-]?secret)=)[^&#\s\"']+"
)
# A fenced block of code, and its language.
_FENCE_RE = re.compile(r"(```[ \t]*([\w+-]*)[^\n]*\n)(.*?)(\n```)", re.DOTALL)
# Languages where a value without quotes is code - a name, a call, a type - and never a literal:
# `api_key: str`, `token = os.environ["KEY"]`. In YAML or a shell, `password: hunter2` is the password.
_EXPRESSION_LANGUAGES = frozenset({"python", "py", "json", "javascript", "js", "typescript", "ts", "tsx", "jsx"})
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        \b(?:authorization|proxy[-_]?authorization|api[-_]?key|apikey|secret[-_]?key|
        access[-_]?token|auth[-_]?token|bearer[-_]?token|password|passwd|pwd|
        private[-_]?key|client[-_]?secret|github[-_]?token|telegram[-_]?bot[-_]?token|
        cookie|set[-_]?cookie|credential)
        \b["']?\s*[:=]\s*
    )
    (?:(?P<quote>["'])(?P<quoted>[^"'\r\n]*)(?P=quote)|(?P<plain>[^\s,;}\]]+))
    """
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)

# Structured log fields are safer to classify from their key than from their
# value. Keep this narrower than ``is_secret_field_name``: metrics such as
# ``tokens_in`` and benign fields such as ``pinned_model`` must remain useful.
_LOG_SECRET_KEY_RE = re.compile(
    r"(?ix)^(?:authorization|proxy[-_]?authorization|cookie|set[-_]?cookie|credential|"
    r"api[-_]?key|apikey|password|passwd|pwd|private[-_]?key|secret|secret[-_]?key|"
    r"client[-_]?secret|token|access[-_]?token|auth[-_]?token|bearer[-_]?token|"
    r"github[-_]?token|telegram[-_]?bot[-_]?token)$"
)

REDACTED = "[redacted]"

# Field names whose value is a secret whatever it looks like. A password of
# "hunter2" matches no pattern; the label is the evidence.
_SECRET_FIELD_NAMES = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|apikey|credential|private[_-]?key|ssn|cvv|pin)"
)


# A word in prose that names a secret: what follows it may be one, whatever it looks like.
_SECRET_WORD_RE = re.compile(
    r"(?i)\b(?:password|passwd|passcode|pwd|pin|secret|token|api[ _-]?key|credential|private[ _-]?key|ssn|cvv)s?\b"
)


def until_secret(text: str) -> str:
    """*text* up to the first word that names a secret, marked as cut; all of it when none does.

    For where only the start of a message is shown, such as a session's title.
    """
    match = _SECRET_WORD_RE.search(text)
    return f"{text[: match.end()]}…" if match and match.end() < len(text) else text


def contains_secret(text: str) -> bool:
    """Whether *text* looks like it carries a credential or a card number."""
    return bool(SECRET_RE.search(text) or TOKEN_RE.search(text) or CC_RE.search(text))


def is_secret_field_name(name: str) -> bool:
    """Whether a field with this name holds a secret regardless of its value."""
    return bool(_SECRET_FIELD_NAMES.search(name))


def redact(text: str) -> str:
    """*text* with anything that looks like a secret replaced.

    Keeps the surrounding text: "the api_key is abc123def" reads as
    "the [redacted]", which still tells a person what the sentence was about
    while leaving nothing to steal.

    In a fenced block of Python, JavaScript, TypeScript or JSON, only literal values
    are taken out: there `api_key: str` and `f"Bearer {token}"` are code that reads a
    secret, and redacting them made a tool's code on its approval card unreadable.
    """
    parts: list[str] = []
    end = 0
    for fence in _FENCE_RE.finditer(text):
        opening, language, body, closing = fence.groups()
        parts += [_redact(text[end : fence.start()], code=False), opening]
        parts += [_redact(body, code=language.lower() in _EXPRESSION_LANGUAGES), closing]
        end = fence.end()
    return "".join([*parts, _redact(text[end:], code=False)])


def _redact(text: str, *, code: bool) -> str:
    """*text* with its secrets replaced; with *code*, a value that is not a literal stays, as code."""

    def assignment(match: re.Match[str]) -> str:
        expression = match.group("plain") is not None or "{" in (match.group("quoted") or "")
        # Without its closing bracket: the plain-value form of the pattern stops before a `]`.
        if REDACTED[:-1] in match.group(0).lower() or (code and expression):
            return match.group(0)
        quote = match.group("quote") or ""
        return f"{match.group('prefix')}{quote}{REDACTED}{quote}"

    def header(match: re.Match[str]) -> str:
        value = match.group(0)[len(match.group(1)) :]
        return match.group(0) if code and not value.startswith(("'", '"')) else f"{match.group(1)}{REDACTED}"

    def query(match: re.Match[str]) -> str:
        return match.group(0) if code and "{" in match.group(0) else f"{match.group(1)}{REDACTED}"

    text = _PRIVATE_KEY_RE.sub(REDACTED, text)
    text = _TELEGRAM_BOT_URL_RE.sub(r"\1[redacted]", text)
    text = _URL_CREDENTIAL_RE.sub(r"\1[redacted]\2", text)
    text = _SENSITIVE_QUERY_RE.sub(query, text)
    text = _AUTH_HEADER_RE.sub(header, text)
    text = _BEARER_RE.sub(r"\1\2[redacted]", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(assignment, text)
    if not code:  # a label followed by a bare value: in code, that value is a name
        text = SECRET_RE.sub(REDACTED, text)
    return CC_RE.sub(REDACTED, TOKEN_RE.sub(REDACTED, text))


_URL_QUERY_RE = re.compile(r"(?i)(\bhttps?://[^\s?#\"'\\]+\?)([^\s#\"'\\]+)")


def blank_query_values(text: str) -> str:
    """*text* with every URL's query values removed and their names kept. For logs, where a value is never needed.

    A list of names known to be secret misses a tool's `appid=` or `key=`.
    """
    return _URL_QUERY_RE.sub(lambda match: match[1] + re.sub(r"=[^&]*", f"={REDACTED}", match[2]), text)


def redact_for_logging(value: object, *, key: str = "") -> object:
    """Return a recursively redacted, JSON-serialisable view of log data.

    Logging accepts arbitrary values through ``extra``. Sanitising only the
    rendered message misses dictionaries and lists, while stringifying first
    loses the field name that most reliably identifies a secret. This helper
    preserves ordinary diagnostic structure and replaces only secret values.
    """
    if key and _LOG_SECRET_KEY_RE.fullmatch(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(item_key): redact_for_logging(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [redact_for_logging(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_for_logging(item) for item in value)
    if isinstance(value, set):
        return sorted((redact_for_logging(item) for item in value), key=str)
    if isinstance(value, str):
        return redact(value)
    return value
