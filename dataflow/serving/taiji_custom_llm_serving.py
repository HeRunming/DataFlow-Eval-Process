"""
TaijiCustomLLMServing
=====================
适配 Taiji 自研「定制协议」的 LLM Serving 实现，与 DataFlow 的 LLMServingABC 对齐，
可无缝替换 APILLMServing_request。

定制协议特征（详见 /data/workspace/test.py）
--------------------------------------------
- 鉴权：HMAC-SHA1 签名（每次请求根据 UTC 时间 + source 生成）
- 端点：``{BASE_URL}/api/v1/data_eval``
- Header：``Apiversion`` + ``Authorization`` + ``Date`` + ``Source`` + ``Content-Type``
- 请求体：
    {
      "request_id":   "<uuid>",
      "model_marker": "<model>",
      "messages": [
        {"role": "user", "content": [{"type": "text", "value": "<prompt>"}]}
      ],
      "params": {
        "max_tokens": <int>,
        "stream":     false,
        "generationConfig": {
          "thinkingConfig": {"includeThoughts": <bool>, "thinkingLevel": <str>}
        }
      },
      "timeout": <int>,
      "system":  "<optional system prompt>"
    }
- 返回体（优先）：
    {"answer": [
        {"type": "reasoning", "value": "..."},
        {"type": "text",      "value": "..."}
    ]}
  兜底：OpenAI 兼容 ``choices[0].message.content``（+ ``reasoning_content``）

与 APILLMServing_request 的差异
-------------------------------
- 不用 ``DF_API_KEY``；改用 ``APP_ID`` + ``APP_KEY`` 两个环境变量
- 线程池并发 + 失败重试完全对齐旧实现
- ``generate_from_input`` 只返回 ``response`` 文本（去掉 ``thinking`` 部分），
  与原先保持一致
- 额外暴露 ``generate_with_thinking`` 方法，同时返回 thinking + response

© 2026 DataFlow Team
"""

import base64
import datetime
import hashlib
import hmac
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm

from dataflow import get_logger
from dataflow.core import LLMServingABC


# ─── 默认配置 ────────────────────────────────────────────────────────────
_DEFAULT_BASE_URL   = "http://trpc-gpt-eval.production.polaris:8080"
_DEFAULT_ENDPOINT   = "/api/v1/data_eval"
_DEFAULT_MODEL      = "api_naci_default_gemini-3.1-flash-lite-preview"
_DEFAULT_SOURCE     = "dataflow-stem-pipeline"
_DEFAULT_APIVERSION = "v2.03"


