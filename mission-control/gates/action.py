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

import json
import os
import uuid
from abc import ABC, abstractmethod
from typing import Callable, Optional

from alarms import Alarm, AlarmType

# X API v2 limit. Declared here, enforced when the payload is built.
TWEET_LIMIT = 280


def build_x_payload(text: str) -> dict:
    """The exact body that POST /2/tweets would receive. Deterministic."""
    return {"text": text}


def render_post(payload: dict, *, author: str = "@focusapp") -> dict:
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

    def __init__(self, store):
        self.store = store

    @property
    def mode(self) -> str:
        return "dry_run"

    def post(self, run_id: str, payload: dict) -> dict:
        post_id = "dryrun-" + uuid.uuid4().hex[:10]
        record = {"post_id": post_id, "mode": self.mode, "payload": payload,
                  "rendered": render_post(payload), "live": False}
        self.store.log(run_id, "action", "post", post_id, ok=True, detail=record)
        return record

    def takedown(self, run_id: str, post_id: str) -> dict:
        record = {"post_id": post_id, "mode": self.mode, "taken_down": True, "live": False}
        self.store.log(run_id, "action", "takedown", post_id, ok=True, detail=record)
        return record


class RealXClient(XClient):
    """Real X API v2 client. Only ever constructed when DRY_RUN is off AND the
    credentials are present. Uses OAuth 1.0a user context for POST /2/tweets."""

    def __init__(self, store, creds: dict):
        self.store = store
        self.creds = creds

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
        import requests

        resp = requests.post("https://api.twitter.com/2/tweets", json=payload,
                             auth=self._auth(), timeout=15.0)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        post_id = data.get("id", "unknown")
        record = {"post_id": post_id, "mode": self.mode, "payload": payload,
                  "rendered": render_post(payload), "live": True, "api_response": data}
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


def select_client(store) -> XClient:
    """DRY_RUN unless EVERY safety condition for a real post is met."""
    dry_run = os.environ.get("DRY_RUN", "1").strip().lower() not in ("0", "false", "no")
    creds = _x_credentials()
    if dry_run or creds is None:
        return DryRunXClient(store)
    return RealXClient(store, creds)


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
    def __init__(self, store, guardrails, approver: Approver):
        self.store = store
        self.guardrails = guardrails
        self.approver = approver
        self.client = select_client(store)

    def run(self, run_id: str, text: str) -> dict:
        """Build the byte-identical payload, enforce the human hold, then post
        (dry-run by default). Returns the post record."""
        payload = build_x_payload(text)
        rendered = render_post(payload)

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
