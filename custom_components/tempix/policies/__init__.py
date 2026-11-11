"""V2 policy pipeline: Goals, Modifiers and Safety Constraints.

Policies are pure: they read only the ``TempixSnapshot`` and produce new
immutable ``PipelineState`` instances (Copy-on-Write). Ordering
is defined exclusively by ``GoalClass`` / ``ConstraintPriority`` — never
by registration or file order.
"""
