# Reference — Reasoning Skill

Defined in `src/skills.metta`. Backed by two reasoning engines in `lib_nal.metta` and `lib_pln.metta`.

---

## `metta`

### Signature
```metta
(metta sexpression)
```

### Purpose
Evaluate an arbitrary MeTTa s-expression in the agent's AtomSpace. Primary use is to invoke **NAL** (`|-`) or **PLN** (`|~`) inference from within the agent loop.

### Parameters
- `sexpression` — a MeTTa s-expression. Read by `sread`, evaluated by `eval`.

### Returns
Whatever the inner expression returns. For NAL/PLN calls, this is a conclusion atom paired with an `(stv frequency confidence)` truth value.

### Examples

**NAL — deduction:**
```metta
(metta (|- ((--> (× sam garfield) friend) (stv 1.0 0.9))
           ((--> garfield animal)         (stv 1.0 0.9))))
```

**NAL — implication with a variable (note `$1`):**
```metta
(metta (|- ((==> (--> (× $1 elephant) eat) (--> $1 ([] dangerous))) (stv 1.0 0.9))
           ((--> (× tiger elephant) eat)                            (stv 1.0 0.9))))
```

**NAL — revision** (same term, two sources): `|-` merges the evidence.

**PLN — forward chaining:**
```metta
(metta (|~ ((Implication (Inheritance $1 (IntSet Feathered))
                         (Inheritance $1 Bird)) (stv 1.0 0.9))
           ((Inheritance Pingu (IntSet Feathered)) (stv 1.0 0.9))))
```

---

## `bounded-metta`

### Signature
```metta
(bounded-metta json_payload)
```

### Purpose
Run bounded symbolic attention scheduling over a set of premises and return deterministic inference/revision queues under hard budgets.

### Parameters
- `json_payload` — JSON string with:
  - `engine`: `"nal"` or `"pln"`
  - `premises`: list of premise strings, each shaped like `((TERM) (stv f c))`
  - optional `budgets` / `config` overrides

### Returns
JSON string containing:
- `all_candidates` with per-candidate features (`R`, `H`, `IG`, `Cost`, conductance, contradiction, scores)
- `inference_queue` and `revision_queue` (disjoint, deterministic)
- `metrics` and any `warnings` (for example budget saturation)

### Example
```text
(bounded-metta "{\"engine\":\"nal\",\"premises\":[\"((--> sam human) (stv 1.0 0.9))\",\"((--> human mortal) (stv 1.0 0.9))\",\"((--> sam mortal) (stv 1.0 0.4))\"],\"budgets\":{\"inference_budget\":2,\"revision_budget\":1}}")
```

### Related control skills
- `bounded-metta-exec` — plans and executes selected `(|-)` / `(|~)` calls in one step.
- `attention-reset` — clear Hebbian/metric state.
- `attention-state` — inspect current attention cycle/state snapshot.

---

## Engine selection, stopping criteria, action thresholds

These are policy decisions. `metta` executes direct reasoning calls; `bounded-metta` performs compute-bounded candidate prioritization before selecting which calls should run. See [reference-orchestration.md](./reference-orchestration.md) for the full policy tables.

---

## Notes / limits

- Independent variables are written `$1`, `$2`, …
- Negated knowledge uses `(stv 0.0 c)`.
- `metta` evaluates **any** MeTTa expression, not just reasoning calls. Malformed input reports errors through `&error` on the next turn.
- Confidence decays ~10% per deduction hop. Chains past 3 hops usually fall below the ACT threshold — see [tutorial-08-reliable-reasoning.md](./tutorial-08-reliable-reasoning.md).
- Premise formulation is the primary failure surface. Verify term order, copula, and granularity before trusting a conclusion. See [reference-failure-modes.md](./reference-failure-modes.md).

---

## See also

- [reference-lib-nal.md](./reference-lib-nal.md) — NAL rule catalogue.
- [reference-lib-pln.md](./reference-lib-pln.md) — PLN rule catalogue.
- [reference-lib-ona.md](./reference-lib-ona.md) — ONA temporal reasoning (experimental, not installed).
- [reference-orchestration.md](./reference-orchestration.md) — full orchestration policy.
- [tutorial-05-reasoning-with-nal-pln.md](./tutorial-05-reasoning-with-nal-pln.md) — worked examples.
