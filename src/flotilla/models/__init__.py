"""Shared vocabulary: pure data shapes and functions every layer may depend on.

This package is the bottom of the dependency graph. It holds the value objects, error
categories, plan and verification shapes that cross layer boundaries, and the pure
functions that validate them. It imports no other flotilla package, and contains no
behavior: nothing here talks to a platform, parses a task, or schedules anything.

Import what you need from the specific module (`flotilla.models.plan`,
`flotilla.models.platform`, ...); symbols stay where they are defined.
"""
