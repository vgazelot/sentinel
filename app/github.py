"""GitHub GraphQL, the way every source calls it: the token, one more try on a 5xx or a dropped
keep-alive connection (both happen a few times a day), GraphQL errors raised."""
import requests

import config

GRAPHQL = "https://api.github.com/graphql"


def graphql(query: str, variables: dict, timeout: int = 30) -> dict:
    if not config.GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN missing from .env")
    for attempt in (1, 2):
        try:
            r = requests.post(GRAPHQL, json={"query": query, "variables": variables}, timeout=timeout,
                              headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}"})
        except requests.ConnectionError:
            if attempt == 2:
                raise
            continue
        if r.status_code >= 500 and attempt == 1:
            continue
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(payload["errors"][0].get("message", "GraphQL error"))
        return payload["data"]
    raise AssertionError("unreachable")
