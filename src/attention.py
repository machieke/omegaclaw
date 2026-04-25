from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


STV_RE = re.compile(
    r"\(stv\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\)"
)
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|-->|<->|==>|[\-\|~]+")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _safe_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _strip_outer_parens(expr: str) -> str:
    s = expr.strip()
    if not s.startswith("(") or not s.endswith(")"):
        return s
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(s):
        if in_string:
            if ch == '"' and not escaped:
                in_string = False
            escaped = (ch == "\\") and not escaped
            if ch != "\\":
                escaped = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i != len(s) - 1:
                return s
    return s[1:-1].strip() if depth == 0 else s


def _top_level_children(expr: str) -> List[str]:
    s = expr.strip()
    if not s:
        return []
    body = _strip_outer_parens(s) if s.startswith("(") and s.endswith(")") else s
    if body == s and not (s.startswith("(") and s.endswith(")")):
        return [s]

    out: List[str] = []
    buf: List[str] = []
    depth = 0
    in_string = False
    escaped = False
    for ch in body:
        if in_string:
            buf.append(ch)
            if ch == '"' and not escaped:
                in_string = False
            escaped = (ch == "\\") and not escaped
            if ch != "\\":
                escaped = False
            continue

        if ch == '"':
            in_string = True
            buf.append(ch)
            continue

        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth -= 1
            buf.append(ch)
            continue

        if ch.isspace() and depth == 0:
            token = "".join(buf).strip()
            if token:
                out.append(token)
            buf = []
            continue

        buf.append(ch)

    token = "".join(buf).strip()
    if token:
        out.append(token)
    return out


def _normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _extract_stv(expr: str) -> Tuple[float, float]:
    m = STV_RE.search(expr)
    if not m:
        return 0.5, 0.5
    return _clamp(_safe_float(m.group(1), 0.5), 0.0, 1.0), _clamp(
        _safe_float(m.group(2), 0.5), 0.0, 1.0
    )


def _extract_term(expr: str) -> str:
    children = _top_level_children(expr)
    if not children:
        return _normalize_space(expr)
    return _normalize_space(children[0])


def _term_head(term: str) -> str:
    parts = _top_level_children(term)
    if not parts:
        return _normalize_space(term)
    return parts[0]


def _term_subj_pred(term: str) -> Tuple[str, str]:
    parts = _top_level_children(term)
    if len(parts) < 3:
        return "", ""
    head = parts[0]
    if head in {"-->", "<->", "==>", "Implication", "Inheritance", "Similarity", "Member"}:
        return parts[1], parts[2]
    return "", ""


def _expectation(f: float, c: float) -> float:
    return _clamp((c * (f - 0.5)) + 0.5, 0.0, 1.0)


def _term_tokens(term: str) -> List[str]:
    ignored = {"stv", "-->", "<->", "==>", "|-", "|~", "Implication", "Inheritance"}
    return [t for t in TOKEN_RE.findall(term) if t and t not in ignored]


