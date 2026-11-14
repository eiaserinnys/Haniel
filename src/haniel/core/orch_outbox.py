"""Durable terminal bundles, retained until the orchestrator acknowledges them."""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class OrchOutbox:
    def __init__(self, config_dir: Path):
        self.path = config_dir / ".local" / "orch_outbox.json"
        self.lock = threading.RLock()
        self.entries = {}
        self.ack_path = self.path.with_name("orch_outbox_acked.json")
        self.acked = {}
        self.correlation_path = self.path.with_name("orch_outbox_correlation.json")
        self.correlations = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("entries"), dict):
                raise ValueError("invalid orchestrator outbox")
            self.entries = data["entries"]
        if self.ack_path.exists():
            try:
                self.acked = json.loads(self.ack_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning(
                    "Failed to read outbox acknowledgement index", exc_info=True
                )
        if self.correlation_path.exists():
            try:
                self.correlations = json.loads(
                    self.correlation_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                logger.warning("Failed to read outbox correlation index", exc_info=True)

    @staticmethod
    def key(letter):
        attempt = letter.get("orchestrator_attempt_id")
        if attempt:
            return "o:" + attempt
        attempt = letter.get("node_attempt_id")
        if attempt:
            return "n:" + attempt
        raise ValueError("terminal letter has no attempt identity")

    def _write(self, path, payload):
        temp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as handle:
                temp = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            return True
        except OSError:
            logger.exception(
                "Failed to persist orchestrator outbox; retaining it in memory"
            )
            return False
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "Failed to remove outbox temporary file", exc_info=True
                    )

    def _save(self):
        return self._write(self.path, {"version": 1, "entries": self.entries})

    def put(self, letters):
        key = self.key(letters[0])
        with self.lock:
            if key in self.entries:
                if self.entries[key]["letters"] != letters:
                    logger.warning("Retaining original outbox bundle for %s", key)
                return self._save()
            self.entries[key] = {
                "deploy_id": letters[0].get("deploy_id"),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "letters": letters,
            }
            return self._save()

    def ack(self, key):
        with self.lock:
            if key not in self.entries:
                return False
            entry = self.entries.pop(key)
            parts = (entry.get("deploy_id") or "").split(":", 3)
            if len(parts) == 4:
                self.acked[parts[1]] = key
                self._write(self.ack_path, self.acked)
            if key in self.correlations:
                del self.correlations[key]
                self._write(self.correlation_path, self.correlations)
            self._save()
            return True

    def was_acked(self, repo, key):
        with self.lock:
            return self.acked.get(repo) == key

    def remember(self, repo, key, generation):
        with self.lock:
            self.correlations[key] = {"repo": repo, "generation": generation}
            return self._write(self.correlation_path, self.correlations)

    def correlation(self, repo, key):
        with self.lock:
            value = self.correlations.get(key, {})
            return value.get("generation") if value.get("repo") == repo else None

    def prune_correlations(self, retained):
        with self.lock:
            self.correlations = {
                key: value
                for key, value in self.correlations.items()
                if key in retained or key in self.entries
            }
            return self._write(self.correlation_path, self.correlations)

    def letters(self):
        with self.lock:
            return [list(entry["letters"]) for entry in self.entries.values()]
