"""Rendering layer for Strikebox reports and exports.

Everything in this package is a pure function of ``(db, evidence_root)``:
the blackboard database plus the on-disk evidence store are the only
inputs, and every consumer (the export/report API routes, the dispatcher
auto-write hooks and the ``board export`` / ``board report`` CLI
commands) imports these renderers instead of keeping its own copy.
"""