def _canonical_pair(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _negated_of(term: str) -> Optional[str]:
    parts = _top_level_children(term)
    if len(parts) == 2 and parts[0] in {"¬", "Not"}:
        return _normalize_space(parts[1])
    return None


@dataclass
class Settings:
    candidate_budget: int = 256
    inference_budget: int = 8
    revision_budget: int = 4
    attention_time_budget_ms: int = 50

    w_R: float = 0.40
    w_H: float = 0.25
    w_IG: float = 0.25
    w_C: float = 0.20
    b: float = 0.00
    eps: float = 1e-6

    eta: float = 0.05
    decay_lambda: float = 0.001
    max_hebbian_edges: int = 200000

    h_min: float = 0.5
    h_base: float = 1.0
    h_max: float = 3.0
    k_uncertainty: float = 1.0
    transport_damping: float = 0.15

    beta: float = 4.0
    min_inference_quota: int = 1
    min_revision_quota: int = 1


@dataclass
class Premise:
    pid: str
    expr: str
    term: str
    term_key: str
    head: str
    subj: str
    pred: str
    f: float
    c: float
    sti: float
    ig: float
    cost: float
    tokens: Tuple[str, ...]


@dataclass
class Candidate:
    pair_id: str
    a: Premise
    b: Premise
    R: float
    H: float
    IG: float
    Cost: float
    conductance: float
    transported: float
    contradiction: float
    gate: float
    score_inf: float
    score_rev: float


@dataclass
class HebbianEdge:
    value: float
    last_cycle: int


class _AttentionState:
    def __init__(self) -> None:
        self.cycle = 0
        self.hebbian: Dict[Tuple[str, str], HebbianEdge] = {}
        self.last_metrics: Dict[str, Any] = {}
        self.last_plan_invocations: List[str] = []


_STATE = _AttentionState()


def _read_settings(payload: Dict[str, Any]) -> Settings:
    raw = payload.get("config", {}) if isinstance(payload.get("config"), dict) else {}
    raw_budget = payload.get("budgets", {}) if isinstance(payload.get("budgets"), dict) else {}
    cfg = {**raw, **raw_budget}
    s = Settings()
    for key in Settings.__dataclass_fields__.keys():
        if key not in cfg:
            continue
        current = getattr(s, key)
        if isinstance(current, int):
            setattr(s, key, int(cfg[key]))
        else:
            setattr(s, key, float(cfg[key]))
    s.candidate_budget = max(1, s.candidate_budget)
    s.inference_budget = max(0, s.inference_budget)
    s.revision_budget = max(0, s.revision_budget)
    s.attention_time_budget_ms = max(1, s.attention_time_budget_ms)
    s.max_hebbian_edges = max(1, s.max_hebbian_edges)
    s.min_inference_quota = max(0, min(s.min_inference_quota, s.inference_budget))
    s.min_revision_quota = max(0, min(s.min_revision_quota, s.revision_budget))
    s.eps = max(1e-9, s.eps)
    return s


def _premise_from_input(item: Any, idx: int) -> Premise:
    if isinstance(item, dict):
        pid = str(item.get("id", f"p{idx+1}"))
        expr = _normalize_space(str(item.get("expr", "")))
        f = _safe_float(item.get("f"), -1.0)
        c = _safe_float(item.get("c"), -1.0)
        if f < 0.0 or c < 0.0:
            f2, c2 = _extract_stv(expr)
            if f < 0.0:
                f = f2
            if c < 0.0:
                c = c2
        f = _clamp(f, 0.0, 1.0)
        c = _clamp(c, 0.0, 1.0)
        term = _extract_term(expr)
        ig_default = _expectation(f, c)
        sti = _clamp(_safe_float(item.get("sti"), ig_default), 0.0, 1.0)
        ig = _clamp(_safe_float(item.get("ig"), ig_default), 0.0, 1.0)
        cost = _clamp(_safe_float(item.get("cost"), 0.5), 0.0, 1.0)
    else:
        pid = f"p{idx+1}"
        expr = _normalize_space(str(item))
        f, c = _extract_stv(expr)
        term = _extract_term(expr)
        ig_default = _expectation(f, c)
        sti = ig_default
        ig = ig_default
        cost = 0.5

    head = _term_head(term)
    subj, pred = _term_subj_pred(term)
    tokens = tuple(_term_tokens(term))
    return Premise(
        pid=pid,
        expr=expr,
        term=term,
        term_key=_normalize_space(term),
        head=head,
        subj=subj,
        pred=pred,
        f=f,
        c=c,
        sti=sti,
        ig=ig,
        cost=cost,
        tokens=tokens,
    )


def _parse_payload(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if not payload:
        return {}
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except Exception:
            return {}
    return {}


def _pair_related(a: Premise, b: Premise) -> bool:
    if a.term_key == b.term_key:
        return True
    if a.head and b.head and a.head == b.head:
        return True
    if a.pred and b.subj and a.pred == b.subj:
        return True
    if b.pred and a.subj and b.pred == a.subj:
        return True
    shared = set(a.tokens).intersection(b.tokens)
    return len(shared) > 0


def _pair_relevance(a: Premise, b: Premise) -> float:
    score = 0.05
    if a.head and a.head == b.head:
        score += 0.35
    if a.term_key == b.term_key:
        score += 0.25
    if (a.pred and b.subj and a.pred == b.subj) or (b.pred and a.subj and b.pred == a.subj):
        score += 0.25
    shared = set(a.tokens).intersection(b.tokens)
    score += min(0.35, 0.07 * len(shared))
    return _clamp(score, 0.0, 1.0)


def _pair_contradiction(a: Premise, b: Premise) -> float:
    if a.term_key == b.term_key:
        return _clamp(abs(a.f - b.f) * min(a.c, b.c), 0.0, 1.0)
    an = _negated_of(a.term_key)
    bn = _negated_of(b.term_key)
    if an is not None and an == b.term_key:
        return _clamp(((a.f + b.f) * 0.5) * min(a.c, b.c), 0.0, 1.0)
    if bn is not None and bn == a.term_key:
        return _clamp(((a.f + b.f) * 0.5) * min(a.c, b.c), 0.0, 1.0)
    return 0.0


def _hebbian_get(pair: Tuple[str, str], cfg: Settings) -> float:
    edge = _STATE.hebbian.get(pair)
    if edge is None:
        return 0.0
    delta = max(0, _STATE.cycle - edge.last_cycle)
    decayed = edge.value * math.exp(-cfg.decay_lambda * delta)
    edge.value = _clamp(decayed, 0.0, 1.0)
    edge.last_cycle = _STATE.cycle
    return edge.value


def _hebbian_update(pair: Tuple[str, str], delta: float, cfg: Settings) -> None:
    current = _hebbian_get(pair, cfg)
    new_v = _clamp(current + delta, 0.0, 1.0)
    _STATE.hebbian[pair] = HebbianEdge(value=new_v, last_cycle=_STATE.cycle)

    if len(_STATE.hebbian) <= cfg.max_hebbian_edges:
        return
    # Deterministic bounded memory: remove lowest-value edge, then oldest cycle tie-break.
    drop_key = min(
        _STATE.hebbian.items(),
        key=lambda kv: (kv[1].value, kv[1].last_cycle, kv[0][0], kv[0][1]),
    )[0]
    _STATE.hebbian.pop(drop_key, None)


def _candidate_from_pair(a: Premise, b: Premise, cfg: Settings) -> Candidate:
    pair = _canonical_pair(a.pid, b.pid)
    pair_id = f"{pair[0]}|{pair[1]}"
    R = _pair_relevance(a, b)
    H = _hebbian_get(pair, cfg)
    IG = _clamp((a.ig + b.ig) * 0.5, 0.0, 1.0)
    Cost = _clamp((a.cost + b.cost) * 0.5 + (0.25 * (1.0 - R)), 0.0, 1.0)
    x = (cfg.w_R * R) + (cfg.w_H * H) + (cfg.w_IG * IG) - (cfg.w_C * Cost) + cfg.b
    c = _clamp(_sigmoid(x), cfg.eps, 1.0 - cfg.eps)

    contradiction = _pair_contradiction(a, b)
    gate = _clamp(_sigmoid(-cfg.beta * contradiction), cfg.eps, 1.0)
    conf = _clamp((a.c + b.c) * 0.5, 0.0, 1.0)
    score_inf = _clamp(c * gate, 0.0, 1.0)
    score_rev = _clamp(contradiction * conf * max(IG, cfg.eps), 0.0, 1.0)
    return Candidate(
        pair_id=pair_id,
        a=a,
        b=b,
        R=R,
        H=H,
        IG=IG,
        Cost=Cost,
        conductance=c,
        transported=c,
        contradiction=contradiction,
        gate=gate,
        score_inf=score_inf,
        score_rev=score_rev,
    )


def _kernel(r: float, h: float) -> float:
    if h <= 0 or r >= h:
        return 0.0
    q = r / h
    return (1.0 - q * q) ** 2


def _candidate_distance(a: Candidate, b: Candidate) -> float:
    if a.pair_id == b.pair_id:
        return 0.0
    if (
        a.a.pid == b.a.pid
        or a.a.pid == b.b.pid
        or a.b.pid == b.a.pid
        or a.b.pid == b.b.pid
    ):
        return 1.0
    return 2.0


def _apply_sph_transport(candidates: List[Candidate], cfg: Settings) -> None:
    if not candidates:
        return
    updated: List[float] = []
    for c in candidates:
        uncertainty = _clamp(1.0 - ((c.a.c + c.b.c) * 0.5), 0.0, 1.0)
        h = _clamp(cfg.h_base + (cfg.k_uncertainty * uncertainty), cfg.h_min, cfg.h_max)
        numer = 0.0
        denom = 0.0
        for other in candidates:
            r = _candidate_distance(c, other)
            w = _kernel(r, h)
            if w <= 0.0:
                continue
            numer += w * other.conductance
            denom += w
        transported = c.conductance if denom <= 0.0 else (numer / denom)
        transported = ((1.0 - cfg.transport_damping) * c.conductance) + (
            cfg.transport_damping * transported
        )
        updated.append(_clamp(transported, cfg.eps, 1.0))

    for c, transported in zip(candidates, updated):
        c.transported = transported
        c.score_inf = _clamp(c.transported * c.gate, 0.0, 1.0)


def _entropy(values: Sequence[float]) -> float:
    xs = [x for x in values if x > 0.0]
    if not xs:
        return 0.0
    total = sum(xs)
    if total <= 0.0:
        return 0.0
    probs = [x / total for x in xs]
    ent = -sum(p * math.log(p, 2) for p in probs if p > 0.0)
    max_ent = math.log(len(probs), 2) if len(probs) > 1 else 1.0
    return _clamp(ent / max_ent, 0.0, 1.0)


def _make_invocation(engine: str, a: Premise, b: Premise) -> str:
    op = "|~" if engine.lower() == "pln" else "|-"
    return f"({op} {a.expr} {b.expr})"


def _candidate_to_dict(c: Candidate, engine: str) -> Dict[str, Any]:
    return {
        "pair_id": c.pair_id,
        "lhs_id": c.a.pid,
        "rhs_id": c.b.pid,
        "lhs_expr": c.a.expr,
        "rhs_expr": c.b.expr,
        "R": round(c.R, 6),
        "H": round(c.H, 6),
        "IG": round(c.IG, 6),
        "Cost": round(c.Cost, 6),
        "conductance": round(c.conductance, 6),
        "transported": round(c.transported, 6),
        "contradiction": round(c.contradiction, 6),
        "gate": round(c.gate, 6),
        "score_inf": round(c.score_inf, 6),
        "score_rev": round(c.score_rev, 6),
        "invocation": _make_invocation(engine, c.a, c.b),
    }


def conductance_score(
    R: Any,
    H: Any,
    IG: Any,
    Cost: Any,
    w_R: Any = 0.40,
    w_H: Any = 0.25,
    w_IG: Any = 0.25,
    w_C: Any = 0.20,
    b: Any = 0.00,
    eps: Any = 1e-6,
) -> float:
    r = _clamp(_safe_float(R, 0.0), 0.0, 1.0)
    h = _clamp(_safe_float(H, 0.0), 0.0, 1.0)
    ig = _clamp(_safe_float(IG, 0.0), 0.0, 1.0)
    cost = _clamp(_safe_float(Cost, 0.0), 0.0, 1.0)
    ww_r = _safe_float(w_R, 0.40)
    ww_h = _safe_float(w_H, 0.25)
    ww_ig = _safe_float(w_IG, 0.25)
    ww_c = _safe_float(w_C, 0.20)
    bb = _safe_float(b, 0.00)
    epsv = max(1e-9, _safe_float(eps, 1e-6))
    x = (ww_r * r) + (ww_h * h) + (ww_ig * ig) - (ww_c * cost) + bb
    return _clamp(_sigmoid(x), epsv, 1.0 - epsv)


def contradiction_score(lhs_expr: Any, rhs_expr: Any) -> float:
    a = _premise_from_input({"id": "lhs", "expr": str(lhs_expr)}, 0)
    b = _premise_from_input({"id": "rhs", "expr": str(rhs_expr)}, 1)
    return _pair_contradiction(a, b)


def _select_queues(
    candidates: List[Candidate], cfg: Settings, start_ms: float
) -> Tuple[List[Candidate], List[Candidate], bool]:
    timed_out = False
    sorted_rev = sorted(candidates, key=lambda c: (-c.score_rev, c.pair_id))
    sorted_inf = sorted(candidates, key=lambda c: (-c.score_inf, c.pair_id))

    revision: List[Candidate] = []
    inference: List[Candidate] = []
    used: set[str] = set()

    for c in sorted_rev:
        if len(revision) >= cfg.revision_budget:
            break
        if (_now_ms() - start_ms) > cfg.attention_time_budget_ms:
            timed_out = True
            break
        revision.append(c)
        used.add(c.pair_id)

    for c in sorted_inf:
        if len(inference) >= cfg.inference_budget:
            break
        if c.pair_id in used:
            continue
        if (_now_ms() - start_ms) > cfg.attention_time_budget_ms:
            timed_out = True
            break
        inference.append(c)
        used.add(c.pair_id)

    if cfg.min_revision_quota > 0 and len(revision) < cfg.min_revision_quota:
        for c in sorted_rev:
            if c.pair_id in {x.pair_id for x in revision}:
                continue
            if c.pair_id in used:
                continue
            revision.append(c)
            used.add(c.pair_id)
            if len(revision) >= cfg.min_revision_quota:
                break

    if cfg.min_inference_quota > 0 and len(inference) < cfg.min_inference_quota:
        for c in sorted_inf:
            if c.pair_id in {x.pair_id for x in inference}:
                continue
            if c.pair_id in used:
                continue
            inference.append(c)
            used.add(c.pair_id)
            if len(inference) >= cfg.min_inference_quota:
                break

    return inference, revision, timed_out


def run_attention_cycle(payload: Any) -> Dict[str, Any]:
    data = _parse_payload(payload)
    engine = str(data.get("engine", "nal")).lower().strip()
    if engine not in {"nal", "pln"}:
        engine = "nal"

    cfg = _read_settings(data)
    start_ms = _now_ms()
    _STATE.cycle += 1

    raw_premises = data.get("premises", [])
    if not isinstance(raw_premises, list):
        raw_premises = []
    premises = [_premise_from_input(item, idx) for idx, item in enumerate(raw_premises)]
    premises = [p for p in premises if p.expr]

    n = len(premises)
    naive_pairs = (n * (n - 1)) // 2
    candidates: List[Candidate] = []
    timed_out = False

    for i in range(n):
        if (_now_ms() - start_ms) > cfg.attention_time_budget_ms:
            timed_out = True
            break
        for j in range(i + 1, n):
            if len(candidates) >= cfg.candidate_budget:
                break
            a = premises[i]
            b = premises[j]
            if not _pair_related(a, b):
                continue
            candidates.append(_candidate_from_pair(a, b, cfg))
        if len(candidates) >= cfg.candidate_budget:
            break

    candidates.sort(key=lambda c: c.pair_id)
    _apply_sph_transport(candidates, cfg)
    inference_queue, revision_queue, timeout_in_select = _select_queues(
        candidates, cfg, start_ms
    )
    timed_out = timed_out or timeout_in_select

    touched = {c.pair_id: c for c in (inference_queue + revision_queue)}
    for c in touched.values():
        pair_key = _canonical_pair(c.a.pid, c.b.pid)
        delta = cfg.eta * c.a.sti * c.b.sti
        _hebbian_update(pair_key, delta, cfg)

    inf_scores = [c.score_inf for c in candidates]
    candidate_reduction_ratio = (
        (len(candidates) / float(naive_pairs)) if naive_pairs > 0 else 0.0
    )
    contradiction_rate = (
        (sum(1 for c in candidates if c.contradiction > 0.0) / float(len(candidates)))
        if candidates
        else 0.0
    )
    cycle_latency_ms = _now_ms() - start_ms

    metrics = {
        "cycle": _STATE.cycle,
        "atoms_seen": n,
        "naive_pairs": naive_pairs,
        "candidates_generated": len(candidates),
        "candidate_reduction_ratio": round(candidate_reduction_ratio, 6),
        "inference_queue_len": len(inference_queue),
        "revision_queue_len": len(revision_queue),
        "revision_fraction": round(
            (len(revision_queue) / float(max(1, len(inference_queue) + len(revision_queue)))),
            6,
        ),
        "avg_conductance": round(
            (sum(c.conductance for c in candidates) / float(max(1, len(candidates)))), 6
        ),
        "attention_entropy": round(_entropy(inf_scores), 6),
        "contradiction_rate": round(contradiction_rate, 6),
        "cycle_latency_ms": round(cycle_latency_ms, 3),
        "timed_out": timed_out,
        "hebbian_edges": len(_STATE.hebbian),
        "truth_updates_count": 0,
    }
    _STATE.last_metrics = metrics
    _STATE.last_plan_invocations = [
        item["invocation"] for item in [_candidate_to_dict(c, engine) for c in inference_queue]
    ] + [item["invocation"] for item in [_candidate_to_dict(c, engine) for c in revision_queue]]

    warnings: List[str] = []
    if timed_out:
        warnings.append("attention_time_budget_exceeded")
    if len(candidates) >= cfg.candidate_budget:
        warnings.append("candidate_budget_saturated")

    return {
        "engine": engine,
        "settings": {
            "candidate_budget": cfg.candidate_budget,
            "inference_budget": cfg.inference_budget,
            "revision_budget": cfg.revision_budget,
            "attention_time_budget_ms": cfg.attention_time_budget_ms,
            "max_hebbian_edges": cfg.max_hebbian_edges,
        },
        "metrics": metrics,
        "inference_queue": [_candidate_to_dict(c, engine) for c in inference_queue],
        "revision_queue": [_candidate_to_dict(c, engine) for c in revision_queue],
        "all_candidates": [_candidate_to_dict(c, engine) for c in candidates],
        "warnings": warnings,
    }


def bounded_metta(payload: Any) -> str:
    """
    Entry point used by MeTTa `(py-call (attention.bounded_metta ...))`.

    Input payload: JSON string or dict:
    {
      "engine": "nal" | "pln",
      "premises": [
        "((--> sam human) (stv 1.0 0.9))",
        {"id":"p2","expr":"((--> human mortal) (stv 1.0 0.9))","sti":0.7}
      ],
      "budgets": {"inference_budget": 4, "revision_budget": 2}
    }
    """

    result = run_attention_cycle(payload)
    return json.dumps(result, sort_keys=True)


def bounded_nal(payload: Any) -> str:
    data = _parse_payload(payload)
    data["engine"] = "nal"
    return bounded_metta(data)


def bounded_pln(payload: Any) -> str:
    data = _parse_payload(payload)
    data["engine"] = "pln"
    return bounded_metta(data)


def bounded_metta_eval_plan(payload: Any) -> str:
    result = run_attention_cycle(payload)
    invocations = [item["invocation"] for item in result["inference_queue"]] + [
        item["invocation"] for item in result["revision_queue"]
    ]
    if not invocations:
        return "()"
    return f"({' '.join(invocations)})"


def _apply_runtime_overrides(
    payload: Any,
    attention_enabled: Any,
    candidate_budget: Any,
    inference_budget: Any,
    revision_budget: Any,
    time_budget_ms: Any,
    w_r: Any,
    w_h: Any,
    w_ig: Any,
    w_cost: Any,
    bias: Any,
    eta: Any,
    decay_lambda: Any,
    max_hebbian_edges: Any,
    h_min: Any,
    h_base: Any,
    h_max: Any,
    k_uncertainty: Any,
    transport_damping: Any,
    beta: Any,
    min_inference_quota: Any,
    min_revision_quota: Any,
) -> Tuple[bool, Dict[str, Any]]:
    enabled = str(attention_enabled) == "True"
    data = _parse_payload(payload)
    budgets = data.get("budgets", {}) if isinstance(data.get("budgets"), dict) else {}
    budgets.update(
        {
            "candidate_budget": int(_safe_float(candidate_budget, 256)),
            "inference_budget": int(_safe_float(inference_budget, 8)),
            "revision_budget": int(_safe_float(revision_budget, 4)),
            "attention_time_budget_ms": int(_safe_float(time_budget_ms, 50)),
            "max_hebbian_edges": int(_safe_float(max_hebbian_edges, 200000)),
        }
    )
    data["budgets"] = budgets

    config = data.get("config", {}) if isinstance(data.get("config"), dict) else {}
    config.update(
        {
            "w_R": _safe_float(w_r, 0.40),
            "w_H": _safe_float(w_h, 0.25),
            "w_IG": _safe_float(w_ig, 0.25),
            "w_C": _safe_float(w_cost, 0.20),
            "b": _safe_float(bias, 0.00),
            "eta": _safe_float(eta, 0.05),
            "decay_lambda": _safe_float(decay_lambda, 0.001),
            "h_min": _safe_float(h_min, 0.5),
            "h_base": _safe_float(h_base, 1.0),
            "h_max": _safe_float(h_max, 3.0),
            "k_uncertainty": _safe_float(k_uncertainty, 1.0),
            "transport_damping": _safe_float(transport_damping, 0.15),
            "beta": _safe_float(beta, 4.0),
            "min_inference_quota": int(_safe_float(min_inference_quota, 1)),
            "min_revision_quota": int(_safe_float(min_revision_quota, 1)),
        }
    )
    data["config"] = config
    return enabled, data


def bounded_metta_runtime(
    payload: Any,
    attention_enabled: Any,
    candidate_budget: Any,
    inference_budget: Any,
    revision_budget: Any,
    time_budget_ms: Any,
    w_r: Any,
    w_h: Any,
    w_ig: Any,
    w_cost: Any,
    bias: Any,
    eta: Any,
    decay_lambda: Any,
    max_hebbian_edges: Any,
    h_min: Any,
    h_base: Any,
    h_max: Any,
    k_uncertainty: Any,
    transport_damping: Any,
    beta: Any,
    min_inference_quota: Any,
    min_revision_quota: Any,
) -> str:
    enabled, data = _apply_runtime_overrides(
        payload,
        attention_enabled,
        candidate_budget,
        inference_budget,
        revision_budget,
        time_budget_ms,
        w_r,
        w_h,
        w_ig,
        w_cost,
        bias,
        eta,
        decay_lambda,
        max_hebbian_edges,
        h_min,
        h_base,
        h_max,
        k_uncertainty,
        transport_damping,
        beta,
        min_inference_quota,
        min_revision_quota,
    )
    if not enabled:
        return "ATTENTION_DISABLED_SET_attentionEnabled_True"
    return bounded_metta(data)


def bounded_metta_eval_plan_runtime(
    payload: Any,
    attention_enabled: Any,
    candidate_budget: Any,
    inference_budget: Any,
    revision_budget: Any,
    time_budget_ms: Any,
    w_r: Any,
    w_h: Any,
    w_ig: Any,
    w_cost: Any,
    bias: Any,
    eta: Any,
    decay_lambda: Any,
    max_hebbian_edges: Any,
    h_min: Any,
    h_base: Any,
    h_max: Any,
    k_uncertainty: Any,
    transport_damping: Any,
    beta: Any,
    min_inference_quota: Any,
    min_revision_quota: Any,
) -> str:
    enabled, data = _apply_runtime_overrides(
        payload,
        attention_enabled,
        candidate_budget,
        inference_budget,
        revision_budget,
        time_budget_ms,
        w_r,
        w_h,
        w_ig,
        w_cost,
        bias,
        eta,
        decay_lambda,
        max_hebbian_edges,
        h_min,
        h_base,
        h_max,
        k_uncertainty,
        transport_damping,
        beta,
        min_inference_quota,
        min_revision_quota,
    )
    if not enabled:
        return "()"
    return bounded_metta_eval_plan(data)


def reset_state() -> str:
    _STATE.cycle = 0
    _STATE.hebbian.clear()
    _STATE.last_metrics = {}
    _STATE.last_plan_invocations = []
    return "ATTENTION-STATE-RESET"


def get_state() -> str:
    data = {
        "cycle": _STATE.cycle,
        "hebbian_edges": len(_STATE.hebbian),
        "last_metrics": _STATE.last_metrics,
        "last_plan_invocations": _STATE.last_plan_invocations,
    }
    return json.dumps(data, sort_keys=True)
