# Conductance-Field Attention Architecture for Bounded Symbolic Inference
PRD - Product Requirements Document

Version: 2.0  
Status: Implementation-ready  
Date: 2026-04-24  
Target repo: `mettaclaw`  
Owner: OmegaClaw Core Maintainers

---

## 1. Executive Summary

This PRD defines a deterministic, bounded inference scheduler that converts symbolic inference from an unbounded pair search into a compute-budgeted attention allocation process.

The architecture separates three concerns:

1. Candidate generation from graph structure
2. Candidate prioritization from learned and stateful attention signals
3. Truth-preserving inference and revision through existing NAL/PLN rules

Expected outcome: at least 10x fewer candidate evaluations versus naive O(N^2) pairing while increasing useful inference yield per compute unit.

---

## 2. Problem Statement

### 2.1 Current State in This Repository

- Inference rules exist in `lib_nal.metta` and `lib_pln.metta`.
- Runtime orchestration is in `src/loop.metta`.
- Skill dispatch, including raw `(metta ...)` execution, is in `src/skills.metta`.
- The current path can invoke inference directly but has no dedicated bounded candidate scheduler between memory/structure and NAL/PLN execution.

### 2.2 Core Problem

When symbolic space grows, naive pairing of atoms/rules causes:

- Explosive candidate count
- Unstable latency per cycle
- Poor compute-to-yield ratio
- No deterministic budget partition between inference and contradiction resolution

---

## 3. Goals and Non-Goals

### 3.1 Goals

- G1: Reduce evaluated candidate pairs by >= 10x versus naive O(N^2) baseline.
- G2: Keep inference cycle deterministic and bounded by explicit budgets.
- G3: Keep NAL/PLN truth semantics unchanged.
- G4: Route contradictions toward revision without starving inference.
- G5: Demonstrate stable execution for >= 10,000 cycles.

### 3.2 Non-Goals

- NG1: Rewriting NAL or PLN truth formulas.
- NG2: Replacing existing MeTTa rule libraries.
- NG3: Adding non-deterministic stochastic search.
- NG4: Building distributed execution in this phase.

---

## 4. Architecture Overview

Pipeline per cycle:

1. Structural candidate generation
2. Conductance scoring
3. SPH-based attention transport
4. Contradiction gating
5. Deterministic queueing into inference queue and revision queue
6. NAL/PLN execution with unchanged rule semantics

Design principle: attention ranks which pairs are worth evaluating; reasoning engines still decide truth updates.

---

## 5. Functional Requirements

### FR-1: Structural Candidate Generator

The system SHALL produce candidate pairs from structural connectivity only (not full cross-product).

Requirements:

- Deterministic traversal order
- Sub-quadratic candidate count in practice
- Valid pair typing only (rule/rule, rule/fact, fact/fact by configured policy)
- Support depth/window constraints

Acceptance:

- Candidate count <= 5% of naive pair count on benchmark sets
- Repeated runs with identical inputs produce identical candidate list

---

### FR-2: Conductance Field Scoring

Each candidate `(i,j)` SHALL receive a conductance score:

`c(i,j) = sigmoid(w_R*R + w_H*H + w_IG*IG - w_C*Cost + b)`

Where:

- `R`: structural relevance [0,1]
- `H`: Hebbian association [0,1]
- `IG`: incentive/importance gain [0,1]
- `Cost`: normalized execution cost [0,1]

Requirements:

- All input terms normalized to [0,1]
- Score clamped to `(eps, 1-eps)` for numerical stability
- Monotonic in each independent dimension by sign

Acceptance:

- No NaN or Inf in score output
- Monotonicity property tests pass for each input axis

---

### FR-3: Sparse Hebbian Layer with Lazy Decay

The system SHALL maintain sparse pair-association state:

`H_t(i,j) = H_prev(i,j)*exp(-lambda*delta_t) + eta*STI(i)*STI(j)`

Requirements:

- O(k) updates per touched neighborhood
- Sparse storage with bounded top-k neighbors per node
- Lazy decay on read/write access (no full-table decay sweep)
- Hard memory cap and eviction policy

Acceptance:

- Memory usage stays below configured cap
- Update cost scales with touched pairs, not global atom count

---

### FR-4: SPH Attention Transport

Attention mass SHALL diffuse over candidate neighborhoods via SPH-style kernel weighting:

