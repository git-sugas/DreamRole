"""
ComfyUI 文生图服务：通过 AI 回复中的标签触发，生成图片插入聊天。

工作流 JSON 模板中使用 {{positive}} 和 {{negative}} 占位符，
服务替换后提交到 ComfyUI /prompt 接口，轮询完成后下载图片。
图片仅插入聊天展示，不加入上下文。
"""
from __future__ import annotations
from src.utils.debug import debug_log
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx

from src.config import paths


# 多 LoRA 叠加上限（lora_name_1..lora_name_5）
MAX_LORAS = 5


@dataclass
class ComfyUiConfig:
    enabled: bool = False
    server_url: str = "http://127.0.0.1:8188"
    workflow_json: str = ""    # 含 {{positive}} {{negative}} 占位符的工作流 JSON
    timeout: int = 120         # 轮询超时（秒）
    poll_interval: float = 2.0
    # 基础采样参数（注入到工作流中的 KSampler / EmptyLatentImage 节点）
    steps: int = 20
    cfg: float = 8.0
    width: int = 512
    height: int = 768
    sampler_name: str = "euler"
    scheduler: str = "normal"
    # LoRA 配置（支持多 lora 叠加，最多 MAX_LORAS 个）
    lora_folder: str = ""      # 本地 lora 文件夹（用于扫描文件名，可选，与 ComfyUI 的 models/loras 对应）
    lora_prefix: str = ""      # 固定前缀（全局共用，所有 lora_name_N 都拼 prefix + name）
    lora_names: list = field(default_factory=lambda: [""])           # 各 lora 文件名（不含前缀）
    lora_strength_models: list = field(default_factory=lambda: [0.8])  # 各 lora 模型强度
    lora_strength_clips: list = field(default_factory=lambda: [0.8])   # 各 lora CLIP 强度
    # 模型文件选择（通用：扫描本地 models 文件夹填下拉，占位符 {{model_name}} 注入工作流）
    # [!] 通用占位符，不限定加载器类型：用户自行决定在工作流里哪个加载器
    # (CheckpointLoaderSimple.ckpt_name / UNETLoader.unet_name / CLIPLoader.clip_name /
    #  VAELoader.vae_name) 用 {{model_name}}。一个工作流里只用一处即可（一个值填不了多个不同模型名）。
    model_folder: str = ""     # 本地模型文件夹（扫描 .safetensors/.ckpt/.pt/.pth/.gguf）
    model_name: str = ""       # 选中的模型文件名（不含路径，含扩展名）

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "server_url": self.server_url,
            "workflow_json": self.workflow_json,
            "timeout": self.timeout,
            "poll_interval": self.poll_interval,
            "steps": self.steps,
            "cfg": self.cfg,
            "width": self.width,
            "height": self.height,
            "sampler_name": self.sampler_name,
            "scheduler": self.scheduler,
            "lora_folder": self.lora_folder,
            "lora_prefix": self.lora_prefix,
            "lora_names": self.lora_names,
            "lora_strength_models": self.lora_strength_models,
            "lora_strength_clips": self.lora_strength_clips,
            "model_folder": self.model_folder,
            "model_name": self.model_name,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ComfyUiConfig":
        # 老数据迁移：单值 lora_name/lora_strength_model/lora_strength_clip -> 单元素 list
        # 新数据用 lora_names/lora_strength_models/lora_strength_clips（list）
        def _to_list(val, default, default_item):
            if val is None:
                return [default_item]
            if isinstance(val, list):
                # 截断到 MAX_LORAS，不足补默认值（保证 list 长度对齐）
                lst = list(val)[:MAX_LORAS]
                while len(lst) < 1:
                    lst.append(default_item)
                return lst
            # 老的单值字段（str/float）：包成单元素 list
            return [val if val != "" and val is not None else default_item] if isinstance(val, str) else [val if val else default_item]

        names = d.get("lora_names")
        if names is None:
            names = [d.get("lora_name", "")]
        sm = d.get("lora_strength_models")
        if sm is None:
            sm = [d.get("lora_strength_model", 0.8)]
        sc = d.get("lora_strength_clips")
        if sc is None:
            sc = [d.get("lora_strength_clip", 0.8)]
        return cls(
            enabled=d.get("enabled", False),
            server_url=d.get("server_url", "http://127.0.0.1:8188"),
            workflow_json=d.get("workflow_json", ""),
            timeout=d.get("timeout", 120),
            poll_interval=d.get("poll_interval", 2.0),
            steps=d.get("steps", 20),
            cfg=d.get("cfg", 8.0),
            width=d.get("width", 512),
            height=d.get("height", 768),
            sampler_name=d.get("sampler_name", "euler"),
            scheduler=d.get("scheduler", "normal"),
            lora_folder=d.get("lora_folder", ""),
            lora_prefix=d.get("lora_prefix", ""),
            lora_names=_to_list(names, [""], ""),
            lora_strength_models=_to_list(sm, [0.8], 0.8),
            lora_strength_clips=_to_list(sc, [0.8], 0.8),
            model_folder=d.get("model_folder", ""),
            model_name=d.get("model_name", ""),
        )


