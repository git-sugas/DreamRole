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


def images_dir() -> str:
    return get_subdir("images")


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