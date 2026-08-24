"""Access tokens for the built-in MCP server.

Kept out of ``settings.json`` deliberately. That file is documented as
hand-editable and is written on nearly every settings interaction; a bearer
token that grants read access to the whole media index does not belong in a
world-readable 0644 file next to the grid-column count.

Tokens live in their own ``mcp_tokens.json`` with 0600 permissions, written
the same way :meth:`Settings.save_app_password` writes its fallback file:
create the temp file with the restrictive mode *before* any secret bytes land
in it, then ``os.replace`` it into place so a crash mid-write cannot truncate
the existing file.

They are stored in clear text rather than hashed. A hash would mean the value
is shown exactly once and is unrecoverable afterwards — on a phone, where the
token has to be typed into a client on another machine, that turns a lost
clipboard into "generate a new one and update every client". The threat model
here is a token sitting in a file only the user can read, which is the same
one the Nextcloud app password already lives under.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG_DIR

LOGGER = logging.getLogger(__name__)

TOKENS_PATH = CONFIG_DIR / "mcp_tokens.json"

# Bytes of entropy per token. 32 bytes → a 43-character URL-safe string, well
# past anything brute-forceable over a network socket.
_TOKEN_BYTES = 32
# Prefix so a leaked string is recognisable as a Muga token in a log or a
# client config, and so a paste of the wrong secret is obvious at a glance.
_TOKEN_PREFIX = "muga_"


@dataclass
class Token:
    """One named bearer token."""

    id: str
    name: str
    token: str
    created: str = ""

    def masked(self) -> str:
        """The token with its middle removed — enough to tell two entries
        apart in the list without putting the full secret on screen."""
        body = self.token[len(_TOKEN_PREFIX):] if self.token.startswith(_TOKEN_PREFIX) else self.token
        if len(body) <= 8:
            return _TOKEN_PREFIX + "…"
        return f"{_TOKEN_PREFIX}{body[:4]}…{body[-4:]}"


@dataclass
class TokenStore:
    """The token list on disk. Load, mutate, save."""

    tokens: list[Token] = field(default_factory=list)
    path: Path = TOKENS_PATH

    @classmethod
    def load(cls, path: Path = TOKENS_PATH) -> "TokenStore":
        """Read the token file. A missing or damaged file yields an empty
        store rather than raising: the MCP page has to open either way, and
        an unreadable token file is recoverable by adding a new token."""
        store = cls(path=path)
        if not path.exists():
            return store
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Could not read %s — starting with no tokens", path, exc_info=True)
            return store
        raw = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return store
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            token = str(entry.get("token") or "")
            if not token:
                # An entry without a secret can never authenticate anything;
                # keeping it would only show a phantom row in Settings.
                continue
            store.tokens.append(Token(
                id=str(entry.get("id") or secrets.token_hex(8)),
                name=str(entry.get("name") or "Token"),
                token=token,
                created=str(entry.get("created") or ""),
            ))
        return store

    def save(self) -> bool:
        """Persist atomically at 0600. Returns False if it could not be
        written — callers surface that rather than pretending the token was
        stored and leaving the user with a client that cannot connect."""
        try:
            parent = self.path.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                parent.chmod(0o700)
            except OSError:
                LOGGER.debug("parent.chmod failed", exc_info=True)
            payload = json.dumps(
                {"tokens": [asdict(t) for t in self.tokens]}, indent=2, ensure_ascii=False,
            )
            tmp = self.path.with_suffix(".json.tmp")
            # O_CREAT|O_EXCL with mode 0600 so the file is never briefly
            # world-readable between creation and chmod.
            tmp.unlink(missing_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        LOGGER.debug("os.fsync failed", exc_info=True)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    LOGGER.debug("os.close failed", exc_info=True)
                raise
            os.replace(tmp, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                LOGGER.debug("chmod on token file failed", exc_info=True)
            return True
        except OSError:
            LOGGER.exception("Could not write %s", self.path)
            try:
                self.path.with_suffix(".json.tmp").unlink(missing_ok=True)
            except OSError:
                LOGGER.debug("tmp.unlink failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Mutations — each one saves, so the UI never holds unsaved token state
    # ------------------------------------------------------------------

    def add(self, name: str) -> Token:
        """Generate and store a new token under *name*."""
        token = Token(
            id=secrets.token_hex(8),
            name=(name or "").strip() or "Token",
            token=_TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES),
            created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.tokens.append(token)
        self.save()
        return token

    def rename(self, token_id: str, name: str) -> bool:
        for token in self.tokens:
            if token.id == token_id:
                token.name = (name or "").strip() or token.name
                return self.save()
        return False

    def remove(self, token_id: str) -> bool:
        before = len(self.tokens)
        self.tokens = [t for t in self.tokens if t.id != token_id]
        if len(self.tokens) == before:
            return False
        return self.save()

    def get(self, token_id: str) -> Token | None:
        for token in self.tokens:
            if token.id == token_id:
                return token
        return None

    def verify(self, presented: str) -> Token | None:
        """Return the token matching *presented*, or None.

        ``compare_digest`` on every candidate: a plain ``==`` leaks how many
        leading characters a guess got right through its timing, and the whole
        list is walked regardless of an early match so the answer does not
        depend on the guess's position either.
        """
        if not presented:
            return None
        match: Token | None = None
        for token in self.tokens:
            if secrets.compare_digest(token.token, presented):
                match = token
        return match
