; SPH transport is applied inside the scheduler.
; This wrapper keeps a dedicated surface for component-level calls.

(= (attention-sph-step $payload)
   (py-call (attention.bounded_metta $payload)))

