"""Production-shaped fake data for the orchestrator dashboard visual check."""

from datetime import datetime, timedelta, timezone


_BASE = datetime.now(timezone.utc)


def iso(minutes_ago):
    # one fixed "now" per run, so created_at (the selection key) is stable across polls
    t = _BASE - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


LONG_REPO = "soulstream-server-src-with-an-extremely-long-repository-name-that-keeps-going"
LONG_NODE = "eias-linegames-wsl-production-secondary-node-with-a-very-long-name"
LONG_BRANCH = "release/2026-11-hotfix-event-ingress-dispatcher-reconnect-after-reset-by-peer"
LONG_MSG = (
    "Reconnect the outbox drain after a reset by peer instead of exiting the whole "
    "dispatcher process and losing the in-flight deliveries (#2094)"
)
LONG_SERVICE = "soulstream-event-ingress-dispatcher-outbox-drain-worker"

DIFF_A = (
    " src/haniel/core/runner.py              | 42 +++++++++++++++++++++++---------\n"
    " src/haniel/core/deploy_coordinator.py  | 18 ++++++++++++----\n"
    " 2 files changed, 47 insertions(+), 13 deletions(-)"
)
ERR_LONG = (
    "verification_failed: soulstream-event-ingress-dispatcher did not become ready within 60s\n"
    "(http://127.0.0.1:3121/health returned 503 Service Unavailable).\n"
    "Rolled back to d143f0d.\n"
    "Journal: /home/eias/haniel-releases/journal/2026-11-26T03-12-08Z-e9416d7.json"
)


def _deploy(did, node, repo, branch, status, commits, services, stat, age, **extra):
    d = {
        "deploy_id": did,
        "node_id": node,
        "repo": repo,
        "branch": branch,
        "status": status,
        "commits": commits,
        "affected_services": services,
        "diff_stat": stat,
        "detected_at": iso(age),
        "approved_by": None,
        "reject_reason": None,
        "error": None,
        "duration_ms": None,
        "created_at": iso(age),
        "updated_at": iso(age),
        "is_self_update": False,
    }
    d.update(extra)
    return d


def _pending_row(did, node, repo, branch, status, commits, services, stat, age, **extra):
    # the fields a pending row carries on top of a history row (api.py get_pending_deploys)
    base = {"inspection_required": False, "last_failure": None, "apply_policy": None, "progress_stage": None}
    base.update(extra)
    return _deploy(did, node, repo, branch, status, commits, services, stat, age, **base)


def _failure(error, reason="deploy_result_failed", age=30):
    return {"kind": "orchestrator_attempt", "attempt_id": "att-1", "reason": reason, "error": error, "at": iso(age)}


