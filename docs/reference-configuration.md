# Reference — Configuration

Every tunable in OmegaClaw is declared as `(= (name) (empty))` and later bound by a `configure` call inside an `init*` function. The `configure` helper in `src/utils.metta` is:

```metta
(= (configure $name $default)
   (let $value (argk $name $default)
        (add-atom &self (= ($name) $value))))
```

This reads a command-line override via `argk` (`name=value` on the MeTTa command line) if present, otherwise falls back to the default.

## Loop (`src/loop.metta`, `initLoop`)

| Parameter | Default | Meaning |
|---|---|---|
| `maxNewInputLoops` | 50 | How many turns the agent keeps running after a new human message before idling. |
| `maxWakeLoops` | 1 | Extra turns granted on each scheduled wake-up. |
| `sleepInterval` | 1 (seconds) | Delay between loop iterations. |
| `LLM` | `gpt-5.4` | Model identifier passed to the provider. |
| `provider` | `Anthropic` | LLM provider — `Anthropic`, `OpenAI`, or `ASICloud`. |
| `maxOutputToken` | 6000 | Output cap passed to the provider. |
| `reasoningMode` | `medium` | Reasoning-effort hint passed to the provider. |
| `wakeupInterval` | 600 (seconds) | How long idle before the next scheduled wake-up. |

### Bounded Attention (`src/loop.metta`, `initLoop`)

| Parameter | Default | Meaning |
|---|---|---|
| `attentionEnabled` | `True` | Enables/disables bounded attention skills (`bounded-metta`). |
| `attentionCandidateBudget` | 256 | Maximum structural candidates generated per cycle. |
| `attentionInferenceBudget` | 8 | Max items emitted in inference queue. |
| `attentionRevisionBudget` | 4 | Max items emitted in revision queue. |
| `attentionTimeBudgetMs` | 50 | Hard time budget for attention scheduling work. |
| `attentionWR` | 0.40 | Conductance weight for structural relevance `R`. |
| `attentionWH` | 0.25 | Conductance weight for Hebbian association `H`. |
| `attentionWIG` | 0.25 | Conductance weight for `IG` signal. |
| `attentionWCost` | 0.20 | Conductance penalty weight for normalized `Cost`. |
| `attentionBias` | 0.0 | Conductance additive bias term. |
| `attentionEta` | 0.05 | Hebbian learning rate. |
| `attentionDecayLambda` | 0.001 | Hebbian lazy-decay factor. |
| `attentionMaxHebbianEdges` | 200000 | Hard cap for stored Hebbian edges. |
| `attentionHMin` | 0.5 | Minimum SPH smoothing radius. |
| `attentionHBase` | 1.0 | Base SPH smoothing radius. |
| `attentionHMax` | 3.0 | Maximum SPH smoothing radius. |
| `attentionKUncertainty` | 1.0 | Uncertainty multiplier for adaptive radius. |
| `attentionTransportDamping` | 0.15 | SPH transport damping factor. |
| `attentionBeta` | 4.0 | Contradiction gate slope parameter. |
| `attentionMinInferenceQuota` | 1 | Starvation guard for minimum inference queue size. |
| `attentionMinRevisionQuota` | 1 | Starvation guard for minimum revision queue size. |

## Memory (`src/memory.metta`, `initMemory`)

| Parameter | Default | Meaning |
|---|---|---|
| `maxFeedback` | 50000 (chars) | Ceiling on `LAST_SKILL_USE_RESULTS` text fed back into the prompt. |
| `maxRecallItems` | 20 | Items returned by `query`. |
| `maxEpisodeRecallLines` | 20 | Lines returned by `episodes`. |
| `maxHistory` | 30000 (chars) | Tail of `memory/history.metta` included in the prompt. |
| `embeddingprovider` | `Local` | `Local` (Python-side model) or `OpenAI`. |

## Channels (`src/channels.metta`, `initChannels`)

| Parameter | Default | Meaning |
|---|---|---|
| `commchannel` | `irc` | Active channel — `irc` or `mattermost`. |
| `IRC_channel` | `##omegaclaw` | IRC channel to join. |
| `IRC_server` | `irc.quakenet.org` | IRC server hostname. |
| `IRC_port` | 6667 | IRC port. |
| `IRC_user` | `omegaclaw` | IRC nickname. |
| `MM_URL` | `https://chat.singularitynet.io` | Mattermost base URL. |
| `MM_CHANNEL_ID` | `8fjrmabjx7gupy7e5kjznpt5qh` | Target channel ID. |
| `MM_BOT_TOKEN` | *(empty — set at runtime)* | Bot auth token. |

## Command-line overrides

Any `configure`d parameter can be overridden at startup:

```bash
metta run.metta provider=Anthropic LLM=claude-opus-4-6 commchannel=mattermost
```

The `argk` helper parses `key=value` pairs from `argv`.
