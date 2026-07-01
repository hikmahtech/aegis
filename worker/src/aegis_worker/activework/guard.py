"""Pure active-work assessment for the alert guard (no IO)."""

from __future__ import annotations


def assess(
    *,
    repo: str,
    service: str,
    prs: list[dict],
    pushes: list[dict],
    tasks: list[dict],
) -> dict:
    """Return ``{"active": bool, "reasons": [...]}``; active iff any signal fires.

    Todoist match filter: a due task counts only if its content or any label
    contains (case-insensitively) the repo basename or the alert service —
    keeping the operator in control via how they name/label tasks, and avoiding
    matching the whole backlog.
    """
    reasons: list[str] = []

    for p in prs:
        num = p.get("number")
        user = p.get("user", "")
        reasons.append(f"open PR #{num} ({user})" if user else f"open PR #{num}")

    for pu in pushes:
        ref_full = str(pu.get("ref", ""))
        ref = ref_full.split("/", 2)[-1] if "/" in ref_full else ref_full
        actor = pu.get("actor", "")
        reasons.append(f"recent push to {ref} ({actor})" if actor else f"recent push to {ref}")

    basename = repo.split("/")[-1].lower() if repo else ""
    svc = (service or "").lower()
    for t in tasks:
        hay = (str(t.get("content", "")) + " " + " ".join(t.get("labels") or [])).lower()
        if (basename and basename in hay) or (svc and svc in hay):
            reasons.append(f'Todoist "{t.get("content", "")}" due {t.get("due_date", "")}')

    return {"active": bool(reasons), "reasons": reasons}