def pending(long=False, returned=False):
    repo = LONG_REPO if long else "soulstream-server-src"
    node = LONG_NODE if long else "eiaserinnys"
    branch = LONG_BRANCH if long else "release/2026-11-hotfix-event-ingress-dispatcher"
    msg = LONG_MSG if long else "Reconnect the outbox drain after a reset by peer instead of exiting"
    self_commits = [
        "e9416d7 Restart services stopped by a deployment when the new config still declares them (#104)",
        "d143f0d Adopt declared services across self-update (#103)",
    ]
    lore = [
        "b7e1f00 Cache embeddings per shard", "b7d0a12 Rebuild the shard map on startup",
        "a9c3e44 Drop the legacy sqlite index", "a8812bd Warm the cache in the background",
        "a7f0c19 Add /health/shards", "a6e2d77 Pin faiss-cpu to 1.9",
        "a5b1c08 Log slow queries over 500ms",
    ]
    # `returned`: the deploying self-update failed and is waiting again (status_change pending, last_failure set)
    deploying_self = (
        _pending_row("eias-linegames:haniel:main:e9416d7", "eias-linegames", "haniel", "main",
                     "pending", self_commits, ["haniel"], DIFF_A, 24, is_self_update=True,
                     apply_policy="approval", inspection_required=True,
                     last_failure=_failure("verification_failed: haniel did not become ready within 60s. Rolled back to d143f0d.", age=1))
        if returned else
        _pending_row("eias-linegames:haniel:main:e9416d7", "eias-linegames", "haniel", "main",
                     "deploying", self_commits, ["haniel"], DIFF_A, 24, is_self_update=True,
                     progress_stage="starting")
    )
    return [
        _pending_row("eias-linegames-wsl:haniel:main:e9416d7", "eias-linegames-wsl", "haniel", "main",
                     "pending", self_commits, ["haniel"], DIFF_A, 20, is_self_update=True, apply_policy="approval"),
        deploying_self,
        _pending_row("eias-linegames:soulstream:main:a1b2c3d", "eias-linegames", "soulstream", "main",
                "pending",
                ["a1b2c3d Fix retry backoff for intervene delivery (#2091)",
                 "9c0d1e2 Keep card brief when the assignee changes (#2090)",
                 "77ab012 Show PAS queue depth in the folder header (#2088)",
                 "5e6f7a8 Bump claude-agent-sdk to 0.9.4 (#2087)"],
                ["soulstream-server", "soulstream-orch", "soul-dashboard"],
                " server/src/intervene/delivery.ts        | 96 ++++++++++++-----\n"
                " 9 files changed, 312 insertions(+), 87 deletions(-)", 12),
        _pending_row(f"{node}:{repo}:{branch}:4f2a91c", node, repo, branch, "pending",
                     [f"4f2a91c {msg}"], [LONG_SERVICE if long else "soulstream-event-ingress-dispatcher"],
                     " ingress/dispatcher.py   | 44 ++++++++++++++++++++++----\n"
                     " 3 files changed, 58 insertions(+), 4 deletions(-)", 8,
                     inspection_required=True, last_failure=_failure(ERR_LONG, age=15)),
        _pending_row("eiaserinnys:lore-search:main:b7e1f00", "eiaserinnys", "lore-search", "main",
                     "pending", lore, ["lore-search"],
                     " lore_search/index/shard.py   | 210 ++++++++++++-------\n"
                     " 14 files changed, 1204 insertions(+), 388 deletions(-)", 120, apply_policy="approval"),
        _pending_row("silent-manari-ts:serendipity:main:7f6be6c", "silent-manari-ts", "serendipity", "main",
                "pending",
                ["7f6be6c test: fix broken unit, browser and visual checks on main (#72)",
                 "d9ff94f Index renewal for the web shell (#68)"],
                ["serendipity-api", "serendipity-web"],
                " packages/web/src/styles/color.css | 88 ++++++++------\n"
                " 6 files changed, 140 insertions(+), 61 deletions(-)", 180,
                apply_policy="approval",
                last_failure=_failure("preflight_timeout: pnpm build did not finish within 600s", "preflight_timeout", 170)),
    ]


def _svc(name, port, pid, up_s, role, health, deps=(), enabled=True):
    running = pid is not None
    return {
        "name": name, "port": port, "pid": pid,
        "process_status": "running" if running else "stopped",
        "health_status": health, "ready": health == "ready" and running,
        "role": role, "uptime_ms": up_s * 1000 if running else 0,
        "enabled": enabled, "deps": list(deps),
    }


def _fetches(*pairs):
    # (repo, minutes since the last fetch or None)
    return [{"repo": repo, "last_fetch": None if ago is None else iso(ago)} for repo, ago in pairs]


def _serving(repo, sha, ago_min):
    return {"repo": repo, "branch": "main", "target_head": sha, "deployed_at": iso(ago_min),
            "deploy_id": f"x:{repo}:main:{sha[:7]}"}


