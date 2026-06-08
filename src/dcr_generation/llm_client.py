"""
llm_client.py

Astrazione per l'accesso agli LLM, con due implementazioni:

  VLLMClient  — se viene passato model_name, carica il modello in-process
                con vLLM (niente server HTTP).
                Se model_name è None, si connette a un server vLLM già
                in ascolto su LLM_HOST:LLM_PORT via API OpenAI-compat.
                In entrambi i casi usa generate() in batch per throughput massimo.

  APIClient   — endpoint OpenAI-compatibile remoto (OpenAI, Anthropic, ecc.).
                complete_batch() è sequenziale.

Entrambe implementano il protocollo LLMClient:
  complete(system, user, max_tokens, temperature) -> str
  complete_batch(requests)                        -> list[str]

Variabili d'ambiente:
    LLM_BACKEND     "vllm" | "api"       (default: "api")
    LLM_MODEL       nome modello         (default: "Qwen/Qwen3-14B-AWQ")
    LLM_HOST        host server vLLM     (default: "localhost")
    LLM_PORT        porta server vLLM    (default: "8000")
    LLM_API_URL     base URL APIClient   (default: http://LLM_HOST:LLM_PORT/v1)
    LLM_API_KEY     chiave API           (default: "EMPTY")
    LLM_TEMPERATURE temperatura default  (default: 0.7)
"""

from __future__ import annotations

import os
import time
from typing import Protocol, TypedDict, runtime_checkable


# ── Protocollo ────────────────────────────────────────────────────────────────

class LLMRequest(TypedDict, total=False):
    system:      str
    user:        str
    max_tokens:  int
    temperature: float


@runtime_checkable
class LLMClient(Protocol):
    def complete(
        self,
        system:      str,
        user:        str,
        max_tokens:  int = 1024,
        temperature: float | None = None,
    ) -> str: ...

    def complete_batch(self, requests: list[LLMRequest]) -> list[str]: ...


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_output(text: str, enable_thinking: bool = False) -> str:
    for tok in ("<|im_end|>", "<|endoftext|>", "<|im_start|>"):
        text = text.replace(tok, "")
    if enable_thinking and "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()


# ── VLLMClient ────────────────────────────────────────────────────────────────

