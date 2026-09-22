"""RecordActivities — the owner's record in the vault (vault record spec §5, §12).

`notes_compile_record` is the last step of the hourly NotesSyncFlow: the
`me/` notes into each agent's `user` document, through
`aegis.services.record`. The seed's drafters are added in the next change.
The layout is read from the pool on every call, so the switch on the admin
page applies without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aegis.services import notes
from aegis.services import record as vault_record
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