def nodes(long=False):
    long_svc = LONG_SERVICE if long else "soulstream-event-ingress-dispatcher"
    long_repo = LONG_REPO if long else "soulstream-server-src"
    return [
        {"node_id": LONG_NODE if long else "eiaserinnys", "hostname": "v2202609419556520961",
         "os": "linux", "arch": "x86_64", "haniel_version": "e9416d7", "connected": 1,
         "last_seen": iso(0), "created_at": iso(9000),
         "poll_interval": 60,
         "repo_fetches": _fetches(("haniel", 2), (long_repo, 2), ("lore-search", 3), ("serendipity", None)),
         "serving": [_serving("haniel", "e9416d7a1b2c3d4e5f60718293a4b5c6d7e8f901", 37),
                     _serving(long_repo, "4f2a91c0d1e2f30415263748596a7b8c9d0e1f23", 2900),
                     _serving("lore-search", "b6c0d11e22f33a44b55c66d77e88f99aa0b1c2d3", 1500)],
         "self_update_deploying": False, "self_update_deploy_id": None,
         "services": [
             _svc("haniel-orchestrator", 9300, 1204, 3 * 86400 + 4 * 3600, "haniel", "ready"),
             _svc("soulstream-server", 3105, 81234, 8040, "soulstream", "ready"),
             _svc("soulstream-orch", 3110, 81240, 7980, "soulstream", "ready", ["soulstream-server"]),
             _svc(long_svc, 3121, None, 0, "soulstream-server-src", "crashed", ["soulstream-server"]),
             _svc("soul-dashboard", 5173, 81302, 12, "soulstream", "starting", ["soulstream-orch"]),
             _svc("serendipity-api", 4100, 22110, 6 * 86400 + 7200, "serendipity", "running"),
             _svc("serendipity-web", 4173, None, 0, "serendipity", "stopped", ["serendipity-api"]),
             _svc("lore-search", 8765, None, 0, "lore-search", "circuit_open"),
             _svc("mtg-edh-recommend", 8090, None, 0, "mtg-edh-recommend", "stopped", enabled=False),
         ]},
        {"node_id": "eias-linegames", "hostname": "LINEGAMES-PC", "os": "windows", "arch": "AMD64",
         "haniel_version": "d143f0d", "connected": 1, "last_seen": iso(0), "created_at": iso(9000),
         "poll_interval": 300,
         "repo_fetches": _fetches(("haniel", 4), ("soulstream", 0.3)),
         "serving": [_serving("haniel", "d143f0d9a8b7c6d5e4f30211a2b3c4d5e6f70819", 9000),
                     _serving("soulstream", "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", 16)],
         "self_update_deploying": False, "self_update_deploy_id": None,
         "services": [
             _svc("soulstream-server", 3105, 14420, 97200, "soulstream", "ready"),
             _svc("eb-portal-builder", None, 9921, 20400, "eb-portal", "running"),
         ]},
        {"node_id": "eias-linegames-wsl", "hostname": "LINEGAMES-WSL", "os": "linux", "arch": "x86_64",
         "haniel_version": "d143f0d", "connected": 1, "last_seen": iso(0), "created_at": iso(9000),
         # a node that predates the new fields: nothing polled, nothing recorded
         "poll_interval": None, "repo_fetches": None, "serving": [],
         "self_update_deploying": False, "self_update_deploy_id": None},
        {"node_id": "silent-manari-ts", "hostname": "silent-manari", "os": "linux", "arch": "aarch64",
         "haniel_version": "d143f0d", "connected": 0, "last_seen": iso(1080), "created_at": iso(9000),
         "poll_interval": None, "repo_fetches": None, "serving": [],
         "self_update_deploying": False, "self_update_deploy_id": None,
         "services": []},
        {"node_id": "eiaserinnys-canary", "hostname": "canary", "os": "linux", "arch": "x86_64",
         "haniel_version": "d143f0d", "connected": 0, "last_seen": iso(1), "created_at": iso(9000),
         "poll_interval": None, "repo_fetches": None,
         "serving": [_serving("haniel", "d143f0d9a8b7c6d5e4f30211a2b3c4d5e6f70819", 9000)],
         "self_update_deploying": True, "self_update_deploy_id": "eiaserinnys-canary:haniel:main:e9416d7",
         "services": []},
    ]


