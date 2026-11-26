import errno
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from dotenv import load_dotenv

from .utils.dns import build_fqdn
from .utils.logger import get_logger

_API_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

# Cloudflare's "Auto" TTL. Proxied records are always served with Auto TTL and
# the API rejects an explicit value on them.
CLOUDFLARE_AUTO_TTL = 1
DEFAULT_TTL = 120

# How many timestamped copies of config.yml to keep before each rewrite.
CONFIG_BACKUPS_KEPT = 10

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


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
        """Persist _raw_config to config.yml.

        The previous file is copied to the backup directory first, and the new
        content is written to a temp file and renamed over the original, so a
        crash mid-write cannot leave a truncated config. If anything fails, the
        in-memory config is reloaded from disk so memory never claims a change
        the file does not have.
        """
        try:
            text = yaml.dump(self._raw_config, default_flow_style=False, allow_unicode=True, sort_keys=False)
            if yaml.safe_load(text) != self._raw_config:
                raise RuntimeError("config.yml would not read back identically, refusing to write it")
            self._backup_config()
            self._write_config(text)
        except Exception:
            try:
                self._config = self._load_config()
            except Exception:
                pass
            raise
        self._config = self._substitute_env_vars(self._raw_config)

    @property
    def backup_dir(self) -> Path:
        """Backups go next to the state file, which is the mounted volume under Docker."""
        return Path(self.state_file).parent / "config-backups"

    def _backup_config(self) -> None:
        if not self.config_path.exists():
            return
        logger = get_logger(__name__)
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            target = self.backup_dir / f"config-{stamp}.yml"
            n = 1
            while target.exists():
                target = self.backup_dir / f"config-{stamp}-{n}.yml"
                n += 1
            shutil.copy2(self.config_path, target)
            backups = sorted(self.backup_dir.glob("config-*.yml"), key=lambda f: f.stat().st_mtime)
            for old in backups[:-CONFIG_BACKUPS_KEPT]:
                old.unlink(missing_ok=True)
        except OSError as e:
            # A missing backup is worth a warning, not a refused API call.
            logger.warning(f"Could not back up config.yml to {self.backup_dir}: {e}")

    def _write_config(self, text: str) -> None:
        path = self.config_path
        try:
            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
        except OSError:
            fd, tmp_path = None, None

        if fd is not None:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                if path.exists():
                    shutil.copymode(path, tmp_path)
                os.replace(tmp_path, path)
                return
            except OSError as e:
                Path(tmp_path).unlink(missing_ok=True)
                # Docker bind-mounts config.yml as a single file, and a mount point
                # cannot be renamed over (EBUSY). Fall through to an in-place write.
                if e.errno not in (errno.EBUSY, errno.EXDEV, errno.EPERM, errno.EACCES):
                    raise

        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

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
    def cf_access_client_id(self) -> str:
        return os.getenv("CF_ACCESS_CLIENT_ID", "")

    @property
    def cf_access_client_secret(self) -> str:
        return os.getenv("CF_ACCESS_CLIENT_SECRET", "")

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
    def telegram_commands_enabled(self) -> bool:
        return _env_bool("TELEGRAM_COMMANDS_ENABLED")

    @property
    def telegram_command_users(self) -> List[int]:
        """Optional Telegram user IDs allowed to run commands, on top of the chat check."""
        users = []
        for part in os.getenv("TELEGRAM_COMMAND_USERS", "").replace(";", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                users.append(int(part))
        return users

    @property
    def telegram_notify_maintenance(self) -> bool:
        return _env_bool("TELEGRAM_NOTIFY_MAINTENANCE", default=True)

    @property
    def telegram_notify_discovery(self) -> bool:
        return _env_bool("TELEGRAM_NOTIFY_DISCOVERY", default=True)

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

    def _int_option(self, key: str, default: int, minimum: int, maximum: int) -> int:
        value = self.get(key, default)
        if isinstance(value, bool):
            return default
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        return value if minimum <= value <= maximum else default

    @property
    def fail_threshold(self) -> int:
        """Consecutive unhealthy cycles before a healthy node is taken out of DNS."""
        return self._int_option("remnawave.fail-threshold", 1, 1, 100)

    @property
    def recover_threshold(self) -> int:
        """Consecutive healthy cycles before an unhealthy node is put back into DNS."""
        return self._int_option("remnawave.recover-threshold", 1, 1, 100)

    @property
    def probe(self) -> Dict[str, Any]:
        """`probe` block: optional TCP reachability check from the monitor."""
        raw = self.get("probe") or {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "enabled": bool(raw.get("enabled", False)),
            "port": self._int_option("probe.port", 443, 1, 65535),
            "timeout": self._int_option("probe.timeout", 3, 1, 60),
            "attempts": self._int_option("probe.attempts", 2, 1, 5),
        }

    @property
    def maintenance(self) -> List[str]:
        """Node names, addresses or UUIDs currently in maintenance."""
        raw = self.get("maintenance") or []
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    @property
    def summary(self) -> Dict[str, Any]:
        """`summary` block: periodic Telegram report."""
        raw = self.get("summary") or {}
        if not isinstance(raw, dict):
            raw = {}
        schedule = str(raw.get("schedule") or "daily").strip().lower()
        at = str(raw.get("time") or "09:00").strip()
        weekday = str(raw.get("weekday") or "monday").strip().lower()
        return {
            "enabled": bool(raw.get("enabled", False)),
            "schedule": schedule if schedule in ("daily", "weekly") else "daily",
            "time": at if _TIME_RE.match(at) else "09:00",
            "weekday": weekday if weekday in _WEEKDAYS else "monday",
        }

    def validate_options(self) -> None:
        """Reject malformed values in the optional blocks instead of silently
        falling back to defaults the user did not ask for."""
        def check_int(key: str, minimum: int, maximum: int) -> None:
            value = self.get(key)
            if value is None:
                return
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"'{key}' must be a whole number between {minimum} and {maximum}, got {value!r}")

        check_int("remnawave.check-interval", 5, 86400)
        check_int("remnawave.fail-threshold", 1, 100)
        check_int("remnawave.recover-threshold", 1, 100)

        for block in ("probe", "summary", "auto-discovery"):
            value = self.get(block)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"'{block}' must be a mapping")
        check_int("probe.port", 1, 65535)
        check_int("probe.timeout", 1, 60)
        check_int("probe.attempts", 1, 5)

        for zone in self.get_all_zones():
            if zone["probe_port"] is not None and not 1 <= zone["probe_port"] <= 65535:
                raise ValueError(
                    f"Zone '{build_fqdn(zone['name'], zone['domain'])}' has an invalid probe-port"
                )

        maintenance = self.get("maintenance")
        if maintenance is not None and not isinstance(maintenance, list):
            raise ValueError("'maintenance' must be a list of node names, addresses or UUIDs")

        schedule = self.get("summary.schedule")
        if schedule is not None and str(schedule).lower() not in ("daily", "weekly"):
            raise ValueError(f"'summary.schedule' must be daily or weekly, got {schedule!r}")
        at = self.get("summary.time")
        if at is not None and not _TIME_RE.match(str(at)):
            raise ValueError(f"'summary.time' must be HH:MM (24h), got {at!r}")
        weekday = self.get("summary.weekday")
        if weekday is not None and str(weekday).lower() not in _WEEKDAYS:
            raise ValueError(f"'summary.weekday' must be a day name like monday, got {weekday!r}")

    @property
    def auto_discovery(self) -> Dict[str, Any]:
        """`auto-discovery` block from config.yml, with defaults filled in."""
        raw = self.get("auto-discovery") or {}
        if not isinstance(raw, dict):
            raw = {}
        proxied = bool(raw.get("proxied", False))
        return {
            "enabled": bool(raw.get("enabled", False)),
            "default-zone": str(raw.get("default-zone") or "").strip().lower().rstrip("."),
            "ttl": self._effective_ttl(raw.get("ttl", DEFAULT_TTL), proxied),
            "proxied": proxied,
        }

    def split_fqdn(self, fqdn: str) -> Optional[Tuple[str, str]]:
        """Map an FQDN to (domain, zone_name) using the most specific configured domain."""
        fqdn = fqdn.strip().lower().rstrip(".")
        best: Optional[Tuple[str, str]] = None
        for domain_config in self.domains:
            domain = str(domain_config.get("domain") or "").lower()
            if not domain:
                continue
            if fqdn == domain:
                zone = "@"
            elif fqdn.endswith("." + domain):
                zone = fqdn[: -len(domain) - 1]
            else:
                continue
            if best is None or len(domain) > len(best[0]):
                best = (domain, zone)
        return best

    def add_discovered_nodes(self, additions: list, ttl: int, proxied: bool) -> list:
        """Add auto-discovered nodes to config.yml in a single write.

        Each addition is a dict with domain, zone, ip, address. Entries whose
        address or IP is already in the target zone are skipped. Returns the
        additions that were applied, each with `new_zone` set.
        """
        applied = []
        for add in additions:
            # Match on the substituted view but edit the raw entry at the same
            # index, so ${VAR} placeholders in config.yml survive the rewrite.
            d_idx = next(
                (i for i, d in enumerate(self.domains)
                 if str(d.get("domain") or "").lower() == add["domain"]),
                None,
            )
            if d_idx is None:
                continue
            zones = self.domains[d_idx].get("zones") or []
            z_idx = next(
                (i for i, z in enumerate(zones) if str(z.get("name")).lower() == add["zone"]),
                None,
            )

            raw_domain = self._raw_config["domains"][d_idx]
            if not raw_domain.get("zones"):
                raw_domain["zones"] = []

            if z_idx is None:
                target = {"name": add["zone"], "ttl": ttl, "proxied": proxied}
                raw_domain["zones"].append(target)
            else:
                existing = self._parse_zone_nodes(zones[z_idx])
                if any(e["address"] == add["address"] or e["ip"] == add["ip"] for e in existing):
                    continue
                target = raw_domain["zones"][z_idx]

            if add["ip"] == add["address"]:
                target["ips"] = list(target.get("ips") or []) + [add["ip"]]
            else:
                target["nodes"] = list(target.get("nodes") or []) + [
                    {"ip": add["ip"], "address": add["address"]}
                ]

            # Refresh the substituted view so later additions in this batch see it.
            self._config = self._substitute_env_vars(self._raw_config)
            applied.append({**add, "new_zone": z_idx is None})

        if applied:
            self._save()
        return applied

    def reload(self) -> None:
        """Re-read config.yml. If the new file fails validation, the previous
        config stays active and the error is raised."""
        previous_raw, previous = self._raw_config, self._config
        try:
            self._config = self._load_config()
            self.validate_zones()
            self.validate_options()
        except Exception:
            self._raw_config, self._config = previous_raw, previous
            raise

    # --- Config mutation methods ---

    def add_maintenance(self, node: str) -> bool:
        """Put a node into maintenance. Returns False if it already was."""
        node = node.strip()
        current = self._raw_config.get("maintenance")
        if not isinstance(current, list):
            current = []
        if any(str(item).strip().lower() == node.lower() for item in current):
            return False
        self._raw_config["maintenance"] = current + [node]
        self._save()
        return True

    def remove_maintenance(self, identifiers: List[str]) -> bool:
        """Take a node out of maintenance, matching any of its identifiers
        (name, address, UUID). Returns False if it was not in maintenance."""
        wanted = {i.strip().lower() for i in identifiers if i}
        current = self._raw_config.get("maintenance")
        if not isinstance(current, list):
            return False
        remaining = [item for item in current if str(item).strip().lower() not in wanted]
        if len(remaining) == len(current):
            return False
        self._raw_config["maintenance"] = remaining
        self._save()
        return True

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

        if bool(self.cf_access_client_id) != bool(self.cf_access_client_secret):
            raise ValueError(
                "CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET must be set together "
                "(Cloudflare Access service token for the panel)"
            )

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
        self.validate_options()

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

    @staticmethod
    def _zone_probe_port(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0  # reported by validate_options as invalid

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
                    "probe_port": self._zone_probe_port(zone.get("probe-port")),
                }
                zones.append(zone_data)
        return zones
