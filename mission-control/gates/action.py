"""Gate 3 -- Action / Governed Posting.

The only place a public action happens. Three locks guard it:

  1. the human hold -- nothing posts (not even a dry-run) without a recorded
     human approval;
  2. DRY_RUN by default -- a real X call requires DRY_RUN explicitly off AND
     credentials present AND the human approval above;
  3. a takedown path -- every post can be rolled back.

``build_x_payload`` is the SINGLE source of truth for the outbound bytes. Both
this gate (which would send it) and Rehearsal (which only inspects it) call this
exact function, which is what makes the rehearsed payload byte-identical to the
real one.

This file holds posting/transport logic only -- no content generation.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import uuid
from abc import ABC, abstractmethod
from typing import Callable, Optional

from alarms import Alarm, AlarmType

# X API v2 limit. Declared here, enforced when the payload is built.
TWEET_LIMIT = 280

# A belt-and-suspenders guard: while the Rehearsal stage runs (the digital twin
# is meant to be OFFLINE), a real outbound post must be impossible. This contextvar
# is per-thread, so concurrent runs are isolated; RealXClient.post checks it.
_rehearsal_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "rehearsal_active", default=False)


@contextlib.contextmanager
def rehearsal_active():
    """Mark that we are inside the Rehearsal stage; a real post raises if reached."""
    tok = _rehearsal_active.set(True)
    try:
        yield
    finally:
        _rehearsal_active.reset(tok)


def build_x_payload(text: str) -> dict:
    """The exact body that POST /2/tweets would receive. Deterministic."""
    return {"text": text}


_CITATION_TOKEN = re.compile(r"\s*\[[A-Za-z0-9_]+\]")


def to_channel(text: str) -> str:
    """Strip inline [fN] provenance tokens to produce the text that is actually
    posted. `grounding` runs on the *un-stripped* draft (it needs the tokens),
    while Rehearsal and Action both build their payload from THIS string -- so the
    byte-identical guarantee holds on the channel form, and the live tweet does
    not contain '[f1]' noise."""
    return _CITATION_TOKEN.sub("", text).strip()


def render_post(payload: dict, *, author: str = "@yourbrand") -> dict:
    """How the post would appear, for the simulated UI. Derived from the payload,
    never from anything the real network returns."""
    text = payload.get("text", "")
    return {
        "author": author,
        "text": text,
        "char_count": len(text),
        "within_limit": len(text) <= TWEET_LIMIT,
    }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class XClient(ABC):
    @property
    @abstractmethod
    def mode(self) -> str: ...

    @abstractmethod
    def post(self, run_id: str, payload: dict) -> dict: ...

    @abstractmethod
    def takedown(self, run_id: str, post_id: str) -> dict: ...


class DryRunXClient(XClient):
    """Default. Records exactly what WOULD be posted; never touches the network.
    Returns a fake id and marks the record for the simulated UI."""

    def __init__(self, store, handle: str = "@yourbrand"):
        self.store = store
        self.handle = handle

    @property
    def mode(self) -> str:
        return "dry_run"

    def post(self, run_id: str, payload: dict) -> dict:
        post_id = "dryrun-" + uuid.uuid4().hex[:10]
        record = {"post_id": post_id, "mode": self.mode, "payload": payload,
                  "rendered": render_post(payload, author=self.handle), "live": False}
        self.store.log(run_id, "action", "post", post_id, ok=True, detail=record)
        return record

    def takedown(self, run_id: str, post_id: str) -> dict:
        record = {"post_id": post_id, "mode": self.mode, "taken_down": True, "live": False}
        self.store.log(run_id, "action", "takedown", post_id, ok=True, detail=record)
        return record


class RealXClient(XClient):
    """Real X API v2 client. Only ever constructed when DRY_RUN is off AND the
    credentials are present. Uses OAuth 1.0a user context for POST /2/tweets."""

    def __init__(self, store, creds: dict, handle: str = "@yourbrand"):
        self.store = store
        self.creds = creds
        self.handle = handle

    @property
    def mode(self) -> str:
        return "live"

    def _auth(self):
        # OAuth 1.0a user context. requests + requests_oauthlib is the standard,
        # correct pairing (httpx does not accept a requests-style auth object).
        try:
            import requests  # noqa: F401
            from requests_oauthlib import OAuth1  # type: ignore
        except Exception as e:  # pragma: no cover - optional dep
            raise RuntimeError(
                "Real posting needs OAuth1 signing (pip install requests requests-oauthlib)."
            ) from e
        return OAuth1(
            self.creds["X_API_KEY"], self.creds["X_API_SECRET"],
            self.creds["X_ACCESS_TOKEN"], self.creds["X_ACCESS_TOKEN_SECRET"],
        )

    def post(self, run_id: str, payload: dict) -> dict:
        # A real post must NEVER originate from inside the Rehearsal stage.
        if _rehearsal_active.get():
            raise RuntimeError("BLOCKED: a real X post was attempted from inside the "
                               "Rehearsal stage (the digital twin is offline-only)")
        import requests

        resp = requests.post("https://api.twitter.com/2/tweets", json=payload,
                             auth=self._auth(), timeout=15.0)
        if resp.status_code >= 400:
            # Structured, actionable error instead of a bare HTTPError stack trace.
            raise RuntimeError(
                f"X API returned {resp.status_code}: {resp.text[:300]} "
                "(403/401 usually means the token lacks write scope — see --verify-x)")
        data = resp.json().get("data", {})
        post_id = data.get("id", "unknown")
        record = {"post_id": post_id, "mode": self.mode, "payload": payload,
                  "rendered": render_post(payload, author=self.handle), "live": True,
                  "api_response": data}
        self.store.log(run_id, "action", "post", post_id, ok=True, detail=record)
        return record

    def takedown(self, run_id: str, post_id: str) -> dict:  # pragma: no cover - network
        import requests

        resp = requests.delete(f"https://api.twitter.com/2/tweets/{post_id}",
                               auth=self._auth(), timeout=15.0)
        resp.raise_for_status()
        record = {"post_id": post_id, "mode": self.mode, "taken_down": True, "live": True}
        self.store.log(run_id, "action", "takedown", post_id, ok=True, detail=record)
        return record


def _x_credentials() -> Optional[dict]:
    keys = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    creds = {k: os.environ.get(k, "") for k in keys}
    return creds if all(creds.values()) else None


def verify_x_credentials() -> dict:
    """READ-ONLY check of the OAuth 1.0a user-context credentials. Calls
    GET /2/users/me -- it never posts. Returns a structured result so the CLI can
    tell the user exactly what is wrong (missing keys vs. wrong permission)."""
    keys = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    present = {k: os.environ.get(k, "") for k in keys}
    missing = [k for k, v in present.items() if not v]
    if missing:
        return {"ok": False, "reason": "missing_credentials", "missing": missing,
                "hint": ("POST /2/tweets needs OAuth1 user-context: the API key + secret AND "
                         "an Access Token + Secret with Read+Write. A Bearer Token cannot post.")}
    try:
        import requests
        from requests_oauthlib import OAuth1  # type: ignore
    except Exception:
        return {"ok": False, "reason": "missing_dependency",
                "hint": "pip install requests requests-oauthlib"}

    auth = OAuth1(present["X_API_KEY"], present["X_API_SECRET"],
                  present["X_ACCESS_TOKEN"], present["X_ACCESS_TOKEN_SECRET"])
    try:
        resp = requests.get("https://api.twitter.com/2/users/me", auth=auth, timeout=15.0)
    except Exception as e:  # pragma: no cover - network
        return {"ok": False, "reason": "network_error", "error": str(e)}

    if resp.status_code == 200:
        data = resp.json().get("data", {})
        return {"ok": True, "username": data.get("username"),
                "name": data.get("name"), "id": data.get("id")}
    return {"ok": False, "reason": "auth_failed", "status": resp.status_code,
            "body": resp.text[:300],
            "hint": ("401/403 usually means the Access Token lacks WRITE scope or was generated "
                     "BEFORE you set the app to Read+Write. Set Read+Write, then regenerate the "
                     "Access Token & Secret.")}


def _dry_run_env() -> bool:
    return os.environ.get("DRY_RUN", "1").strip().lower() not in ("0", "false", "no")


def would_post_live() -> bool:
    """True only if the environment is configured for a REAL post (DRY_RUN off and
    all X creds present). The human-hold is the third, separate condition."""
    return (not _dry_run_env()) and _x_credentials() is not None


def select_client(store, handle: str = "@yourbrand", interactive: bool = True) -> XClient:
    """DRY_RUN unless EVERY safety condition for a real post is met -- including
    an INTERACTIVE human approver. A non-interactive approver (`--yes`, tests, the
    dashboard auto-path) can NEVER trigger a live post, even with DRY_RUN=0 and
    creds present. This closes the `--real --yes` foot-gun."""
    if not would_post_live() or not interactive:
        return DryRunXClient(store, handle)
    return RealXClient(store, creds=_x_credentials(), handle=handle)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
# An approver decides the human hold. It receives (run_id, rendered_post) and
# returns True to approve. The engine supplies a CLI prompt or an auto-approver.
Approver = Callable[[str, dict], bool]


class HumanDeclined(Exception):
    def __init__(self, alarm: Alarm):
        super().__init__(alarm.context)
        self.alarm = alarm


class ActionGate:
    def __init__(self, store, guardrails, approver: Approver, handle: str = "@yourbrand"):
        self.store = store
        self.guardrails = guardrails
        self.approver = approver
        self.handle = handle
        # An approver is "interactive" (a real human at a prompt) only if it says
        # so. Unknown/auto approvers are treated as non-interactive -> dry-run.
        self.interactive = bool(getattr(approver, "interactive", False))
        self.client = select_client(store, handle, interactive=self.interactive)

    def run(self, run_id: str, text: str) -> dict:
        """Build the byte-identical payload, enforce the human hold, then post
        (dry-run by default). Returns the post record."""
        payload = build_x_payload(text)
        rendered = render_post(payload, author=self.handle)

        # If the environment was configured for a live post but the approver is
        # non-interactive, we have safely downgraded to dry-run -- record why, so
        # it is visible and never a silent surprise.
        if would_post_live() and not self.interactive:
            note = Alarm(AlarmType.AWAITING_HUMAN,
                         "live post was configured (DRY_RUN=0 + creds) but approval is "
                         "non-interactive; downgraded to DRY_RUN. Use the interactive CLI to post for real.",
                         "action")
            self.store.log(run_id, "action", "alarm", note.type.value, ok=None, detail=note.as_dict())

        # --- the human hold ---
        if self.guardrails.human_hold_required:
            awaiting = Alarm(AlarmType.AWAITING_HUMAN,
                             f"post is staged ({self.client.mode}); awaiting explicit human approval",
                             "action")
            self.store.log(run_id, "action", "alarm", awaiting.type.value, ok=None,
                           detail={**awaiting.as_dict(), "rendered": rendered})
            approved = bool(self.approver(run_id, rendered))
            self.store.log(run_id, "action", "approval", "human", ok=approved,
                           detail={"approved": approved, "mode": self.client.mode})
            if not approved:
                esc = Alarm(AlarmType.ESCALATE_HUMAN, "human declined the post; nothing was sent", "action")
                self.store.log(run_id, "action", "alarm", esc.type.value, ok=False, detail=esc.as_dict())
                raise HumanDeclined(esc)

        # --- only now do we (dry-run) post ---
        record = self.client.post(run_id, payload)
        return record

    def takedown(self, run_id: str, post_id: str) -> dict:
        return self.client.takedown(run_id, post_id)
