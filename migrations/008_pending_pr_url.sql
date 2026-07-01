-- Add pr_url to pending_prs and extend status to include 'failed'.
ALTER TABLE pending_prs ADD COLUMN IF NOT EXISTS pr_url TEXT NOT NULL DEFAULT '';
ALTER TABLE pending_prs DROP CONSTRAINT IF EXISTS pending_prs_status_valid;
ALTER TABLE pending_prs ADD CONSTRAINT pending_prs_status_valid
    CHECK (status IN ('pending', 'opened', 'closed', 'discarded', 'failed'));
