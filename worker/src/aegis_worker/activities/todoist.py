"""TodoistActivities — projection apply, outbox drain, bootstrap.

These activities are called by TodoistSyncFlow. Each is idempotent so that
re-running a workflow (e.g. due to a worker restart) doesn't corrupt state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import asyncpg
from aegis.clarify_note import AGENT_REPLY_PREFIX, CLARIFY_NOTE_PREFIX
from temporalio import activity

_ASSIGNEE_LABELS = {"@me", "@sebas", "@raphael", "@maou", "@pandora"}


def _pick_assignee(labels: list[str]) -> str | None:
    """First label in _ASSIGNEE_LABELS; defaults None for projection layer
    (the read layer in chat tools applies the @me default on the fly).
    """
    for lab in labels:
        if lab in _ASSIGNEE_LABELS:
            return lab
    return None


def _pick_source_tag(labels: list[str]) -> str | None:
    """First label starting with '#' is the source tag."""
    for lab in labels:
        if lab.startswith("#"):
            return lab
    return None


def _parse_date(value: str | None) -> date | None:
    """Parse a Todoist due-date string into a datetime.date, or None.

    Todoist sends due dates as 'YYYY-MM-DD' for date-only, or
    'YYYY-MM-DDTHH:MM:SS' for date+time. We store only the date.
    asyncpg's DATE codec requires a datetime.date object — it does
    not coerce strings.
    """
    if not value:
        return None
    try:
        if "T" in value:
            return datetime.fromisoformat(value.rstrip("Z")).date()
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string from the Todoist API into a
    timezone-aware datetime, or return None if the value is absent.

    Todoist returns timestamps as strings like '2026-05-18T12:00:00Z' or
    '2026-05-18T12:00:00.000000Z'. asyncpg requires a datetime object for
    TIMESTAMPTZ columns — it does not coerce strings.
    """
    if not value:
        return None
    # Replace trailing Z with +00:00 for fromisoformat compat (Python < 3.11)
    normalized = value.rstrip("Z") + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


