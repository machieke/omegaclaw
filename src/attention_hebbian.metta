; Hebbian state control.

(= (attention-reset)
   (py-call (attention.reset_state)))

(= (attention-state)
   (py-call (attention.get_state)))

