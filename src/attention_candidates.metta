; Candidate generation + planning entrypoint.
; Input: JSON payload string.
; Output: JSON plan with candidates and dual queues.

(= (attention-generate $payload)
   (py-call (attention.bounded_metta $payload)))