# 默认工作流模板（基础 txt2img）
# 全部参数通过通配符占位符注入，不依赖节点 class_type，兼容第三方改名插件。
# 数值占位符在 JSON 中【不带引号】：{{seed}} {{steps}} {{cfg}} {{width}} {{height}}
#   {{lora_strength_model}} {{lora_strength_clip}}
# 字符串占位符在 JSON 中【带引号】：{{positive}} {{negative}} {{sampler_name}}
#   {{scheduler}} {{lora_name}}
DEFAULT_WORKFLOW = """{
  "3": {
    "class_type": "KSampler",
    "inputs": {
      "seed": {{seed}},
      "steps": {{steps}},
      "cfg": {{cfg}},
      "sampler_name": "{{sampler_name}}",
      "scheduler": "{{scheduler}}",
      "denoise": 1,
      "model": ["4", 0],
      "positive": ["6", 0],
      "negative": ["7", 0],
      "latent_image": ["5", 0]
    }
  },
  "4": {
    "class_type": "CheckpointLoaderSimple",
    "inputs": {"ckpt_name": "model.safetensors"}
  },
  "5": {
    "class_type": "EmptyLatentImage",
    "inputs": {"width": {{width}}, "height": {{height}}, "batch_size": 1}
  },
  "6": {
    "class_type": "CLIPTextEncode",
    "inputs": {"text": "{{positive}}", "clip": ["4", 1]}
  },
  "7": {
    "class_type": "CLIPTextEncode",
    "inputs": {"text": "{{negative}}", "clip": ["4", 1]}
  },
  "8": {
    "class_type": "VAEDecode",
    "inputs": {"samples": ["3", 0], "vae": ["4", 2]}
  },
  "9": {
    "class_type": "SaveImage",
    "inputs": {"images": ["8", 0]}
  }
}"""

# 全部支持的占位符（供外部引用 / 校验）
# 多 LoRA 占位符：lora_name_1..lora_name_5 + lora_strength_model_1..5 + lora_strength_clip_1..5
# 老 {{lora_name}}/{{lora_strength_model}}/{{lora_strength_clip}} 向后兼容（等价于 _1）
NUMERIC_PLACEHOLDERS = (
    "{{seed}}", "{{steps}}", "{{cfg}}", "{{width}}", "{{height}}",
) + tuple(f"{{{{lora_strength_model_{n}}}}}" for n in range(1, MAX_LORAS + 1)) \
  + tuple(f"{{{{lora_strength_clip_{n}}}}}" for n in range(1, MAX_LORAS + 1)) \
  + ("{{lora_strength_model}}", "{{lora_strength_clip}}")  # 老占位符向后兼容
