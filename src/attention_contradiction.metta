; Contradiction scoring helper.

(= (attention-contradiction-score $lhs $rhs)
   (py-call (attention.contradiction_score $lhs $rhs)))

