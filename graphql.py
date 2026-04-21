"""Thin wrapper over the Brightspace Pulse GraphQL endpoint.

Endpoint, fragments, and the queries used by both the editor and the
downloader live here so neither has to know anything about GraphQL transport.
"""
import sys

import requests

ENDPOINT = "https://usergraph.api.brightspace.com/graphql"

CONTENT_ITEM_FRAGMENT = """
  __typename
  ... on ContentModule { id title }
  ... on ContentTopic {
    id title type fileName viewHref downloadHref pdfHref modifiedDate
  }
"""

ENROLLMENT_QUERY = """
query($id: String) {
  enrollmentPage(id: $id) {
    next
    enrollments {
      pinned
      organization { id name isActive }
    }
  }
}
"""

ROOT_QUERY = f"""
query($id: String!) {{
  contentRoot(organizationId: $id) {{
    modules {{
      id title
      children {{ {CONTENT_ITEM_FRAGMENT} }}
    }}
  }}
}}
"""

MODULE_QUERY = f"""
query($id: String!) {{
  contentModule(moduleId: $id) {{
    id title
    children {{ {CONTENT_ITEM_FRAGMENT} }}
  }}
}}
"""


def gql(token, query, variables=None):
    r = requests.post(
        ENDPOINT,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if r.status_code != 200:
        sys.exit(f"GraphQL HTTP {r.status_code}: {r.text[:500]}")
    body = r.json() or {}
    if "errors" in body:
        sys.exit(f"GraphQL errors: {body['errors']}")
    return body.get("data") or {}


def fetch_courses(token):
    """Return every enrollment as a list of flat dicts, sorted for display."""
    out, next_id = [], None
    while True:
        page = gql(token, ENROLLMENT_QUERY, {"id": next_id})["enrollmentPage"]
        for e in page["enrollments"]:
            org = e["organization"]
            out.append({
                "id": org["id"],
                "num": org["id"].rsplit("/", 1)[-1],
                "name": org["name"],
                "pinned": e["pinned"],
                "active": org["isActive"],
            })
        next_id = page.get("next")
        if not next_id:
            break
    out.sort(key=lambda c: (not c["pinned"], not c["active"], c["name"].lower()))
    return out