def history(include_superseded, long=False):
    repo = LONG_REPO if long else "soulstream-server-src"
    node = LONG_NODE if long else "eiaserinnys"
    msg = LONG_MSG if long else "Split the event ingress dispatcher into its own service"

    def h(did, age, status, r, n, commit, **extra):
        return _deploy(did, n, r, "main", status, [commit], [], None, age, **extra)

    rows = [
        h("h1", 5, "deploying", "haniel", "eias-linegames",
          "e9416d7 Restart services stopped by a deployment when the new config still declares them (#104)",
          approved_by="director", duration_ms=72000),
        h("h2", 16, "success", "soulstream", "eias-linegames",
          "a1b2c3d Fix retry backoff for intervene delivery (#2091)", approved_by="director", duration_ms=84000),
        h("h3", 34, "failed", "haniel", "eiaserinnys",
          "e9416d7 Restart services stopped by a deployment when the new config still declares them (#104)",
          approved_by="director", duration_ms=123000, error=ERR_LONG,
          terminal_kind="deploy_result_failed", terminal_stage="execution",
          terminal_reason="deploy_result_failed", terminal_error=ERR_LONG,
          failed_stage="starting", recovery_attempted=True),
        h("h4", 48, "rejected", repo, node, f"4f2a91c {msg}", reject_reason="WIP — 아직 검토 중"),
        h("h6", 100, "pending", "serendipity", "silent-manari-ts",
          "7f6be6c test: fix broken unit, browser and visual checks on main (#72)"),
        h("h7", 1500, "success", "lore-search", "eias-linegames-wsl", "b6c0d11 Shard the embedding index",
          approved_by="auto_apply", duration_ms=41200),
        h("h8", 1600, "failed", "serendipity", "eiaserinnys", "d9ff94f Index renewal for the web shell (#68)",
          approved_by="director", duration_ms=600000,
          error="preflight_timeout: pnpm build did not finish within 600s."),
        h("h12", 1650, "failed", "lore-search", "eias-linegames-wsl", "a9c3e44 Drop the legacy sqlite index",
          approved_by="director", duration_ms=30000, error="node did not acknowledge the approval",
          terminal_kind="ack_timeout", terminal_stage="deadline", terminal_reason="ack_timeout",
          terminal_error="node did not acknowledge the approval",
          failed_stage=None, recovery_attempted=None),
        h("h13", 1655, "failed", "soulstream", "eiaserinnys", "5e6f7a8 Bump claude-agent-sdk to 0.9.4 (#2087)",
          approved_by="director", duration_ms=90000,
          error="verification_failed: soulstream-server did not become ready within 60s",
          terminal_kind="deploy_result_failed", terminal_stage="execution", terminal_reason="deploy_result_failed",
          terminal_error="verification_failed: soulstream-server did not become ready within 60s",
          failed_stage="verifying", recovery_attempted=False),
        # recovery_attempted only says the node entered recovery; whether it recovered is in the node's sentence
        h("h14", 1660, "failed", "serendipity", "eias-linegames", "7f6be6c test: fix broken unit, browser and visual checks on main (#72)",
          approved_by="director", duration_ms=48000,
          error="migration_failed: schema 41 -> 42 aborted; recovery failed, services are down",
          terminal_kind="deploy_result_failed", terminal_stage="execution", terminal_reason="deploy_result_failed",
          terminal_error="migration_failed: schema 41 -> 42 aborted; recovery failed, services are down",
          failed_stage="migrating", recovery_attempted=True),
        h("h9", 1700, "success", "soulstream", "eiaserinnys", "5e6f7a8 Bump claude-agent-sdk to 0.9.4 (#2087)",
          approved_by="director", duration_ms=161000),
        h("h11", 4000, "success", "haniel", "silent-manari-ts",
          "d143f0d Adopt declared services across self-update (#103)", approved_by="auto_apply",
          duration_ms=55000),
    ]
    if include_superseded:
        rows.insert(4, h("h5", 60, "rejected", "soulstream", "eias-linegames",
                         "9c0d1e2 Keep card brief when the assignee changes (#2090)",
                         reject_reason="superseded by a1b2c3d"))
        rows.insert(8, h("h10", 1650, "rejected", "soulstream", "eiaserinnys",
                         "3d4e5f6 Show PAS queue depth in the folder header (#2088)",
                         reject_reason="superseded by 5e6f7a8"))
    return rows
