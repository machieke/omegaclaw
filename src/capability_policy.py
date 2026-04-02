import hashlib
import json
import os
import shlex
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    description: str = ""
    side_effecting: bool = True


@dataclass(frozen=True)
class PolicyDecision:
    capability: str
    allowed: bool
    role: str
    reason: str


class CapabilityPolicyInterface(ABC):
    @abstractmethod
    def register_capability(self, capability: CapabilityDefinition):
        raise NotImplementedError

    @abstractmethod
    def decide(self, capability: str, actor: str, channel: str, target: str = "") -> PolicyDecision:
        raise NotImplementedError

    @abstractmethod
    def mutate_dynamic_policy(
        self,
        actor: str,
        channel: str,
        action: str,
        role: str = "",
        capability: str = "",
        user: str = "",
    ):
        raise NotImplementedError


_DEFAULT_STATIC_ROLES = {
    "default_role": "untrusted/public",
    "user_roles": {},
    "channel_roles": {},
    "channel_user_roles": {},
    "role_admin_roles": {
        "*": ["trusted-admin"],
    },
}

_DEFAULT_STATIC_POLICIES = {
    "role_capabilities": {
        "trusted-admin": ["*"],
        "trusted-user": [
            "send",
            "query",
            "remember",
            "pin",
            "search",
            "list-models",
            "current-model",
            "list-channels",
            "show-current-channel",
        ],
        "untrusted/public": [
            "send",
            "query",
        ],
    },
    "always_denied": [],
    "policy_admin_roles": {
        "*": ["trusted-admin"],
    },
}

_DEFAULT_DYNAMIC_ROLES = {
    "user_roles": {},
    "channel_user_roles": {},
    "role_admin_roles": {},
}

_DEFAULT_DYNAMIC_POLICIES = {
    "allow": {},
    "deny": {},
    "policy_admin_roles": {},
}


def _norm(value):
    return str(value or "").strip().lower()


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class CapabilityPolicyEngine(CapabilityPolicyInterface):
    def __init__(self):
        self._lock = threading.Lock()
        self._capabilities = {}
        self._static_roles = {}
        self._dynamic_roles = {}
        self._static_policies = {}
        self._dynamic_policies = {}
        self._load_policies()

    def _project_root(self):
        return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    def _configured_path(self, env_var, default_name):
        configured = str(os.getenv(env_var, "") or "").strip()
        if configured:
            return configured
        return os.path.join(self._project_root(), "memory", default_name)

    def _static_roles_path(self):
        return self._configured_path("CAPABILITY_STATIC_ROLES_PATH", "capability_roles_static.metta")

    def _dynamic_roles_path(self):
        return self._configured_path("CAPABILITY_DYNAMIC_ROLES_PATH", "capability_roles_dynamic.metta")

    def _static_policies_path(self):
        return self._configured_path("CAPABILITY_STATIC_POLICIES_PATH", "capability_policies_static.metta")

    def _dynamic_policies_path(self):
        return self._configured_path("CAPABILITY_DYNAMIC_POLICIES_PATH", "capability_policies_dynamic.metta")

    def _deepcopy_value(self, value):
        return json.loads(json.dumps(value))

    def _metta_quote(self, value):
        return json.dumps(str(value or ""), ensure_ascii=False)

    def _parse_metta_forms(self, text):
        forms = []
        for raw_line in str(text or "").splitlines():
            line = str(raw_line or "").strip()
            if not line or line.startswith(";") or line.startswith("#"):
                continue
            if not (line.startswith("(") and line.endswith(")")):
                continue
            inner = line[1:-1].strip()
            if not inner:
                continue
            try:
                parts = shlex.split(inner, posix=True)
            except Exception:
                continue
            if not parts:
                continue
            forms.append((_norm(parts[0]), [str(x or "").strip() for x in parts[1:]]))
        return forms

    def _append_mapping_list(self, mapping, key, value):
        key_text = str(key or "").strip()
        value_text = str(value or "").strip()
        if not key_text or not value_text:
            return
        mapping.setdefault(key_text, []).append(value_text)

    def _parse_roles_metta(self, text):
        payload = {
            "user_roles": {},
            "channel_roles": {},
            "channel_user_roles": {},
            "role_admin_roles": {},
        }
        for head, args in self._parse_metta_forms(text):
            if head == "default-role" and len(args) >= 1:
                payload["default_role"] = args[0]
                continue
            if head == "user-role" and len(args) >= 2:
                payload["user_roles"][args[0]] = args[1]
                continue
            if head == "channel-role" and len(args) >= 2:
                payload["channel_roles"][args[0]] = args[1]
                continue
            if head == "channel-user-role" and len(args) >= 3:
                channel_name, user_name, role_name = args[:3]
                channel_map = payload["channel_user_roles"].setdefault(channel_name, {})
                channel_map[user_name] = role_name
                continue
            if head == "role-admin" and len(args) >= 2:
                self._append_mapping_list(payload["role_admin_roles"], args[0], args[1])
                continue
        return payload

    def _parse_policies_metta(self, text):
        payload = {
            "role_capabilities": {},
            "always_denied": [],
            "policy_admin_roles": {},
            "allow": {},
            "deny": {},
        }
        for head, args in self._parse_metta_forms(text):
            if head == "role-capability" and len(args) >= 2:
                self._append_mapping_list(payload["role_capabilities"], args[0], args[1])
                continue
            if head == "always-denied" and len(args) >= 1:
                payload["always_denied"].append(args[0])
                continue
            if head == "policy-admin" and len(args) >= 2:
                self._append_mapping_list(payload["policy_admin_roles"], args[0], args[1])
                continue
            if head == "allow" and len(args) >= 2:
                self._append_mapping_list(payload["allow"], args[0], args[1])
                continue
            if head == "deny" and len(args) >= 2:
                self._append_mapping_list(payload["deny"], args[0], args[1])
                continue
        return payload

    def _serialize_roles_metta(self, payload):
        lines = ["; capability roles"]
        default_role = str((payload or {}).get("default_role") or "").strip()
        if default_role:
            lines.append(f"(default-role {self._metta_quote(default_role)})")
        for user, role in sorted(((payload or {}).get("user_roles") or {}).items()):
            lines.append(f"(user-role {self._metta_quote(user)} {self._metta_quote(role)})")
        for channel, role in sorted(((payload or {}).get("channel_roles") or {}).items()):
            lines.append(f"(channel-role {self._metta_quote(channel)} {self._metta_quote(role)})")
        channel_user_roles = (payload or {}).get("channel_user_roles") or {}
        for channel, users in sorted(channel_user_roles.items()):
            for user, role in sorted((users or {}).items()):
                lines.append(
                    f"(channel-user-role {self._metta_quote(channel)} {self._metta_quote(user)} {self._metta_quote(role)})"
                )
        for target_role, admin_roles in sorted(((payload or {}).get("role_admin_roles") or {}).items()):
            for admin_role in sorted(admin_roles or []):
                lines.append(f"(role-admin {self._metta_quote(target_role)} {self._metta_quote(admin_role)})")
        return "\n".join(lines).rstrip() + "\n"

    def _serialize_policies_metta(self, payload):
        lines = ["; capability policies"]
        for role, capabilities in sorted(((payload or {}).get("role_capabilities") or {}).items()):
            for capability in sorted(capabilities or []):
                lines.append(f"(role-capability {self._metta_quote(role)} {self._metta_quote(capability)})")
        for capability in sorted((payload or {}).get("always_denied") or []):
            lines.append(f"(always-denied {self._metta_quote(capability)})")
        for target_role, admin_roles in sorted(((payload or {}).get("policy_admin_roles") or {}).items()):
            for admin_role in sorted(admin_roles or []):
                lines.append(f"(policy-admin {self._metta_quote(target_role)} {self._metta_quote(admin_role)})")
        for role, capabilities in sorted(((payload or {}).get("allow") or {}).items()):
            for capability in sorted(capabilities or []):
                lines.append(f"(allow {self._metta_quote(role)} {self._metta_quote(capability)})")
        for role, capabilities in sorted(((payload or {}).get("deny") or {}).items()):
            for capability in sorted(capabilities or []):
                lines.append(f"(deny {self._metta_quote(role)} {self._metta_quote(capability)})")
        return "\n".join(lines).rstrip() + "\n"

    def _load_policy_or_default(self, paths, default_value, metta_parser):
        for candidate in paths:
            if not candidate:
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except Exception:
                continue

            stripped = str(text or "").lstrip()
            if not stripped:
                continue
            if stripped.startswith("{"):
                # JSON payloads are intentionally unsupported; only MeTTa forms are allowed.
                continue
            if not self._parse_metta_forms(text):
                continue
            try:
                parsed = metta_parser(text)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        return self._deepcopy_value(default_value)

    def _write_policy(self, path, payload, metta_serializer):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(metta_serializer(payload))
        os.replace(tmp, path)

    def _normalize_list(self, values):
        if values is None:
            return []
        if isinstance(values, (list, tuple, set)):
            source = values
        else:
            source = [values]
        return sorted({_norm(item) for item in source if _norm(item)})

    def _normalize_user_roles(self, mapping):
        if not isinstance(mapping, dict):
            return {}
        user_roles = {}
        for user, role in (mapping or {}).items():
            user_name = _norm(user)
            role_name = _norm(role)
            if user_name and role_name:
                user_roles[user_name] = role_name
        return user_roles

    def _normalize_channel_user_roles(self, mapping):
        if not isinstance(mapping, dict):
            return {}
        channel_user_roles = {}
        for channel, users in mapping.items():
            channel_name = _norm(channel)
            if not channel_name:
                continue
            normalized_users = self._normalize_user_roles(users or {})
            if normalized_users:
                channel_user_roles[channel_name] = normalized_users
        return channel_user_roles

    def _normalize_role_list_map(self, mapping):
        if not isinstance(mapping, dict):
            return {}
        out = {}
        for role, items in (mapping or {}).items():
            role_name = _norm(role)
            if not role_name:
                continue
            values = self._normalize_list(items)
            if values:
                out[role_name] = values
        return out

    def _normalize_policy_admin_map(self, mapping):
        if isinstance(mapping, dict):
            return self._normalize_role_list_map(mapping)
        wildcard_admins = self._normalize_list(mapping)
        if wildcard_admins:
            return {"*": wildcard_admins}
        return {}

    def _normalize_static_roles(self, payload):
        data = json.loads(json.dumps(_DEFAULT_STATIC_ROLES))
        if isinstance(payload, dict):
            for key in ("default_role", "user_roles", "channel_roles", "channel_user_roles", "role_admin_roles"):
                if key in payload and payload[key] is not None:
                    data[key] = payload[key]
        data["default_role"] = _norm(data.get("default_role")) or "untrusted/public"
        data["user_roles"] = self._normalize_user_roles(data.get("user_roles") or {})
        data["channel_roles"] = self._normalize_user_roles(data.get("channel_roles") or {})
        data["channel_user_roles"] = self._normalize_channel_user_roles(data.get("channel_user_roles") or {})
        data["role_admin_roles"] = self._normalize_role_list_map(data.get("role_admin_roles") or {})
        return data

    def _normalize_dynamic_roles(self, payload):
        data = json.loads(json.dumps(_DEFAULT_DYNAMIC_ROLES))
        if isinstance(payload, dict):
            for key in ("user_roles", "channel_user_roles", "role_admin_roles"):
                if key in payload and payload[key] is not None:
                    data[key] = payload[key]
        data["user_roles"] = self._normalize_user_roles(data.get("user_roles") or {})
        data["channel_user_roles"] = self._normalize_channel_user_roles(data.get("channel_user_roles") or {})
        data["role_admin_roles"] = self._normalize_role_list_map(data.get("role_admin_roles") or {})
        return data

    def _normalize_static_policies(self, payload):
        data = json.loads(json.dumps(_DEFAULT_STATIC_POLICIES))
        if isinstance(payload, dict):
            for key in ("role_capabilities", "always_denied", "policy_admin_roles"):
                if key in payload and payload[key] is not None:
                    data[key] = payload[key]
        data["role_capabilities"] = self._normalize_role_list_map(data.get("role_capabilities") or {})
        data["always_denied"] = self._normalize_list(data.get("always_denied") or [])
        data["policy_admin_roles"] = self._normalize_policy_admin_map(data.get("policy_admin_roles"))
        return data

    def _normalize_dynamic_policies(self, payload):
        data = json.loads(json.dumps(_DEFAULT_DYNAMIC_POLICIES))
        if isinstance(payload, dict):
            for key in ("allow", "deny", "policy_admin_roles"):
                if key in payload and payload[key] is not None:
                    data[key] = payload[key]
        data["allow"] = self._normalize_role_list_map(data.get("allow") or {})
        data["deny"] = self._normalize_role_list_map(data.get("deny") or {})
        data["policy_admin_roles"] = self._normalize_policy_admin_map(data.get("policy_admin_roles"))
        return data

    def _load_policies(self):
        static_roles_path = self._static_roles_path()
        dynamic_roles_path = self._dynamic_roles_path()
        static_policies_path = self._static_policies_path()
        dynamic_policies_path = self._dynamic_policies_path()

        static_roles_raw = self._load_policy_or_default(
            [static_roles_path],
            _DEFAULT_STATIC_ROLES,
            self._parse_roles_metta,
        )
        dynamic_roles_raw = self._load_policy_or_default(
            [dynamic_roles_path],
            _DEFAULT_DYNAMIC_ROLES,
            self._parse_roles_metta,
        )
        static_policies_raw = self._load_policy_or_default(
            [static_policies_path],
            _DEFAULT_STATIC_POLICIES,
            self._parse_policies_metta,
        )
        dynamic_policies_raw = self._load_policy_or_default(
            [dynamic_policies_path],
            _DEFAULT_DYNAMIC_POLICIES,
            self._parse_policies_metta,
        )

        self._static_roles = self._normalize_static_roles(static_roles_raw)
        self._dynamic_roles = self._normalize_dynamic_roles(dynamic_roles_raw)
        self._static_policies = self._normalize_static_policies(static_policies_raw)
        self._dynamic_policies = self._normalize_dynamic_policies(dynamic_policies_raw)

        for path, payload, serializer in (
            (static_roles_path, self._static_roles, self._serialize_roles_metta),
            (dynamic_roles_path, self._dynamic_roles, self._serialize_roles_metta),
            (static_policies_path, self._static_policies, self._serialize_policies_metta),
            (dynamic_policies_path, self._dynamic_policies, self._serialize_policies_metta),
        ):
            try:
                self._write_policy(path, payload, serializer)
            except Exception:
                pass

    def _save_dynamic_roles(self):
        self._write_policy(self._dynamic_roles_path(), self._dynamic_roles, self._serialize_roles_metta)

    def _save_dynamic_policies(self):
        self._write_policy(self._dynamic_policies_path(), self._dynamic_policies, self._serialize_policies_metta)

    def _audit(self, actor, channel, capability, role, allowed, reason, target=""):
        actor_name = _norm(actor) or "unknown"
        channel_name = _norm(channel) or "unknown"
        capability_name = _norm(capability) or "unknown"
        role_name = _norm(role) or "unknown"
        target_hash = ""
        target_text = str(target or "").strip()
        if target_text:
            target_hash = hashlib.sha256(target_text.encode("utf-8")).hexdigest()[:12]
        print(
            f"[policy] time={_now_utc()} actor={actor_name} channel={channel_name} role={role_name} "
            f"capability={capability_name} decision={'allow' if allowed else 'deny'} reason={reason} target_hash={target_hash}",
            flush=True,
        )

    def register_capability(self, capability: CapabilityDefinition):
        name = _norm(capability.name)
        if not name:
            return False
        with self._lock:
            self._capabilities[name] = capability
        return True

    def capability_names(self):
        with self._lock:
            return sorted(self._capabilities.keys())

    def _actor_role_unlocked(self, actor, channel):
        actor_name = _norm(actor)
        channel_name = _norm(channel)
        dynamic_channel_user_roles = self._dynamic_roles.get("channel_user_roles") or {}
        channel_dynamic_roles = dynamic_channel_user_roles.get(channel_name) or {}
        if actor_name and actor_name in channel_dynamic_roles:
            return _norm(channel_dynamic_roles.get(actor_name))
        static_channel_user_roles = self._static_roles.get("channel_user_roles") or {}
        channel_static_roles = static_channel_user_roles.get(channel_name) or {}
        if actor_name and actor_name in channel_static_roles:
            return _norm(channel_static_roles.get(actor_name))
        user_roles = self._dynamic_roles.get("user_roles") or {}
        if actor_name and actor_name in user_roles:
            return _norm(user_roles.get(actor_name))
        static_user_roles = self._static_roles.get("user_roles") or {}
        if actor_name and actor_name in static_user_roles:
            return _norm(static_user_roles.get(actor_name))
        channel_roles = self._static_roles.get("channel_roles") or {}
        if channel_name and channel_name in channel_roles:
            return _norm(channel_roles.get(channel_name))
        return _norm(self._static_roles.get("default_role")) or "untrusted/public"

    def _role_allows_static_unlocked(self, role, capability):
        role_name = _norm(role)
        cap_name = _norm(capability)
        caps = set((_norm(x) for x in (self._static_policies.get("role_capabilities", {}).get(role_name) or [])))
        if "*" in caps:
            return True
        return cap_name in caps

    def _dynamic_allows_unlocked(self, role, capability):
        role_name = _norm(role)
        cap_name = _norm(capability)
        caps = set((_norm(x) for x in (self._dynamic_policies.get("allow", {}).get(role_name) or [])))
        if "*" in caps:
            return True
        return cap_name in caps

    def _dynamic_denies_unlocked(self, role, capability):
        role_name = _norm(role)
        cap_name = _norm(capability)
        caps = set((_norm(x) for x in (self._dynamic_policies.get("deny", {}).get(role_name) or [])))
        if "*" in caps:
            return True
        return cap_name in caps

    def _policy_admin_map_unlocked(self):
        policy_admin_map = {}
        for mapping in (
            self._static_policies.get("policy_admin_roles") or {},
            self._dynamic_policies.get("policy_admin_roles") or {},
        ):
            for target_policy, admin_roles in mapping.items():
                target = _norm(target_policy)
                if not target:
                    continue
                merged = set(policy_admin_map.get(target) or [])
                merged.update(self._normalize_list(admin_roles))
                if merged:
                    policy_admin_map[target] = sorted(merged)
        return policy_admin_map

    def _is_policy_admin_for_role_unlocked(self, actor_role, target_role):
        actor_role_name = _norm(actor_role)
        target_role_name = _norm(target_role)
        if not actor_role_name or not target_role_name:
            return False
        policy_admin_map = self._policy_admin_map_unlocked()
        allowed = set(policy_admin_map.get(target_role_name) or [])
        allowed.update(policy_admin_map.get("*") or [])
        return actor_role_name in allowed

    def _role_admin_map_unlocked(self):
        role_admins = {}
        for mapping in (
            self._static_roles.get("role_admin_roles") or {},
            self._dynamic_roles.get("role_admin_roles") or {},
        ):
            for target_role, admin_roles in mapping.items():
                target = _norm(target_role)
                if not target:
                    continue
                merged = set(role_admins.get(target) or [])
                merged.update(self._normalize_list(admin_roles))
                if merged:
                    role_admins[target] = sorted(merged)
        return role_admins

    def _is_role_admin_for_role_unlocked(self, actor_role, target_role):
        actor_role_name = _norm(actor_role)
        target_role_name = _norm(target_role)
        if not actor_role_name or not target_role_name:
            return False
        role_admin_map = self._role_admin_map_unlocked()
        allowed = set(role_admin_map.get(target_role_name) or [])
        allowed.update(role_admin_map.get("*") or [])
        return actor_role_name in allowed

    def decide(self, capability, actor="", channel="", target=""):
        cap_name = _norm(capability)
        actor_name = _norm(actor)
        channel_name = _norm(channel)
        with self._lock:
            role = self._actor_role_unlocked(actor_name, channel_name)
            if not cap_name or cap_name not in self._capabilities:
                decision = PolicyDecision(capability=cap_name or "unknown", allowed=False, role=role, reason="unregistered capability")
                self._audit(actor_name, channel_name, cap_name, role, False, decision.reason, target=target)
                return decision
            if cap_name in set((_norm(x) for x in (self._static_policies.get("always_denied") or []))):
                decision = PolicyDecision(capability=cap_name, allowed=False, role=role, reason="always denied by static policy")
                self._audit(actor_name, channel_name, cap_name, role, False, decision.reason, target=target)
                return decision
            allowed = self._role_allows_static_unlocked(role, cap_name)
            reason = "allowed by static policy" if allowed else "not allowed by static policy"
            if self._dynamic_allows_unlocked(role, cap_name):
                allowed = True
                reason = "allowed by dynamic policy"
            if self._dynamic_denies_unlocked(role, cap_name):
                allowed = False
                reason = "denied by dynamic policy"
            decision = PolicyDecision(capability=cap_name, allowed=bool(allowed), role=role, reason=reason)
            self._audit(actor_name, channel_name, cap_name, role, decision.allowed, decision.reason, target=target)
            return decision

    def mutate_dynamic_policy(self, actor, channel, action, role="", capability="", user=""):
        actor_name = _norm(actor)
        channel_name = _norm(channel)
        action_name = _norm(action)
        role_name = _norm(role)
        cap_name = _norm(capability)
        user_name = _norm(user)
        with self._lock:
            if action_name == "bootstrap-secret-admin":
                target_user = user_name or actor_name
                target_channel = channel_name
                target_role = role_name or "trusted-admin"
                if not target_user:
                    return False, "missing user for bootstrap admin"
                if not target_channel:
                    return False, "missing channel for bootstrap admin"
                channel_user_roles = self._dynamic_roles.get("channel_user_roles") or {}
                role_map = dict(channel_user_roles.get(target_channel) or {})
                role_map[target_user] = target_role
                channel_user_roles[target_channel] = role_map
                self._dynamic_roles["channel_user_roles"] = channel_user_roles
                self._save_dynamic_roles()
                return True, f"bootstrap admin granted: user={target_user} channel={target_channel} role={target_role}"
            if action_name == "renounce-role":
                if not actor_name:
                    return False, "missing actor"
                dynamic_channel_user_roles = self._dynamic_roles.get("channel_user_roles") or {}
                channel_dynamic_roles = dict(dynamic_channel_user_roles.get(channel_name) or {})
                if actor_name in channel_dynamic_roles:
                    channel_dynamic_roles.pop(actor_name, None)
                    if channel_dynamic_roles:
                        dynamic_channel_user_roles[channel_name] = channel_dynamic_roles
                    elif channel_name in dynamic_channel_user_roles:
                        dynamic_channel_user_roles.pop(channel_name, None)
                    self._dynamic_roles["channel_user_roles"] = dynamic_channel_user_roles
                    self._save_dynamic_roles()
                    return True, f"user role removed in channel {channel_name}: {actor_name}"
                static_channel_user_roles = self._static_roles.get("channel_user_roles") or {}
                channel_static_roles = static_channel_user_roles.get(channel_name) or {}
                if actor_name in channel_static_roles:
                    return False, "cannot renounce static channel role; edit static roles file"
                user_roles = self._dynamic_roles.get("user_roles") or {}
                if actor_name in user_roles:
                    user_roles.pop(actor_name, None)
                    self._dynamic_roles["user_roles"] = user_roles
                    self._save_dynamic_roles()
                    return True, f"user role removed: {actor_name}"
                static_user_roles = self._static_roles.get("user_roles") or {}
                if actor_name in static_user_roles:
                    return False, "cannot renounce static role; edit static roles file"
                return False, "no dynamic role set for actor"

            actor_role = self._actor_role_unlocked(actor_name, channel_name)
            if action_name in {"allow", "deny", "unallow", "undeny"}:
                if not role_name:
                    return False, "missing role"
                if not self._is_policy_admin_for_role_unlocked(actor_role, role_name):
                    return False, f"policy admin role required for role '{role_name}' (current role: {actor_role})"
                if not cap_name:
                    return False, "missing capability"
                if cap_name != "*" and cap_name not in self._capabilities:
                    return False, f"unknown capability: {cap_name}"
                key = "allow" if action_name in {"allow", "unallow"} else "deny"
                role_map = self._dynamic_policies.get(key) or {}
                caps = set((_norm(x) for x in (role_map.get(role_name) or [])))
                if action_name in {"allow", "deny"}:
                    caps.add(cap_name)
                else:
                    caps.discard(cap_name)
                if caps:
                    role_map[role_name] = sorted(caps)
                elif role_name in role_map:
                    role_map.pop(role_name, None)
                self._dynamic_policies[key] = role_map
                self._save_dynamic_policies()
                return True, f"dynamic policy updated: {action_name} role={role_name} capability={cap_name}"
            if action_name in {"set-policy-admin", "unset-policy-admin"}:
                target_role_name = role_name
                admin_role_name = cap_name
                if not target_role_name:
                    return False, "missing target role"
                if not admin_role_name:
                    return False, "missing admin role"
                if not self._is_policy_admin_for_role_unlocked(actor_role, target_role_name):
                    return False, f"policy admin role required for role '{target_role_name}' (current role: {actor_role})"
                policy_admin_map = self._dynamic_policies.get("policy_admin_roles") or {}
                admin_roles = set(policy_admin_map.get(target_role_name) or [])
                if action_name == "set-policy-admin":
                    admin_roles.add(admin_role_name)
                else:
                    admin_roles.discard(admin_role_name)
                if admin_roles:
                    policy_admin_map[target_role_name] = sorted(admin_roles)
                elif target_role_name in policy_admin_map:
                    policy_admin_map.pop(target_role_name, None)
                self._dynamic_policies["policy_admin_roles"] = policy_admin_map
                self._save_dynamic_policies()
                return True, f"policy admin updated: {action_name} target={target_role_name} admin_role={admin_role_name}"
            if action_name in {"set-role-admin", "unset-role-admin"}:
                target_role_name = role_name
                admin_role_name = cap_name
                if not target_role_name:
                    return False, "missing target role"
                if not admin_role_name:
                    return False, "missing admin role"
                if not self._is_policy_admin_for_role_unlocked(actor_role, target_role_name):
                    return False, f"policy admin role required for role '{target_role_name}' (current role: {actor_role})"
                role_admin_map = self._dynamic_roles.get("role_admin_roles") or {}
                admin_roles = set(role_admin_map.get(target_role_name) or [])
                if action_name == "set-role-admin":
                    admin_roles.add(admin_role_name)
                else:
                    admin_roles.discard(admin_role_name)
                if admin_roles:
                    role_admin_map[target_role_name] = sorted(admin_roles)
                elif target_role_name in role_admin_map:
                    role_admin_map.pop(target_role_name, None)
                self._dynamic_roles["role_admin_roles"] = role_admin_map
                self._save_dynamic_roles()
                return True, f"role admin updated: {action_name} target={target_role_name} admin_role={admin_role_name}"
            if action_name == "set-role":
                if not user_name or not role_name:
                    return False, "missing user or role"
                if not channel_name:
                    return False, "missing channel"
                if not self._is_role_admin_for_role_unlocked(actor_role, role_name):
                    return False, f"role admin required for role '{role_name}' (current role: {actor_role})"
                channel_user_roles = self._dynamic_roles.get("channel_user_roles") or {}
                role_map = dict(channel_user_roles.get(channel_name) or {})
                role_map[user_name] = role_name
                channel_user_roles[channel_name] = role_map
                self._dynamic_roles["channel_user_roles"] = channel_user_roles
                self._save_dynamic_roles()
                return True, f"user role set in channel {channel_name}: {user_name} -> {role_name}"
            if action_name == "unset-role":
                if not user_name:
                    return False, "missing user"
                if user_name == actor_name:
                    return False, "cannot revoke your own role with revoke/unrole; use 'renounce role'"
                if not channel_name:
                    return False, "missing channel"
                channel_user_roles = self._dynamic_roles.get("channel_user_roles") or {}
                role_map = dict(channel_user_roles.get(channel_name) or {})
                if user_name in role_map:
                    target_role = _norm(role_map.get(user_name))
                    if not self._is_role_admin_for_role_unlocked(actor_role, target_role):
                        return False, f"role admin required for role '{target_role}' (current role: {actor_role})"
                    role_map.pop(user_name, None)
                    if role_map:
                        channel_user_roles[channel_name] = role_map
                    elif channel_name in channel_user_roles:
                        channel_user_roles.pop(channel_name, None)
                    self._dynamic_roles["channel_user_roles"] = channel_user_roles
                    self._save_dynamic_roles()
                    return True, f"user role removed in channel {channel_name}: {user_name}"
                static_channel_user_roles = self._static_roles.get("channel_user_roles") or {}
                static_role_map = static_channel_user_roles.get(channel_name) or {}
                if user_name in static_role_map:
                    return False, "cannot unset static channel role; edit static roles file"
                user_roles = self._dynamic_roles.get("user_roles") or {}
                if user_name in user_roles:
                    target_role = _norm(user_roles.get(user_name))
                    if not self._is_role_admin_for_role_unlocked(actor_role, target_role):
                        return False, f"role admin required for role '{target_role}' (current role: {actor_role})"
                    user_roles.pop(user_name, None)
                    self._dynamic_roles["user_roles"] = user_roles
                    self._save_dynamic_roles()
                    return True, f"user global role removed: {user_name}"
                static_user_roles = self._static_roles.get("user_roles") or {}
                if user_name in static_user_roles:
                    return False, "cannot unset static role; edit static roles file"
                return False, f"user role not found: {user_name}"
            return False, f"unsupported action: {action_name}"

    def describe_policy(self, actor="", channel=""):
        actor_name = _norm(actor)
        channel_name = _norm(channel)
        with self._lock:
            role = self._actor_role_unlocked(actor_name, channel_name)
            policy_admin_targets = sorted(
                target
                for target in self._policy_admin_map_unlocked()
                if self._is_policy_admin_for_role_unlocked(role, target)
            )
            caps = sorted(self._capabilities.keys())
            role_admin_targets = sorted(
                target
                for target in self._role_admin_map_unlocked()
                if self._is_role_admin_for_role_unlocked(role, target)
            )
            return (
                f"role={role} capabilities={','.join(caps)} "
                f"policy_admin_targets={','.join(policy_admin_targets)} "
                f"role_admin_targets={','.join(role_admin_targets)} "
                f"default_role={_norm(self._static_roles.get('default_role'))}"
            )


_ENGINE_LOCK = threading.Lock()
_ENGINE = None


def get_engine():
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = CapabilityPolicyEngine()
        return _ENGINE
