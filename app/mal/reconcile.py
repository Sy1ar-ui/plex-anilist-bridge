"""The full AniList -> MyAnimeList reconcile, as a preview the user approves.

:func:`build` reads both whole lists and records every difference as an item
of a draft plan, without writing anything:

* **add**: on AniList, not on MAL;
* **update**: on both, MAL behind or different;
* **lower**: making MAL match would lower its progress or move it off
  COMPLETED. These start *unticked*: the poller never does this on its own;
* **unmapped**: AniList has no MAL id, so it cannot be mirrored;
* **only_on_mal**: on MAL but not AniList. Shown, never deleted.

:func:`apply` then writes only the items the user ticked, through the write
gate (so dry run applies), and stops at the first MAL outage leaving the rest
ticked for another try.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app import db, tokens
from app.mal.client import MALUnavailable
from app.mal.rules import plan_mal_write
from app.mal.sync import log_item, mal_settings
from app.sync.writegate import RESULT_DEFERRED, WriteGate

log = logging.getLogger(__name__)

KINDS = ("add", "update", "lower", "unmapped", "only_on_mal")
ACTIONABLE = ("add", "update", "lower")


@dataclass
class ApplyReport:
    outcomes: dict[str, int] = field(default_factory=dict)
    stopped: str | None = None

    def summary(self) -> str:
        if not self.outcomes:
            return self.stopped or "nothing was ticked"
        parts = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(self.outcomes.items()))
        return parts + (f"; stopped early: {self.stopped}" if self.stopped else "")


def _title(entry: dict[str, Any]) -> str:
    names = (entry.get("media") or {}).get("title") or {}
    return names.get("english") or names.get("romaji") or f"AniList {(entry.get('media') or {}).get('id')}"


async def build(*, anilist, mal) -> int:
    """Compare both lists and store a draft plan. Returns the plan id."""
    user_id = tokens.get(tokens.ANILIST).account_id
    if user_id is None:
        raise ValueError("AniList is not connected")
    entries = await anilist.list_collection(int(user_id))
    mal_list = await mal.animelist()
    settings = mal_settings()

    items: list[tuple] = []
    counts = {kind: 0 for kind in KINDS}
    counts["same"] = 0
    mapped: set[int] = set()
    for entry in sorted(entries, key=_title):
        media = entry.get("media") or {}
        mal_id = media.get("idMal")
        if not mal_id:
            counts["unmapped"] += 1
            items.append((media.get("id"), None, _title(entry), None, None, "unmapped", "info"))
            continue
        mal_id = int(mal_id)
        mapped.add(mal_id)
        current = mal_list.get(mal_id)
        if current is not None:
            current = {k: v for k, v in current.items() if k != "title"}
        plan = plan_mal_write(entry, current, settings, allow_lower=True)
        if plan.action != "write":
            counts["same"] += 1
            continue
        kind = "add" if current is None else ("lower" if plan.lowers else "update")
        counts[kind] += 1
        items.append((media.get("id"), mal_id, _title(entry), json.dumps(current),
                      json.dumps(plan.fields), kind, "pending" if kind == "lower" else "approved"))

    for mal_id, entry in sorted(mal_list.items(), key=lambda kv: str(kv[1].get("title") or "")):
        if mal_id not in mapped:
            counts["only_on_mal"] += 1
            items.append((None, mal_id, entry.get("title") or f"MyAnimeList {mal_id}",
                          json.dumps({k: v for k, v in entry.items() if k != "title"}), None,
                          "only_on_mal", "info"))

    with db.session() as conn:
        conn.execute("UPDATE mal_reconcile_plan SET status = 'cancelled' WHERE status = 'draft'")
        cursor = conn.execute(
            "INSERT INTO mal_reconcile_plan (created_at, status, summary_json)"
            " VALUES (datetime('now'), 'draft', ?)",
            (json.dumps(counts),),
        )
        plan_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO mal_reconcile_item (plan_id, anilist_id, mal_id, title, before_json,"
            " after_json, kind, decision) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(plan_id, *item) for item in items],
        )
    log.info("MAL reconcile preview built", extra={"plan_id": plan_id, "counts": counts})
    return plan_id


def draft() -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT *, (julianday('now') - julianday(created_at)) * 24 AS age_hours"
        " FROM mal_reconcile_plan WHERE status = 'draft' ORDER BY id DESC LIMIT 1"
    )
    if row is None:
        return None
    plan = dict(row)
    plan["summary"] = json.loads(plan.get("summary_json") or "{}")
    return plan


def items(plan_id: int) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {kind: [] for kind in KINDS}
    for row in db.query(
        "SELECT * FROM mal_reconcile_item WHERE plan_id = ? ORDER BY id", (plan_id,)
    ):
        item = dict(row)
        item["before"] = json.loads(item["before_json"]) if item.get("before_json") else None
        item["after"] = json.loads(item["after_json"]) if item.get("after_json") else {}
        grouped.setdefault(item.get("kind") or "update", []).append(item)
    return grouped


def discard(plan_id: int) -> None:
    db.execute("UPDATE mal_reconcile_plan SET status = 'cancelled' WHERE id = ? AND status = 'draft'",
               (plan_id,))


async def apply(plan_id: int, ticked: set[int], *, mal, gate: WriteGate | None = None) -> ApplyReport:
    """Write the ticked items. Unticked ones are left as they are on MAL."""
    gate = gate or WriteGate()
    report = ApplyReport()
    plan = db.query_one("SELECT status FROM mal_reconcile_plan WHERE id = ?", (plan_id,))
    if plan is None or plan["status"] != "draft":
        report.stopped = "that preview is no longer current; build a new one"
        return report

    with db.session() as conn:
        for kind in ACTIONABLE:
            conn.execute(
                "UPDATE mal_reconcile_item SET decision = 'pending'"
                " WHERE plan_id = ? AND kind = ? AND decision = 'approved'",
                (plan_id, kind),
            )
        if ticked:
            marks = ",".join("?" * len(ticked))
            conn.execute(
                f"UPDATE mal_reconcile_item SET decision = 'approved' WHERE plan_id = ?"
                f" AND kind IN ('add', 'update', 'lower') AND decision = 'pending' AND id IN ({marks})",
                (plan_id, *sorted(ticked)),
            )

    rows = db.query(
        "SELECT * FROM mal_reconcile_item WHERE plan_id = ? AND decision = 'approved' ORDER BY id",
        (plan_id,),
    )
    for row in rows:
        fields = json.loads(row["after_json"] or "{}")
        before = json.loads(row["before_json"]) if row["before_json"] else None
        try:
            outcome = await gate.mal_save(mal, int(row["mal_id"]), fields, before=before,
                                          after={**(before or {}), **fields})
        except MALUnavailable as exc:   # raised outside the gate, e.g. by a refresh
            report.stopped = str(exc)
            break
        if outcome.result == RESULT_DEFERRED:
            report.stopped = f"{outcome.detail}. The rest stay ticked; apply again later."
            break
        decision = "error" if not outcome.ok else ("would_write" if outcome.dry_run else "applied")
        report.outcomes[decision] = report.outcomes.get(decision, 0) + 1
        db.execute(
            "UPDATE mal_reconcile_item SET decision = ?, error = ?, applied_at = datetime('now') WHERE id = ?",
            (decision, None if outcome.ok else outcome.detail, row["id"]),
        )
        log_item(source="reconcile", entry=None, mal_id=int(row["mal_id"]), title=row["title"],
                 anilist_id=row["anilist_id"],
                 outcome="written" if decision == "applied" else decision, detail=outcome.detail)

    if report.stopped is None:
        db.execute("UPDATE mal_reconcile_plan SET status = 'applied', applied_at = datetime('now')"
                   " WHERE id = ?", (plan_id,))
    return report
