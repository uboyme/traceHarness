# Evaluation comparison

Run: defc017b-c542-40d6-89f8-5bdb35bb99b2
Status: regressed
Measurement complete: True

Descriptive results only; this report grants no adoption or installation authority.

| Case | Replicate | Baseline | Candidate | Change |
|---|---|---|---|---|
| pvlib__pvlib-python-1154 | 1 | passed | passed | unchanged |

## Frozen result

```json
{
  "format": 1,
  "run_id": "defc017b-c542-40d6-89f8-5bdb35bb99b2",
  "benchmark_id": "traceh-real-repository-dev-v1",
  "task_type": "product_task",
  "experiment_digest": "d65dcc6cc3ec77fe4f6d66b46d36704dd289f5fc933a7596004eba75fe086123",
  "execution_run": "C:\\Users\\caojie\\tae-sv1",
  "adoption_authorized": false,
  "remote_model_revision": "unavailable",
  "complete": true,
  "planned_trials": [
    {
      "case_id": "pvlib__pvlib-python-1154",
      "group_id": "pvlib--pvlib-python",
      "material_digest": "fcdb91abb6326f4b59f0c7c3c46f3f283abc797acd123233d01fcabcc0084923",
      "material_seed": null,
      "replicate": 1,
      "requested_mode": "single",
      "trial_id": "f1ae5536a5a7d6aaefe175cbbc629d409e63e440a8b91708bce61e1e587449d4",
      "variant_id": "baseline"
    },
    {
      "case_id": "pvlib__pvlib-python-1154",
      "group_id": "pvlib--pvlib-python",
      "material_digest": "fcdb91abb6326f4b59f0c7c3c46f3f283abc797acd123233d01fcabcc0084923",
      "material_seed": null,
      "replicate": 1,
      "requested_mode": "single",
      "trial_id": "4aaf9d6cc473c44fd5a785ad38e003507ba63911c3c355d31deacf09774f30a3",
      "variant_id": "candidate"
    }
  ],
  "planned_pairs": 1,
  "status": "regressed",
  "quality_status": "no_change",
  "pairs": [
    {
      "key": {
        "case_id": "pvlib__pvlib-python-1154",
        "group_id": "pvlib--pvlib-python",
        "material_digest": "fcdb91abb6326f4b59f0c7c3c46f3f283abc797acd123233d01fcabcc0084923",
        "material_seed": null,
        "replicate": 1,
        "requested_mode": "single"
      },
      "change": "unchanged",
      "requested_modes": [
        "single",
        "single"
      ],
      "reason": null,
      "assessment": [
        "passed",
        "passed"
      ],
      "execution": [
        {
          "reason": null,
          "status": "completed"
        },
        {
          "reason": null,
          "status": "completed"
        }
      ],
      "invariants": [
        "passed",
        "passed"
      ],
      "convergence": [
        "converged",
        "converged"
      ],
      "metrics": [
        {
          "attempts": 18,
          "unknown_attempts": 0,
          "estimated_attempts": 0,
          "total_tokens": 147323,
          "known_subtotal_tokens": 147323,
          "tool_calls": 15,
          "search_read_calls": 6,
          "repeated_call_arguments": 1,
          "non_success_tool_results": 1,
          "failed_attempts": 0,
          "phase_usage": {
            "execution": {
              "input_tokens": 137303,
              "output_tokens": 10020,
              "quality": "exact"
            },
            "unattributed": {
              "input_tokens": 0,
              "output_tokens": 0,
              "quality": "exact"
            }
          }
        },
        {
          "attempts": 19,
          "unknown_attempts": 0,
          "estimated_attempts": 0,
          "total_tokens": 166048,
          "known_subtotal_tokens": 166048,
          "tool_calls": 16,
          "search_read_calls": 4,
          "repeated_call_arguments": 0,
          "non_success_tool_results": 0,
          "failed_attempts": 0,
          "phase_usage": {
            "execution": {
              "input_tokens": 158501,
              "output_tokens": 7547,
              "quality": "exact"
            },
            "unattributed": {
              "input_tokens": 0,
              "output_tokens": 0,
              "quality": "exact"
            }
          }
        }
      ],
      "preparation_text_equal": true
    }
  ],
  "changes": {
    "gain": 0,
    "loss": 0,
    "unchanged": 1,
    "unknown": 0
  },
  "arms": [
    {
      "planned": 1,
      "assessment_counts": {
        "passed": 1
      },
      "passed_over_planned": 1.0,
      "assessable": 1,
      "cost": {
        "total_tokens": 147323,
        "tool_calls": 15
      }
    },
    {
      "planned": 1,
      "assessment_counts": {
        "passed": 1
      },
      "passed_over_planned": 1.0,
      "assessable": 1,
      "cost": {
        "total_tokens": 166048,
        "tool_calls": 16
      }
    }
  ],
  "groups": {
    "pvlib--pvlib-python": [
      {
        "passed": 1
      },
      {
        "passed": 1
      }
    ]
  },
  "cost_delta": {
    "total_tokens": 18725,
    "tool_calls": 1
  },
  "thresholds": {
    "min_pass_gain": true,
    "max_token_ratio": false,
    "max_tool_call_delta": false
  },
  "hard_constraints": "passed",
  "bindings": [
    {
      "run_id": "dcdc7809-94ab-48b1-92c8-cfce70df734a",
      "frozen_digest": "eef15b99bf7c6a5f00daca9d37f85b45d077f02cd6af1cfb2a4f0eeef2ab3e09",
      "evidence_digest": "22e8318416918f1762c3325fc91187d04ded201be0aa6cf2714ebcea5e95b619",
      "report_digest": "ae9ef2cf27a90a14bf0612070f6399f37f4f623a1f687e8a8f6d3a657826c4e2",
      "controls_digest": "c6252c708f73ed3c29fd3cadabc711d2dfdf4873ec184ef1b93dc19dd99723ea",
      "run_directory": "arms/01/run",
      "resource_convergence": "converged",
      "forced_stop": false
    },
    {
      "run_id": "2a5ffe58-cb19-44da-ad73-9603405e7dfc",
      "frozen_digest": "fdd5fec6527f69e1f56a9a48a49d13385641ff79bc3672f56c744231f02b7521",
      "evidence_digest": "c52b176d81d6a08a2eee4137f4c2f8bc975a7e60a435026911e06ee9fe2c70c6",
      "report_digest": "e16e248cd70f09b6504c1f58cc3f74c32d0c15c3bd12ab557e09528dba7e9a94",
      "controls_digest": "c6252c708f73ed3c29fd3cadabc711d2dfdf4873ec184ef1b93dc19dd99723ea",
      "run_directory": "arms/02/run",
      "resource_convergence": "converged",
      "forced_stop": false
    }
  ],
  "execution_errors": []
}
```
