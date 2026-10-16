"""Which of a GitHub pull request's comments and reviews a person wrote.

A person here is an account GitHub types as ``User`` that is not the pull
request's author. Review lanes and CI post as ``Bot`` accounts, and the session
that opened the pull request posts as its author, so neither of them is a person:
what is left is someone the session has to answer.

Only fingerprints are kept, never bodies, for the reason ``_body_fingerprint`` in
the adapter gives: a body is unbounded third-party text.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

#: Commented reviews -- a review submitted with a body and no verdict -- read in the
#: first page of the comment document. One page: a pull request with more
#: commented reviews than that reads as incomplete, which names no person edge.
PR_REVIEW_PAGE_SIZE = 100
PR_REVIEWS_SELECTION = (
    f"reviews(first:{PR_REVIEW_PAGE_SIZE},states:[COMMENTED])"
    "{totalCount nodes{body author{__typename login}}}"
)
_PERSON_TYPENAME = "User"


def person_fingerprints(connection: object, pr_author: str) -> tuple[list[str], bool]:
    """Fingerprint each non-empty body a person wrote in one comment or review page.

    Returns the fingerprints and whether every node could be read. A node with no
    author is an account GitHub deleted, which is nobody to answer, so it is
    skipped rather than refused. A malformed node marks the page incomplete.
    """
    if not isinstance(connection, Mapping):
        return [], False
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        return [], False
    author_key = pr_author.casefold()
    fingerprints: list[str] = []
    complete = True
    for node in nodes:
        if not isinstance(node, Mapping) or not isinstance(node.get("body"), str):
            complete = False
            continue
        author = node.get("author")
        if author is None:
            continue
        if not isinstance(author, Mapping):
            complete = False
            continue
        login = author.get("login")
        if author.get("__typename") != _PERSON_TYPENAME or not isinstance(login, str) or not login:
            continue
        if author_key and login.casefold() == author_key:
            continue
        body = node["body"]
        if not body:
            continue
        encoded = json.dumps([login.casefold(), body], ensure_ascii=True, separators=(",", ":"))
        fingerprints.append(hashlib.sha256(encoded.encode("utf-8")).hexdigest())
    return fingerprints, complete


def review_fingerprints(node: Mapping[str, object], pr_author: str) -> tuple[list[str], bool]:
    """Fingerprint the commented reviews a person wrote, from the first comment page."""
    reviews = node.get("reviews")
    fingerprints, complete = person_fingerprints(reviews, pr_author)
    if not isinstance(reviews, Mapping):
        return [], False
    total = reviews.get("totalCount")
    nodes = reviews.get("nodes")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or not isinstance(nodes, list)
        or total > len(nodes)
    ):
        return fingerprints, False
    return fingerprints, complete


def person_comment_digest(fingerprints: list[str]) -> str:
    """``"<count>:<digest>"``, page-order independent, or "" when no person wrote.

    The count travels with the digest so a reader can tell a comment that was
    deleted (fewer) from one that was posted or edited (as many or more).
    """
    kept = sorted(fingerprints)
    if not kept:
        return ""
    encoded = json.dumps(kept, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return f"{len(kept)}:{hashlib.sha256(encoded).hexdigest()}"
