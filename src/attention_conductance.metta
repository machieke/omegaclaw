; Conductance helpers for scoring and debugging.

(= (attention-conductance-score $R $H $IG $Cost)
   (py-call (attention.conductance_score $R $H $IG $Cost)))

(= (attention-conductance-score $R $H $IG $Cost $wR $wH $wIG $wC $b)
   (py-call (attention.conductance_score $R $H $IG $Cost $wR $wH $wIG $wC $b)))

