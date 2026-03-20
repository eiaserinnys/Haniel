"""
haniel - Configuration-based, intentionally indifferent service runner.

haniel doesn't care what it runs. It checks git repos, pulls changes,
and starts processes as specified. That's all it does.
"""

__version__ = "0.1.0"
__author__ = "Dorothy"

# Exit codes (see ADR-0002)
EXIT_CLEAN = 0
EXIT_SELF_UPDATE = 10
EXIT_RESTART = 11


class SelfUpdateExit(SystemExit):
    """Raised when haniel needs to exit for self-update.

    The wrapper script (haniel-runner.ps1) interprets exit code 10
    as a signal to update haniel and restart.
    """

    def __init__(self) -> None:
        super().__init__(EXIT_SELF_UPDATE)