class VLLMClient:
    """
    Due modalità di funzionamento, scelte automaticamente dal costruttore:

    1. IN-PROCESS (model_name fornito):
       Carica il modello direttamente in memoria GPU con vLLM.
       Non serve nessun server esterno.
       Usa LLM.generate() nativo per batch massivo.

       client = VLLMClient(model_name="Qwen/Qwen3-14B-AWQ")

    2. REMOTE SERVER (model_name=None):
       Si connette a un server vLLM già in ascolto su LLM_HOST:LLM_PORT
       tramite endpoint OpenAI-compatibile.
       Utile se il modello è già caricato in un processo separato.

       client = VLLMClient()   # legge LLM_HOST e LLM_PORT dall'ambiente
    """

    def __init__(
        self,
        model_name:      str | None  = None,
        temperature:     float | None = None,
        enable_thinking: bool = False,
        # Parametri per in-process
        tensor_parallel_size:    int   = 1,
        gpu_memory_utilization:  float = 0.90,
        max_model_len:           int   = 8192,
        # max_model_len = context window totale (input + output).
        # Deve essere >= stub_input_tokens + MAX_TOKENS_COMPLETE.
        # Default 8192: stub ~2000-3000 tok input + 4096 tok output + margine.
        # Parametri per remote server
        wait_timeout:            int   = 120,
    ):
        self._thinking     = enable_thinking
        self._default_temp = temperature or float(os.getenv("LLM_TEMPERATURE", "0.7"))
        self._mode         = "inprocess" if model_name else "remote"

        if self._mode == "inprocess":
            self._load_inprocess(
                model_name,
                tensor_parallel_size,
                gpu_memory_utilization,
                max_model_len,
            )
        else:
            self._load_remote(wait_timeout)

    # ── In-process ────────────────────────────────────────────────────────────

    def _load_inprocess(
        self,
        model_name: str,
        tensor_parallel_size:   int,
        gpu_memory_utilization: float,
        max_model_len:          int,
    ) -> None:
        try:
            from vllm import LLM, SamplingParams          # type: ignore[import]
            from transformers import AutoTokenizer        # type: ignore[import]
        except ImportError as e:
            raise ImportError(
                "In-process VLLMClient requires 'vllm' and 'transformers'. "
                "Install them or use VLLMClient() without model_name to connect "
                "to a remote server."
            ) from e

        print(f"[VLLMClient] Loading {model_name} in-process ...")
        # Sopprime le progress bar per-request di vLLM nell'output
        os.environ.setdefault("VLLM_DISABLE_TQDM", "1")
        self._llm = LLM(
            model=model_name,
            tensor_parallel_size=tensor_parallel_size,
            gpu_memory_utilization=gpu_memory_utilization,
            max_model_len=max_model_len,
            dtype="float16",
            trust_remote_code=True,
            enforce_eager=True,
            disable_custom_all_reduce=True,
            max_num_seqs=4,
            quantization="AWQ" if "AWQ" in model_name else None,
            disable_log_stats=True,
        )
        self._tokenizer = AutoTokenizer.from_pretrained(model_name)
        self._sampling_params = SamplingParams(
            temperature=self._default_temp,
            top_p=0.8,
            top_k=20,
            repetition_penalty=1.05,
            max_tokens=1024,
            stop=["<|im_end|>", "<|endoftext|>"],
            include_stop_str_in_output=False,
        )
        self._model_name = model_name
        print(f"[VLLMClient] Model loaded.")

    def _format_prompt(self, system: str, user: str) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        try:
            return self._tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=self._thinking,
            )
        except Exception:
            prefix = f"{system}\n\n" if system else ""
            return f"{prefix}{user}"

    # ── Remote server ─────────────────────────────────────────────────────────

    def _load_remote(self, wait_timeout: int) -> None:
        try:
            from openai import OpenAI                     # type: ignore[import]
        except ImportError as e:
            raise ImportError(
                "Remote VLLMClient requires 'openai'. "
                "Install it or pass model_name= for in-process mode."
            ) from e

        host     = os.getenv("LLM_HOST", "localhost")
        port     = os.getenv("LLM_PORT", "8000")
        base_url = f"http://{host}:{port}/v1"

        print(f"[VLLMClient] Connecting to {base_url} ...", end="", flush=True)
        start = time.time()
        client = OpenAI(base_url=base_url, api_key="EMPTY")
        while time.time() - start < wait_timeout:
            try:
                models = client.models.list().data
                self._remote_model = models[0].id if models else ""
                print(f" ready  (model: {self._remote_model})")
                break
            except Exception:
                print(".", end="", flush=True)
                time.sleep(3)
        else:
            raise RuntimeError(
                f"vLLM server at {base_url} did not respond within {wait_timeout}s"
            )

        self._remote_client = client

    # ── LLMClient interface ───────────────────────────────────────────────────

    def complete(
        self,
        system:      str,
        user:        str,
        max_tokens:  int = 1024,
        temperature: float | None = None,
    ) -> str:
        return self.complete_batch([{
            "system": system, "user": user,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }])[0]

    def complete_batch(self, requests: list[LLMRequest]) -> list[str]:
        if self._mode == "inprocess":
            return self._batch_inprocess(requests)
        return self._batch_remote(requests)

    def _batch_inprocess(self, requests: list[LLMRequest]) -> list[str]:
        from vllm import SamplingParams                   # type: ignore[import]

        prompts = [
            self._format_prompt(r.get("system", ""), r.get("user", ""))
            for r in requests
        ]
        # Usa il max_tokens massimo tra tutte le richieste del batch.
        # Tutti i request in un batch di vLLM condividono i SamplingParams,
        # quindi usiamo il valore più alto per non troncare nessuna risposta.
        max_tokens = max((r.get("max_tokens", 1024) for r in requests), default=1024)
        temp       = requests[0].get("temperature") or self._default_temp

        params = SamplingParams(
            temperature=temp,
            top_p=self._sampling_params.top_p,
            top_k=self._sampling_params.top_k,
            repetition_penalty=self._sampling_params.repetition_penalty,
            max_tokens=max_tokens,
            stop=self._sampling_params.stop,
            include_stop_str_in_output=False,
        )
        outputs = self._llm.generate(prompts, params)
        results = []
        for out in outputs:
            text = out.outputs[0].text if out.outputs else ""
            results.append(_clean_output(text, self._thinking))
        return results

    def _batch_remote(self, requests: list[LLMRequest]) -> list[str]:
        """Sequenziale via API remota — il server gestisce il batching internamente."""
        results = []
        for r in requests:
            temp = r.get("temperature") or self._default_temp
            messages = []
            if r.get("system"):
                messages.append({"role": "system", "content": r["system"]})
            messages.append({"role": "user", "content": r.get("user", "")})
            try:
                resp = self._remote_client.chat.completions.create(
                    model=self._remote_model,
                    messages=messages,
                    max_tokens=r.get("max_tokens", 1024),
                    temperature=temp,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                text = (resp.choices[0].message.content or "").strip()
            except Exception as e:
                print(f"  [VLLMClient remote] error: {e}")
                text = ""
            results.append(_clean_output(text, self._thinking))
        return results


# ── APIClient ─────────────────────────────────────────────────────────────────

class APIClient:
    """
    Endpoint OpenAI-compatibile remoto (OpenAI, Anthropic via proxy, ecc.).
    complete_batch() è sequenziale — non c'è batch nativo nelle API standard.
    """

    def __init__(
        self,
        model:       str | None  = None,
        base_url:    str | None  = None,
        api_key:     str | None  = None,
        temperature: float | None = None,
    ):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("APIClient requires the 'openai' package.") from e

        host = os.getenv("LLM_HOST", "localhost")
        port = os.getenv("LLM_PORT", "8000")

        self._model        = model    or os.getenv("LLM_MODEL", "Qwen/Qwen3-14B-AWQ")
        self._base_url     = base_url or os.getenv("LLM_API_URL", f"http://{host}:{port}/v1")
        self._api_key      = api_key  or os.getenv("LLM_API_KEY", "EMPTY")
        self._default_temp = temperature or float(os.getenv("LLM_TEMPERATURE", "0.7"))
        self._client       = OpenAI(base_url=self._base_url, api_key=self._api_key)

    def complete(
        self,
        system:      str,
        user:        str,
        max_tokens:  int = 1024,
        temperature: float | None = None,
    ) -> str:
        return self.complete_batch([{
            "system": system, "user": user,
            "max_tokens": max_tokens, "temperature": temperature,
        }])[0]

    def complete_batch(self, requests: list[LLMRequest]) -> list[str]:
        results = []
        for r in requests:
            temp = r.get("temperature") or self._default_temp
            messages = []
            if r.get("system"):
                messages.append({"role": "system", "content": r["system"]})
            messages.append({"role": "user", "content": r.get("user", "")})
            try:
                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    max_tokens=r.get("max_tokens", 1024),
                    temperature=temp,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                text = (resp.choices[0].message.content or "").strip()
            except Exception as e:
                print(f"  [APIClient] error: {e}")
                text = ""
            results.append(text)
        return results


# ── Factory ───────────────────────────────────────────────────────────────────

def make_client(backend: str | None = None, **kwargs) -> LLMClient:
    """
    Crea il client corretto.

      make_client("vllm", model_name="Qwen/Qwen3-14B-AWQ")  → in-process
      make_client("vllm")                                    → remote server
      make_client("api",  model="gpt-4o-mini", ...)          → API remota
      make_client()                                          → legge LLM_BACKEND
    """
    backend = backend or os.getenv("LLM_BACKEND", "api")
    if backend == "vllm":
        return VLLMClient(**kwargs)
    return APIClient(**kwargs)