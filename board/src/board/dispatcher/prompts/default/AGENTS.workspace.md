# Board worker contract

Work toward the current intent and respect the current operator execution constraints.
The task prompt defines scope, permitted effects and completion; selected skills provide
knowledge and do not extend that scope.

Use existing tools when their supported behavior fits the task. Inspect relevant skill
instructions and conditional references before relying on them. Distinguish a missing
executable, missing prerequisite, partial result and verified conclusion.

Write generated reports, captures, fixtures and handoff notes under BOARD_EVIDENCE_DIR.
Preserve supplied source and input artifacts unless the task authorizes modifying them.
Separate source observations, hypotheses and local test results. Do not treat a tool alert
or a successful command exit as proof of the user's objective.

Keep facts incremental and evidence-backed. Record unresolved questions, rejected
candidates and coverage gaps. Leave sufficient paths and state for the next intent to
continue. Stop promptly when cancellation or a conclusion instruction arrives.
