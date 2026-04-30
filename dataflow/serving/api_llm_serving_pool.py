"""
APILLMServing_pool
==================
Drop-in replacement for ``APILLMServing_request`` with a persistent thread pool
and connection-error-aware retries.

Why a new class?
----------------
``APILLMServing_request`` rebuilds a ``ThreadPoolExecutor`` on every
``generate_from_input`` call.  When an operator (e.g. CoTLLMJudgeRefiner)
invokes the serving layer once per row, the per-call pool
teardown/build + per-batch barrier create a visible concurrency drop:

* The tqdm throughput drops to a trickle at the tail of each batch
  (long-tail stragglers block the barrier).
* A tiny batch (e.g. ``num_candidates=1`` in CoTChunkCompressRefiner)
  cannot benefit from ``max_workers=200`` at all.
* ``RuntimeError: Cannot connect to LLM server`` currently skips retry
  because the ``_api_chat_id_retry`` loop only retries ``response is None``
  (it never reaches that check when the inner call raises).

This class keeps ONE executor alive for the whole lifetime of the serving
object and submits tasks directly into it.  ``generate_from_input`` still
returns an ordered list (identical API); internally it just waits on the
futures it submitted without tearing down the pool.

The behaviour and retry budget are intentionally matched to
``APILLMServing_request`` so results are bit-for-bit comparable when
``max_workers`` and ``max_retries`` are equal.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import warnings
from concurrent.futures import Future, ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm

from dataflow.core import LLMServingABC

from ..logger import get_logger


class APILLMServing_pool(LLMServingABC):
    """LLM serving backed by a persistent ``ThreadPoolExecutor``.

    The interface (``generate_from_input``, ``generate_from_conversations``,
    ``generate_embedding_from_input``, ``cleanup``) matches
    :class:`APILLMServing_request` so operators can use either class.
    """

    # --------------------------------------------------------------------- #
    # Construction / teardown                                                #
    # --------------------------------------------------------------------- #

    def __init__(
        self,
        api_url: str = "https://api.openai.com/v1/chat/completions",
        key_name_of_api_key: str = "DF_API_KEY",
        model_name: str = "gpt-4o",
        temperature: float = 0.0,
        max_workers: int = 10,
        max_retries: int = 5,
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,
        **configs,
    ):
        self.api_url = api_url
        self.model_name = model_name
        self.max_workers = max_workers
        self.max_retries = max_retries
        self.timeout = (connect_timeout, read_timeout)

        if "timeout" in configs:
            warnings.warn(
                "The `timeout` parameter is deprecated. Please use "
                "`connect_timeout` and `read_timeout` instead.",
                DeprecationWarning,
            )
            self.timeout = (connect_timeout, configs["timeout"])
            configs.pop("timeout")

        self.configs = dict(configs)
        self.configs["temperature"] = temperature

        self.logger = get_logger()

        self.api_key = os.environ.get(key_name_of_api_key)
        if self.api_key is None:
            msg = (
                f"Lack of `{key_name_of_api_key}` in environment variables. "
                f"Please export `{key_name_of_api_key}` before using "
                f"APILLMServing_pool."
            )
            self.logger.error(msg)
            raise ValueError(msg)

        # Persistent requests.Session with a connection pool sized to the
        # thread pool so sockets are reused.
        self.session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=self.max_workers,
            pool_maxsize=self.max_workers,
            max_retries=0,  # we handle retries ourselves
            pool_block=True,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Apifox/1.0.0 (https://apifox.com)",
        }

        # The key difference: one executor, kept alive for the object's
        # lifetime.  We set thread_name_prefix so stack traces are readable.
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="APILLMServingPool",
        )
        self._closed = False
        self._lock = threading.Lock()

    def start_serving(self) -> None:
        self.logger.info("APILLMServing_pool: no local service to start.")

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.logger.info("Cleaning up APILLMServing_pool.")
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
        # Best-effort cleanup.  Do not raise from __del__.
        try:
            self.cleanup()
        except Exception:
            pass

    # --------------------------------------------------------------------- #
    # Response formatting (copied verbatim from APILLMServing_request so    #
    # outputs remain byte-identical).                                       #
    # --------------------------------------------------------------------- #

    def format_response(self, response: dict, is_embedding: bool = False):
        if is_embedding:
            return response.get("data", [{}])[0].get("embedding", [])

        message = response.get("choices", [{}])[0].get("message", {})
        content = message.get("content", "")

        if re.search(
            r"<think>.*?</think>.*?<answer>.*?</answer>", content, re.DOTALL
        ):
            return content

        reasoning_content = message.get("reasoning_content")
        if reasoning_content:
            return f"<think>{reasoning_content}</think>\n<answer>{content}</answer>"
        return content

    # --------------------------------------------------------------------- #
    # Single HTTP call (with response-code handling but no retry loop).     #
    # --------------------------------------------------------------------- #

    _SENTINEL_CONNECTION_ERROR = "__CONNECTION_ERROR__"

    def _api_chat_with_id(
        self,
        id: int,
        payload,
        model: str,
        is_embedding: bool = False,
        json_schema: dict = None,
    ):
        """One HTTP round-trip.

        Returns ``(id, result, status)`` where ``status`` is one of:
          * ``"ok"``              - ``result`` is the formatted response.
          * ``"empty"``           - server returned non-200 / JSON parse failed.
          * ``"read_timeout"``    - server reachable but slow (retry cheaply).
          * ``"conn_error"``      - connection-level failure (retry with
                                     exponential backoff; the old class
                                     raised here and skipped retries).
        """
        start = time.time()
        try:
            if is_embedding:
                body = {"model": model, "input": payload}
            elif json_schema is None:
                body = {"model": model, "messages": payload}
            else:
                body = {
                    "model": model,
                    "messages": payload,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "custom_response",
                            "strict": True,
                            "schema": json_schema,
                        },
                    },
                }
            body.update(self.configs)
            data = json.dumps(body)

            response = self.session.post(
                self.api_url,
                headers=self.headers,
                data=data,
                timeout=self.timeout,
            )
            cost = time.time() - start

            if response.status_code == 200:
                return id, self.format_response(response.json(), is_embedding), "ok"

            # Non-200 from server.  Treat 429 / 5xx as retryable empties.
            self.logger.error(
                f"API request failed id={id} status={response.status_code} "
                f"cost={cost:.2f}s body={response.text[:500]}"
            )
            return id, None, "empty"

        except requests.exceptions.ConnectTimeout as e:
            cost = time.time() - start
            self.logger.warning(
                f"API connect timeout (id={id}) cost={cost:.2f}s: {e}"
            )
            return id, None, "conn_error"

        except requests.exceptions.ReadTimeout as e:
            cost = time.time() - start
            self.logger.warning(
                f"API read timeout (id={id}) cost={cost:.2f}s: {e}"
            )
            return id, None, "read_timeout"

        except requests.exceptions.Timeout as e:
            cost = time.time() - start
            self.logger.warning(
                f"API timeout (id={id}) cost={cost:.2f}s: {e}"
            )
            return id, None, "read_timeout"

        except requests.exceptions.ConnectionError as e:
            cost = time.time() - start
            msg = str(e).lower()
            if "read timed out" in msg:
                self.logger.warning(
                    f"API read timeout (id={id}) cost={cost:.2f}s: {e}"
                )
                return id, None, "read_timeout"
            self.logger.warning(
                f"API connection error (id={id}) cost={cost:.2f}s: {e}"
            )
            return id, None, "conn_error"

        except Exception as e:
            cost = time.time() - start
            self.logger.exception(
                f"API request error (id={id}) cost={cost:.2f}s: {e}"
            )
            return id, None, "empty"

    # --------------------------------------------------------------------- #
    # Retry loop (retries ALL transient failures, including connection      #
    # errors that the old class raised-and-skipped).                        #
    # --------------------------------------------------------------------- #

    def _api_chat_id_retry(
        self,
        id: int,
        payload,
        model: str,
        is_embedding: bool = False,
        json_schema: dict = None,
    ):
        last_status = "empty"
        for attempt in range(self.max_retries):
            id, response, status = self._api_chat_with_id(
                id, payload, model, is_embedding, json_schema
            )
            if response is not None:
                return id, response
            last_status = status
            # Exponential backoff capped at 30 s.  Read-timeouts are usually
            # the server being slow; connection errors may be transient
            # network hiccups.  Both benefit from a small sleep.
            time.sleep(min(30.0, 2 ** attempt))
        self.logger.error(
            f"API request giving up id={id} after {self.max_retries} "
            f"attempts (last_status={last_status})"
        )
        return id, None

    # --------------------------------------------------------------------- #
    # Public batch entry points.                                             #
    # --------------------------------------------------------------------- #

    def _submit_batch(
        self,
        task_args_list: list[dict],
    ) -> list[Future]:
        """Submit tasks into the persistent pool and return the futures.

        The caller is responsible for draining them (usually via
        ``_drain_to_list`` below).  Splitting submit and drain lets callers
        interleave other work or combine multiple batches into one barrier.
        """
        if self._closed:
            raise RuntimeError("APILLMServing_pool is closed.")
        return [
            self._executor.submit(self._api_chat_id_retry, **args)
            for args in task_args_list
        ]

    def _drain_to_list(
        self,
        futures: list[Future],
        total_len: int,
        desc: str,
        show_progress: bool = True,
    ) -> list:
        """Collect ``futures`` into a list indexed by the task's ``id``."""
        responses = [None] * total_len
        iterator = futures if not show_progress else tqdm(
            _as_completed_stream(futures), total=len(futures), desc=desc
        )
        for fut in iterator:
            try:
                rid, response = fut.result()
                responses[rid] = response
            except Exception:
                self.logger.exception(
                    "Worker crashed unexpectedly in APILLMServing_pool"
                )
        return responses

    def generate_from_input(
        self,
        user_inputs: list[str],
        system_prompt: str = "You are a helpful assistant",
        json_schema: dict = None,
        show_progress: bool = True,
    ) -> list[str]:
        task_args_list = [
            dict(
                id=idx,
                payload=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": question},
                ],
                model=self.model_name,
                json_schema=json_schema,
            )
            for idx, question in enumerate(user_inputs)
        ]
        futures = self._submit_batch(task_args_list)
        return self._drain_to_list(
            futures,
            total_len=len(task_args_list),
            desc="Generating responses from prompts......",
            show_progress=show_progress,
        )

    def generate_from_conversations(
        self, conversations: list[list[dict]]
    ) -> list[str]:
        task_args_list = [
            dict(id=idx, payload=dialog, model=self.model_name)
            for idx, dialog in enumerate(conversations)
        ]
        futures = self._submit_batch(task_args_list)
        return self._drain_to_list(
            futures,
            total_len=len(task_args_list),
            desc="Generating responses from conversations......",
        )

    def generate_embedding_from_input(
        self, texts: list[str]
    ) -> list[list[float]]:
        task_args_list = [
            dict(id=idx, payload=txt, model=self.model_name, is_embedding=True)
            for idx, txt in enumerate(texts)
        ]
        futures = self._submit_batch(task_args_list)
        return self._drain_to_list(
            futures,
            total_len=len(task_args_list),
            desc="Generating embeddings......",
        )


# ------------------------------------------------------------------------- #
# Helper: an iterator that mirrors ``concurrent.futures.as_completed`` but   #
# yields futures in the order they finish without holding them all in the    #
# queue (important when the number of prompts is very large).                #
# ------------------------------------------------------------------------- #

def _as_completed_stream(futures: list[Future]):
    from concurrent.futures import as_completed
    yield from as_completed(futures)
