import json
import os
import tempfile
from pathlib import Path
from typing import Dict

from .utils.logger import get_logger

_SCHEMA_VERSION = 1


class StateStore:
    """Persists node and host transition state across restarts.

    Without this, a restart wipes the in-memory history: the first cycle back up
    treats every node as newly seen and suppresses its transition notification,
    so a restart during an incident hides the incident. Host states are
    persisted for the same reason — a fresh process silently re-syncs hosts
    instead of reporting what changed.

    Persistence is best-effort by design. A read-only or missing state directory
    degrades to in-memory behaviour and logs a warning; it never takes the
    monitor down.
    """

    def __init__(self, path: str = "data/state.json"):
        self.path = Path(path)
        self.logger = get_logger(__name__)
        self.node_states: Dict[str, bool] = {}
        self.host_states: Dict[str, bool] = {}
        self._writable = True
        self._dirty = False

    def load(self) -> None:
        if not self.path.exists():
            self.logger.info(f"No previous state at {self.path}, starting fresh")
            return

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self.logger.warning(f"Could not read state from {self.path}, starting fresh: {e}")
            return

        if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
            self.logger.warning(f"Ignoring incompatible state file at {self.path}")
            return

        self.node_states = self._coerce_bool_map(data.get("node_states"))
        self.host_states = self._coerce_bool_map(data.get("host_states"))
        self.logger.info(
            f"Restored state from {self.path}: "
            f"{len(self.node_states)} node(s), {len(self.host_states)} host(s)"
        )

    @staticmethod
    def _coerce_bool_map(raw) -> Dict[str, bool]:
        if not isinstance(raw, dict):
            return {}
        return {str(k): bool(v) for k, v in raw.items() if isinstance(v, bool)}

    def mark_dirty(self) -> None:
        self._dirty = True

    def save(self, force: bool = False) -> None:
        if not self._writable or (not self._dirty and not force):
            return

        payload = {
            "version": _SCHEMA_VERSION,
            "node_states": self.node_states,
            "host_states": self.host_states,
        }

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file in the same directory, then rename: a crash
            # mid-write must not leave a truncated state file behind.
            fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.path)
            except BaseException:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except OSError as e:
            self._writable = False
            self.logger.warning(
                f"State directory {self.path.parent} is not writable, continuing without persistence "
                f"(mount a volume there to keep state across restarts): {e}"
            )
            return

        self._dirty = False
