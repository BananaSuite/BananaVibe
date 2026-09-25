"""OpenCode in a disposable container; only its credential gateway has egress."""

import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

import requests


class Interrupted(RuntimeError):
    pass


class EngineUnavailable(RuntimeError):
    pass


class DockerEngine:
    def __init__(self, config, model, workspace, pulse):
        self.config, self.model, self.workspace, self.pulse = config, model, Path(workspace), pulse
        self.name = "bananavibe-" + secrets.token_hex(8)
        self.network = self.name + "-net"
        self.server_secret, self.control_secret = secrets.token_hex(32), secrets.token_hex(32)
        self.temporary = tempfile.TemporaryDirectory(prefix="bananavibe-engine-")
        self.root = Path(self.temporary.name)
        self.port, self.session_id = None, None
        self.started = False
        self.frozen = False

    def docker(self, *args, check=True, timeout=120):
        result = subprocess.run(["docker", *map(str, args)], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f"Docker {args[0]} failed. Check the runner's Docker installation and available resources.")
        return result

    @staticmethod
    def build(config, pulse=None):
        check = subprocess.run(["docker", "image", "inspect", config.image], capture_output=True, timeout=30)
        if check.returncode:
            source = Path(__file__).resolve().parents[1]
            process = subprocess.Popen(["docker", "build", "-f", str(source / "Dockerfile.sandbox"), "-t", config.image, str(source)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 900
            try:
                while process.poll() is None:
                    if pulse:
                        pulse("Building the pinned coding engine")
                    if time.monotonic() >= deadline:
                        raise RuntimeError("The coding engine image build timed out.")
                    time.sleep(2)
                if process.returncode:
                    raise RuntimeError("The pinned OpenCode image could not be built. Check Docker and registry access.")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)

    def limits(self, memory):
        uid = os.getuid() or 65532
        gid = os.getgid() if os.getuid() else 65532
        return ["--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--pids-limit=256",
                "--memory", f"{memory}m", "--memory-swap", f"{memory}m", "--cpus", str(self.config.cpus),
                "--user", f"{uid}:{gid}", "--ulimit", "fsize=134217728:134217728", "--label", "org.bananavibe.actions=true",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777", "--log-opt", "max-size=5m", "--log-opt", "max-file=1"]

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        if os.getuid() == 0:
            os.chown(path, 65532, 65532)
        return path

    def start(self):
        self.build(self.config, self.pulse)
        token = secrets.token_hex(32)
        gateway = self.write_json("gateway.json", {"token": token, "endpoint": self.model.endpoint, "api_key": self.model.api_key(),
                      "model": self.model.model, "package": self.model.package, "token_param": self.model.token_param,
                      "max_calls": self.config.max_calls, "expires_at": time.time() + self.config.max_minutes * 60,
                      "control_token": self.control_secret})
        package = "@ai-sdk/openai-compatible" if self.model.provider == "azure" else self.model.package
        agent = self.write_json("agent.json", {"$schema": "https://opencode.ai/config.json", "model": "maintenance/" + self.model.model,
                    "provider": {"maintenance": {"npm": package, "name": "Configured maintenance model",
                        "options": {"baseURL": "http://gateway:8000/api", "apiKey": token},
                        "models": {self.model.model: {"name": self.model.model, "tool_call": True}}}},
                    "enabled_providers": ["maintenance"], "permission": "allow", "autoupdate": False, "share": "disabled"})
        state = self.root / "state"
        state.mkdir(mode=0o700)
        if os.getuid() == 0:
            os.chown(state, 65532, 65532)
        self.docker("network", "create", "--internal", "--opt", "com.docker.network.bridge.inhibit_ipv4=true", self.network)
        self.docker("create", "--name", self.name + "-gateway", "--network", "bridge", "--publish", "127.0.0.1::8082",
                    *self.limits(256), "--mount", f"type=bind,src={gateway},dst=/run/gateway.json,readonly",
                    "--entrypoint", "python3", self.config.image, "/opt/bananavibe/gateway.py", "/run/gateway.json")
        self.docker("network", "connect", "--alias", "gateway", self.network, self.name + "-gateway")
        self.docker("start", self.name + "-gateway")
        env = {"HOME": "/state", "XDG_DATA_HOME": "/state/data", "XDG_CONFIG_HOME": "/state/config", "XDG_CACHE_HOME": "/state/cache",
               "XDG_STATE_HOME": "/state/state", "OPENCODE_SERVER_PASSWORD": self.server_secret, "OPENCODE_SERVER_USERNAME": "opencode",
               "OPENCODE_CONFIG": "/run/agent.json", "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_AUTOUPDATE": "true",
               "HTTP_PROXY": "http://gateway:8080", "HTTPS_PROXY": "http://gateway:8080", "http_proxy": "http://gateway:8080",
               "https_proxy": "http://gateway:8080", "NO_PROXY": "localhost,127.0.0.1,gateway", "no_proxy": "localhost,127.0.0.1,gateway"}
        self.docker("run", "--detach", "--name", self.name, "--network", self.network, "--network-alias", "agent",
                    *self.limits(self.config.memory_mb), "--mount", f"type=bind,src={self.workspace},dst=/workspace",
                    "--mount", f"type=bind,src={state},dst=/state", "--mount", f"type=bind,src={agent},dst=/run/agent.json,readonly",
                    "--workdir", "/workspace", *[part for key, value in env.items() for part in ("--env", f"{key}={value}")],
                    "--entrypoint", "opencode", self.config.image, "serve", "--hostname", "0.0.0.0", "--port", "4096")
        ports = json.loads(self.docker("inspect", self.name + "-gateway").stdout)[0]["NetworkSettings"]["Ports"]["8082/tcp"]
        if not ports or ports[0]["HostIp"] != "127.0.0.1":
            raise RuntimeError("The engine controller was not bound to loopback.")
        self.port = int(ports[0]["HostPort"])
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            self.pulse("Starting the coding engine")
            try:
                if self.request("GET", "/global/health").get("healthy"):
                    self.started = True
                    return self
            except RuntimeError:
                pass
            time.sleep(2)
        raise RuntimeError("The coding engine did not become ready.")

    def request(self, method, path, body=None):
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                return self.request_once(method, path, body)
            except EngineUnavailable:
                if attempt == attempts - 1:
                    raise
                # OpenCode can be briefly busy loading a provider after the
                # server becomes healthy. Retry reads, never submit a task twice.
                self.pulse()
                time.sleep(2)

    def request_once(self, method, path, body=None):
        client = requests.Session()
        client.trust_env = False
        try:
            with client.request(method, f"http://127.0.0.1:{self.port}{path}", json=body,
                                params={"directory": "/workspace"}, auth=("opencode", self.server_secret),
                                headers={"X-BananaVibe-Control": self.control_secret}, timeout=(3, 15 if method == "GET" else 60),
                                stream=True, allow_redirects=False) as response:
                if response.status_code in {502, 503, 504}:
                    raise EngineUnavailable("The coding engine is starting or temporarily unavailable.")
                if response.status_code >= 300:
                    raise RuntimeError(f"The coding engine returned HTTP {response.status_code}.")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        raise RuntimeError("The coding engine response is too large.")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                return json.loads(raw) if raw else None
        except (requests.RequestException, ValueError):
            raise EngineUnavailable("The coding engine is unavailable.") from None
        finally:
            client.close()

    def prompt(self, text):
        if not self.session_id:
            self.session_id = self.request("POST", "/session", {"title": "Maintenance draft"})["id"]
        before = {item.get("info", {}).get("id") for item in (self.request("GET", f"/session/{self.session_id}/message") or [])}
        self.request("POST", f"/session/{self.session_id}/prompt_async", {
            "model": {"providerID": "maintenance", "modelID": self.model.model}, "agent": "build",
            "parts": [{"type": "text", "text": text}]})
        while True:
            self.pulse("Implementing and checking the maintenance draft")
            for kind in ("question", "permission"):
                pending = self.request("GET", "/" + kind) or []
                matching = [item for item in pending if item.get("sessionID") == self.session_id]
                if matching:
                    return {"status": "blocked", "question": "The coding engine needs guidance: " + json.dumps(matching[0].get("questions", matching[0].get("permission", kind)))[:5000]}
            status = (self.request("GET", "/session/status") or {}).get(self.session_id, {"type": "idle"})
            messages = self.request("GET", f"/session/{self.session_id}/message") or []
            answers = [item for item in messages if item.get("info", {}).get("role") == "assistant" and item.get("info", {}).get("id") not in before]
            if answers and status.get("type") == "idle":
                if any(item.get("info", {}).get("error") for item in answers):
                    raise RuntimeError("The model provider or coding engine failed; check the configured model and quota.")
                return {"status": "idle"}
            time.sleep(self.config.poll_seconds)

    def command(self, command, *, phase="Running configured checks"):
        with tempfile.TemporaryFile(mode="w+b") as log:
            process = subprocess.Popen(["docker", "exec", "--workdir", "/workspace", self.name, *command], stdout=log, stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    self.pulse(phase)
                    if log.tell() > 8 * 1024 * 1024:
                        raise RuntimeError("A validation command produced excessive output.")
                    time.sleep(min(self.config.poll_seconds, 2))
                log.seek(max(0, log.tell() - 16000))
                output = log.read().decode(errors="replace")
                return process.returncode, output
            finally:
                if process.poll() is None:
                    # Stop the container too: terminating docker exec alone leaves its command alive.
                    self.docker("stop", "--time", "5", self.name, check=False, timeout=15)
                    process.terminate()
                    process.wait(timeout=15)

    def abort(self):
        if self.session_id and self.started:
            try:
                self.request("POST", f"/session/{self.session_id}/abort", {})
            except RuntimeError:
                pass

    def freeze(self):
        self.docker("pause", self.name)
        self.frozen = True

    def thaw(self):
        self.docker("unpause", self.name)
        self.frozen = False

    def close(self):
        if not self.frozen:
            self.abort()
        self.docker("rm", "--force", self.name, self.name + "-gateway", check=False, timeout=30)
        self.docker("network", "rm", self.network, check=False, timeout=30)
        self.temporary.cleanup()

    def __enter__(self):
        try:
            return self.start()
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()
