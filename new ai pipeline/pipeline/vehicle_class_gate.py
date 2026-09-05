"""
pipeline/vehicle_class_gate.py
--------------------------------
Hard guard clause: helmet and triple-riding checks must never evaluate
against a vehicle that isn't confirmed as a two-wheeler.

Bug 2.4 root cause
------------------
The rule engine in rules.py checked helmet/rider counts on every
tracked vehicle without first confirming it was a motorcycle or bicycle.
This produced false positives: a car with passengers seen from certain
angles was flagged for "helmet violation" or "triple riding".

Fix
---
This is a guard clause, not a threshold tweak. The check simply refuses
to evaluate at all unless the vehicle class is in TWO_WHEELER_CLASSES.

Usage in rules.py
-----------------
    from pipeline.vehicle_class_gate import is_two_wheeler, guard_two_wheeler

    # Option 1: boolean check
    if not is_two_wheeler(vehicle_class):
        return  # skip helmet/triple-riding entirely

    # Option 2: decorator / context guard (raises GuardFailed on non-two-wheeler)
    guard_two_wheeler(vehicle_class, track_id=track_id)
"""

from __future__ import annotations

TWO_WHEELER_CLASSES: frozenset[str] = frozenset({
    "motorcycle",
    "bicycle",
    "motorbike",
    "bike",
    "two_wheeler",
    "scooter",
    "moped",
})


class GuardFailed(Exception):
    """Raised when a check is blocked by the vehicle-class gate."""


def is_two_wheeler(vehicle_class: str | None) -> bool:
    """
    True if vehicle_class is a confirmed two-wheeler.

    Deliberately strict: returns False for None, 'unknown', 'vehicle'
    (generic catch-all), and any four-wheeled class. If the class is
    uncertain, it is safer to skip the check than to risk a false
    positive on a car.
    """
    if not vehicle_class:
        return False
    return vehicle_class.strip().lower() in TWO_WHEELER_CLASSES


def guard_two_wheeler(vehicle_class: str | None, *, track_id: int = -1) -> None:
    """
    Raises GuardFailed if vehicle_class is not a confirmed two-wheeler.

    Use this at the top of any function that should only run for
    motorcycles/bicycles, so the caller's logic never runs for cars:

        def check_helmet_violation(vehicle_class, ...):
            guard_two_wheeler(vehicle_class, track_id=track_id)
            # ... rest of helmet logic here

    The GuardFailed exception message includes track_id for debugging.
    """
    if not is_two_wheeler(vehicle_class):
        raise GuardFailed(
            f"Vehicle class '{vehicle_class}' (track {track_id}) is not a "
            "confirmed two-wheeler — helmet/triple-riding checks skipped."
        )


def safe_two_wheeler_check(fn):
    """
    Decorator version of guard_two_wheeler for functions whose first
    positional argument is vehicle_class.

    Usage:
        @safe_two_wheeler_check
        def check_triple_riding(vehicle_class, rider_count, track_id=-1):
            ...  # only runs when vehicle_class is a two-wheeler

    Returns None silently when the guard blocks the call.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(vehicle_class, *args, **kwargs):
        if not is_two_wheeler(vehicle_class):
            return None
        return fn(vehicle_class, *args, **kwargs)

    return wrapper
