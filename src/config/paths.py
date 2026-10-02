"""
数据目录管理。
打包为 exe 后，data 目录放在 exe 同级路径下，保证便携与可写。
"""
import os
import sys


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def get_app_dir() -> str:
    """应用程序根目录（exe 所在目录 或 项目根目录）。"""
    if _is_frozen():
        return os.path.dirname(sys.executable)
    # 开发模式：项目根目录
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_data_dir() -> str:
    """用户数据根目录。"""
    path = os.path.join(get_app_dir(), "data")
    os.makedirs(path, exist_ok=True)
    return path


def get_subdir(name: str) -> str:
    """获取 data 下的子目录，自动创建。"""
    path = os.path.join(get_data_dir(), name)
    os.makedirs(path, exist_ok=True)
    return path


# ---- 各资源目录 ----
def characters_dir() -> str:
    return get_subdir("characters")


def users_dir() -> str:
    return get_subdir("users")


def apis_dir() -> str:
    return get_subdir("apis")


def presets_dir() -> str:
    return get_subdir("presets")


def world_books_dir() -> str:
    return get_subdir("world_books")


def chats_dir() -> str:
    return get_subdir("chats")


def avatars_dir() -> str:
    return get_subdir("avatars")


def covers_dir() -> str:
    """世界书封面图片目录（矩形缩略图，区别于 avatars 的圆形头像）。"""
    return get_subdir("covers")


def images_dir() -> str:
    return get_subdir("images")


def tts_voices_dir() -> str:
    """TTS 音色设定 JSON 目录（一音色一文件，仿 apis_dir 多文件模式）。"""
    return get_subdir("tts_voices")


def tts_audio_dir() -> str:
    """TTS 生成的音频文件目录（仿 images_dir）。"""
    return get_subdir("tts_audio")


def chroma_dir() -> str:
    return get_subdir("chroma")


def danbooru_db_dir() -> str:
    """Danbooru tag 向量库持久化目录（独立于记忆 chroma_dir，避免混用）。"""
    return get_subdir("danbooru_db")


def danbooru_dict_dir() -> str:
    """Danbooru jieba 自定义词典目录（含 nsfw.dict 等）。
    放用户数据目录下便于随时增删词；首次启动由 DanbooruService 生成默认 nsfw 词典。"""
    return get_subdir("danbooru_dict")


def db_path() -> str:
    return os.path.join(get_data_dir(), "ai_roleplay.db")


def config_path() -> str:
    return os.path.join(get_data_dir(), "app_config.json")


def render_rules_path() -> str:
    """气泡配色规则配置文件路径。"""
    return os.path.join(get_data_dir(), "render_rules.json")


def memory_preset_path() -> str:
    """记忆整理预设配置文件路径。"""
    return os.path.join(get_data_dir(), "memory_preset.json")


def summary_preset_path() -> str:
    """上文总结预设配置文件路径。"""
    return os.path.join(get_data_dir(), "summary_preset.json")


def danbooru_preset_path() -> str:
    """Danbooru tag 加工预设配置文件路径。"""
    return os.path.join(get_data_dir(), "danbooru_preset.json")


def tts_rules_path() -> str:
    """TTS 正则规则配置文件路径。"""
    return os.path.join(get_data_dir(), "tts_rules.json")


def tts_preset_path() -> str:
    """TTS 预设配置文件路径（全局连接 + 情绪 LLM）。"""
    return os.path.join(get_data_dir(), "tts_preset.json")


# ============ 世界模拟（独立 SLG 系统，§21）============
def worlds_dir() -> str:
    """世界模拟 - 世界卡 JSON 目录（一世界一文件 {id}.json，仿 characters_dir）。"""
    return get_subdir("worlds")


def world_images_dir() -> str:
    """世界模拟 - 图像目录（{world_id}_banner.png / {loc_id}_bg.png / {npc_id}_avatar.png）。"""
    return get_subdir("world_images")


def world_sim_preset_path() -> str:
    """世界模拟全局预设配置文件路径（单例）。"""
    return os.path.join(get_data_dir(), "world_sim_preset.json")


def scene_path(world_id: str) -> str:
    """世界模拟 - 场景日志文件路径（一世界一份 {id}_scene.json，独立于 World JSON）。

    场景会话状态（旁白流/回合计数）独立持久化，保持 World JSON 精简（世界定义静态、
    场景动态）。P2 场景交互循环用。
    """
    return os.path.join(worlds_dir(), f"{world_id}_scene.json")


def scene_image_state_path(world_id: str) -> str:
    """世界模拟 - 场景事件生图状态文件路径（{id}_scene_images.json）。

    独立于 SceneLog（场景日志只记对话流，不混入生图状态）与 World JSON。
    P2 场景事件生图限频（last_image_tick/generated_count）用。
    """
    return os.path.join(worlds_dir(), f"{world_id}_scene_images.json")


def npc_memory_dir(world_id: str) -> str:
    """[P6c] 世界模拟 - NPC 个人记忆目录（{id}_npc_memory/，每 NPC 一份 {npc_id}.json）。

    独立于 World JSON（世界定义静态，记忆是动态沉淀）；delete_world 级联删此目录。
    NPC 个人记忆（人际关系/爱好之外的「经历记忆」，跨场景沉淀）用，仿角色记忆。
    [!] 纯路径拼接不建目录：读路径（_load/recent_lines/_with_chat_excerpt/load_npc_chat/
    delete_world 的 isdir 判定）调一次建一次空目录，删掉的世界重启又冒出空壳 +
    单测污染真实 data/worlds。建目录只在写路径做（_save_json_atomic/save_npc_chat
    内已有 makedirs）。
    """
    return os.path.join(get_data_dir(), "worlds", f"{world_id}_npc_memory")


def npc_chat_dir(world_id: str) -> str:
    """[P10] 世界模拟 - 好友 NPC 私聊记录目录（{id}_npc_chat/，每 NPC 一份 {npc_id}.json）。

    独立于 SceneLog（场景日志是回合旁白流，私聊是好友间的独立对话）；
    delete_world 级联删此目录。每份 JSON 是 [{role, content, tick}] 消息数组。
    [!] 纯路径拼接不建目录（同 npc_memory_dir，读时建目录 = 删后复活空壳）。
    """
    return os.path.join(get_data_dir(), "worlds", f"{world_id}_npc_chat")