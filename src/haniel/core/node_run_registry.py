"""In-memory execution leaves and their durable terminal bundles."""

from statemachine.exceptions import TransitionNotAllowed

from .node_run import initial_row, step
from .orch_outbox import OrchOutbox


class NodeRunRegistry:
    @staticmethod
    def _failed(entry):
        letters = entry["letters"]
        return any(
            letter.get("status") == "failed"
            or letter.get("phase") == "failed"
            or (
                letter.get("type") == "repo_reconciliation"
                and letter.get("phase") == "settled"
                and letter.get("local_head") != letter.get("remote_head")
            )
            for letter in letters
        )

    def __init__(self, config_dir):
        self.outbox = OrchOutbox(config_dir)
        self.lock = self.outbox.lock
        self.runs = {}
        for key, entry in self.outbox.entries.items():
            failed = self._failed(entry)
            self.runs[key[2:] if key.startswith("o:") else key] = {
                "deploy_id": entry["deploy_id"],
                "row": {"exec_": "failed" if failed else "ok", "report": "r_owed"},
                "reason": None,
                "auto": False,
                "letters": entry["letters"],
            }

    def _advance(self, run, event, facts=None):
        run["row"], todo = step(run["row"], event, facts)
        return todo

    def permit(self, deploy_id, attempt_id, *, auto, waiting):
        with self.lock:
            old = self.runs.get(attempt_id)
            if old is not None and old["row"]["exec_"] != "asking":
                if (
                    old["row"]["exec_"] == "declined"
                    and old.get("reason") is None
                    and auto
                    and not waiting
                ):
                    old["reason"] = "no_waiter"
                todo = self._advance(old, "permit")
                return todo[0], old.get("reason")
            reason = None
            for run in self.runs.values():
                if run["deploy_id"] == deploy_id and run["row"]["exec_"] in {
                    "queued",
                    "running",
                }:
                    reason = "already_held"
                    break
            if reason is None:
                for entry in self.outbox.entries.values():
                    if entry["deploy_id"] != deploy_id:
                        continue
                    failed = self._failed(entry)
                    if not failed:
                        reason = "success_owed"
                        break
                    if auto:
                        reason = "failure_owed"
                        break
            if reason is None and auto and not waiting:
                reason = "no_waiter"
            run = old or {
                "deploy_id": deploy_id,
                "row": initial_row(),
                "reason": reason,
                "auto": auto,
            }
            run["reason"] = reason
            self.runs[attempt_id] = run
            todo = self._advance(
                run,
                "permit",
                {
                    "holds_other": reason is not None and reason != "no_waiter",
                    "unexpected_auto": reason == "no_waiter",
                    "free": False,
                },
            )
            return todo[0], reason

    def ask(self, deploy_id, attempt_id):
        with self.lock:
            run = {
                "deploy_id": deploy_id,
                "row": initial_row(),
                "reason": None,
                "auto": True,
            }
            self.runs[attempt_id] = run
            return self._advance(run, "ask")

    def give_up(self, attempt_id):
        with self.lock:
            run = self.runs.get(attempt_id)
            if run and run["row"]["exec_"] == "asking":
                run["reason"] = "no_waiter"
                return self._advance(run, "give_up")
            return []

    def start(self, attempt_id, *, reason=None):
        with self.lock:
            run = self.runs[attempt_id]
            if reason is None:
                for other_id, other in self.runs.items():
                    if (
                        other_id != attempt_id
                        and other["deploy_id"] == run["deploy_id"]
                        and other["row"]["exec_"] == "running"
                    ):
                        reason = "already_held"
                        break
            if reason is None:
                for entry in self.outbox.entries.values():
                    if entry["deploy_id"] != run["deploy_id"]:
                        continue
                    failed = self._failed(entry)
                    if not failed:
                        reason = "success_owed"
                        break
                    if run.get("auto"):
                        reason = "failure_owed"
                        break
            self.runs[attempt_id]["reason"] = reason
            return self._advance(
                self.runs[attempt_id],
                "slot_free",
                {
                    "holds_other": reason is not None
                    and reason != "approval_revalidation_failed",
                    "unexpected_auto": False,
                    "revalidation_failed": reason == "approval_revalidation_failed",
                },
            )

    def own_start(self, attempt_id, deploy_id):
        with self.lock:
            key = "n:" + attempt_id
            run = {
                "deploy_id": deploy_id,
                "row": initial_row(),
                "reason": None,
                "auto": False,
            }
            self.runs[key] = run
            return self._advance(run, "own_start")

    def acknowledge(self, message):
        with self.lock:
            key = self.outbox.key(message)
            if key not in self.outbox.entries:
                return False
            run = self.runs.get(key[2:] if key.startswith("o:") else key)
            if run is not None:
                self._advance(run, "terminal_ack")
            return self.outbox.ack(key)

    def connection(self, up):
        with self.lock:
            result = []
            for run in self.runs.values():
                was_asking = run["row"]["exec_"] == "asking"
                try:
                    todo = self._advance(run, "link_up" if up else "link_down")
                except TransitionNotAllowed:
                    continue
                if not up and was_asking:
                    run["reason"] = "no_waiter"
                result.append((run.get("approval"), todo))
            return result

    def held(self):
        with self.lock:
            result = []
            for attempt_id, run in self.runs.items():
                state = run["row"]["exec_"]
                if state in {"queued", "running"}:
                    result.append(
                        {
                            "orchestrator_attempt_id": None
                            if attempt_id.startswith("n:")
                            else attempt_id,
                            "node_attempt_id": attempt_id[2:]
                            if attempt_id.startswith("n:")
                            else None,
                            "deploy_id": run["deploy_id"],
                            "state": state,
                        }
                    )
            for key, entry in self.outbox.entries.items():
                result.append(
                    {
                        "orchestrator_attempt_id": key[2:]
                        if key.startswith("o:")
                        else None,
                        "node_attempt_id": key[2:] if key.startswith("n:") else None,
                        "deploy_id": entry["deploy_id"],
                        "state": "owed",
                    }
                )
            return result

    def finish(self, attempt_id, letters, *, success):
        with self.lock:
            todo = self._advance(
                self.runs[attempt_id], "finish_ok" if success else "finish_fail"
            )
            if "bundle" in todo:
                self.runs[attempt_id]["letters"] = letters
                self.runs[attempt_id]["persisted"] = self.outbox.put(letters)
            return todo

    def enqueue_owed(self, letters):
        with self.lock:
            key = self.outbox.key(letters[0])
            run_key = key[2:] if key.startswith("o:") else key
            run = self.runs.get(run_key)
            failed = self._failed({"letters": letters})
            if run and run["row"]["exec_"] == "running":
                self.finish(run_key, letters, success=not failed)
                return run["persisted"]
            saved = self.outbox.put(letters)
            if run is None:
                self.runs[run_key] = {
                    "deploy_id": letters[0].get("deploy_id"),
                    "row": {"exec_": "failed" if failed else "ok", "report": "r_owed"},
                    "reason": None,
                    "auto": False,
                    "letters": letters,
                }
            return saved