- Base kernel: `W(r,h)`
- Effective transport: `W_eff(i,j) = W(r(i,j), h_i) * c(i,j)`

Requirements:

- Atom structure is fixed; only attention mass propagates
- Adaptive smoothing radius:
  - `h_i = clamp(h_min, h_base + k_uncertainty*U_i, h_max)`
- Conservation tolerance and damping to prevent oscillation

Acceptance:

- No divergent oscillation in stress tests
- Locality preserved (mass does not jump arbitrarily far in one step)

---

### FR-5: Contradiction Detection and Gating

The system SHALL estimate contradiction pressure per candidate and split inference from revision pressure.

Definitions:

- Inference gate: `G = sigmoid(-beta*contradiction)`
- Revision pressure: `S_rev = contradiction * confidence * IG`

Requirements:

- Higher contradiction lowers inference priority via `G`
- Higher contradiction raises revision queue score via `S_rev`
- Starvation guard reserves minimal inference and revision quotas every cycle

Acceptance:

- Contradiction-heavy scenarios increase revision throughput
- No zero-throughput starvation for either queue under prolonged conflict

---

### FR-6: Deterministic Dual-Queue Scheduler

The scheduler SHALL emit two disjoint queues every cycle:

- Inference queue: top-K by `score_inf`
- Revision queue: top-K by `score_rev`

Requirements:

- Separate configurable budgets: `K_inf`, `K_rev`
- Deterministic tie-break: score desc, then lexical pair ID asc
- Budget carry-over policy explicitly defined and deterministic

Acceptance:

- Queue outputs are bit-identical across repeated runs with same input

---

### FR-7: NAL/PLN Integration Boundary

The attention stack SHALL feed selected pairs to existing inference engines without changing truth logic.

Requirements:

- NAL entry remains through existing `|-` machinery (`lib_nal.metta`)
- PLN entry remains through existing `|~pln` rules (`lib_pln.metta`)
- Adapter layer only selects/evaluates candidate invocation order
- Output truth values and revision formulas unchanged

Acceptance:

- Regression tests show identical truth output for identical evaluated pair sequences

---

### FR-8: Runtime Controls and Safety Bounds

The system SHALL expose cycle-level bounds:

- `candidate_budget`
- `inference_budget`
- `revision_budget`
- `attention_time_budget_ms`
- `max_hebbian_edges`
- `max_attention_mass`

Acceptance:

- Runtime never exceeds configured hard budgets except explicit fail-fast path

---

## 6. Data Contracts

Canonical candidate record:

```text
Candidate {
  pair_id: string
  lhs_atom_id: string
  rhs_atom_id: string
  relation_type: string
  R: float
  H: float
  IG: float
  Cost: float
  conductance: float
  contradiction: float
  score_inf: float
  score_rev: float
}
```

Queue outputs:

```text
InferenceQueue: [pair_id...]
RevisionQueue:  [pair_id...]
```

Deterministic identity:

- `pair_id = canonical_hash(min(lhs,rhs), max(lhs,rhs), relation_type)`

---

## 7. Repository-Level Implementation Plan

### 7.1 New Modules

- `src/attention_types.metta`
- `src/attention_candidates.metta`
- `src/attention_hebbian.metta`
- `src/attention_conductance.metta`
- `src/attention_sph.metta`
- `src/attention_contradiction.metta`
- `src/attention_scheduler.metta`
- `src/attention_metrics.metta`

### 7.2 Modified Modules

- `src/loop.metta`
  - Add bounded attention cycle call before inference dispatch
  - Add config defaults and runtime knobs
- `src/skills.metta`
  - Add bounded symbolic skill entrypoint (for example `bounded-metta`)
  - Keep existing `metta` behavior for compatibility
- `run.metta`
  - Import new attention modules
- Optional Python bridge:
  - `src/helper.py` for benchmark harness I/O only if needed

### 7.3 Backward Compatibility

- Existing `metta` skill remains available.
- New bounded path is opt-in via config flag:
  - `attention_enabled True|False`

---

## 8. Algorithm Details and Defaults

### 8.1 Default Weights

- `w_R = 0.40`
- `w_H = 0.25`
- `w_IG = 0.25`
- `w_C = 0.20`
- `b = 0.00`

### 8.2 Hebbian Defaults

- `eta = 0.05`
- `lambda = 0.001`
- `hebbian_top_k = 32`
- `max_hebbian_edges = 200000`

