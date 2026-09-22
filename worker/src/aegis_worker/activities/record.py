"""RecordActivities — the owner's record in the vault (vault record spec §5, §12).

* `notes_compile_record` is the last step of the hourly NotesSyncFlow: the
  `me/` notes into each agent's `user` document, through
  `aegis.services.record`.
* `record_seed_general`, `record_seed_money` and `record_seed_interests` are
  RecordSeedFlow's three drafters, through `aegis.services.record_seed`.

The layout is read from the pool on every call, so the switch on the admin
page applies without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aegis.services import books, notes
from aegis.services import record as vault_record
from aegis.services import record_seed as rs
from aegis.services.vault_layout import get_layout
from temporalio import activity


@dataclass
class RecordActivities:
    settings: Any = None
    db_pool: Any = None
    llm_client: Any = None
    model: str = ""

    def _cfg(self) -> notes.NotesConfig:
        return notes.config_from_settings(self.settings)

    @activity.defn
    async def notes_compile_record(self) -> dict:
        """Compile the record into the `user` rows. `{"status": "off"}` and no
        write of any kind while `record.enabled` is off."""
        layout = await get_layout(self.db_pool)
        if not layout.record.enabled:
            return {"status": "off"}
        return await vault_record.compile_all(self.db_pool, self._cfg(), layout)

    @activity.defn
    async def record_seed_general(self, agent_id: str) -> dict:
        """The about/work/people/health drafts, as the `gtd` holder (spec §12)."""
        return await rs.draft_general(
            self.db_pool, self._cfg(), await get_layout(self.db_pool), self.llm_client, self.model, agent_id
        )

    @activity.defn
    async def record_seed_money(self, agent_id: str) -> dict:
        """The money draft, as the `finance` holder: columns, no model, no amounts."""
        return await rs.draft_money(
            self.db_pool, self._cfg(), books.config_from_settings(self.settings),
            await get_layout(self.db_pool), agent_id,
        )

    @activity.defn
    async def record_seed_interests(self, agent_id: str) -> dict:
        """The interests draft, as the `research` holder: one model call over titles and tags."""
        return await rs.draft_interests(
            self.db_pool, self._cfg(), await get_layout(self.db_pool), self.llm_client, self.model, agent_id
        )