class TaijiCustomLLMServing(LLMServingABC):
    """
    Taiji 定制协议 LLM Serving 适配器。

    Args:
        base_url:           API 根地址（默认 trpc-gpt-eval.production.polaris:8080）
        endpoint:           API 路径（默认 /api/v1/data_eval）
        model_marker:       定制协议的 model_marker 字段
        source:             调用方 source 标识（鉴权会参与签名）
        api_version:        Apiversion 请求头值
        app_id:             APP_ID（鉴权 id），None 时从环境变量 APP_ID 读取
        app_key:            APP_KEY（签名密钥），None 时从环境变量 APP_KEY 读取
        max_workers:        并发线程数（默认 500）
        connect_timeout:    建连超时（秒）
        read_timeout:       读超时（秒，即整个请求的上限；默认 1800）
        max_retries:        单条请求失败后的最大重试次数
        max_tokens:         max_tokens 参数
        enable_thinking:    是否启用 thinking 模式
        thinking_level:     thinking 级别：none / low / medium / high
        retain_thinking:    True 时把 thinking 也拼到返回文本前（包一层 <think>）
                            False 时只返回 response（默认 False，兼容旧接口）
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
        max_workers:      int = 500,
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

        # ─── Endpoint / model ──────────────────────────────────────────
        self.base_url    = base_url.rstrip("/")
        self.endpoint    = endpoint if endpoint.startswith("/") else "/" + endpoint
        self.url         = self.base_url + self.endpoint
        self.model_marker = model_marker
        self.source      = source
        self.api_version = api_version

        # ─── Auth ───────────────────────────────────────────────────────
        self.app_id  = app_id  or os.environ.get("APP_ID")
        self.app_key = app_key or os.environ.get("APP_KEY")
        if not self.app_id or not self.app_key:
            raise ValueError(
                "TaijiCustomLLMServing 需要 APP_ID 和 APP_KEY。"
                "请传入参数或设置环境变量。"
            )

        # ─── Concurrency / timeout ─────────────────────────────────────
        self.max_workers     = max_workers
        self.timeout         = (connect_timeout, read_timeout)
        self.max_retries     = max_retries

        # ─── Generation params ─────────────────────────────────────────
        self.max_tokens      = max_tokens
        self.enable_thinking = enable_thinking
        self.thinking_level  = thinking_level
        self.retain_thinking = retain_thinking

        # ─── Requests session with connection pool ──────────────────────
        self.session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=self.max_workers,
            pool_maxsize=self.max_workers,
            max_retries=0,
            pool_block=True,
        )
        self.session.mount("http://",  adapter)
        self.session.mount("https://", adapter)

        self.logger.info(
            f"[TaijiCustomLLMServing] url={self.url} model={self.model_marker} "
            f"workers={self.max_workers} timeout={self.timeout} "
            f"thinking={self.enable_thinking}({self.thinking_level})"
        )

    # ==================================================================
    # LLMServingABC 必需接口
    # ==================================================================
    def start_serving(self) -> None:
        self.logger.info("[TaijiCustomLLMServing] remote service, nothing to start.")

    def cleanup(self) -> None:
        self.logger.info("[TaijiCustomLLMServing] cleanup: close session.")
        try:
            if self.session:
                self.session.close()
        except Exception:
            pass

    def generate_from_input(
        self,
        user_inputs: List[str],
        system_prompt: str = "You are a helpful assistant",
        json_schema: Optional[dict] = None,
    ) -> List[str]:
        """
        批量调用，保持原始顺序返回 response 文本。
        与 APILLMServing_request 接口严格对齐。
        """
        if json_schema is not None:
            self.logger.warning(
                "[TaijiCustomLLMServing] 定制协议不支持 json_schema，参数被忽略"
            )
        if not user_inputs:
            return []

        return self._run_threadpool(
            prompts=user_inputs,
            system_prompt=system_prompt,
            desc="Generating responses from prompts......",
        )

    def generate_from_conversations(
        self, conversations: List[List[dict]]
    ) -> List[str]:
        """
        多轮对话模式。每条 conversation 是 [{"role":..., "content":...}, ...]。
        会把非 user/assistant 的 role（如 system）单独提出来放进 body.system。
        """
        if not conversations:
            return []

        # 将每条多轮 conversation 转为 (system_prompt, plain_prompt) 二元组
        prompts: List[str] = []
        systems: List[str] = []
        for conv in conversations:
            sys_msgs = [m["content"] for m in conv if m.get("role") == "system"]
            non_sys = [m for m in conv if m.get("role") != "system"]
            # 把非 system 消息串成一段 user 输入（定制协议当前示例只演示了单 user）
            parts = []
            for m in non_sys:
                role = m.get("role", "user")
                parts.append(f"[{role}] {m.get('content', '')}")
            prompts.append("\n".join(parts))
            systems.append("\n".join(sys_msgs) if sys_msgs else "")

        results: List[Optional[str]] = [None] * len(prompts)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._call_one, prompts[i], systems[i]): i
                for i in range(len(prompts))
            }
            with tqdm(total=len(prompts),
                      desc="Generating responses from conversations......",
                      unit="req") as pbar:
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        results[i] = fut.result()
                    except Exception as e:
                        self.logger.error(f"[TaijiCustomLLMServing] 任务失败: {e}")
                        results[i] = ""
                    pbar.update(1)

        return [r if r is not None else "" for r in results]

    def generate_with_thinking(
        self,
        user_inputs: List[str],
        system_prompt: str = "You are a helpful assistant",
    ) -> List[Tuple[str, str]]:
        """
        批量调用并同时返回 (thinking, response) 二元组列表。
        """
        results: List[Tuple[str, str]] = [("", "")] * len(user_inputs)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(
                    self._call_one_with_thinking, prompt, system_prompt
                ): i
                for i, prompt in enumerate(user_inputs)
            }
            with tqdm(total=len(user_inputs),
                      desc="Generating with thinking......",
                      unit="req") as pbar:
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        results[i] = fut.result()
                    except Exception as e:
                        self.logger.error(
                            f"[TaijiCustomLLMServing] 任务 {i} 失败: {e}"
                        )
                        results[i] = ("", "")
                    pbar.update(1)
        return results

    # ==================================================================
    # 内部工具
    # ==================================================================
    def _build_headers(self) -> dict:
        """生成带 HMAC-SHA1 签名的请求头。每次调用都会重新签名。"""
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
            "Apiversion":     self.api_version,
            "Authorization":  auth,
            "Date":           date_time,
            "Source":         self.source,
            "Content-Type":   "application/json",
        }

    def _build_body(
        self, user_prompt: str, system_prompt: str = ""
    ) -> dict:
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

    # ---------- 单次调用 -----------------------------------------------
    def _call_one(self, prompt: str, system_prompt: str) -> str:
        """返回 response 部分。若 retain_thinking=True，前面包上 <think>...</think>。"""
        thinking, response = self._call_one_with_thinking(prompt, system_prompt)
        if self.retain_thinking and thinking:
            return f"<think>{thinking}</think>\n{response}"
        return response

    def _call_one_with_thinking(
        self, prompt: str, system_prompt: str
    ) -> Tuple[str, str]:
        """单条请求 + 重试 + thinking/response 拆分。"""
        body = self._build_body(prompt, system_prompt)
        for attempt in range(1, self.max_retries + 1):
            try:
                # 每次重试都重新生成签名（避免 Date 过期）
                headers = self._build_headers()
                resp = self.session.post(
                    self.url,
                    headers=headers,
                    json=body,
                    timeout=self.timeout,
                )
                # 某些网关层面错误直接重试
                if resp.status_code >= 500:
                    raise requests.HTTPError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                resp.raise_for_status()
                raw = resp.json()
                return self._extract(raw)
            except Exception as e:
                if attempt >= self.max_retries:
                    self.logger.error(
                        f"[TaijiCustomLLMServing] 请求最终失败（{attempt} 次）: "
                        f"{type(e).__name__}: {e}"
                    )
                    return ("", "")
                # 指数退避，上限 30s
                back = min(2 ** attempt, 30)
                self.logger.warning(
                    f"[TaijiCustomLLMServing] 第 {attempt} 次失败，{back}s 后重试: "
                    f"{type(e).__name__}: {e}"
                )
                time.sleep(back)
        return ("", "")

    # ---------- 响应解析 -----------------------------------------------
    @staticmethod
    def _extract(raw: dict) -> Tuple[str, str]:
        """
        从定制协议响应中提取 (thinking, response)。
        兼容两种格式：定制协议 answer 数组 / OpenAI 兼容 choices。
        """
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

        # 兜底：OpenAI 兼容
        if "choices" in raw and raw["choices"]:
            msg = raw["choices"][0].get("message", {}) or {}
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or "").strip()
            if reasoning:
                return reasoning, content
            # 从 <think>...</think> 拆
            m = re.search(r"<think>(.*?)</think>(.*)", content, re.DOTALL)
            if m:
                return m.group(1).strip(), m.group(2).strip()
            return "", content

        return "", ""

    # ---------- 线程池 --------------------------------------------------
    def _run_threadpool(
        self,
        prompts: List[str],
        system_prompt: str,
        desc: str,
    ) -> List[str]:
        n = len(prompts)
        results: List[Optional[str]] = [None] * n

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._call_one, p, system_prompt): i
                for i, p in enumerate(prompts)
            }
            with tqdm(total=n, desc=desc, unit="req") as pbar:
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        results[i] = fut.result()
                    except Exception as e:
                        self.logger.error(
                            f"[TaijiCustomLLMServing] 任务 {i} 异常: "
                            f"{type(e).__name__}: {e}"
                        )
                        results[i] = ""
                    pbar.update(1)

        return [r if r is not None else "" for r in results]