### 8.3 SPH Defaults

- `h_min = 0.5`
- `h_base = 1.0`
- `h_max = 3.0`
- `k_uncertainty = 1.0`
- `transport_damping = 0.15`

### 8.4 Contradiction Defaults

- `beta = 4.0`
- `min_inference_quota = 1`
- `min_revision_quota = 1`

---

## 9. Non-Functional Requirements

### NFR-1 Determinism

- Identical inputs and configs must produce identical queues and outputs.

### NFR-2 Stability

- No attention collapse or oscillatory divergence over 10k-cycle run.

### NFR-3 Bounded Memory

- Respect configured hard cap for Hebbian and queue structures.

### NFR-4 Performance

- Candidate generation and scheduling must remain sub-quadratic in practical workloads.

### NFR-5 Observability

- Emit cycle metrics and saturation warnings.

---

## 10. Metrics and Telemetry

Per-cycle required metrics:

- `atoms_seen`
- `candidates_generated`
- `candidate_reduction_ratio` (vs naive baseline)
- `inference_queue_len`
- `revision_queue_len`
- `revision_fraction`
- `avg_conductance`
- `attention_entropy`
- `contradiction_rate`
- `cycle_latency_ms`
- `truth_updates_count`

Rollup KPIs:

- Throughput gain (updates/sec)
- Useful inference yield per 1k candidate evaluations
- Mean contradiction resolution latency (cycles)

---

## 11. Test Plan and Acceptance Matrix

### 11.1 Unit Tests

- U1: Conductance monotonicity per feature axis
- U2: Hebbian lazy decay equivalence to eager decay on sampled pairs
- U3: Queue tie-break determinism
- U4: Contradiction gate monotonic response
- U5: SPH transport stability under fixed synthetic graph

### 11.2 Integration Tests

- I1: End-to-end dual queue emits deterministic outputs from same seed state
- I2: NAL/PLN output equivalence when fed identical candidate sequences
- I3: Budget saturation path does not exceed hard caps
- I4: Contradiction-heavy workload increases revision queue utilization

### 11.3 Benchmark Tests

- B1: Candidate count <= 5% of naive pairing
- B2: >= 10x reduction in evaluated candidates
- B3: >= 10,000 cycles without instability
- B4: >= 10x throughput gain on representative workloads

Acceptance gate: all U/I tests pass, and at least B1+B2+B3 pass on CI benchmark profile before merge; B4 required before default-enable.

---

## 12. Rollout Plan

### Phase A (Feature Complete, Off by Default)

- Implement all FR modules
- Add metrics and tests
- Run synthetic benchmarks

Exit criteria:

- Unit + integration tests green
- No determinism regressions

### Phase B (Shadow Mode)

- Run bounded scheduler in parallel with legacy path
- Compare queue quality and truth update yield

Exit criteria:

- Meets B1/B2/B3 thresholds
- No major truth-regression deltas

### Phase C (Default On, Legacy Fallback Preserved)

- Enable `attention_enabled=True` by default
- Keep flag to disable rapidly if required

Exit criteria:

- B4 reached in production-like environment
- Operational stability over multi-day run

---

## 13. Risks and Mitigations

- Risk: Premature pruning of valid inference paths  
  Mitigation: increase `K_inf`, reduce `w_C`, lower `beta`

- Risk: Hebbian lock-in and reduced exploration  
  Mitigation: stronger decay (`lambda`), entropy floor, periodic edge pruning

- Risk: Attention diffusion collapse  
  Mitigation: damping, entropy regularization, radius clamps

- Risk: Revision starvation under low contradiction  
  Mitigation: enforce `min_revision_quota`

- Risk: Latency spikes from pathological neighborhoods  
  Mitigation: neighborhood caps and early budget stop

---

## 14. Definition of Done

This PRD is considered finished when all conditions below are true:

1. All FR-1..FR-8 implemented in repository modules.
2. NFR-1..NFR-5 validated by automated tests and benchmark logs.
3. Acceptance tests B1/B2/B3 pass in CI benchmark profile.
4. B4 throughput target met before default-on rollout.
5. Architecture and config documented in repo docs.
6. Feature can be disabled via single runtime flag without code changes.

---

## 15. Key Insight

The system reframes symbolic inference from an operator search problem into a deterministic allocation problem over a bounded attention field.

Truth remains the job of NAL/PLN; attention decides where finite compute is spent.

---
