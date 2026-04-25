; Deterministic scheduler wrappers.
; Input payload should be a JSON string:
; {
;   "engine": "nal" | "pln",
;   "premises": ["((--> a b) (stv 1.0 0.9))", ...],
;   "budgets": {"inference_budget": 4, "revision_budget": 2}
; }

(= (attention-schedule $payload)
   (py-call (attention.bounded_metta $payload)))

(= (attention-schedule-nal $payload)
   (py-call (attention.bounded_nal $payload)))

(= (attention-schedule-pln $payload)
   (py-call (attention.bounded_pln $payload)))

