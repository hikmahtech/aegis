import { useEffect, useState } from 'react';
import { toast } from '../components/Toast';

// The load / saving / error state machine every settings-row form repeats.
//
// A settings page is the same five lines each time: read the row on mount,
// keep an `error` for the banner and a `saving` flag for the button, and on
// save clear the error, write, toast, and clear the flag whatever happened.
// Written out per form it drifts — a `finally` that forgets `setSaving(false)`
// leaves the button dead until a reload, and a missing `setError(null)` leaves
// yesterday's failure on screen above a successful save.
//
// The form's own state stays in the form: a panel turns its row into text
// fields, and how it does that is not shared. So `load` and `write` do their
// own applying and this hook owns only the three pieces of state around them.
//
//   const { error, setError, loading, saving, save } = useConfigRow(load);
//   …
//   <button disabled={saving} onClick={() => save(write, 'Saved. …')}>
//
// `save` takes the message to toast on success; omit it for a form that shows
// its own confirmation instead.

export function useConfigRow(load: () => Promise<void>) {
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  async function reload() {
    setError(null);
    setLoading(true);
    try {
      await load();
    } catch (e: any) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void reload(); }, []);

  async function save(write: () => Promise<void>, ok?: string) {
    setSaving(true);
    setError(null);
    try {
      await write();
      if (ok) toast.ok(ok);
    } catch (e: any) {
      setError(e);
    } finally {
      setSaving(false);
    }
  }

  return { error, setError, loading, saving, save, reload };
}
