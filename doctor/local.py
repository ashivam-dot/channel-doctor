"""The doctor's second brain: Qwen3.5-35B-A3B (Apache-2.0) served by llama.cpp on the runner when no Gemini model
answers. No account, key or quota; about 13 GB downloaded per job, so only the heal and improve jobs use it
(`DOCTOR_LOCAL=1`). Scout bench 2026-10-13: same gate passes as Gemma 12B on a CPU runner, 2.2x faster."""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

DIR = Path(os.environ.get("RUNNER_TEMP") or "/tmp") / "doctor-llm"
RELEASE = "b11517"
MODEL = ("unsloth/Qwen3.5-35B-A3B-GGUF", "Qwen3.5-35B-A3B-UD-IQ3_XXS.gguf")
NAME = "qwen3.5-35b-a3b (local)"
CTX = 16384
MAX_PROMPT_CHARS = 36_000  # about 10k tokens: CPU prompt reading runs near 20 tokens/s
NEED_FREE = 15 * 1024 ** 3


def enabled() -> bool:
    return os.environ.get("DOCTOR_LOCAL") == "1"


def _download(url: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "channel-doctor"}),
                                    timeout=120) as r, part.open("wb") as fh:
            shutil.copyfileobj(r, fh, 1 << 22)
        part.rename(path)
    finally:
        part.unlink(missing_ok=True)
    return path


def server() -> Path:
    exe = next(DIR.rglob("llama-server"), None)
    if exe:
        return exe
    arch = "macos-arm64" if platform.system() == "Darwin" else "ubuntu-x64"
    tgz = _download(f"https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}/llama-{RELEASE}-bin-{arch}.tar.gz",
                    DIR / f"llama-{arch}.tar.gz")
    with tarfile.open(tgz) as t:
        t.extractall(DIR, filter="data")
    tgz.unlink()
    exe = next(DIR.rglob("llama-server"), None)
    if exe is None:
        raise RuntimeError("llama.cpp release has no llama-server")
    exe.chmod(0o755)
    return exe


def weights() -> Path:
    path = DIR / MODEL[1]
    if not path.exists():
        DIR.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(DIR).free < NEED_FREE:
            raise RuntimeError("not enough disk for the local model")
        _download(f"https://huggingface.co/{MODEL[0]}/resolve/main/{MODEL[1]}", path)
    return path


def fit(prompt: str, limit: int = MAX_PROMPT_CHARS) -> str:
    """Keep the instructions at the top and the most recent log at the bottom; cut the middle."""
    if len(prompt) <= limit:
        return prompt
    head = limit * 2 // 3
    return prompt[:head] + "\n...[cut to fit the local model]...\n" + prompt[-(limit - head):]


def ask_json(prompt: str, timeout: float = 1800):
    """One JSON answer from the local model, or None."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    exe, model = server(), weights()
    env = dict(os.environ, LD_LIBRARY_PATH=str(exe.parent))
    proc = subprocess.Popen([str(exe), "-m", str(model), "--port", str(port), "--host", "127.0.0.1", "-c", str(CTX),
                             "-t", str(os.cpu_count() or 4), "--jinja", "--no-webui"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        deadline = time.monotonic() + 600
        while True:
            if proc.poll() is not None:
                raise RuntimeError("llama-server exited")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("llama-server did not become ready")
            time.sleep(2)
        body = {"messages": [{"role": "user", "content": fit(prompt)}], "temperature": 0.2, "max_tokens": 3000,
                "response_format": {"type": "json_object"}, "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = json.load(r)["choices"][0]["message"]["content"]
        return json.loads(text)
    finally:
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
