; Runtime attention configuration keys.
; Values are set in src/loop.metta via (configure ...).

(= (attentionEnabled) (empty))
(= (attentionCandidateBudget) (empty))
(= (attentionInferenceBudget) (empty))
(= (attentionRevisionBudget) (empty))
(= (attentionTimeBudgetMs) (empty))

(= (attentionWR) (empty))
(= (attentionWH) (empty))
(= (attentionWIG) (empty))
(= (attentionWCost) (empty))
(= (attentionBias) (empty))

(= (attentionEta) (empty))
(= (attentionDecayLambda) (empty))
(= (attentionMaxHebbianEdges) (empty))

(= (attentionHMin) (empty))
(= (attentionHBase) (empty))
(= (attentionHMax) (empty))
(= (attentionKUncertainty) (empty))
(= (attentionTransportDamping) (empty))

(= (attentionBeta) (empty))
(= (attentionMinInferenceQuota) (empty))
(= (attentionMinRevisionQuota) (empty))

(= (attentionDefaultPayload)
   "{\"engine\":\"nal\",\"premises\":[],\"budgets\":{}}")

