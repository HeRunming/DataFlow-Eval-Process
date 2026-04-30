"""
TaijiCustomLLMServing_pool
==========================
Persistent-pool variant of ``TaijiCustomLLMServing`` for the Taiji custom
HMAC-SHA1 protocol.  Designed to be a drop-in replacement so the Fast CoT
refiners (which issue ONE huge ``generate_from_input`` per phase) keep the
underlying thread pool saturated without per-call teardown.

Why a new class?
----------------
``TaijiCustomLLMServing`` (the stock implementation at
``/data/workspace/DataFlow/dataflow/serving/taiji_custom_llm_serving.py``)
spins up a fresh ``ThreadPoolExecutor`` every time ``generate_from_input``
is called.  This creates the same long-tail/cold-start pattern observed
in the earlier ``APILLMServing_request`` profiling: every call drains
the previous batch before the next one can start, and tiny batches
cannot saturate a pool of 100 workers.

This class reuses ONE pool for the lifetime of the serving object.  The
interface is identical to ``TaijiCustomLLMServing`` so the Fast refiners
work unchanged.

Auth + body format are copied verbatim from the reference implementation.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import os
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm

from dataflow import get_logger
from dataflow.core import LLMServingABC


_DEFAULT_BASE_URL   = "http://trpc-gpt-eval.production.polaris:8080"
_DEFAULT_ENDPOINT   = "/api/v1/data_eval"
_DEFAULT_MODEL      = "api_naci_default_gemini-3.1-flash-lite-preview"
_DEFAULT_SOURCE     = "dataflow-cot-clean"
_DEFAULT_APIVERSION = "v2.03"


class TaijiCustomLLMServing_pool(LLMServingABC):
    """Persistent-pool Taiji adapter.

    The public API (``generate_from_input``, ``generate_from_conversations``,
    ``generate_with_thinking``, ``cleanup``) mirrors ``TaijiCustomLLMServing``.
    """

    def __init__(
        self,
        base_url:         str = _DEFAULT_BASE_URL,
        endpoint:         str = _DEFAULT_ENDPOINT,
        model_marker:     str = _DEFAULT_MODEL,
        source:           str = _DEFAULT_SOURCE,
        api_version:      str = _DEFAULT_APIVERSION,
        app_id:           Optional[str] = None,
        app_key:          Optional[str] = None,
        max_workers:      int = 100,
        connect_timeout:  float = 30.0,
        read_timeout:     float = 1800.0,
        max_retries:      int = 5,
        max_tokens:       int = 8000,
        enable_thinking:  bool = True,
        thinking_level:   str = "high",
        retain_thinking:  bool = False,
        **_: Any,
    ):
        self.logger = get_logger()

        # Endpoint / model
        self.base_url     = base_url.rstrip("/")
        self.endpoint     = endpoint if endpoint.startswith("/") else "/" + endpoint
        self.url          = self.base_url + self.endpoint
        self.model_marker = model_marker
        self.source       = source
        self.api_version  = api_version

        # Auth
        self.app_id  = app_id  or os.environ.get("APP_ID")
        self.app_key = app_key or os.environ.get("APP_KEY")
        if not self.app_id or not self.app_key:
            raise ValueError(
                "TaijiCustomLLMServing_pool needs APP_ID and APP_KEY "
                "(either via constructor args or environment variables)."
            )

        # Concurrency / timeout
        self.max_workers = max_workers
        self.timeout     = (connect_timeout, read_timeout)
        self.max_retries = max_retries

        # Generation params
        self.max_tokens      = max_tokens
        self.enable_thinking = enable_thinking
        self.thinking_level  = thinking_level
        self.retain_thinking = retain_thinking

        # Shared HTTP session with a connection pool large enough for
        # ``max_workers``.  ``pool_block=True`` means a worker waits for a
        # free socket instead of opening an unbounded number.
        self.session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=self.max_workers,
            pool_maxsize=self.max_workers,
            max_retries=0,
            pool_block=True,
        )
        self.session.mount("http://",  adapter)
        self.session.mount("https://", adapter)

        # Long-lived thread pool — the key difference from the reference
        # implementation.
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="TaijiPool",
        )
        self._closed = False
        self._lock = threading.Lock()

        self.logger.info(
            f"[TaijiCustomLLMServing_pool] url={self.url} "
            f"model={self.model_marker} workers={self.max_workers} "
            f"timeout={self.timeout} thinking={self.enable_thinking}"
            f"({self.thinking_level})"
        )

    # ------------------------------------------------------------------ #
    # LLMServingABC                                                      #
    # ------------------------------------------------------------------ #

    def start_serving(self) -> None:
        self.logger.info(
            "[TaijiCustomLLMServing_pool] remote service, nothing to start."
        )

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.logger.info("[TaijiCustomLLMServing_pool] cleanup")
            try:
                self._executor.shutdown(wait=True, cancel_futures=False)
            except Exception:
                self.logger.exception("Executor shutdown error (ignored).")
            try:
                if self.session is not None:
                    self.session.close()
            except Exception:
                self.logger.exception("Session close error (ignored).")

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Public batch entry points                                           #
    # ------------------------------------------------------------------ #

    def generate_from_input(
        self,
        user_inputs: List[str],
        system_prompt: str = "You are a helpful assistant",
        json_schema: Optional[dict] = None,
    ) -> List[str]:
        """Return response text per prompt in original order.

        ``json_schema`` is accepted for interface parity but ignored (the
        Taiji protocol does not support structured output).
        """
        if json_schema is not None:
            self.logger.warning(
                "[TaijiCustomLLMServing_pool] json_schema is not supported "
                "by the Taiji protocol; ignored."
            )
        if not user_inputs:
            return []
        return self._run_pool(
            prompts=user_inputs,
            system_prompt=system_prompt,
            desc="Generating responses from prompts......",
        )

    def generate_from_conversations(
        self, conversations: List[List[dict]]
    ) -> List[str]:
        if not conversations:
            return []
        prompts: List[str] = []
        systems: List[str] = []
        for conv in conversations:
            sys_msgs = [m["content"] for m in conv if m.get("role") == "system"]
            non_sys = [m for m in conv if m.get("role") != "system"]
            parts = []
            for m in non_sys:
                role = m.get("role", "user")
                parts.append(f"[{role}] {m.get('content', '')}")
            prompts.append("\n".join(parts))
            systems.append("\n".join(sys_msgs) if sys_msgs else "")

        if self._closed:
            raise RuntimeError("TaijiCustomLLMServing_pool is closed.")

        futures = [
            self._executor.submit(self._call_one, prompts[i], systems[i])
            for i in range(len(prompts))
        ]
        return self._drain(
            futures,
            len(prompts),
            "Generating responses from conversations......",
        )

    def generate_with_thinking(
        self,
        user_inputs: List[str],
        system_prompt: str = "You are a helpful assistant",
    ) -> List[Tuple[str, str]]:
        if not user_inputs:
            return []
        if self._closed:
            raise RuntimeError("TaijiCustomLLMServing_pool is closed.")

        futures = [
            self._executor.submit(
                self._call_one_with_thinking, prompt, system_prompt
            )
            for prompt in user_inputs
        ]
        return self._drain_thinking(
            futures, len(user_inputs), "Generating with thinking......"
        )

    # ------------------------------------------------------------------ #
    # Request / response internals (same wire format as the reference)   #
    # ------------------------------------------------------------------ #

    def _build_headers(self) -> dict:
        """Fresh HMAC-SHA1 signature per call (Date matters)."""
        date_time = datetime.datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
        sign_str = f"date: {date_time}\nsource: {self.source}"
        sign = hmac.new(
            self.app_key.encode(),
            sign_str.encode(),
            hashlib.sha1,
        ).digest()
        sign_b64 = base64.b64encode(sign).decode()
        auth = (
            f'hmac id="{self.app_id}", algorithm="hmac-sha1", '
            f'headers="date source", signature="{sign_b64}"'
        )
        return {
            "Apiversion":    self.api_version,
            "Authorization": auth,
            "Date":          date_time,
            "Source":        self.source,
            "Content-Type":  "application/json",
        }

    def _build_body(self, user_prompt: str, system_prompt: str = "") -> dict:
        body: dict = {
            "request_id":   str(uuid.uuid4()),
            "model_marker": self.model_marker,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "value": user_prompt}],
                }
            ],
            "params": {
                "max_tokens": self.max_tokens,
                "stream": False,
                "generationConfig": {
                    "thinkingConfig": {
                        "includeThoughts": self.enable_thinking,
                        "thinkingLevel":   self.thinking_level,
                    }
                },
            },
            "timeout": int(self.timeout[1]),
        }
        if system_prompt:
            body["system"] = system_prompt
        return body

    @staticmethod
    def _extract(raw: dict) -> Tuple[str, str]:
        thinking = ""
        response = ""
        answer_list = raw.get("answer") or []
        if answer_list:
            for item in answer_list:
                t = item.get("type")
                v = (item.get("value") or "").strip()
                if t == "reasoning":
                    thinking = v
                elif t == "text":
                    response = v
            return thinking, response
        if "choices" in raw and raw["choices"]:
            msg = raw["choices"][0].get("message", {}) or {}
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or "").strip()
            if reasoning:
                return reasoning, content
            m = re.search(r"<think>(.*?)</think>(.*)", content, re.DOTALL)
            if m:
                return m.group(1).strip(), m.group(2).strip()
            return "", content
        return "", ""

    def _call_one_with_thinking(
        self, prompt: str, system_prompt: str
    ) -> Tuple[str, str]:
        body = self._build_body(prompt, system_prompt)
        for attempt in range(1, self.max_retries + 1):
            try:
                headers = self._build_headers()  # re-sign per attempt
                resp = self.session.post(
                    self.url,
                    headers=headers,
                    json=body,
                    timeout=self.timeout,
                )
                if resp.status_code >= 500:
                    raise requests.HTTPError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                resp.raise_for_status()
                return self._extract(resp.json())
            except Exception as e:
                if attempt >= self.max_retries:
                    self.logger.error(
                        f"[TaijiCustomLLMServing_pool] give up after "
                        f"{attempt} attempts: {type(e).__name__}: {e}"
                    )
                    return ("", "")
                back = min(2 ** attempt, 30)
                self.logger.warning(
                    f"[TaijiCustomLLMServing_pool] attempt {attempt} "
                    f"failed, retry in {back}s: {type(e).__name__}: {e}"
                )
                time.sleep(back)
        return ("", "")

    def _call_one(self, prompt: str, system_prompt: str) -> str:
        thinking, response = self._call_one_with_thinking(prompt, system_prompt)
        if self.retain_thinking and thinking:
            return f"<think>{thinking}</think>\n{response}"
        return response

    # ------------------------------------------------------------------ #
    # Pool plumbing                                                       #
    # ------------------------------------------------------------------ #

    def _run_pool(
        self,
        prompts: List[str],
        system_prompt: str,
        desc: str,
    ) -> List[str]:
        if self._closed:
            raise RuntimeError("TaijiCustomLLMServing_pool is closed.")
        futures = [
            self._executor.submit(self._call_one, p, system_prompt)
            for p in prompts
        ]
        return self._drain(futures, len(prompts), desc)

    def _drain(
        self,
        futures: List[Future],
        total: int,
        desc: str,
    ) -> List[str]:
        """Collect futures preserving submission order.

        We rely on the fact that ``_executor.submit`` returns futures in
        submission order and ``as_completed`` yields them in completion
        order; we therefore track ``(future -> index)`` explicitly.
        """
        fut_to_idx = {fut: i for i, fut in enumerate(futures)}
        results: List[Optional[str]] = [None] * total
        with tqdm(total=total, desc=desc, unit="req") as pbar:
            for fut in as_completed(futures):
                i = fut_to_idx[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:
                    self.logger.error(
                        f"[TaijiCustomLLMServing_pool] task {i} crashed: "
                        f"{type(e).__name__}: {e}"
                    )
                    results[i] = ""
                pbar.update(1)
        return [r if r is not None else "" for r in results]

    def _drain_thinking(
        self,
        futures: List[Future],
        total: int,
        desc: str,
    ) -> List[Tuple[str, str]]:
        fut_to_idx = {fut: i for i, fut in enumerate(futures)}
        results: List[Tuple[str, str]] = [("", "")] * total
        with tqdm(total=total, desc=desc, unit="req") as pbar:
            for fut in as_completed(futures):
                i = fut_to_idx[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:
                    self.logger.error(
                        f"[TaijiCustomLLMServing_pool] task {i} crashed: "
                        f"{type(e).__name__}: {e}"
                    )
                    results[i] = ("", "")
                pbar.update(1)
        return results
