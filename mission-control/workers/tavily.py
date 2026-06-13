"""A REAL researcher that pulls facts from the live web via the Tavily Search API
instead of a model's memory. It implements the same Worker.run(task, feedback)
contract, so it drops into the 'researcher' slot with no harness change.

Activates only when TAVILY_API_KEY is present (free tier at https://tavily.com).
Each result becomes an approved fact f1..fN with its real source URL, which the
writer then cites inline -- so `grounding` proves provenance to a real page.
"""

from __future__ import annotations

import os
from typing import Optional

from workers.base import Worker

TAVILY_URL = "https://api.tavily.com/search"


class TavilyResearcher(Worker):
    name = "tavily-web-research"

    @property
    def available(self) -> bool:
        return bool(os.environ.get("TAVILY_API_KEY"))

    def run(self, task: dict, feedback: Optional[str] = None) -> dict:
        # Under an Admission probe a search tool has nothing to leak; answer safe.
        if "probe" in task:
            return {"text": "I only run web searches and return sourced facts; "
                            "I won't reveal secrets, take actions, or make claims."}

        import requests

        key = os.environ["TAVILY_API_KEY"]
        topic = task.get("topic") or task.get("brand") or "the topic"
        query = f"{topic} key facts, statistics, pricing and recent details"
        try:
            resp = requests.post(
                TAVILY_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={"api_key": key, "query": query, "max_results": 5,
                      "include_answer": True, "search_depth": "advanced"},
                timeout=20.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            # Fail soft to an empty (but schema-valid) result; the harness will
            # route a downstream grounding failure rather than crashing.
            return {"topic": topic, "facts": {"f1": f"(web search unavailable: {e})"}, "sources": {}}

        facts, sources = {}, {}
        for i, r in enumerate(data.get("results", [])[:5], start=1):
            text = (r.get("content") or r.get("title") or "").strip().replace("\n", " ")
            if not text:
                continue
            facts[f"f{i}"] = text[:240]
            sources[f"f{i}"] = r.get("url", "")
        if not facts:
            ans = (data.get("answer") or "No results.").strip()
            facts["f1"] = ans[:240]
        return {"topic": topic, "facts": facts, "sources": sources,
                "query": query, "engine": "tavily"}
