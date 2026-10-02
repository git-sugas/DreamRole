"""Embedding API 客户端（OpenAI 兼容）。"""
from __future__ import annotations
from src.utils.debug import debug_log
import json
from typing import Optional

import httpx

from src.models import ApiConfig


def _should_truncate(model: str, dim: int) -> bool:
    """是否需要把返回向量截断到 dim 维。

    仅当维度>0 且 emb 模型名含 qwen 时才截断（qwen3-embedding 系列支持 MRL，
    重要语义集中在前 N 维）。非 qwen 模型（如 bge-m3）忽略此设置，用原生维度。

    [!] 双保险：请求体仍带 dimensions=N（对支持的服务端如 SiliconFlow 有效，
    能省网络传输）；服务端忽略时（如 LM Studio/llama.cpp）由 _truncate_vector
    客户端兜底截断。两路任一生效即可，最终存储的都是目标维度。
    """
    return dim > 0 and "qwen" in (model or "").lower()


def _truncate_vector(vec: list[float], dim: int) -> list[float]:
    """客户端截断向量到前 dim 维并重新 L2 归一化（MRL 模型前 N 维可独立用）。

    完整向量是归一化的（模长=1），截取前 dim 维后模长 <1。
    cosine 相似度对模长敏感，必须重新归一化，否则相似度全偏低导致召回失效。
    向量维度 < dim 时不截断（服务端已截断或模型原生维度更低），原样返回。
    """
    if dim <= 0 or len(vec) <= dim:
        return vec
    sub = vec[:dim]
    norm = sum(x * x for x in sub) ** 0.5
    if norm > 0:
        return [x / norm for x in sub]
    return sub


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
            dim = getattr(self.api, "embedding_dimensions", 0)
            if _should_truncate(self.api.embedding_model, dim):
                body["dimensions"] = dim
            debug_log(lambda: f"[Embedding.embed] POST {self._url}")
            debug_log(lambda: f"[Embedding.embed] 入参 body: {json.dumps(body, ensure_ascii=False)}")
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                resp = client.post(
                    self._url, headers=self._headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                vec = data["data"][0]["embedding"]
                if _should_truncate(self.api.embedding_model, dim):
                    vec = _truncate_vector(vec, dim)
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
            dim = getattr(self.api, "embedding_dimensions", 0)
            if _should_truncate(self.api.embedding_model, dim):
                body["dimensions"] = dim
            debug_log(lambda: f"[Embedding.embed_batch] POST {self._url}")
            debug_log(lambda: f"[Embedding.embed_batch] 入参 input 条数: {len(texts)}")
            debug_log(lambda: f"[Embedding.embed_batch] 入参 body: {json.dumps(body, ensure_ascii=False)}")
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                resp = client.post(
                    self._url, headers=self._headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                debug_log(lambda: f"[Embedding.embed_batch] 出参 返回向量数: {len(data.get('data', []))}")
                # 按 index 排序确保顺序
                items = sorted(data["data"], key=lambda x: x.get("index", 0))
                vecs = [item["embedding"] for item in items]
                if _should_truncate(self.api.embedding_model, dim):
                    vecs = [_truncate_vector(v, dim) for v in vecs]
                return vecs
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
    want_dim = getattr(api_config, "embedding_dimensions", 0)
    if _should_truncate(api_config.embedding_model, want_dim):
        body["dimensions"] = want_dim

    start = time.time()
    try:
        debug_log(lambda: f"[Embedding.test_connection] POST {url}")
        debug_log(lambda: f"[Embedding.test_connection] 入参 body: {json.dumps(body, ensure_ascii=False)}")
        with httpx.Client(timeout=timeout, trust_env=False) as client:
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
        raw_vec = data["data"][0]["embedding"]
        raw_dim = len(raw_vec)
        truncated = _should_truncate(api_config.embedding_model, want_dim)
        if truncated:
            final_vec = _truncate_vector(raw_vec, want_dim)
            final_dim = len(final_vec)
            # 服务端返回维度已等于目标 -> 服务端截断生效；否则客户端兜底截断
            if raw_dim == want_dim:
                dim_note = f"（服务端已截断到 {want_dim} 维）"
            else:
                dim_note = f"（服务端返回 {raw_dim} 维，客户端兜底截断到 {want_dim} 维）"
            show_dim = final_dim
        elif want_dim == 0:
            dim_note = "（未截断，用原生维度）"
            show_dim = raw_dim
        else:
            dim_note = "（非 qwen 模型，忽略维度设置，用原生维度）"
            show_dim = raw_dim
        debug_log(lambda: f"[Embedding.test_connection] 出参 向量维度: {raw_dim}->{show_dim}（{elapsed_ms}ms）")
        detail = f"连接成功（{elapsed_ms}ms）\n模型: {api_config.embedding_model}\n向量维度: {show_dim}{dim_note}"
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