"""Embedding API 客户端（OpenAI 兼容）。"""
from __future__ import annotations
from src.utils.debug import debug_log
import json
from typing import Optional

import httpx

from src.models import ApiConfig


class EmbeddingClient:
    """OpenAI 兼容 Embedding 客户端。"""

    def __init__(self, api_config: ApiConfig, timeout: float = 60.0):
        self.api = api_config
        self.timeout = timeout

    @property
    def _url(self) -> str:
        base = self.api.effective_embedding_base_url.rstrip("/")
        return f"{base}/embeddings"

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api.effective_embedding_api_key}",
            "Content-Type": "application/json",
        }

    @property
    def model(self) -> str:
        return self.api.embedding_model

    def embed(self, text: str) -> Optional[list[float]]:
        """获取单条文本的 embedding 向量。"""
        if not self.api.embedding_model:
            return None
        try:
            body = {"model": self.api.embedding_model, "input": text}
            debug_log(lambda: f"[Embedding.embed] POST {self._url}")
            debug_log(lambda: f"[Embedding.embed] 入参 body: {json.dumps(body, ensure_ascii=False)}")
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    self._url, headers=self._headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                vec = data["data"][0]["embedding"]
                debug_log(lambda: f"[Embedding.embed] 出参 向量维度: {len(vec)}")
                return vec
        except Exception as e:
            debug_log(lambda: f"[Embedding.embed] 出参 异常: {e}")
            return None

    def embed_batch(self, texts: list[str]) -> Optional[list[list[float]]]:
        """批量获取 embedding。"""
        if not self.api.embedding_model or not texts:
            return None
        try:
            body = {"model": self.api.embedding_model, "input": texts}
            debug_log(lambda: f"[Embedding.embed_batch] POST {self._url}")
            debug_log(lambda: f"[Embedding.embed_batch] 入参 input 条数: {len(texts)}")
            debug_log(lambda: f"[Embedding.embed_batch] 入参 body: {json.dumps(body, ensure_ascii=False)}")
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    self._url, headers=self._headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                debug_log(lambda: f"[Embedding.embed_batch] 出参 返回向量数: {len(data.get('data', []))}")
                # 按 index 排序确保顺序
                items = sorted(data["data"], key=lambda x: x.get("index", 0))
                return [item["embedding"] for item in items]
        except Exception as e:
            debug_log(lambda: f"[Embedding.embed_batch] 出参 异常: {e}")
            return None


# ============ 连接测试（独立函数，供设置界面调用）============
def test_connection(api_config: ApiConfig, timeout: float = 30.0) -> tuple[bool, str]:
    """
    测试 Embedding API 连通性与可用性。

    对测试文本 "测试" 做 embedding，返回 (成功?, 详情文本)。
    成功详情含向量维度与延迟；失败详情含错误原因。
    """
    import time

    if not api_config.embedding_model:
        return False, "未配置 Embedding 模型"
    base_url = api_config.effective_embedding_base_url
    api_key = api_config.effective_embedding_api_key
    if not base_url:
        return False, "未配置 Embedding URL 或 Base URL"
    if not api_key:
        return False, "未配置 Embedding Key 或 API Key"

    base = base_url.rstrip("/")
    url = f"{base}/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {"model": api_config.embedding_model, "input": "测试"}

    start = time.time()
    try:
        debug_log(lambda: f"[Embedding.test_connection] POST {url}")
        debug_log(lambda: f"[Embedding.test_connection] 入参 body: {json.dumps(body, ensure_ascii=False)}")
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, json=body)
            elapsed_ms = int((time.time() - start) * 1000)
        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = err.get("error", {}).get("message") or resp.text[:300]
            except Exception:
                msg = resp.text[:300]
            debug_log(lambda: f"[Embedding.test_connection] 出参 HTTP {resp.status_code}: {resp.text[:500]}")
            return False, f"HTTP {resp.status_code}：{msg}"
        data = resp.json()
        dim = len(data["data"][0]["embedding"])
        debug_log(lambda: f"[Embedding.test_connection] 出参 向量维度: {dim}（{elapsed_ms}ms）")
        detail = f"连接成功（{elapsed_ms}ms）\n模型: {api_config.embedding_model}\n向量维度: {dim}"
        return True, detail
    except httpx.ConnectError as e:
        debug_log(lambda: f"[Embedding.test_connection] 出参 连接失败: {e}")
        return False, f"连接失败：{e}"
    except httpx.TimeoutException:
        debug_log(lambda: f"[Embedding.test_connection] 出参 请求超时（{int(timeout)}s）")
        return False, f"请求超时（{int(timeout)}s）"
    except Exception as e:
        debug_log(lambda: f"[Embedding.test_connection] 出参 异常: {e}")
        return False, f"请求出错：{e}"