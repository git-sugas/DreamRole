"""TTS 语音合成服务。

调 Fish Audio S2 模型 TTS API（POST /v1/tts），支持：
- synthesize(): 单段文本合成音频文件
- process_message_tts(): 对一条消息按正则规则切分文本段、选音色、可选情绪 LLM 加工、
  逐段合成，返回音频路径列表
- cleanup_message_audio(): 删除消息绑定的所有音频文件
- test_connection(): 测试 TTS 连接

只兼容 Fish Audio S2 模型（方括号 [happy] 情绪语法）。非通用参数通过 TtsVoice.extra_params
键值对透传到 TTS 请求 body，兼容不同 TTS 服务的差异化字段。

仿 comfyui_service.py 的 httpx 同步调用 + 文件落盘模式，仿 danbooru_service.py 的
独立 LLM 调用（_resolve_emotion_api 三级兜底 + _jb_prefix）模式。
"""
from __future__ import annotations
import os
import re
import time
import uuid
import json
from typing import Callable, Optional

import httpx

from src.config import paths
from src.utils.debug import debug_log
from src.models import (
    Message, TtsVoice, TtsRulesConfig, TtsPreset,
    ApiConfig, Preset,
)


# Fish Audio TTS API 常量
TTS_DEFAULT_MODEL = "s2.1-pro-free"  # 默认模型（免费版），可被 preset.tts_model 覆盖
TTS_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0)  # 分阶段超时

# 数值型 extra_params 键名（尝试自动转 int/float；其余按 str 透传）
_INT_PARAM_KEYS = {"chunk_length", "max_new_tokens", "mp3_bitrate", "sample_rate", "opus_bitrate"}
_FLOAT_PARAM_KEYS = {"temperature", "top_p", "repetition_penalty"}


def _coerce_value(key: str, value: str):
    """尝试把 extra_params 的值转为合适的类型。

    优先 int -> float -> str，保证 Fish Audio API 收到正确 JSON 类型。
    嵌套键（含 '.'，如 prosody.volume）取最后一段判断。
    """
    if not isinstance(value, str):
        return value
    leaf = key.rsplit(".", 1)[-1]
    if leaf in _INT_PARAM_KEYS:
        try:
            return int(value)
        except ValueError:
            pass
    if leaf in _FLOAT_PARAM_KEYS:
        try:
            return float(value)
        except ValueError:
            pass
    # 通用尝试：int -> float -> str
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


# 命中段首尾成对定界符 -> 内部净文本剥离表（覆盖默认规则全部定界符 + 常见自定义）。
# 中间内容完全不动（含 [happy] 这类命中区间内部标记）。首尾不成对则原样返回不误伤。
_DELIM_PAIRS = {
    "「": "」", "『": "』", "（": "）", "(": ")", "《": "》",
    '"': '"', "'": "'", "*": "*", "“": "”", "‘": "’",
}


def _strip_delims(text: str) -> str:
    """剥离命中段首尾成对的定界符，返回中间净文本。

    用于把正则命中的带符号段（如 「台词」/「台词」/*旁白*/（心声））剥成净文本提交给
    情绪 LLM 与 TTS：定界符是排版符号，Fish Audio 可能当普通字符读出影响效果，且
    提交 LLM 时去掉符号可让模型专注判断文本内容的情绪。

    仅当首字符在配对表内、且末字符恰为对应闭符时剥离各一个字符；否则原样返回
    （对用户自定义规则只要首尾是成对定界符就有效，不成对不误伤）。中间内容不动。
    """
    if not text or len(text) < 2:
        return text
    opener = text[0]
    closer = _DELIM_PAIRS.get(opener)
    if closer is not None and text[-1] == closer:
        return text[1:-1]
    return text


