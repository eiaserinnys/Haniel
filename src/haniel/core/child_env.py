"""
Environment sanitization for child processes.

haniel itself is often supervised by pm2, which injects Node.js IPC
variables (NODE_CHANNEL_FD and friends) into its direct child. Those
variables leak down to every descendant haniel spawns. A Node.js child
that inherits NODE_CHANNEL_FD tries to open that file descriptor as an
IPC channel; the fd does not refer to a valid pipe in our children, so
it collides with libuv's own descriptors (typically the epoll fd) and
the process dies with SIGABRT in uv__epoll_ctl_prep during teardown.

Long-running services never hit the teardown path, but short-lived
lifecycle hooks abort on every run. Strip the variables unconditionally:
they are never meaningful for processes haniel spawns.
"""

import os

# Node.js child-process IPC variables injected by pm2 (and by any other
# Node supervisor using child_process.fork).
_NODE_IPC_VARS = (
    "NODE_CHANNEL_FD",
    "NODE_CHANNEL_SERIALIZATION_MODE",
    "NODE_UNIQUE_ID",
)


def sanitized_child_env() -> dict[str, str]:
    """Return a copy of os.environ safe to pass to spawned children."""
    env = dict(os.environ)
    for var in _NODE_IPC_VARS:
        env.pop(var, None)
    return env
