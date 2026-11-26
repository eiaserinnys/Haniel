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


def pending(long=False):
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
    return [
        _deploy("eias-linegames-wsl:haniel:main:e9416d7", "eias-linegames-wsl", "haniel", "main",
                "pending", self_commits, ["haniel"], DIFF_A, 20, is_self_update=True),
        _deploy("eias-linegames:haniel:main:e9416d7", "eias-linegames", "haniel", "main",
                "deploying", self_commits, ["haniel"], DIFF_A, 24, is_self_update=True),
        _deploy("eias-linegames:soulstream:main:a1b2c3d", "eias-linegames", "soulstream", "main",
                "pending",
                ["a1b2c3d Fix retry backoff for intervene delivery (#2091)",
                 "9c0d1e2 Keep card brief when the assignee changes (#2090)",
                 "77ab012 Show PAS queue depth in the folder header (#2088)",
                 "5e6f7a8 Bump claude-agent-sdk to 0.9.4 (#2087)"],
                ["soulstream-server", "soulstream-orch", "soul-dashboard"],
                " server/src/intervene/delivery.ts        | 96 ++++++++++++-----\n"
                " 9 files changed, 312 insertions(+), 87 deletions(-)", 12),
        _deploy(f"{node}:{repo}:{branch}:4f2a91c", node, repo, branch, "pending",
                [f"4f2a91c {msg}"], [LONG_SERVICE if long else "soulstream-event-ingress-dispatcher"],
                " ingress/dispatcher.py   | 44 ++++++++++++++++++++++----\n"
                " 3 files changed, 58 insertions(+), 4 deletions(-)", 8),
        _deploy("eiaserinnys:lore-search:main:b7e1f00", "eiaserinnys", "lore-search", "main",
                "pending", lore, ["lore-search"],
                " lore_search/index/shard.py   | 210 ++++++++++++-------\n"
                " 14 files changed, 1204 insertions(+), 388 deletions(-)", 120),
        _deploy("silent-manari-ts:serendipity:main:7f6be6c", "silent-manari-ts", "serendipity", "main",
                "pending",
                ["7f6be6c test: fix broken unit, browser and visual checks on main (#72)",
                 "d9ff94f Index renewal for the web shell (#68)"],
                ["serendipity-api", "serendipity-web"],
                " packages/web/src/styles/color.css | 88 ++++++++------\n"
                " 6 files changed, 140 insertions(+), 61 deletions(-)", 180),
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


def nodes(long=False):
    long_svc = LONG_SERVICE if long else "soulstream-event-ingress-dispatcher"
    return [
        {"node_id": LONG_NODE if long else "eiaserinnys", "hostname": "v2202609419556520961",
         "os": "linux", "arch": "x86_64", "haniel_version": "e9416d7", "connected": 1,
         "last_seen": iso(0), "created_at": iso(9000),
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
         "services": [
             _svc("soulstream-server", 3105, 14420, 97200, "soulstream", "ready"),
             _svc("eb-portal-builder", None, 9921, 20400, "eb-portal", "running"),
         ]},
        {"node_id": "eias-linegames-wsl", "hostname": "LINEGAMES-WSL", "os": "linux", "arch": "x86_64",
         "haniel_version": "d143f0d", "connected": 1, "last_seen": iso(0), "created_at": iso(9000)},
        {"node_id": "silent-manari-ts", "hostname": "silent-manari", "os": "linux", "arch": "aarch64",
         "haniel_version": "d143f0d", "connected": 0, "last_seen": iso(1080), "created_at": iso(9000),
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
          approved_by="director", duration_ms=123000, error=ERR_LONG),
        h("h4", 48, "rejected", repo, node, f"4f2a91c {msg}", reject_reason="WIP — 아직 검토 중"),
        h("h6", 100, "pending", "serendipity", "silent-manari-ts",
          "7f6be6c test: fix broken unit, browser and visual checks on main (#72)"),
        h("h7", 1500, "success", "lore-search", "eias-linegames-wsl", "b6c0d11 Shard the embedding index",
          approved_by="auto_apply", duration_ms=41200),
        h("h8", 1600, "failed", "serendipity", "eiaserinnys", "d9ff94f Index renewal for the web shell (#68)",
          approved_by="director", duration_ms=600000,
          error="preflight_timeout: pnpm build did not finish within 600s."),
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
