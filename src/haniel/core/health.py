"""
Health monitoring for haniel services.

Implements:
- Circuit breaker pattern for crash loop detection
- Exponential backoff for restart delays
- Service state tracking
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class ServiceState(Enum):
    """Possible states of a managed service."""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"
    CIRCUIT_OPEN = "circuit_open"  # Circuit breaker tripped


@dataclass
class RestartRecord:
    """Record of a restart attempt."""

    timestamp: float
    exit_code: int | None
    reason: str


@dataclass
class ServiceHealth:
    """Health state for a single service.

    Tracks:
    - Current state
    - Restart history
    - Circuit breaker state
    - Backoff delay
    """

    service_name: str
    state: ServiceState = ServiceState.STOPPED
    start_time: float | None = None
    restart_count: int = 0
    consecutive_failures: int = 0
    last_exit_code: int | None = None
    restart_history: list[RestartRecord] = field(default_factory=list)

    # Circuit breaker settings (set by HealthManager)
    circuit_breaker_threshold: int = 5
    circuit_breaker_window: int = 300

    # Backoff settings (set by HealthManager)
    base_delay: int = 5
    max_delay: int = 300
    current_backoff: float = 0

    def record_start(self) -> None:
        """Record that the service started."""
        self.state = ServiceState.STARTING
        self.start_time = time.time()

    def record_ready(self) -> None:
        """Record that the service is ready."""
        self.state = ServiceState.READY
        # Reset consecutive failures on successful ready
        self.consecutive_failures = 0
        self.current_backoff = 0

    def record_running(self) -> None:
        """Record that the service is running (ready passed or no ready condition)."""
        self.state = ServiceState.RUNNING
        # Reset consecutive failures on successful start
        self.consecutive_failures = 0
        self.current_backoff = 0

    def record_stop(self) -> None:
        """Record that the service stopped gracefully."""
        self.state = ServiceState.STOPPED
        self.start_time = None

    def record_crash(self, exit_code: int | None, reason: str = "") -> None:
        """Record that the service crashed.

        Args:
            exit_code: The exit code of the process
            reason: Optional reason for the crash
        """
        self.state = ServiceState.CRASHED
        self.last_exit_code = exit_code
        self.consecutive_failures += 1
        self.restart_count += 1

        # Record in history
        record = RestartRecord(
            timestamp=time.time(),
            exit_code=exit_code,
            reason=reason,
        )
        self.restart_history.append(record)

        # Calculate backoff
        self._update_backoff()

    def record_circuit_open(self) -> None:
        """Record that the circuit breaker has tripped."""
        self.state = ServiceState.CIRCUIT_OPEN

    def reset_circuit(self) -> None:
        """Reset the circuit breaker, allowing restarts again."""
        self.state = ServiceState.STOPPED
        self.consecutive_failures = 0
        self.current_backoff = 0

    def _update_backoff(self) -> None:
        """Update the current backoff delay using exponential backoff."""
        # Exponential backoff: base_delay * 2^(failures-1)
        # Capped at max_delay
        exponent = min(
            self.consecutive_failures - 1, 10
        )  # Cap exponent to prevent overflow
        delay = self.base_delay * (2**exponent)
        self.current_backoff = min(delay, self.max_delay)

    def get_restart_delay(self) -> float:
        """Get the current restart delay.

        Returns:
            Delay in seconds before the next restart attempt
        """
        return self.current_backoff

    def should_circuit_break(self) -> bool:
        """Check if the circuit breaker should trip.

        Returns:
            True if too many failures within the window
        """
        if self.consecutive_failures < self.circuit_breaker_threshold:
            return False

        # Count failures within the window
        now = time.time()
        window_start = now - self.circuit_breaker_window
        failures_in_window = sum(
            1 for record in self.restart_history if record.timestamp >= window_start
        )

        return failures_in_window >= self.circuit_breaker_threshold

    def get_uptime(self) -> float | None:
        """Get the current uptime in seconds.

        Returns:
            Uptime in seconds, or None if not running
        """
        if self.start_time is None:
            return None
        if self.state not in (
            ServiceState.STARTING,
            ServiceState.READY,
            ServiceState.RUNNING,
        ):
            return None
        return time.time() - self.start_time


class HealthManager:
    """Manages health state for all services.

    Provides:
    - Circuit breaker pattern
    - Exponential backoff
    - State change callbacks
    """

    def __init__(
        self,
        base_delay: int = 5,
        max_delay: int = 300,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_window: int = 300,
    ):
        """Initialize the health manager.

        Args:
            base_delay: Base delay for exponential backoff (seconds)
            max_delay: Maximum delay for exponential backoff (seconds)
            circuit_breaker_threshold: Number of failures before circuit opens
            circuit_breaker_window: Time window for counting failures (seconds)
        """
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_window = circuit_breaker_window

        self._services: dict[str, ServiceHealth] = {}
        self._callbacks: list[Callable[[str, ServiceState, ServiceState], None]] = []

    def get_health(self, service_name: str) -> ServiceHealth:
        """Get or create health state for a service.

        Args:
            service_name: Name of the service

        Returns:
            ServiceHealth instance
        """
        if service_name not in self._services:
            self._services[service_name] = ServiceHealth(
                service_name=service_name,
                circuit_breaker_threshold=self.circuit_breaker_threshold,
                circuit_breaker_window=self.circuit_breaker_window,
                base_delay=self.base_delay,
                max_delay=self.max_delay,
            )
        return self._services[service_name]

    def record_start(self, service_name: str) -> None:
        """Record that a service started."""
        health = self.get_health(service_name)
        old_state = health.state
        health.record_start()
        self._notify_state_change(service_name, old_state, health.state)

    def record_ready(self, service_name: str) -> None:
        """Record that a service is ready."""
        health = self.get_health(service_name)
        old_state = health.state
        health.record_ready()
        self._notify_state_change(service_name, old_state, health.state)

    def record_running(self, service_name: str) -> None:
        """Record that a service is running."""
        health = self.get_health(service_name)
        old_state = health.state
        health.record_running()
        self._notify_state_change(service_name, old_state, health.state)

    def record_stop(self, service_name: str) -> None:
        """Record that a service stopped gracefully."""
        health = self.get_health(service_name)
        old_state = health.state
        health.record_stop()
        self._notify_state_change(service_name, old_state, health.state)

    def record_crash(
        self,
        service_name: str,
        exit_code: int | None = None,
        reason: str = "",
    ) -> float:
        """Record that a service crashed.

        Args:
            service_name: Name of the service
            exit_code: Exit code of the process
            reason: Reason for the crash

        Returns:
            Delay in seconds before restart (0 if circuit broken)
        """
        health = self.get_health(service_name)
        old_state = health.state
        health.record_crash(exit_code, reason)

        # Check circuit breaker
        if health.should_circuit_break():
            health.record_circuit_open()
            self._notify_state_change(service_name, old_state, health.state)
            return 0  # No restart when circuit is open

        self._notify_state_change(service_name, old_state, health.state)
        return health.get_restart_delay()

    def reset_circuit(self, service_name: str) -> None:
        """Reset the circuit breaker for a service.

        Args:
            service_name: Name of the service
        """
        health = self.get_health(service_name)
        old_state = health.state
        health.reset_circuit()
        self._notify_state_change(service_name, old_state, health.state)

    def should_restart(self, service_name: str) -> bool:
        """Check if a service should be restarted.

        Args:
            service_name: Name of the service

        Returns:
            True if the service should be restarted
        """
        health = self.get_health(service_name)
        return health.state != ServiceState.CIRCUIT_OPEN

    def add_callback(
        self,
        callback: Callable[[str, ServiceState, ServiceState], None],
    ) -> None:
        """Add a callback for state changes.

        Args:
            callback: Function(service_name, old_state, new_state)
        """
        self._callbacks.append(callback)

    def _notify_state_change(
        self,
        service_name: str,
        old_state: ServiceState,
        new_state: ServiceState,
    ) -> None:
        """Notify callbacks of a state change."""
        if old_state == new_state:
            return
        for callback in self._callbacks:
            try:
                callback(service_name, old_state, new_state)
            except Exception:
                # Don't let callback errors break health management
                pass

    def get_all_states(self) -> dict[str, ServiceState]:
        """Get the current state of all services.

        Returns:
            Dict mapping service names to their states
        """
        return {name: health.state for name, health in self._services.items()}

    def get_summary(self) -> dict:
        """Get a summary of all service health.

        Returns:
            Dict with service health information
        """
        return {
            name: {
                "state": health.state.value,
                "uptime": health.get_uptime(),
                "restart_count": health.restart_count,
                "consecutive_failures": health.consecutive_failures,
                "last_exit_code": health.last_exit_code,
            }
            for name, health in self._services.items()
        }