STRING_PLACEHOLDERS = (
    "{{positive}}", "{{negative}}", "{{sampler_name}}",
    "{{scheduler}}", "{{model_name}}",
) + tuple(f"{{{{lora_name_{n}}}}}" for n in range(1, MAX_LORAS + 1)) \
  + ("{{lora_name}}",)  # 老占位符向后兼容
# 占位符匹配正则：{{word}}，一次性扫描替换避免串行 replace 的二次替换风险
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def _esc_json_str(s: str) -> str:
    """转义为合法 JSON 字符串内容（去外层引号）。

    占位符值注入工作流 JSON 文本时，若含 " / \\ / 换行 / 制表符等会破坏 JSON
    结构。json.dumps 会正确转义这些字符，去掉外层引号后即可安全插入 JSON 文本。
    """
    return json.dumps(s, ensure_ascii=False)[1:-1]


class ComfyUiService:
    """ComfyUI 文生图客户端。"""

    def __init__(self, config: Optional[ComfyUiConfig] = None):
        self.config = config or ComfyUiConfig()

    def is_enabled(self) -> bool:
        return self.config.enabled and bool(self.config.workflow_json)

    def _build_workflow(self, positive: str, negative: str) -> dict:
        """
        用通配符占位符替换工作流 JSON 后解析为 dict。
        全部参数（基础参数 + LoRA + seed）均通过占位符注入，不依赖节点 class_type，
        兼容第三方插件改名（如 KSampler 被包装为自定义采样器节点）。
        工作流中无对应占位符则该参数不生效（静默跳过）。

        [!] 字符串占位符（positive/negative/sampler/scheduler/lora_name）的值
        必须做 JSON 字符串转义后再注入，否则提示词含 " / \\ / 换行会破坏工作流 JSON
        导致 json.loads 抛错被 generate 吞为 None（静默不出图）。
        占位符替换用 re.sub 一次性扫描，避免用户提示词含 {{seed}} 字面量被二次替换。
        """
        cfg = self.config
        seed = int(time.time() * 1000) % (2**32)
        # 多 LoRA 占位符：lora_name_1..lora_name_5 + lora_strength_model_1..5 + lora_strength_clip_1..5
        # 每个 lora 名称 = 全局 lora_prefix + 对应 lora_names[i]（前缀共用）
        # list 不足 MAX_LORAS 时按默认值补齐（避免工作流含 lora_name_3 但配置只有 2 个时空串）
        names = list(cfg.lora_names) + [""] * (MAX_LORAS - len(cfg.lora_names))
        sm = list(cfg.lora_strength_models) + [0.8] * (MAX_LORAS - len(cfg.lora_strength_models))
        sc = list(cfg.lora_strength_clips) + [0.8] * (MAX_LORAS - len(cfg.lora_strength_clips))
        # 字符串值需转义；数值值直接 str() 后是安全字符
        string_vals = {
            "{{positive}}": positive,
            "{{negative}}": negative or "lowres, bad anatomy, bad hands, text, error",
            "{{sampler_name}}": cfg.sampler_name,
            "{{scheduler}}": cfg.scheduler,
            # 通用模型名占位符：用户在工作流里任一加载器字段用它（一处即可）。
            # 空值注入空串（工作流里若无此占位符则不生效，静默跳过）。
            "{{model_name}}": cfg.model_name,
        }
        numeric_vals = {
            "{{seed}}": str(seed),
            "{{steps}}": str(cfg.steps),
            "{{cfg}}": str(cfg.cfg),
            "{{width}}": str(cfg.width),
            "{{height}}": str(cfg.height),
        }
        # 逐个 lora 槽位注入占位符（1-based 序号）
        for i in range(MAX_LORAS):
            n = i + 1
            full_lora = (cfg.lora_prefix + names[i]).strip() if names[i] else ""
            string_vals[f"{{{{lora_name_{n}}}}}"] = full_lora
            numeric_vals[f"{{{{lora_strength_model_{n}}}}}"] = str(sm[i])
            numeric_vals[f"{{{{lora_strength_clip_{n}}}}}"] = str(sc[i])
        # [!] 向后兼容：老工作流里的 {{lora_name}}/{{lora_strength_model}}/{{lora_strength_clip}}
        # 等价于第 1 个 lora（lora_name_1），老配置不改动即可继续用
        string_vals["{{lora_name}}"] = string_vals["{{lora_name_1}}"]
        numeric_vals["{{lora_strength_model}}"] = numeric_vals["{{lora_strength_model_1}}"]
        numeric_vals["{{lora_strength_clip}}"] = numeric_vals["{{lora_strength_clip_1}}"]
        string_set = set(string_vals.keys())

        def _replacer(m: re.Match) -> str:
            key = "{{" + m.group(1) + "}}"
            if key in string_vals:
                return _esc_json_str(string_vals[key])
            if key in numeric_vals:
                return numeric_vals[key]
            return m.group(0)  # 未知占位符保留原样

        wf_str = _PLACEHOLDER_RE.sub(_replacer, cfg.workflow_json)
        return json.loads(wf_str)

    @staticmethod
    def validate_workflow_json(text: str) -> tuple[bool, str]:
        """
        校验含占位符的工作流 JSON 是否合法：
        数值占位符替换为 0、字符串占位符替换为 "x" 后再 json.loads。
        返回 (是否合法, 错误信息)。
        """
        tmp = text
        for p in NUMERIC_PLACEHOLDERS:
            tmp = tmp.replace(p, "0")
        for p in STRING_PLACEHOLDERS:
            tmp = tmp.replace(p, "x")
        try:
            json.loads(tmp)
            return True, ""
        except json.JSONDecodeError as e:
            return False, str(e)

    def generate(self, positive: str, negative: str = "",
                 dest_dir: Optional[str] = None) -> Optional[str]:
        """
        生成图片，返回本地图片路径。失败返回 None。
        dest_dir：图片保存目录，None 时落 paths.images_dir()（聊天出图默认）；
                  头像生成等场景传 paths.avatars_dir() 让图片直接进头像目录。
        """
        if not self.is_enabled():
            return None
        try:
            workflow = self._build_workflow(positive, negative)
            client_id = str(uuid.uuid4())
            prompt_data = {"prompt": workflow, "client_id": client_id}

            debug_log(lambda: f"[ComfyUI.generate] POST {self.config.server_url}/prompt")
            debug_log(lambda: f"[ComfyUI.generate] 入参 prompt_data: {json.dumps(prompt_data, ensure_ascii=False)}")
            with httpx.Client(timeout=30.0) as client:
                # 提交
                resp = client.post(
                    f"{self.config.server_url}/prompt", json=prompt_data
                )
                # [!] 4xx 时 ComfyUI 响应体含具体错误（哪个节点哪个字段），raise_for_status
                # 的异常 str 只显示状态行会丢失关键信息，先打印响应体再抛。
                if resp.status_code >= 400:
                    debug_log(lambda: f"[ComfyUI.generate] 出参 HTTP {resp.status_code} 响应体: {resp.text}")
                    resp.raise_for_status()
                result = resp.json()
                debug_log(lambda: f"[ComfyUI.generate] 出参 提交响应: {json.dumps(result, ensure_ascii=False)}")
                prompt_id = result.get("prompt_id")
                if not prompt_id:
                    return None

                # 轮询
                image_info = self._poll_history(client, prompt_id)
                if not image_info:
                    return None

                # 下载图片
                return self._download_image(client, image_info, dest_dir=dest_dir)
        except Exception as e:
            debug_log(lambda: f"[ComfyUI.generate] 出参 异常: {e}")
            return None

    def _poll_history(self, client: httpx.Client, prompt_id: str) -> Optional[dict]:
        """轮询 /history 直到完成。"""
        deadline = time.time() + self.config.timeout
        while time.time() < deadline:
            try:
                debug_log(lambda: f"[ComfyUI._poll_history] GET {self.config.server_url}/history/{prompt_id}")
                debug_log(lambda: f"[ComfyUI._poll_history] 入参 prompt_id: {prompt_id}")
                resp = client.get(f"{self.config.server_url}/history/{prompt_id}")
                if resp.status_code == 200:
                    data = resp.json()
                    debug_log(lambda: f"[ComfyUI._poll_history] 出参 status=200 keys={list(data.keys())}")
                    if prompt_id in data:
                        outputs = data[prompt_id].get("outputs", {})
                        # 查找包含 images 的输出
                        for node_id, node_output in outputs.items():
                            if "images" in node_output and node_output["images"]:
                                found = node_output["images"][0]
                                debug_log(lambda: f"[ComfyUI._poll_history] 出参 找到图片: {json.dumps(found, ensure_ascii=False)}")
                                return found
                else:
                    debug_log(lambda: f"[ComfyUI._poll_history] 出参 status={resp.status_code}")
            except Exception as e:
                debug_log(lambda: f"[ComfyUI._poll_history] 出参 异常: {e}")
                pass
            time.sleep(self.config.poll_interval)
        return None

    def _download_image(self, client: httpx.Client, image_info: dict,
                        dest_dir: Optional[str] = None) -> Optional[str]:
        """下载图片到本地。dest_dir 为 None 时落 paths.images_dir()。"""
        filename = image_info.get("filename", "")
        subfolder = image_info.get("subfolder", "")
        img_type = image_info.get("type", "output")
        if not filename:
            return None
        params = {"filename": filename, "subfolder": subfolder, "type": img_type}
        debug_log(lambda: f"[ComfyUI._download_image] GET {self.config.server_url}/view")
        debug_log(lambda: f"[ComfyUI._download_image] 入参 params: {json.dumps(params, ensure_ascii=False)}")
        resp = client.get(f"{self.config.server_url}/view", params=params)
        resp.raise_for_status()
        debug_log(lambda: f"[ComfyUI._download_image] 出参 status={resp.status_code} 图片大小={len(resp.content)} bytes")
        # 保存
        local_name = f"img_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}.png"
        save_dir = dest_dir if dest_dir else paths.images_dir()
        local_path = f"{save_dir}/{local_name}"
        with open(local_path, "wb") as f:
            f.write(resp.content)
        return local_path

    def test_connection(self) -> tuple[bool, str]:
        """测试 ComfyUI 连接。"""
        try:
            with httpx.Client(timeout=10.0) as client:
                debug_log(lambda: f"[ComfyUI.test_connection] GET {self.config.server_url}/system_stats")
                resp = client.get(f"{self.config.server_url}/system_stats")
                resp.raise_for_status()
                debug_log(lambda: f"[ComfyUI.test_connection] 出参 status={resp.status_code} 连接成功")
                return True, "连接成功"
        except Exception as e:
            debug_log(lambda: f"[ComfyUI.test_connection] 出参 异常: {e}")
            return False, str(e)


# 配置文件读写
def _comfyui_config_path() -> str:
    # 用 dirname + join 推导，避免依赖 config_path() 返回值恰好以 app_config.json 结尾
    return os.path.join(os.path.dirname(paths.config_path()), "comfyui_config.json")


def load_comfyui_config() -> ComfyUiConfig:
    path = _comfyui_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return ComfyUiConfig.from_dict(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = ComfyUiConfig(workflow_json=DEFAULT_WORKFLOW)
        return cfg


def save_comfyui_config(config: ComfyUiConfig):
    # 原子写防半写损坏（与 storage._save_json_atomic 一致）
    path = _comfyui_config_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise