import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

from .utils.dns import build_fqdn

_API_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

# Cloudflare's "Auto" TTL. Proxied records are always served with Auto TTL and
# the API rejects an explicit value on them.
CLOUDFLARE_AUTO_TTL = 1
DEFAULT_TTL = 120


def _env_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key, "").strip().lower()
    if not value:
        return default
    return value in ("true", "1", "yes")


class Config:
    def __init__(self, config_path: str = "config.yml"):
        load_dotenv()

        self.config_path = Path(config_path)
        self._raw_config: Dict[str, Any] = {}
        self._config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        with open(self.config_path, "r") as f:
            self._raw_config = yaml.safe_load(f)

        return self._substitute_env_vars(self._raw_config)

    def _save(self) -> None:
        with open(self.config_path, "w") as f:
            yaml.dump(self._raw_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        self._config = self._substitute_env_vars(self._raw_config)

    def _substitute_env_vars(self, config: Any) -> Any:
        if isinstance(config, dict):
            return {k: self._substitute_env_vars(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [self._substitute_env_vars(item) for item in config]
        elif isinstance(config, str):
            pattern = re.compile(r"\$\{([^}]+)}")
            matches = pattern.findall(config)
            result = config
            for var_name in matches:
                var_value = os.getenv(var_name, "")
                result = result.replace(f"${{{var_name}}}", var_value)
            return result
        else:
            return config

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        value = self._config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default

        return value

    # --- Environment variables ---

    @property
    def remnawave_url(self) -> str:
        return os.getenv("REMNAWAVE_API_URL", "")

    @property
    def remnawave_api_key(self) -> str:
        return os.getenv("REMNAWAVE_API_KEY", "")

    @property
    def cloudflare_token(self) -> str:
        return os.getenv("CLOUDFLARE_API_TOKEN", "")

    @property
    def log_level(self) -> str:
        return os.getenv("LOG_LEVEL", "INFO")

    @property
    def api_enabled(self) -> bool:
        return _env_bool("API_ENABLED")

    @property
    def api_host(self) -> str:
        return os.getenv("API_HOST", "0.0.0.0")

    @property
    def api_port(self) -> int:
        try:
            return int(os.getenv("API_PORT", "8741"))
        except ValueError:
            return 8741

    @property
    def api_docs_enabled(self) -> bool:
        return _env_bool("API_DOCS")

    @property
    def api_token(self) -> str:
        return os.getenv("API_TOKEN", "")

    @property
    def telegram_enabled(self) -> bool:
        return _env_bool("TELEGRAM_ENABLED")

    @property
    def telegram_bot_token(self) -> str:
        return os.getenv("TELEGRAM_BOT_TOKEN", "")

    @property
    def telegram_chat_id(self) -> str:
        return os.getenv("TELEGRAM_CHAT_ID", "")

    @property
    def telegram_topic_id(self) -> int | None:
        topic_id = os.getenv("TELEGRAM_TOPIC_ID", "")
        if topic_id and topic_id.strip():
            try:
                return int(topic_id.strip())
            except ValueError:
                return None
        return None

    @property
    def timezone(self) -> str:
        return os.getenv("TIMEZONE", "UTC")

    @property
    def time_format(self) -> str:
        return os.getenv("TIME_FORMAT", "%d.%m.%Y %H:%M:%S")

    @property
    def language(self) -> str:
        return os.getenv("LANGUAGE", "en")

    @property
    def telegram_notify_dns_changes(self) -> bool:
        return _env_bool("TELEGRAM_NOTIFY_DNS_CHANGES", default=True)

    @property
    def telegram_notify_node_changes(self) -> bool:
        return _env_bool("TELEGRAM_NOTIFY_NODE_CHANGES", default=True)

    @property
    def telegram_notify_errors(self) -> bool:
        return _env_bool("TELEGRAM_NOTIFY_ERRORS", default=True)

    @property
    def telegram_notify_critical(self) -> bool:
        return _env_bool("TELEGRAM_NOTIFY_CRITICAL", default=True)

    @property
    def telegram_notify_api_changes(self) -> bool:
        return _env_bool("TELEGRAM_NOTIFY_API_CHANGES", default=True)

    @property
    def disable_unreachable_hosts(self) -> bool:
        return _env_bool("DISABLE_UNREACHABLE_HOSTS")

    @property
    def telegram_notify_host_changes(self) -> bool:
        return _env_bool("TELEGRAM_NOTIFY_HOST_CHANGES", default=True)

    @property
    def state_file(self) -> str:
        return os.getenv("STATE_FILE", "data/state.json")

    # --- YAML config ---

    @property
    def check_interval(self) -> int:
        return self.get("remnawave.check-interval", 30)

    @property
    def domains(self) -> list:
        return self.get("domains") or []

    def reload(self) -> None:
        self._config = self._load_config()

    # --- Config mutation methods ---

    def update_check_interval(self, interval: int) -> None:
        self._raw_config.setdefault("remnawave", {})["check-interval"] = interval
        self._save()

    def add_domain(self, domain: str, zones: list) -> None:
        domains = self._raw_config.setdefault("domains", [])
        for d in domains:
            if d.get("domain") == domain:
                raise ValueError(f"Domain '{domain}' already exists")
        domains.append({"domain": domain, "zones": zones})
        self._save()

    def remove_domain(self, domain: str) -> None:
        domains = self._raw_config.get("domains") or []
        new_domains = [d for d in domains if d.get("domain") != domain]
        if len(new_domains) == len(domains):
            raise ValueError(f"Domain '{domain}' not found")
        self._raw_config["domains"] = new_domains
        self._save()

    def add_zone(self, domain: str, zone: dict) -> None:
        for d in self._raw_config.get("domains") or []:
            if d.get("domain") == domain:
                zones = d.setdefault("zones", [])
                for z in zones:
                    if z.get("name") == zone["name"]:
                        raise ValueError(f"Zone '{zone['name']}' already exists for '{domain}'")
                zones.append(zone)
                self._save()
                return
        raise ValueError(f"Domain '{domain}' not found")

    def remove_zone(self, domain: str, zone_name: str) -> None:
        for d in self._raw_config.get("domains") or []:
            if d.get("domain") == domain:
                zones = d.get("zones") or []
                new_zones = [z for z in zones if z.get("name") != zone_name]
                if len(new_zones) == len(zones):
                    raise ValueError(f"Zone '{zone_name}' not found for '{domain}'")
                d["zones"] = new_zones
                self._save()
                return
        raise ValueError(f"Domain '{domain}' not found")

    def update_zone(self, domain: str, zone_name: str, **kwargs) -> None:
        for d in self._raw_config.get("domains") or []:
            if d.get("domain") == domain:
                for z in d.get("zones") or []:
                    if z.get("name") == zone_name:
                        for key, value in kwargs.items():
                            z[key] = value
                        self._save()
                        return
                raise ValueError(f"Zone '{zone_name}' not found for '{domain}'")
        raise ValueError(f"Domain '{domain}' not found")

    def validate(self) -> None:
        missing = []
        if not self.remnawave_url:
            missing.append("REMNAWAVE_API_URL")
        if not self.remnawave_api_key:
            missing.append("REMNAWAVE_API_KEY")
        if not self.cloudflare_token:
            missing.append("CLOUDFLARE_API_TOKEN")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        if self.api_enabled:
            token = self.api_token
            if not token:
                raise ValueError(
                    "API_TOKEN is required when API_ENABLED is true. "
                    "Generate one with: openssl rand -hex 32"
                )
            if not _API_TOKEN_RE.match(token):
                raise ValueError(
                    "API_TOKEN must be a 64-character lowercase hex string. "
                    "Generate one with: openssl rand -hex 32"
                )

        self.validate_zones()

    def validate_zones(self) -> list:
        """Check domain/zone structure and return human-readable warnings.

        Structural problems raise: a malformed zone would otherwise only surface
        as a failed Cloudflare call on every monitoring cycle. Cosmetic
        normalisations (proxied TTL) are returned as warnings instead.
        """
        warnings = []
        seen_fqdns = set()

        for domain_config in self.domains:
            if not isinstance(domain_config, dict):
                raise ValueError(f"Invalid domain entry (expected a mapping): {domain_config!r}")

            domain = domain_config.get("domain")
            if not domain:
                raise ValueError("Domain entry is missing a 'domain' value")

            zones = domain_config.get("zones") or []
            if not zones:
                warnings.append(f"Domain '{domain}' has no zones configured")

            for zone in zones:
                if not isinstance(zone, dict):
                    raise ValueError(f"Invalid zone entry for '{domain}' (expected a mapping): {zone!r}")

                name = zone.get("name")
                if not name:
                    raise ValueError(f"Zone for '{domain}' is missing a 'name' value")

                fqdn = build_fqdn(str(name), domain)
                if fqdn in seen_fqdns:
                    raise ValueError(f"Duplicate zone '{fqdn}' — two entries would fight over the same records")
                seen_fqdns.add(fqdn)

                if not self._parse_zone_nodes(zone):
                    raise ValueError(f"Zone '{fqdn}' has no usable node entries in 'nodes' or 'ips'")

                proxied = bool(zone.get("proxied", False))
                raw_ttl = zone.get("ttl", DEFAULT_TTL)
                if proxied and raw_ttl not in (None, CLOUDFLARE_AUTO_TTL):
                    warnings.append(
                        f"Zone '{fqdn}' is proxied, so ttl={raw_ttl} is ignored — "
                        f"Cloudflare always serves proxied records with Auto TTL"
                    )

        return warnings

    @staticmethod
    def _parse_zone_nodes(zone: dict) -> list:
        """Normalize a zone's node entries from either 'nodes' or legacy 'ips' format.

        Each returned entry has:
          ip      – the IP to write into Cloudflare DNS
          address – the node.address to match in Remnawave (defaults to ip)
        """
        result = []

        for entry in zone.get("nodes") or []:
            if not isinstance(entry, dict):
                continue
            ip = entry.get("ip")
            if not ip:
                continue
            result.append({
                "ip": ip,
                "address": entry.get("address") or ip,
            })

        for ip in zone.get("ips") or []:
            result.append({
                "ip": ip,
                "address": ip,
            })

        return result

    @staticmethod
    def _effective_ttl(ttl: Any, proxied: bool) -> int:
        """Cloudflare forces Auto TTL (1) on proxied records and rejects any other
        value, so normalise here rather than letting every create call fail."""
        if proxied:
            return CLOUDFLARE_AUTO_TTL
        try:
            return int(ttl)
        except (TypeError, ValueError):
            return DEFAULT_TTL

    def get_all_zones(self) -> list:
        zones = []
        for domain_config in self.domains:
            domain = domain_config.get("domain")
            for zone in domain_config.get("zones") or []:
                nodes = self._parse_zone_nodes(zone)
                proxied = bool(zone.get("proxied", False))
                zone_data = {
                    "domain": domain,
                    "name": zone.get("name"),
                    "ttl": self._effective_ttl(zone.get("ttl", DEFAULT_TTL), proxied),
                    "proxied": proxied,
                    "nodes": nodes,
                    "ips": [n["ip"] for n in nodes],  # backward-compat: list of DNS IPs
                }
                zones.append(zone_data)
        return zones
