"""Trusted configuration, loaded from the control repository's default branch."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


def repository(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ConfigurationError("Use a repository name in owner/repository form.")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ConfigurationError("Invalid repository name.")
    return value


def branch(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", value)
            or any(part in {"", ".", ".."} or part.endswith((".lock", ".")) for part in value.split("/"))
            or ".." in value):
        raise ConfigurationError("Invalid branch name.")
    return value


def endpoint(value, *, local=False):
    if not isinstance(value, str):
        raise ConfigurationError("Configure a URL for each endpoint.")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname:
        raise ConfigurationError("Use an HTTPS endpoint (HTTP requires explicit allow_http).")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in value):
        raise ConfigurationError("Endpoints must not contain credentials, queries, or fragments.")
    return value.rstrip("/")


PACKAGES = {"openai": "@ai-sdk/openai", "openai-compatible": "@ai-sdk/openai-compatible",
            "anthropic": "@ai-sdk/anthropic", "google": "@ai-sdk/google", "azure": "@ai-sdk/azure"}


@dataclass(frozen=True)
class Model:
    alias: str
    provider: str
    model: str
    endpoint: str
    key_env: str
    token_param: str = ""

    @property
    def package(self):
        return PACKAGES[self.provider]

    def api_key(self):
        key = os.environ.get(self.key_env, "") if self.key_env else ""
        if self.key_env and not key:
            raise ConfigurationError(f"The configured model credential {self.key_env} is missing.")
        return key


@dataclass(frozen=True)
class Config:
    forge: str
    server_url: str
    api_url: str
    control_repository: str
    target_repository: str
    base_branch: str
    models: dict[str, Model]
    default_model: str
    prepare: list[list[str]]
    checks: list[list[str]]
    state_branch: str = "bananavibe-state"
    image: str = "bananavibe-sandbox:2.0.0"
    max_minutes: int = 45
    max_iterations: int = 12
    max_calls: int = 1000
    poll_seconds: int = 15
    progress_seconds: int = 120
    lease_seconds: int = 300
    memory_mb: int = 4096
    cpus: int = 2
    max_workspace_mb: int = 512
    forbidden_paths: tuple[str, ...] = (".github/workflows/", ".forgejo/workflows/", ".gitea/workflows/", ".bananavibe.toml")
    open_issues: bool = True
    allow_http: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path, *, control_repository=None):
        with Path(path).open("rb") as source:
            data = tomllib.load(source)
        forge = data.get("forge", "github")
        if forge not in {"github", "forgejo"}:
            raise ConfigurationError("forge must be github or forgejo.")
        allow_http = data.get("allow_http", False)
        if type(allow_http) is not bool or type(data.get("open_issues", True)) is not bool:
            raise ConfigurationError("allow_http and open_issues must be true or false.")
        server = endpoint(data.get("server_url", "https://github.com"), local=allow_http)
        default_api = "https://api.github.com" if server == "https://github.com" else server + ("/api/v1" if forge == "forgejo" else "/api/v3")
        api = endpoint(data.get("api_url", default_api), local=allow_http)
        control = repository(control_repository or data.get("control_repository", ""))
        target = repository(data.get("target_repository", control))
        models = {}
        model_config = data.get("models", {})
        if not isinstance(model_config, dict):
            raise ConfigurationError("models must contain named model tables.")
        for alias, item in model_config.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", alias) or not isinstance(item, dict) or item.get("provider") not in PACKAGES:
                raise ConfigurationError("Invalid model alias or provider.")
            model = item.get("model", "")
            if not re.fullmatch(r"[A-Za-z0-9_./:@-]{1,200}", model):
                raise ConfigurationError("Invalid provider model ID.")
            key = item.get("api_key_env", "")
            if key and not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ConfigurationError("api_key_env must name an environment variable.")
            if key in {"GITHUB_TOKEN", "FORGEJO_TOKEN", "BANANAVIBE_TOKEN"}:
                raise ConfigurationError("Forge credentials cannot be used as provider credentials.")
            token_param = item.get("token_param", "")
            if token_param not in {"", "max_completion_tokens"}:
                raise ConfigurationError("Unsupported token parameter.")
            models[alias] = Model(alias, item["provider"], model, endpoint(item.get("endpoint"), local=allow_http), key, token_param)
        default = data.get("default_model", next(iter(models), ""))
        if not models or default not in models:
            raise ConfigurationError("Configure at least one model and a valid default_model.")
        limits = data.get("limits", {})
        if not isinstance(limits, dict):
            raise ConfigurationError("limits must be a table.")
        numeric = {name: limits.get(name, field.default) for name, field in cls.__dataclass_fields__.items()
                   if name in {"max_minutes", "max_iterations", "max_calls", "poll_seconds", "progress_seconds", "lease_seconds", "memory_mb", "cpus", "max_workspace_mb"}}
        bounds = {"max_minutes": (1, 300), "max_iterations": (1, 100), "max_calls": (1, 10000),
                  "poll_seconds": (1, 60), "progress_seconds": (30, 1800), "lease_seconds": (60, 900),
                  "memory_mb": (512, 32768), "cpus": (1, 32), "max_workspace_mb": (16, 8192)}
        for name, value in numeric.items():
            low, high = bounds[name]
            if type(value) is not int or not low <= value <= high:
                raise ConfigurationError(f"{name} must be between {low} and {high}.")
        def commands(name, required=False):
            value = data.get(name, [])
            if not isinstance(value, list) or (required and not value):
                raise ConfigurationError(f"{name} must contain command argument lists.")
            for command in value:
                if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in command):
                    raise ConfigurationError(f"{name} commands must be nonempty argument lists.")
            return value
        state = branch(data.get("state_branch", "bananavibe-state"))
        base = branch(data.get("base_branch", "main"))
        if state == base or state.startswith("bananavibe/"):
            raise ConfigurationError("The state branch must be separate from source and task branches.")
        image = data.get("image", "bananavibe-sandbox:2.0.0")
        if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,250}", image):
            raise ConfigurationError("Invalid container image name.")
        protected = data.get("forbidden_paths", cls.__dataclass_fields__["forbidden_paths"].default)
        if not isinstance(protected, (list, tuple)) or not all(isinstance(path, str) and path and not path.startswith("/") and ".." not in path.split("/") for path in protected):
            raise ConfigurationError("forbidden_paths must contain relative repository paths.")
        return cls(forge, server, api, control, target, base, models, default,
                   commands("prepare"), commands("checks", True), state_branch=state,
                   image=image, forbidden_paths=tuple(protected),
                   open_issues=bool(data.get("open_issues", True)), allow_http=bool(allow_http), raw=data, **numeric)