@dataclass
class TodoistActivities:
    """Pool + connector are injected. db_pool may be None in unit tests that
    only build commands; connector may be None in tests that only apply diffs.
    seed_dir points at the directory containing todoist.yaml — passed from
    Settings.seed_dir at worker boot."""

    db_pool: asyncpg.Pool | None
    connector: Any
    seed_dir: str = "./config/seed"

    @activity.defn
    async def apply_sync_diff(self, diff: dict) -> dict:
        """Upsert projects, labels, tasks; save sync_token.

        NOTE: projects and items are inserted in the order returned by the
        Todoist Sync API. The Sync API typically returns parents before
        children, so the non-deferrable FKs (todoist_projects.parent_id
        and todoist_tasks.parent_id) are satisfied without explicit sorting.
        If Todoist ever returns children before parents a FK violation will
        occur. Phase 5+ (nested project hierarchies) should add a topological
        sort here. See task-5 implementation notes.
        """
        if self.db_pool is None:
            return {"projects_upserted": 0, "tasks_upserted": 0, "labels_upserted": 0}

        projects = diff.get("projects") or []
        labels = diff.get("labels") or []
        items = diff.get("items") or []
        notes = diff.get("notes") or []

        is_full_sync = bool(diff.get("full_sync", False))

        async with self.db_pool.acquire() as conn:  # noqa: SIM117
            async with conn.transaction():
                # Full-sync snapshot: Todoist sends every live resource in
                # this diff. Anything in our local projection NOT present in
                # the snapshot is stale (deleted while we were offline) —
                # purge it before upserting so ghost rows don't linger.
                if is_full_sync:
                    snapshot_proj_ids = [p["id"] for p in projects if p.get("id")]
                    snapshot_label_ids = [lab["id"] for lab in labels if lab.get("id")]
                    snapshot_item_ids = [it["id"] for it in items if it.get("id")]
                    snapshot_note_ids = [n["id"] for n in notes if n.get("id")]
                    # Order matters: notes → items → labels → projects (FK).
                    await conn.execute(
                        "DELETE FROM todoist_notes WHERE id <> ALL($1::text[])",
                        snapshot_note_ids,
                    )
                    await conn.execute(
                        "DELETE FROM todoist_tasks WHERE id <> ALL($1::text[])",
                        snapshot_item_ids,
                    )
                    await conn.execute(
                        "DELETE FROM todoist_labels WHERE id <> ALL($1::text[])",
                        snapshot_label_ids,
                    )
                    # Projects are sacred — they may have is_managed=true
                    # markers from bootstrap. Preserve them even if the
                    # snapshot didn't include them (Todoist's "show me
                    # everything" can be filtered by archive state).
                    await conn.execute(
                        "DELETE FROM todoist_projects "
                        "WHERE id <> ALL($1::text[]) AND is_managed = false",
                        snapshot_proj_ids,
                    )
                    activity.logger.info(
                        "todoist_full_sync_purge projs_kept=%d labels_kept=%d items_kept=%d notes_kept=%d",
                        len(snapshot_proj_ids),
                        len(snapshot_label_ids),
                        len(snapshot_item_ids),
                        len(snapshot_note_ids),
                    )

                # Projects — NOTE: relies on parent-before-child API ordering (see docstring).
                # Skip is_deleted rows + delete any local row that flips to is_deleted.
                deleted_project_ids: list[str] = []
                for p in projects:
                    if not p.get("id"):
                        continue
                    if p.get("is_deleted"):
                        deleted_project_ids.append(p["id"])
                        continue
                    await conn.execute(
                        """
                        INSERT INTO todoist_projects (id, parent_id, name, is_archived, order_idx, raw, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, now())
                        ON CONFLICT (id) DO UPDATE
                          SET parent_id = EXCLUDED.parent_id,
                              name = EXCLUDED.name,
                              is_archived = EXCLUDED.is_archived,
                              order_idx = EXCLUDED.order_idx,
                              raw = EXCLUDED.raw,
                              updated_at = now()
                        """,
                        p["id"],
                        p.get("parent_id"),
                        p.get("name") or "",
                        bool(p.get("is_archived", False)),
                        p.get("child_order"),
                        p,
                    )
                # NOTE: project DELETEs are deferred to the very end of the
                # transaction (just before the sync-token write). Tasks in this
                # same diff that reference these projects must be DELETE'd first
                # or the FK constraint todoist_tasks.project_id_fkey blows up
                # the entire transaction — caught 2026-05-25 when a user
                # deleted the drwho-marketing project (134 tasks); sync jammed
                # for 6 hours until manual unblock.

                # Labels. Skip is_deleted=true entries AND empty-name
                # rows — Todoist's sync diff keeps re-sending deleted
                # labels with `name=""`, and the UNIQUE(name) index on
                # todoist_labels collides every time more than one
                # is_deleted label arrives in the same diff. Also delete
                # any already-stored row that flips to is_deleted so the
                # local projection mirrors Todoist's reality.
                deleted_label_ids: list[str] = []
                for lab in labels:
                    if lab.get("is_deleted") or not (lab.get("name") or "").strip():
                        deleted_label_ids.append(lab["id"])
                        continue
                    await conn.execute(
                        """
                        INSERT INTO todoist_labels (id, name, color, raw)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (id) DO UPDATE
                          SET name = EXCLUDED.name,
                              color = EXCLUDED.color,
                              raw = EXCLUDED.raw
                        """,
                        lab["id"],
                        lab["name"],
                        lab.get("color"),
                        lab,
                    )
                if deleted_label_ids:
                    await conn.execute(
                        "DELETE FROM todoist_labels WHERE id = ANY($1::text[])",
                        deleted_label_ids,
                    )

                # Tasks — defensive ordering + orphan guards.
                #
                # Project FK: skip items whose project_id isn't in the projection.
                # This can happen when Todoist returns an item referencing a project
                # not in the same sync diff (transient mid-edit state); replaying would
                # FK-violate forever otherwise. Build the set of known project IDs
                # ONCE per diff: every project from this diff plus everything already
                # in the projection.
                known_project_ids: set[str] = {str(p["id"]) for p in projects}
                if items:
                    existing_proj_rows = await conn.fetch("SELECT id FROM todoist_projects")
                    known_project_ids.update(str(r["id"]) for r in existing_proj_rows)

                # Parent FK: Todoist sometimes returns child items before
                # their parent in the same diff (observed 2026-05-21 on
                # subtask `6ggwF8FfPHhprxMp`), and the previous code relied
                # on Todoist's API ordering which isn't guaranteed. Two
                # defences here:
                #   1. Build the set of parent IDs that exist in this batch
                #      OR in our projection, so we know which `parent_id`
                #      values are referenceable.
                #   2. Topologically sort items so parents are written before
                #      their children within the same diff.
                # Any item whose parent_id points to something neither in the
                # batch nor in our DB gets its parent_id nulled (with a log
                # line) — a true orphan we surface as a top-level task rather
                # than dropping outright.
                items_present = {
                    str(it["id"]): it for it in items if it.get("id") and not it.get("is_deleted")
                }
                known_task_ids: set[str] = set(items_present.keys())
                if items_present:
                    existing_task_rows = await conn.fetch("SELECT id FROM todoist_tasks")
                    known_task_ids.update(str(r["id"]) for r in existing_task_rows)

                def _topo_order(batch: list[dict]) -> list[dict]:
                    """Order items so any item appears AFTER its parent
                    when both are in the same batch. Stable on input order
                    for unrelated items.
                    """
                    ordered: list[dict] = []
                    visited: set[str] = set()
                    visiting: set[str] = set()

                    def visit(it: dict) -> None:
                        item_id = str(it.get("id") or "")
                        if not item_id or item_id in visited:
                            return
                        if item_id in visiting:
                            # Cycle (Todoist shouldn't produce these but be safe)
                            return
                        visiting.add(item_id)
                        parent_id = it.get("parent_id")
                        if parent_id and str(parent_id) in items_present:
                            visit(items_present[str(parent_id)])
                        visiting.discard(item_id)
                        visited.add(item_id)
                        ordered.append(it)

                    for it in batch:
                        if it.get("is_deleted") or not it.get("id"):
                            # Deletes are handled in a separate pass below;
                            # null/empty IDs are dropped here.
                            continue
                        visit(it)
                    return ordered

                # Collect deletes BEFORE reordering — _topo_order strips them.
                deleted_item_ids: list[str] = [
                    it["id"] for it in items if it.get("id") and it.get("is_deleted")
                ]
                items = _topo_order(items)

                for it in items:
                    if not it.get("id"):
                        continue
                    project_id = it.get("project_id")
                    if project_id and str(project_id) not in known_project_ids:
                        activity.logger.warning(
                            "todoist_apply_skipped_orphan_item item_id=%s project_id=%s content=%s",
                            it.get("id"),
                            project_id,
                            (it.get("content") or "")[:60],
                        )
                        continue
                    parent_id = it.get("parent_id")
                    if parent_id and str(parent_id) not in known_task_ids:
                        activity.logger.warning(
                            "todoist_apply_nulled_orphan_parent item_id=%s parent_id=%s",
                            it.get("id"),
                            parent_id,
                        )
                        parent_id = None
                    # Reflect the nulled parent back on the item so the
                    # INSERT below uses the cleaned value.
                    if it.get("parent_id") != parent_id:
                        it = {**it, "parent_id": parent_id}
                    item_labels = list(it.get("labels") or [])
                    assignee = _pick_assignee(item_labels)
                    source_tag = _pick_source_tag(item_labels)
                    due_date = None
                    if isinstance(it.get("due"), dict):
                        due_date = _parse_date(it["due"].get("date"))
                    # Defensive coercions: Todoist occasionally returns
                    # priority as a string; child_order similarly. Cast
                    # safely so the asyncpg SMALLINT/INT bind doesn't
                    # explode the whole transaction (which would prevent
                    # the sync_token from advancing → infinite poison-poll).
                    priority_val = it.get("priority")
                    if priority_val is not None:
                        try:
                            priority_val = int(priority_val)
                        except (TypeError, ValueError):
                            priority_val = None
                    await conn.execute(
                        """
                        INSERT INTO todoist_tasks
                          (id, project_id, parent_id, content, description, due_date,
                           priority, labels, is_completed, completed_at,
                           assignee_label, source_tag, raw, updated_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,now())
                        ON CONFLICT (id) DO UPDATE
                          SET project_id = EXCLUDED.project_id,
                              parent_id = EXCLUDED.parent_id,
                              content = EXCLUDED.content,
                              description = EXCLUDED.description,
                              due_date = EXCLUDED.due_date,
                              priority = EXCLUDED.priority,
                              labels = EXCLUDED.labels,
                              is_completed = EXCLUDED.is_completed,
                              completed_at = EXCLUDED.completed_at,
                              assignee_label = EXCLUDED.assignee_label,
                              source_tag = EXCLUDED.source_tag,
                              raw = EXCLUDED.raw,
                              updated_at = now()
                        """,
                        it["id"],
                        it.get("project_id"),
                        it.get("parent_id"),
                        it.get("content") or "",
                        it.get("description"),
                        due_date,
                        priority_val,
                        item_labels,
                        bool(it.get("checked", False)),
                        _parse_ts(it.get("completed_at")),
                        assignee,
                        source_tag,
                        it,
                    )
                if deleted_item_ids:
                    # Cascade through notes first.
                    await conn.execute(
                        "DELETE FROM todoist_notes WHERE item_id = ANY($1::text[])",
                        deleted_item_ids,
                    )
                    await conn.execute(
                        "DELETE FROM todoist_tasks WHERE id = ANY($1::text[])",
                        deleted_item_ids,
                    )

                # Notes — Phase 3 projection.
                # Comment-loop guard: bumps to todoist_tasks.last_note_at are
                # filtered to user-authored comments. AEGIS-authored notes
                # must NOT trigger re-classification — otherwise ClarifyFlow
                # loops on its own output (ClarifyFlow's `[ClarifyFlow @ `
                # tag) and on Pandora's investigation comments
                # (which include `Workflow run: ` as a stable footer in
                # every shape — start-comment, verdict-comment, PR-opened,
                # fix-discarded, transcript-attached, etc.). Caught
                # 2026-05-21 when ClarifyFlow's 12:00 tick re-spawned 5
                # pandora-jira investigations 15 min after they were first
                # spawned — because the start-comments from those very
                # spawns bumped last_note_at and re-surfaced the tasks.
                deleted_note_ids: list[str] = []
                for n in notes:
                    if not n.get("id"):
                        continue
                    if n.get("is_deleted"):
                        deleted_note_ids.append(n["id"])
                        continue
                    item_id = n.get("item_id")
                    if not item_id:
                        continue
                    posted = _parse_ts(n.get("posted_at"))
                    await conn.execute(
                        """
                        INSERT INTO todoist_notes
                          (id, item_id, content, posted_uid, posted_at, raw, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, now())
                        ON CONFLICT (id) DO UPDATE
                          SET content = EXCLUDED.content,
                              posted_uid = EXCLUDED.posted_uid,
                              posted_at = EXCLUDED.posted_at,
                              raw = EXCLUDED.raw,
                              updated_at = now()
                        """,
                        n["id"],
                        item_id,
                        n.get("content", ""),
                        n.get("posted_uid"),
                        posted,
                        n,
                    )
                    content = n.get("content") or ""
                    if (
                        posted is not None
                        and not content.startswith(CLARIFY_NOTE_PREFIX)
                        and not content.startswith(AGENT_REPLY_PREFIX)
                        and "Workflow run:" not in content
                    ):
                        await conn.execute(
                            "UPDATE todoist_tasks "
                            "SET last_note_at = GREATEST(last_note_at, $1) "
                            "WHERE id = $2",
                            posted,
                            item_id,
                        )
                if deleted_note_ids:
                    await conn.execute(
                        "DELETE FROM todoist_notes WHERE id = ANY($1::text[])",
                        deleted_note_ids,
                    )

                # Project DELETEs run last — deferred from the projects loop
                # above so any tasks in this same diff that reference these
                # projects have already been DELETE'd via the items pass.
                # Managed projects are preserved even if Todoist says they're
                # gone (the user may have archived them and we want the
                # projection to retain history).
                if deleted_project_ids:
                    await conn.execute(
                        "DELETE FROM todoist_projects "
                        "WHERE id = ANY($1::text[]) AND is_managed = false",
                        deleted_project_ids,
                    )

                # Sync token
                token = diff.get("sync_token")
                if token:
                    await conn.execute(
                        """
                        UPDATE todoist_sync_state
                          SET sync_token = $1,
                              last_incremental_at = now(),
                              last_full_sync_at = CASE WHEN $2 THEN now() ELSE last_full_sync_at END
                          WHERE key = 'main'
                        """,
                        token,
                        bool(diff.get("full_sync", False)),
                    )

        activity.logger.info(
            "todoist_apply_sync_diff projects=%d labels=%d items=%d notes=%d",
            len(projects),
            len(labels),
            len(items),
            len(notes),
        )
        return {
            "projects_upserted": len(projects),
            "labels_upserted": len(labels),
            "tasks_upserted": len(items),
            "notes_upserted": len(notes),
        }

    @activity.defn
    async def drain_outbox(self) -> dict:
        """Submit all pending outbox commands; mark committed or failed.

        Strategy:
        - Pull up to 50 pending rows (Sync API batch limit).
        - Submit as a single commands batch.
        - For each row: if response says committed → mark committed + store id.
          If retryable error → increment attempt_count, leave pending.
          If non-retryable OR attempt_count >= 5 → mark failed.
        """
        if self.db_pool is None or self.connector is None:
            return {"committed": 0, "failed": 0}

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, temp_id, command, attempt_count FROM todoist_outbox "
                "WHERE status = 'pending' ORDER BY created_at, id LIMIT 50"
            )

        if not rows:
            return {"committed": 0, "failed": 0}

        commands = [r["command"] for r in rows]
        result = await self.connector.commands(commands)
        from aegis.connectors.todoist import TodoistConnector as _Tc

        committed = 0
        failed = 0
        async with self.db_pool.acquire() as conn, conn.transaction():
            if result.get("ok"):
                mapping = (result.get("data") or {}).get("temp_id_mapping", {}) or {}
                sync_status = (result.get("data") or {}).get("sync_status", {}) or {}
                for r in rows:
                    cmd = r["command"]
                    st = sync_status.get(cmd.get("uuid"))
                    if st == "ok":
                        real_id = mapping.get(r["temp_id"])
                        await conn.execute(
                            "UPDATE todoist_outbox "
                            "SET status='committed', committed_id=$1, last_attempt_at=now(), attempt_count=attempt_count+1 "
                            "WHERE id=$2",
                            real_id,
                            r["id"],
                        )
                        committed += 1
                    else:
                        # Per-command failure inside an otherwise-ok batch.
                        # Distinguish permanent rejections (4xx-class: ITEM_NOT_FOUND,
                        # INVALID_ARGUMENT, etc.) from transient (5xx-class). The
                        # former are poison — five wasted retries each. Mark them
                        # failed immediately so the operator can inspect.
                        next_attempts = r["attempt_count"] + 1
                        permanent = _Tc._is_permanent_error(st)
                        new_status = "failed" if permanent or next_attempts >= 5 else "pending"
                        await conn.execute(
                            "UPDATE todoist_outbox SET status=$1, attempt_count=$2, last_attempt_at=now() WHERE id=$3",
                            new_status,
                            next_attempts,
                            r["id"],
                        )
                        if new_status == "failed":
                            failed += 1
                            activity.logger.warning(
                                "todoist_outbox_command_rejected temp_id=%s cmd_type=%s permanent=%s status=%s",
                                r["temp_id"],
                                cmd.get("type"),
                                permanent,
                                str(st)[:200],
                            )
            else:
                retryable = result.get("retryable", False)
                for r in rows:
                    next_attempts = r["attempt_count"] + 1
                    if retryable and next_attempts < 5:
                        await conn.execute(
                            "UPDATE todoist_outbox SET attempt_count=$1, last_attempt_at=now() WHERE id=$2",
                            next_attempts,
                            r["id"],
                        )
                    else:
                        await conn.execute(
                            "UPDATE todoist_outbox SET status='failed', attempt_count=$1, last_attempt_at=now() WHERE id=$2",
                            next_attempts,
                            r["id"],
                        )
                        failed += 1

        activity.logger.info("todoist_drain_outbox committed=%d failed=%d", committed, failed)
        return {"committed": committed, "failed": failed}

    @activity.defn
    async def bootstrap_if_empty(self) -> dict:
        """One-shot: if managed projects don't exist in settings, create them.

        Safety: refuses to run if the Todoist account already has projects
        the user/another process created (anything not in our managed set).
        This protects re-runs from clobbering a populated account.
        """
        if self.db_pool is None or self.connector is None:
            return {"bootstrapped": False, "reason": "no_pool_or_connector"}

        # Already bootstrapped via settings row? Self-heal when keys are missing.
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM settings WHERE key = 'todoist_managed_project_ids'"
            )
            if row is not None and row["value"]:
                existing = dict(row["value"])
                # Expected keys = inbox (adopted) + every key listed in seed YAML
                from pathlib import Path

                import yaml

                seed_path = Path(self.seed_dir) / "todoist.yaml"
                if not seed_path.exists():
                    return {"bootstrapped": False, "reason": "already_done"}
                seed = yaml.safe_load(seed_path.read_text())
                expected_keys = {"inbox"} | {e["key"] for e in seed["managed_projects"]}
                missing = sorted(expected_keys - set(existing.keys()))
                if not missing:
                    return {"bootstrapped": False, "reason": "already_done"}
                return await self._create_missing_managed_projects(existing, missing, seed)
            # Partial-failure recovery: if a prior bootstrap created the
            # Todoist projects but crashed before writing settings, the
            # projection has is_managed=true rows but settings is empty.
            # Re-fetch them and rebuild the settings row.
            #
            # Recovery strategy (2026-05-23): match by name. If any
            # `is_managed=true` row has a name that isn't in the seed, we
            # refuse to recover — the user likely renamed a managed project
            # in Todoist UI and a name-only match would mis-classify it as
            # the adopted Inbox (or, worse, fall through to normal bootstrap
            # which would create duplicate `📚 Reference` etc. projects).
            # Operator must either rename the project back or clear the
            # is_managed flag manually before re-running bootstrap.
            managed_rows = await conn.fetch(
                "SELECT id, name FROM todoist_projects WHERE is_managed = true"
            )
        if managed_rows:
            activity.logger.warning(
                "todoist_bootstrap_recovering_from_partial_state projection_rows=%d",
                len(managed_rows),
            )
            # Load seed to know the canonical key → name mapping
            from pathlib import Path

            import yaml as _yaml

            seed_path = Path(self.seed_dir) / "todoist.yaml"
            if not seed_path.exists():
                return {"bootstrapped": False, "reason": f"seed_missing:{seed_path}"}
            seed = _yaml.safe_load(seed_path.read_text())
            seed_names = {entry["name"] for entry in seed["managed_projects"]}
            seed_name_to_key = {entry["name"]: entry["key"] for entry in seed["managed_projects"]}
            managed_ids: dict[str, str] = {}
            adopted_inbox_id: str | None = None
            unknown_names: list[tuple[str, str]] = []
            for r in managed_rows:
                key = seed_name_to_key.get(r["name"])
                if key:
                    managed_ids[key] = r["id"]
                elif r["name"] in seed_names:
                    # Defensive — shouldn't happen given the keys()
                    continue
                else:
                    # Either the adopted Inbox (sole non-seed row, expected)
                    # OR a renamed managed project (problem). Track and
                    # disambiguate below.
                    unknown_names.append((r["id"], r["name"]))
            if len(unknown_names) == 1:
                # Exactly one — treat as the adopted Inbox.
                adopted_inbox_id = unknown_names[0][0]
                managed_ids["inbox"] = adopted_inbox_id
            elif len(unknown_names) > 1:
                # Ambiguous — refuse to recover. Log every unknown name so
                # the operator can fix it.
                activity.logger.error(
                    "todoist_bootstrap_recovery_ambiguous unknown_names=%s",
                    [n for _, n in unknown_names],
                )
                return {
                    "bootstrapped": False,
                    "reason": "recovery_ambiguous_unknown_names",
                }
            # Total expected: N created (from seed) + 1 adopted (the inbox) = N+1.
            # Restore only if we matched the full set; otherwise fall through.
            if len(managed_ids) == len(seed["managed_projects"]) + 1:
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO settings (key, value) VALUES "
                        "('todoist_managed_project_ids', $1) ON CONFLICT (key) "
                        "DO UPDATE SET value = EXCLUDED.value",
                        managed_ids,
                    )
                activity.logger.info(
                    "todoist_bootstrap_recovered_settings_from_projection ids=%s",
                    managed_ids,
                )
                return {"bootstrapped": False, "reason": "recovered_from_projection"}

        # Probe Todoist for projects: we adopt the default Inbox (project
        # with inbox_project=true) as our 'inbox' slot rather than creating
        # a duplicate '📥 Inbox' project alongside it. Any other existing
        # projects are left alone — AEGIS only touches projects whose IDs
        # appear in settings.todoist_managed_project_ids.
        probe = await self.connector.sync("*", ["projects"])
        if not probe.get("ok"):
            return {"bootstrapped": False, "reason": f"probe_failed:{probe.get('error')}"}
        existing_projects = (probe.get("data") or {}).get("projects") or []
        default_inbox = next(
            (p for p in existing_projects if p.get("inbox_project") and not p.get("is_archived")),
            None,
        )
        if default_inbox is None:
            return {"bootstrapped": False, "reason": "no_inbox_project_found"}
        non_default = [
            p for p in existing_projects if not p.get("inbox_project") and not p.get("is_archived")
        ]
        if non_default:
            activity.logger.info(
                "todoist_bootstrap_creating_alongside_existing projects=%s",
                [p.get("name") for p in non_default],
            )

        # Load seed
        from pathlib import Path

        import yaml

        seed_path = Path(self.seed_dir) / "todoist.yaml"
        if not seed_path.exists():
            activity.logger.error("todoist_bootstrap_seed_missing path=%s", str(seed_path))
            return {"bootstrapped": False, "reason": f"seed_missing:{seed_path}"}
        seed = yaml.safe_load(seed_path.read_text())

        # Build command batch: projects first (so their temp_ids can be used),
        # then labels, then filters. The seed YAML does NOT list 'inbox' under
        # managed_projects — we adopted Todoist's default Inbox above.
        from aegis.connectors.todoist import TodoistConnector

        project_cmds = []
        project_temp_to_key: dict[str, str] = {}
        for entry in seed["managed_projects"]:
            cmd = TodoistConnector.build_create_project_command(entry["name"])
            project_cmds.append(cmd)
            project_temp_to_key[cmd["temp_id"]] = entry["key"]

        label_cmds = []
        for label_group in seed["labels"].values():
            for lab in label_group:
                label_cmds.append(TodoistConnector.build_create_label_command(lab["name"]))

        filter_cmds = [
            TodoistConnector.build_create_filter_command(f["name"], f["query"])
            for f in seed["filters"]
        ]

        all_cmds = project_cmds + label_cmds + filter_cmds
        result = await self.connector.commands(all_cmds)
        from aegis.connectors.todoist import TodoistConnector as _Tc

        status = _Tc.check_sync_status(result, [c["uuid"] for c in all_cmds])
        if not status["ok"]:
            # Either envelope failed or some commands inside were rejected.
            # We can't proceed with partial state — settings would be written
            # with missing keys and the self-heal at the top of this function
            # would loop on every tick. Bail loudly so the operator sees the
            # underlying error_tag.
            activity.logger.error(
                "todoist_bootstrap_commands_failed envelope_err=%s rejected=%s",
                status["envelope_error"],
                str(status["rejected"])[:500],
            )
            return {
                "bootstrapped": False,
                "reason": (
                    f"commands_failed:{status['envelope_error']}"
                    if status["envelope_error"]
                    else f"commands_rejected:{list(status['rejected'].keys())}"
                ),
            }

        mapping = (result.get("data") or {}).get("temp_id_mapping", {}) or {}

        managed_ids: dict[str, str] = {"inbox": default_inbox["id"]}
        for temp_id, key in project_temp_to_key.items():
            real_id = mapping.get(temp_id)
            if real_id is not None:
                managed_ids[key] = real_id

        async with self.db_pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ('todoist_managed_project_ids', $1) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                managed_ids,
            )
            # Mark the projection rows as managed once they arrive via sync.
            # (The next sync_diff apply will see them and update raw + name;
            # is_managed is set here lazily.)
            for pid in managed_ids.values():
                await conn.execute(
                    "INSERT INTO todoist_projects (id, name, is_managed, raw) "
                    "VALUES ($1, '<pending>', true, '{}'::jsonb) "
                    "ON CONFLICT (id) DO UPDATE SET is_managed = true",
                    pid,
                )

        activity.logger.info("todoist_bootstrap_complete project_ids=%s", managed_ids)
        return {
            "bootstrapped": True,
            "projects_created": len(project_cmds),  # 2 in current seed (inbox adopted, not created)
            "inbox_adopted_id": default_inbox["id"],
            "labels_created": len(label_cmds),
            "filters_created": len(filter_cmds),
        }

    async def _create_missing_managed_projects(
        self, existing: dict, missing: list[str], seed: dict
    ) -> dict:
        """Add only the missing managed projects to Todoist + patch settings.

        Called from bootstrap_if_empty when settings already exists but lacks
        keys introduced by a later phase (e.g. 'reference' added in Phase 3).
        """
        from aegis.connectors.todoist import TodoistConnector

        # 'inbox' is adopted, not created. Filter it out if it shows up missing
        # (shouldn't, since the first bootstrap adopts the default Inbox).
        creatable = [m for m in missing if m != "inbox"]
        if not creatable:
            activity.logger.warning(
                "todoist_bootstrap_inbox_missing_skipping settings=%s", existing
            )
            return {
                "bootstrapped": False,
                "reason": "inbox_missing_cannot_self_heal",
            }

        seed_by_key = {e["key"]: e["name"] for e in seed["managed_projects"]}

        # Probe live projects to adopt any that already exist by exact name
        # (idempotent w.r.t. projects created out-of-band, e.g. by the
        # projects→labels migration). Adopted keys skip creation so we never
        # spawn a duplicate "Next"/"Someday / Later".
        existing_by_name: dict[str, str] = {}
        probe = await self.connector.sync("*", ["projects"])
        if probe.get("ok"):
            for p in (probe.get("data") or {}).get("projects") or []:
                if not p.get("is_archived"):
                    existing_by_name[p.get("name")] = p.get("id")

        updated = dict(existing)
        commands = []
        temp_to_key: dict[str, str] = {}
        for key in creatable:
            name = seed_by_key[key]
            if name in existing_by_name:
                updated[key] = existing_by_name[name]
                continue
            cmd = TodoistConnector.build_create_project_command(name)
            commands.append(cmd)
            temp_to_key[cmd["temp_id"]] = key

        if commands:
            result = await self.connector.commands(commands)
            status = TodoistConnector.check_sync_status(result, [c["uuid"] for c in commands])
            if not status["ok"]:
                activity.logger.error(
                    "todoist_self_heal_commands_failed envelope_err=%s rejected=%s",
                    status["envelope_error"],
                    str(status["rejected"])[:500],
                )
                return {
                    "bootstrapped": False,
                    "reason": (
                        f"commands_failed:{status['envelope_error']}"
                        if status["envelope_error"]
                        else f"commands_rejected:{list(status['rejected'].keys())}"
                    ),
                }
            mapping = (result.get("data") or {}).get("temp_id_mapping", {}) or {}
            for temp_id, key in temp_to_key.items():
                real_id = mapping.get(temp_id)
                if real_id is not None:
                    updated[key] = real_id

        async with self.db_pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES "
                "('todoist_managed_project_ids', $1) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                updated,
            )
            for key in creatable:
                pid = updated.get(key)
                if pid:
                    await conn.execute(
                        "INSERT INTO todoist_projects (id, name, is_managed, raw) "
                        "VALUES ($1, '<pending>', true, '{}'::jsonb) "
                        "ON CONFLICT (id) DO UPDATE SET is_managed = true",
                        pid,
                    )

        activity.logger.info("todoist_bootstrap_self_heal_complete added=%s", creatable)
        return {
            "bootstrapped": True,
            "missing_keys": missing,
            "projects_created": len(temp_to_key),
        }

    @activity.defn
    async def fetch_sync(self) -> dict:
        """Read current sync_token, call connector.sync, return the raw diff."""
        if self.db_pool is None or self.connector is None:
            return {"sync_token": "", "projects": [], "items": [], "labels": []}
        async with self.db_pool.acquire() as conn:
            token = await conn.fetchval(
                "SELECT sync_token FROM todoist_sync_state WHERE key = 'main'"
            )
        token = token or "*"
        result = await self.connector.sync(
            token, resource_types=["items", "projects", "labels", "notes"]
        )
        if not result.get("ok"):
            return {
                "sync_token": "",
                "projects": [],
                "items": [],
                "labels": [],
                "error": result.get("error"),
            }
        return result["data"]
