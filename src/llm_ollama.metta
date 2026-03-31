; Local Ollama helpers, intentionally using dedicated names so calls do not route
; through lib_llm's useGPT/useGPTEmbedding predicates.

(= (useLocalLLM $prompt)
   (useLocalLLM (LLM) (maxOutputToken) (reasoningMode) $prompt))

(= (useLocalLLM $model $max_tokens $effort $prompt)
   (py-call (helper.ollama_chat $model $prompt $max_tokens $effort)))

(= (useLocalLLMWithUser $model $max_tokens $effort $prompt $user_msg)
   (py-call (helper.generate_skill_candidate $model $prompt $max_tokens $effort $user_msg)))

(= (useLocalEmbedding $text)
   (py-call (helper.ollama_embedding (string-safe $text))))
