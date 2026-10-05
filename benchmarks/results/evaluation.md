# DAOGraph offline research benchmark

Split: evaluation. Development-selected fixed depth: 4.

## 📊 Measured outcomes

| Mode | Correct outcomes | Planner calls | Retrieval attempts | Invalid citations |
| --- | --- | --- | --- | --- |
| fixed | 36/36 | 36 | 105 | 0 |
| always | 36/36 | 225 | 105 | 0 |
| selective | 36/36 | 138 | 105 | 0 |

Measured planner-call reduction: 38.7%.

## ✅ Release gates

- zero_incorrect_assertions: PASS
- zero_invalid_citations: PASS
- all_expected_outcomes: PASS
- quality_matches_always: PASS
- quality_no_worse_than_fixed: PASS
- planner_reduction_at_least_30_percent: PASS
- zero_duplicate_requests: PASS
- no_more_retrieval_than_always: PASS
- repeated_runs_identical: PASS

## 🔎 Scope and limitations

These project-authored structured scenarios test control behavior. They do not
measure arbitrary language entailment, model token cost, web-search quality, or
production workloads. No model is called. Planner-call reduction is not a measured
monetary saving. JSON contains every case, family, trace, fixed-depth candidate,
and integrity hash; elapsed time is informational. Scorer labels never enter the policy.
