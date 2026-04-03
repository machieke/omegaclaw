## MeTTaClaw

<img width="362" alt="image" src="https://github.com/user-attachments/assets/197d745f-1562-4d31-88c2-b813a56ccbf1" />

An agentic AI system implemented in MeTTa, guided by the MeTTaClaw proposal and an agent core inspired by Nanobot.
Beyond basic tool use, it features embedding-based long-term memory represented entirely in MeTTa AtomSpace format.

Long-term memory is deliberately maintained by the agent via `(remember string)` for adding memory items and `(query string)` for querying related memories.
The agent can learn and apply new skills and declarative knowledge through the use of memory items.

In addition, an initial set of OpenClaw-like tools is implemented, including web search, file modification, communication channels, and access to the operating system shell and its associated tools.

Simplicity of design, ease of prototyping, ease of extension, and transparent implementation in MeTTa were the primary design criteria.
The agent core comprises approximately 200 lines of code.

**Special Features**

- MeTTaClaw uses a token-efficient agentic loop, enabling low-cost long-term operation and embodiment in domains that require real-time learning and decision-making.

- The agent can learn to represent its memories in different ways, including such that allow other Hyperon components to operate on the same memories within the same Atomspace. Each memory item is stored as a triplet `(timestamp, atom, embedding)`, while the agent remains flexible in choosing the representation for the atom itself. Consequently, the agent is not hardcoded to any particular memory representation, and different formats can co-exist in the same atom space.