# TTS 朗读前的文本净化：清理会被 Fish Audio 当普通字符读出的排版符号。
# [!] LLM 常用连续减号 `--`/`---` 表示破折号/长停顿，Fish Audio 会读成「减减」。
# 换成 Fish Audio S2 停顿标记 `[pause]`：让 TTS 在破折号处真正停顿，保留原本的
# 停顿语气而非读出符号。保留单个 `-`（合法连字符如 well-known）。
_SPEAK_NOISE_PATTERNS = [
    (re.compile(r"-{2,}"), "[pause]"),  # -- / --- / ---- ... -> [pause] 停顿标记
]


def _clean_speak_text(text: str) -> str:
    """清理 TTS 朗读文本中的排版符号噪声（合成前调用）。

    与 _strip_delims 互补：_strip_delims 剥首尾定界符（加工前），本函数清中间的
    朗读噪声符号（合成前，覆盖情绪 LLM 加工后可能重新引入的 `--`）。
    """
    if not text:
        return text
    for pat, repl in _SPEAK_NOISE_PATTERNS:
        text = pat.sub(repl, text)
    return text


class TtsService:
    """TTS 语音合成服务。"""

    def __init__(self, storage):
        self.storage = storage

    # ============ 正则规则编译（仿 markup._rebuild_compiled 容错）============
    def _compile_rules(self, config: TtsRulesConfig):
        """编译 TTS 正则规则，返回 (合并 Pattern | None, group_to_rule dict)。

        单条编译容错（坏规则跳过不影响整体），按 priority 升序，合并命名分组大正则
        加速 finditer；合并失败回退 None（调用方逐规则 finditer 兜底）。
        """
        compiled = []
        for rule in config.rules:
            if not rule.enabled or not rule.pattern:
                continue
            try:
                compiled.append((rule, re.compile(rule.pattern)))
            except re.error as e:
                print(f"[TTS] 规则「{rule.name}」正则编译失败，已跳过: {e}")
        compiled.sort(key=lambda x: x[0].priority)

        if not compiled:
            return None, {}

        # 合并命名分组大正则：(?P<r0>...)|(?P<r1>...)
        parts = []
        group_to_rule = {}
        for i, (rule, _) in enumerate(compiled):
            gname = f"t{i}"
            parts.append(f"(?P<{gname}>{rule.pattern})")
            group_to_rule[gname] = rule
        try:
            merged = re.compile("|".join(parts))
            return merged, group_to_rule
        except re.error:
            return None, group_to_rule

    def _segment_content(self, content: str, config: TtsRulesConfig):
        """用 TTS 规则切分消息内容，返回 [(text段, voice_id), ...]。

        匹配命中的段带上对应 voice_id；未命中的段 voice_id 为空（调用方跳过不配音）。
        """
        merged, group_to_rule = self._compile_rules(config)
        if merged is None or not group_to_rule:
            return []  # 无有效规则，整条不配音

        segments = []
        last_end = 0
        for m in merged.finditer(content):
            # 命中前的未匹配段（不配音）
            if m.start() > last_end:
                segments.append((content[last_end:m.start()], ""))
            # 命中段：逐顶层命名组判断哪个命中（不依赖 m.lastgroup，
            # 因用户 pattern 内部可能含自己的命名子组致 lastgroup 返回内层组名，
            # 仿 markup.py 逐组判断更稳）
            voice_id = ""
            for gname, rule in group_to_rule.items():
                if m.group(gname) is not None:
                    voice_id = rule.voice_id if rule else ""
                    break
            segments.append((m.group(), voice_id))
            last_end = m.end()
        # 末尾未匹配段
        if last_end < len(content):
            segments.append((content[last_end:], ""))
        return segments

    # ============ 情绪 LLM 加工 ============
    def _jb_prefix(self) -> str:
        """读取破限前缀：开关关或 prefix 空返回空串。"""
        cfg = self.storage.load_app_config()
        if cfg.jailbreak_enabled and cfg.jailbreak_prefix:
            return cfg.jailbreak_prefix
        return ""

    def _resolve_emotion_api(
        self, preset: TtsPreset, session_api=None
    ) -> Optional[ApiConfig]:
        """情绪标注 LLM API 三级兜底：preset.api_id -> session_api -> 首个 enabled。"""
        if preset.api_id:
            api = self.storage.load_api(preset.api_id)
            if api and api.enabled:
                return api
        if session_api and session_api.enabled:
            return session_api
        for api in self.storage.load_all_apis():
            if api.enabled:
                return api
        return None

    def _emotion_enhance_batch(self, texts: list[str], full_content: str,
                               preset: TtsPreset, session_api=None) -> list[str]:
        """批量调情绪 LLM 加工多段净文本，返回等长结果列表。

        把同音色的多段净文本用 `` / `` 拼接成一条 user 消息一次性提交 LLM（提示词要求
        输出也用 `` / `` 分隔成相同数量片段、不增删不改写只加标记），返回后按 `` / ``
        拆分还原到各段。比逐段调用省 N 倍往返延迟，且 LLM 能跨段感知语气连贯性。

        兜底：LLM 失败 / 返回空 / 拆分后段数 != 输入段数 -> 该组回退各段原文净文本
        （不加工但不丢段）。失败不阻断 TTS，调用方仍能用原文合成。
        """
        n = len(texts)
        if n == 0:
            return []
        api = self._resolve_emotion_api(preset, session_api)
        if api is None:
            return list(texts)
        # 批量场景输出含全部片段+标记，按段数动态放大 max_tokens 防截断（截断会致段数
        # 不符触发回退原文，丧失加工）。估算 = 各段总长（chars）* 1.3 标记开销余量 + 200
        # 固定开销，上限 8192 防滥用，不低于用户配置值。
        est_tokens = sum(len(t) for t in texts) * 1.3 + 200
        batch_max = int(min(8192, max(preset.max_tokens, est_tokens)))
        tmp_preset = Preset(
            name="tts_emotion",
            system_prompt=preset.system_prompt,
            temperature=preset.temperature,
            max_tokens=batch_max,
            top_p=preset.top_p,
        )
        # 多段用 " / " 拼接：上下文 + 需要标注的片段（已去定界符），让 LLM 一次性标注
        joined = " / ".join(texts)
        user_prompt = (
            f"以下是角色扮演中一段气泡的完整内容（作为上下文参考）：\n"
            f"{full_content}\n\n"
            f"请为以下需要配音的文本片段添加情绪标记。多个片段用 / 分隔，"
            f"输出时也用 / 分隔成相同数量片段，不要增删文字、不要合并或拆分：\n"
            f"{joined}"
        )
        # 惰性 import 避免循环依赖
        from src.services.llm_client import LlmClient
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix(), http2=False)
        result = llm.chat([
            {"role": "system", "content": preset.system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        if result.error or not result.content:
            debug_log(lambda: f"[TTS.emotion] LLM 批量加工失败回退原文: {result.error}")
            return list(texts)
        # 按 " / " 拆分；LLM 可能输出 "/" 或 " / "，统一按 "/" 切再 strip
        parts = [p.strip() for p in result.content.split("/")]
        # 段数不符（LLM 误拆/合并/含 / 的文本）-> 回退原文，避免错位
        if len(parts) != n:
            debug_log(lambda: f"[TTS.emotion] 拆分段数 {len(parts)} != 输入 {n}，回退原文")
            return list(texts)
        debug_log(lambda: f"[TTS.emotion] 批量加工 {n} 段成功: {result.content[:80]}")
        return parts

    # ============ 单段合成 ============
    def synthesize(self, text: str, voice: TtsVoice, preset: TtsPreset,
                   cancel_check: Optional[Callable] = None) -> Optional[str]:
        """调 Fish Audio TTS 合成单段文本，返回音频文件路径，失败返回 None。

        通用字段（text/reference_id/format/latency/prosody.speed）+ extra_params 展开
        成 body 顶层键（嵌套键如 prosody.volume 展开为 {"prosody": {"volume": 0}}）。
        """
        if not text.strip():
            return None
        if not voice.reference_id:
            debug_log(lambda: f"[TTS.synth] 音色「{voice.name}」无 reference_id，跳过")
            return None
        if cancel_check and cancel_check():
            return None

        # 构造请求 body
        body = {
            "text": text,
            "reference_id": voice.reference_id,
            "format": voice.format,
            "latency": voice.latency,
            "prosody": {"speed": voice.speed},
        }
        # extra_params 展开：嵌套键（含 '.'）展开成嵌套 dict，如 prosody.volume -> body["prosody"]["volume"]
        for p in voice.extra_params:
            key = p.get("key", "").strip()
            val = p.get("value", "")
            if not key:
                continue
            coerced = _coerce_value(key, val)
            if "." in key:
                parts = key.split(".")
                cur = body
                for part in parts[:-1]:
                    if part not in cur or not isinstance(cur[part], dict):
                        cur[part] = {}
                    cur = cur[part]
                cur[parts[-1]] = coerced
            else:
                body[key] = coerced

        base_url = (preset.tts_base_url or "https://api.fish.audio").rstrip("/")
        url = f"{base_url}/v1/tts"
        headers = {
            "Authorization": f"Bearer {preset.tts_api_key}",
            "Content-Type": "application/json",
            "model": preset.tts_model or TTS_DEFAULT_MODEL,
        }

        try:
            debug_log(lambda: f"[TTS.synth] POST {url} voice={voice.name} text={text[:40]}")
            # 打印请求头（含 model）和 body，便于排查
            safe_headers = {k: (v[:8] + "***" if k.lower() == "authorization" else v) for k, v in headers.items()}
            debug_log(lambda: f"[TTS.synth] headers={json.dumps(safe_headers, ensure_ascii=False)}")
            debug_log(lambda: f"[TTS.synth] body={json.dumps(body, ensure_ascii=False)[:300]}")
            # 代理：preset.tts_proxy 非空时走代理（如 http://127.0.0.1:7890）
            proxy = preset.tts_proxy.strip() if hasattr(preset, "tts_proxy") else ""
            client_kwargs = {"timeout": TTS_TIMEOUT, "http2": False, "trust_env": False}
            if proxy:
                client_kwargs["proxy"] = proxy
                debug_log(lambda: f"[TTS.synth] 使用代理: {proxy}")
            # 强制 HTTP/1.1（httpx 默认 HTTP/2，部分网络环境 SSL EOF）；带重试
            last_err = None
            for attempt in range(3):
                try:
                    with httpx.Client(**client_kwargs) as client:
                        resp = client.post(url, headers=headers, json=body)
                    break
                except Exception as e:
                    last_err = e
                    debug_log(lambda: f"[TTS.synth] 第{attempt+1}次失败: {type(e).__name__}: {e}")
                    if attempt < 2:
                        import time as _time
                        _time.sleep(1.0 * (attempt + 1))  # 递增等待
            else:
                raise last_err
            if resp.status_code >= 400:
                debug_log(lambda: f"[TTS.synth] HTTP {resp.status_code} 响应体: {resp.text[:300]}")
                resp.raise_for_status()
            if cancel_check and cancel_check():
                return None
            # 落盘
            ext = voice.format if voice.format in ("mp3", "wav", "pcm", "opus") else "mp3"
            filename = f"tts_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.{ext}"
            local_path = os.path.join(paths.tts_audio_dir(), filename)
            with open(local_path, "wb") as f:
                f.write(resp.content)
            debug_log(lambda: f"[TTS.synth] 保存音频: {local_path} ({len(resp.content)} bytes)")
            return local_path
        except Exception as e:
            debug_log(lambda: f"[TTS.synth] 合成失败: {e}")
            return None

    # ============ 消息级 TTS 流程 ============
    def process_message_tts(self, msg: Message, session_api=None,
                           cancel_check: Optional[Callable] = None) -> list:
        """对一条消息做完整 TTS 流程，返回生成的段映射列表 [{text:str, path:str}, ...]。

        1. 加载 TtsRulesConfig + 切分消息内容
        2. 命中段去定界符得净文本，按音色分组
        3. 同音色段合并成一次情绪 LLM 调用（关闭则直接用净文本），返回后逐段合成
        4. 未命中的段跳过不配音

        [!] 返回段映射（含净文本 + 音频路径），调用方据此落库到 msg.tts_segments，
        供气泡渲染「点击播放此段」按钮。text 为 _strip_delims 后的净文本（不含
        「」等定界符），与气泡正文展示的原文可能略有差异（属预期：配音段文本是
        TTS 实际朗读的内容）。

        [!] 情绪 LLM 批量加工：同 voice_id 的命中段用 / 拼接一次性提交，避免逐段
        N 次 LLM 往返；LLM 能跨段感知语气连贯性。段数不符回退原文净文本。
        """
        if not msg.content or not msg.content.strip():
            return []

        preset = self.storage.load_tts_preset()
        if not preset.tts_api_key:
            debug_log(lambda: "[TTS.process] 未配置 TTS API key，跳过")
            return []

        rules_cfg = self.storage.load_tts_rules()
        segments = self._segment_content(msg.content, rules_cfg)

        # 收集命中段（voice_id 非空 + 音色存在且 enabled），保留原始顺序 + 预加载音色
        hit = []  # [(净文本, voice), ...]
        for text, voice_id in segments:
            if not voice_id:
                continue  # 未匹配的段不配音
            voice = self.storage.load_tts_voice(voice_id)
            if not voice or not voice.enabled:
                debug_log(lambda: f"[TTS.process] 音色 {voice_id} 不存在或禁用，跳过")
                continue
            hit.append((_strip_delims(text), voice))

        if not hit:
            return []

        # 情绪加工：开启时按 voice.id 分组，各组批量调一次 LLM；关闭则直接用净文本
        tts_texts = [t for t, _ in hit]
        if preset.emotion_enabled and preset.system_prompt:
            # 按 voice.id 分组（保持各组内原顺序），记录每段的全局索引
            groups: dict[str, list[int]] = {}
            for i, (_, voice) in enumerate(hit):
                groups.setdefault(voice.id, []).append(i)
            for voice_id, idxs in groups.items():
                if cancel_check and cancel_check():
                    break
                group_texts = [tts_texts[i] for i in idxs]
                enhanced = self._emotion_enhance_batch(
                    group_texts, msg.content, preset, session_api)
                # 等长回填（_emotion_enhance_batch 保证等长，兜底也是等长原文）
                for i, txt in zip(idxs, enhanced):
                    tts_texts[i] = txt

        result = []  # [{text, path}, ...] 段映射（只含成功合成段）
        for i, (_, voice) in enumerate(hit):
            if cancel_check and cancel_check():
                break
            # 合成前净化：清理 LLM 加工后可能残留的 -- 等排版符号（Fish Audio 会读成「减减」）
            speak_text = _clean_speak_text(tts_texts[i])
            path = self.synthesize(speak_text, voice, preset, cancel_check)
            if path:
                result.append({"text": tts_texts[i], "path": path})

        return result

    # ============ 清理 ============
    def cleanup_message_audio(self, msg: Message):
        """删除消息绑定的所有音频文件（不删 DB 行）。用于重新生成 TTS 前清旧文件。"""
        for path in msg.audio_paths:
            try:
                os.remove(path)
            except OSError:
                pass
        msg.audio_paths = []
        msg.tts_segments = []
        msg.has_tts = False

    # ============ 测试连接 ============
    def test_connection(self, voice: TtsVoice, preset: TtsPreset) -> tuple:
        """测试 TTS 连接：合成一句测试文本并保留文件供试听。返回 (ok, msg)。

        文件保留在 tts_audio_dir 不删除，msg 里带回文件路径让用户知道在哪试听。
        """
        try:
            path = self.synthesize("你好，这是 TTS 测试。", voice, preset)
            if path:
                return True, f"连接成功！音频已保存，可试听：{path}"
            return False, "合成失败（请检查 API key / base_url / reference_id / 代理）"
        except Exception as e:
            return False, f"连接失败: {e}"
