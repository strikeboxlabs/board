# Planner reliability and efficiency

The planner uses graph evidence to decide whether to finish, add useful independent work, or wait. `target_open_intents` is an upper target, not a quota. Planning shares the configured project worker limit with exploration; this does not promise three simultaneous explorers while planning is running. Blocked intents count toward the target. Graph changes during a planning call remain pending for the next decision; cooldown coalesces rapid changes. Pending exploration can still dispatch during planner backoff.

Pi planning uses one tool-free request with the complete graph attached as a file. The explicit extension applies `pi_reasoning_effort` and `pi_max_output_tokens` to OpenAI-compatible completion requests only. Other providers retain their native limits. The default maximum is 16384 output tokens, with low effort requested; the backend ultimately determines how effort is applied. Exploration settings and host Pi credentials are unchanged.

The Pi adapter explicitly rejects output-limit, empty-answer, and missing-answer results rather than parsing stream metadata. Per-process metrics preserve stop reason and token usage. An output cap still bounds an unsuccessful request; it is not a guarantee of a usable answer.

Retries are limited to three attempts per graph/policy revision, with 30/60-second delays before the second/third attempt. The state persists in each project's `reports/planner-state.json`; a restart does not reset exhausted attempts. After success, a 15-second cooldown applies. New facts, hints, changed intents, job status, or reason configuration permit reconsideration after the cooldown. Lease heartbeats do not. Corrupt/unreadable state blocks planning and logs an error. An explicit `data.wait` explanation is permitted even with no open intents; it does not mark the goal complete. A wait remains idle until relevant state changes.

To explicitly retry an exhausted unchanged graph, add a meaningful hint through Board, or, after resolving the underlying failure, move aside that project's `planner-state.json` while the dispatcher is stopped. Preserve it for diagnosis. Do not automate state removal or defeat the retry budget.

Metrics are appended to `reports/efficiency.jsonl` for new executions, and summarized with:

```
board efficiency proj_002 --root /data/cairn-runs
```

The summary reports usage, phase execution time, task outcomes, current observed graph counts/status, and successful explore tasks per million recorded tokens. Reasoning is included in output. Cache reads are reported separately; counts are not billing prices. Unsupported adapters report missing usage, not zero-cost work. Successful tasks and facts are activity proxies, not proof of semantic value.

For a fair evaluation, compare matched tasks or fixed time windows with the same models and exploration limits. Review which conclusions contributed new, supported information relevant to the stated goal, count redundant investigations, and compare useful conclusions per recorded token plus elapsed time to proven completion. Include failed planner calls in the denominator. Background jobs may still run during periods without active explorers. No fixed speedup or cost reduction is guaranteed.
