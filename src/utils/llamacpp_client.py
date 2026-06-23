"""
llamacpp_client.py

A drop-in client for generate_source.py that talks to a llama.cpp server
(the one started by serve_llamacpp.sh) over its OpenAI-compatible
/v1/chat/completions endpoint.

Why this exists instead of reusing VLLMClient's remote mode directly:
  - VLLMClient._batch_remote sends extra_body={"chat_template_kwargs":
    {"enable_thinking": False}}, which is a vLLM-only field. llama.cpp's
    server does not understand it and can error or ignore it.
  - llama.cpp uses sampling params like top_k / min_p / repeat_penalty that
    we want to pass through the OpenAI-compatible "extra_body".

It implements the same LLMClient protocol as VLLMClient:
    complete(system, user, max_tokens, temperature) -> str
    complete_batch(requests) -> list[str]

so generate_source.py can use it unchanged:
    client.complete_batch(reqs)

Environment variables:
    LLAMACPP_HOST     default 127.0.0.1
    LLAMACPP_PORT     default 8080
    LLAMACPP_API_KEY  default local-llamacpp-key   (matches serve_llamacpp.sh)
    LLAMACPP_MODEL    served model name; llama.cpp accepts any string here
    LLM_TEMPERATURE   default 0.7
    LLAMACPP_MAX_PARALLEL  how many requests to send concurrently (default 3,
                           matching --parallel in serve_llamacpp.sh; do NOT
                           exceed the server's --parallel or requests queue)

Usage in generate_source.py entry point:
    from llamacpp_client import LlamaCppClient
    client = LlamaCppClient()        # reads env, connects to the server
    run_oneshot(contracts=contracts, client=client, ...)
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import re

def _clean_output(text: str, enable_thinking: bool = False) -> str:
    for tok in ("<|im_end|>", "<|endoftext|>", "<|im_start|>",
                "<|END_OF_TURN_TOKEN|>", "<|END_RESPONSE|>",
                "<|START_RESPONSE|>", "<|START_OF_TURN_TOKEN|>",
                "<|USER_TOKEN|>", "<|CHATBOT_TOKEN|>", "<|SYSTEM_TOKEN|>"):
        text = text.replace(tok, "")
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    text = re.sub(r"<\|[A-Z0-9_]+\|>", "", text)
    return text.strip()


class LlamaCppClient:
    """
    OpenAI-compatible client for a llama.cpp server.

    complete_batch() sends requests concurrently (bounded by max_parallel),
    because llama.cpp's server handles --parallel slots server-side; firing
    more than that simply queues. We cap concurrency at the server's slot
    count to avoid head-of-line blocking and timeouts.
    """

    def __init__(
        self,
        host:         str | None = None,
        port:         int | None = None,
        api_key:      str | None = None,
        model:        str | None = None,
        temperature:  float | None = None,
        enable_thinking: bool = False,
        max_parallel: int | None = None,
        wait_timeout: int = 180,
        request_timeout: int = 600,
    ):
        self._host = host or os.getenv("LLAMACPP_HOST", "127.0.0.1")
        self._port = int(port or os.getenv("LLAMACPP_PORT", "8080"))
        self._api_key = api_key or os.getenv("LLAMACPP_API_KEY", "local-llamacpp-key")
        self._model = model or os.getenv("LLAMACPP_MODEL", "local-llamacpp")
        self._default_temp = (
            temperature if temperature is not None
            else float(os.getenv("LLM_TEMPERATURE", "0.7"))
        )
        self._thinking = enable_thinking
        self._max_parallel = int(
            max_parallel or os.getenv("LLAMACPP_MAX_PARALLEL", "3")
        )
        self._request_timeout = request_timeout
        self._base_url = f"http://{self._host}:{self._port}/v1/chat/completions"

        self._wait_until_ready(wait_timeout)

    # ── readiness probe ───────────────────────────────────────────────────
    def _wait_until_ready(self, wait_timeout: int) -> None:
        models_url = f"http://{self._host}:{self._port}/v1/models"
        print(f"[LlamaCppClient] Connecting to {self._base_url} ...",
              end="", flush=True)
        start = time.time()
        while time.time() - start < wait_timeout:
            try:
                req = urllib.request.Request(
                    models_url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        print(" ready")
                        return
            except Exception:
                print(".", end="", flush=True)
                time.sleep(3)
        raise RuntimeError(
            f"llama.cpp server at {self._base_url} did not respond within "
            f"{wait_timeout}s. Is serve_llamacpp.sh running?"
        )

    # ── single HTTP call ──────────────────────────────────────────────────
    def _post_one(self, request: dict) -> str:
        temp = request.get("temperature")
        if temp is None:
            temp = self._default_temp
        messages = []
        if request.get("system"):
            messages.append({"role": "system", "content": request["system"]})
        messages.append({"role": "user", "content": request.get("user", "")})

        payload = {
            "model": self._model,
            "messages": messages,
            "max_tokens": request.get("max_tokens", 1024),
            "temperature": temp,
            # llama.cpp-friendly sampling knobs (OpenAI-compatible server
            # accepts these as top-level fields):
            "top_p": 0.8,
            "top_k": 20,
            "min_p": 0.0,
            "repeat_penalty": 1.05,
            "stream": False,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._base_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._request_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            text = body["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            print(f"  [LlamaCppClient] HTTP {e.code}: {err[:200]}")
            text = ""
        except Exception as e:
            print(f"  [LlamaCppClient] error: {e}")
            text = ""
        return _clean_output(text, self._thinking)

    # ── LLMClient protocol ────────────────────────────────────────────────
    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> str:
        return self.complete_batch([{
            "system": system, "user": user,
            "max_tokens": max_tokens, "temperature": temperature,
        }])[0]

    def complete_batch(self, requests: list[dict]) -> list[str]:
        """
        Send the batch concurrently, bounded by the server's parallel slots.
        Order is preserved (results[i] corresponds to requests[i]).
        """
        if not requests:
            return []
        results: list[str] = [""] * len(requests)
        workers = min(self._max_parallel, len(requests))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._post_one, req): i
                for i, req in enumerate(requests)
            }
            for fut in futures:
                idx = futures[fut]
                try:
                    results[idx] = fut.result()
                except Exception as e:
                    print(f"  [LlamaCppClient] worker error: {e}")
                    results[idx] = ""
        return results