The following example demonstrates learning and decision-making in a textually represented grid-world environment adapted from [NACE](https://github.com/patham9/NACE):

![mettaclaw_in_nace_world](https://github.com/user-attachments/assets/c6c01839-234d-4505-baf6-4f2f3787c7b9)


This project also aims to explore the potential of Agentic Physical AI, a ROS2 package for mobile robots with manipulators is underway.

**Installation**

First, get [SWI-Prolog](https://www.swi-prolog.org/). Then:

```
git clone https://github.com/trueagi-io/PeTTa
cd PeTTa
mkdir -p repos && git clone https://github.com/patham9/mettaclaw repos/mettaclaw
```

**Usage**

Run the system via the following command which ensures the system is started from the root folder of PeTTa:

```
cp repos/mettaclaw/run.metta ./
OLLAMA_BASE_URL=http://127.0.0.1:11434 \
OLLAMA_MODEL=llama3.2:1b \
OLLAMA_EMBED_MODEL=nomic-embed-text \
sh run.sh run.metta
```

**Docker (Hardened Multi-Stage + Docker-Native Networking)**

Build and run with Docker Compose:

```
docker compose up --build
```

Security-focused defaults now included:
- Multi-stage build (toolchain isolated from runtime image)
- Reduced runtime packages compared to the old single-stage style
- `.dockerignore` to reduce build-context leakage risk
- No in-container iptables/firewall script at runtime
- Process runs directly as non-root (host-mapped UID/GID by default: `1000:1000`)
- `security_opt: [no-new-privileges:true]`
- `read_only: true` with `tmpfs` mounts
- Persistent writable mount only for `./memory`
- User-defined bridge network (`appnet`)
- Published ports for vibe voice mode:
  - `8012` (MeTTaClaw `vibe-voice` websocket)
  - `5173` (React frontend)

Default local-LLM wiring:
- `ollama` sidecar service is started in the same Compose project
- `OLLAMA_BASE_URL=http://ollama:11434`
- `OLLAMA_MODEL=llama3.2:1b`
- `OLLAMA_EMBED_MODEL=nomic-embed-text`
- `OLLAMA_AUTO_PULL=true` (auto-pulls missing local models)
- Alternative provider: set `METTACLAW_LLM_PROVIDER=codex` to run chat via the `codex` CLI.
  - Config knobs: `CODEX_MODEL` (default `gpt-5.4`), `CODEX_MODEL_REASONING_EFFORT` (default `xhigh`), `CODEX_TIMEOUT_S` (default `600`).
  - The runtime command is `codex e --model <model> -c model_reasoning_effort=<effort> --ephemeral --skip-git-repo-check "$PROMPT" 2>/dev/null | tail -n 1`.
- If your host UID/GID is not `1000:1000`, set:
  `METTACLAW_UID=$(id -u) METTACLAW_GID=$(id -g) docker compose up --build`

Vibe voice channel + React frontend:
- Set `METTACLAW_COMMCHANNEL=vibe-voice` in `.env` (or export it in shell).
- Keep `VIBE_VOICE_ENABLED=true` (default in `.env.example`) so websocket server starts.
- Run `docker compose up --build`.
- Open `http://localhost:5173`.
- Click `Connect`, then `Start Voice`.
- Voice input uses browser speech-recognition and assistant TTS uses browser speech-synthesis.
- WebSocket endpoint is `ws://localhost:8012/ws/vibe` by default.
- If `METTACLAW_AUTH_REQUIRED=true`, first send `auth <startup-secret>` from the UI (the secret is printed in `docker compose logs mettaclaw`).
- Non-Docker runs need `fastapi` and `uvicorn` installed for the `vibe-voice` websocket server.

Persona-driven system prompt:
- Persona definitions now live in MeTTa-formatted files under `personas/`, for example `personas/system-thinking-strategist.metta`.
- `getPrompt` composes `memory/prompt.txt` with the selected persona at runtime.
- Defaults: `METTACLAW_PERSONA_ENABLED=true` and `METTACLAW_PERSONA_ID=system-thinking-strategist`.
- Optional overrides:
  - `METTACLAW_PERSONA_PATH` (absolute or relative path, supports `{persona_id}` template)
  - `METTACLAW_PERSONA_SECTIONS` (comma-separated subset)
  - `METTACLAW_PERSONA_MAX_CHARS` (caps injected persona size; `0` disables capping)
- Runtime commands:
  - `persona list`
  - `persona current`
  - `persona use <persona-id>`

Capability policy enforcement:
- Capabilities register into a shared policy interface at startup.
- Policy evaluation is applied before capability execution (default-deny for non-allowed capabilities).
- Static roles file: `memory/capability_roles_static.metta`
- Dynamic roles file: `memory/capability_roles_dynamic.metta` (runtime-managed)
- Static policies file: `memory/capability_policies_static.metta`
- Dynamic policies file: `memory/capability_policies_dynamic.metta` (runtime-managed)
- Files are persisted/loaded as MeTTa expressions (not JSON blobs), for example:
  - `(user-role "alice" "trusted-admin")`
  - `(channel-user-role "irc" "bob" "trusted-user")`
  - `(role-capability "trusted-user" "query")`
  - `(policy-admin "*" "trusted-admin")`
- No automatic role grant is applied to all channel users by default (`channel_roles` is empty unless explicitly configured).
- Configure role-admin scope in `role_admin_roles` (map of target role -> admin roles; `*` applies to all roles).
- Configure policy-admin scope in `policy_admin_roles` (map of target policy role -> admin roles; `*` applies to all policy roles).
- Startup bootstrap: a one-time auth secret is generated; the first user who posts it in a channel is granted `trusted-admin` in that channel only.
- Admin-capable users can mutate dynamic roles/policies via channel commands:
  - `policy show`
  - `policy allow <role> <capability>`
  - `policy deny <role> <capability>`
  - `policy unallow <role> <capability>`
  - `policy undeny <role> <capability>`
  - `policy role <user> <role>` (grants role in current channel only)
  - `policy unrole <user>` (revokes role in current channel only)
  - `policy set-policy-admin <target-role> <admin-role>` (for target policy role)
  - `policy unset-policy-admin <target-role> <admin-role>` (for target policy role)
  - `policy set-role-admin <target-role> <admin-role>` (for target role)
  - `policy unset-role-admin <target-role> <admin-role>` (for target role)
  - `grant <user> <role>` (alias for `policy role`, channel-scoped)
  - `revoke <user>` (alias for `policy unrole`, channel-scoped)
  - `renounce role` (drop your own dynamic role)
  - `grant role <role> <capability>` (alias for `policy allow`)
  - `revoke role <role> <capability>` (alias for `policy unallow`)
  - `revoke <user>` / `policy unrole <user>` cannot target yourself; use `renounce role` instead

**Auto-install/run**

Alternatively, if PeTTa is already installed and the latest version pulled (v1.0.2 or latest commit), then running the following MeTTa file from the root folder installs and runs MeTTaClaw (assuming a reachable Ollama instance):

```
!(import! &self (library lib_import))
!(git-import! "https://github.com/patham9/mettaclaw.git")
!(import! &self (library mettaclaw lib_mettaclaw))

!(mettaclaw)
```

**Illustrations**

Long-Term Memory Recall:

<img width="638" height="125" alt="image" src="https://github.com/user-attachments/assets/0d4817ed-e743-4e44-8bd4-a10e27ea6380" />

Tool use:

<img width="1323" height="188" alt="image" src="https://github.com/user-attachments/assets/18ef19c4-010a-4c94-84ce-bb49277dccfc" />

Shell output of the actual invocation of the generated MeTTa code:

<img width="416" height="486" alt="image" src="https://github.com/user-attachments/assets/f5b27205-cdb2-47e7-821a-ffd93b3dd7c6" />

System also added it into its Atom Space storage (embedding vector omitted):

<img width="379" height="69" alt="image" src="https://github.com/user-attachments/assets/6aa59deb-33b4-42b9-a535-ae153b4b7a18" />
