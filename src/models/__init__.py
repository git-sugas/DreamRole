from .character import Character
from .user import User
from .message import Message
from .session import Session
from .api_config import ApiConfig
from .preset import Preset
from .world_book import WorldBook, WorldBookEntry
from .stats import ApiStats
from .app_config import AppConfig, default_app_config
from .render_rules import (
    RenderRule, RenderRulesConfig,
    SCOPE_AI, SCOPE_USER, SCOPE_ALL, SCOPE_LABELS,
    default_rules, default_config,
)
from .memory_preset import (
    MemoryPreset, default_memory_preset,
)
from .summary_preset import SummaryPreset, default_summary_preset, DEFAULT_SUMMARY_SYSTEM_PROMPT
from .danbooru_preset import (
    DanbooruPreset, default_danbooru_preset, parse_tag_output,
    DEFAULT_DANBOORU_SYSTEM_PROMPT, DEFAULT_DANBOORU_NSFW_PROMPT,
    DEFAULT_NEGATIVE_PROMPT, DEFAULT_POSITIVE_PREFIX,
)
from .danbooru_category import (
    DANBOORU_CATEGORIES, DANBOORU_CATEGORY_LIST, category_label,
)
from .tts_voice import TtsVoice, default_tts_voice
from .tts_rule import TtsRule, TtsRulesConfig, default_tts_rules
from .tts_preset import (
    TtsPreset, default_tts_preset,
    DEFAULT_TTS_EMOTION_SYSTEM_PROMPT,
)
from .world_sim_preset import (
    WorldSimPreset, default_world_sim_preset,
    CUTOUT_BG_COLORS, CUTOUT_BG_LABELS,
    DEFAULT_WORLDSIM_SETTLE_SYSTEM_PROMPT,
    DEFAULT_WORLDSIM_NARRATIVE_SYSTEM_PROMPT, DEFAULT_WORLDSIM_SIM_SYSTEM_PROMPT,
    DEFAULT_WORLDSIM_SHOP_SYSTEM_PROMPT,
    DEFAULT_STOCK_MARKET_PROMPT, DEFAULT_AUCTION_GENERATE_PROMPT,
    WORLDSIM_NARRATIVE_IMG_REQ, WORLDSIM_NARRATIVE_IMG_TAIL,
    DEFAULT_NPC_MEMORY_SUMMARY_PROMPT, DEFAULT_NPC_MEMORY_HYBRID_PROMPT,
    DEFAULT_NPC_MEMORY_BATCH_SUMMARY_PROMPT, DEFAULT_NPC_MEMORY_BATCH_HYBRID_PROMPT,
)
from .shop import Shop, ShopStockEntry
from .world import (
    World, WorldLore, Faction, Location, NPC, Item, Quest, PlayerState, WorldEvent,
    ResourceNode, Skill, Recipe, Talent, Home, Pet, Dungeon, DungeonFloor, DungeonRoom,
    WorldBoss, BossForm, Place, PlayerDomain,
    Commodity, StockMarket,
    AuctionLot, AuctionEvent,
    StoryArc, StoryStage,
    reputation_title,
)
from .scene import SceneLog, SceneEntry
from .scene_image_state import SceneImageState
# P3 战斗引擎数据快照（供 service/UI 用，本身无 to_dict/from_dict，纯传输对象）
from src.services.combat_engine import CombatSnapshot, AttackResult, XpResult

__all__ = [
    "Character", "User", "Message", "Session", "ApiConfig", "Preset",
    "WorldBook", "WorldBookEntry", "ApiStats",
    "AppConfig", "default_app_config",
    "RenderRule", "RenderRulesConfig",
    "SCOPE_AI", "SCOPE_USER", "SCOPE_ALL", "SCOPE_LABELS",
    "default_rules", "default_config",
    "MemoryPreset", "default_memory_preset",
    "SummaryPreset", "default_summary_preset", "DEFAULT_SUMMARY_SYSTEM_PROMPT",
    "DanbooruPreset", "default_danbooru_preset", "parse_tag_output",
    "DEFAULT_DANBOORU_SYSTEM_PROMPT", "DEFAULT_DANBOORU_NSFW_PROMPT",
    "DEFAULT_NEGATIVE_PROMPT", "DEFAULT_POSITIVE_PREFIX",
    "DANBOORU_CATEGORIES", "DANBOORU_CATEGORY_LIST", "category_label",
    "TtsVoice", "default_tts_voice",
    "TtsRule", "TtsRulesConfig", "default_tts_rules",
    "TtsPreset", "default_tts_preset", "DEFAULT_TTS_EMOTION_SYSTEM_PROMPT",
    "WorldSimPreset", "default_world_sim_preset",
    "CUTOUT_BG_COLORS", "CUTOUT_BG_LABELS",
    "DEFAULT_WORLDSIM_SETTLE_SYSTEM_PROMPT",
    "DEFAULT_WORLDSIM_NARRATIVE_SYSTEM_PROMPT", "DEFAULT_WORLDSIM_SIM_SYSTEM_PROMPT",
    "DEFAULT_WORLDSIM_SHOP_SYSTEM_PROMPT", "DEFAULT_STOCK_MARKET_PROMPT",
    "DEFAULT_AUCTION_GENERATE_PROMPT", "Shop", "ShopStockEntry",
    "WORLDSIM_NARRATIVE_IMG_REQ", "WORLDSIM_NARRATIVE_IMG_TAIL",
    "DEFAULT_NPC_MEMORY_SUMMARY_PROMPT", "DEFAULT_NPC_MEMORY_HYBRID_PROMPT",
    "DEFAULT_NPC_MEMORY_BATCH_SUMMARY_PROMPT", "DEFAULT_NPC_MEMORY_BATCH_HYBRID_PROMPT",
    "World", "WorldLore", "Faction", "Location", "NPC", "Item", "Quest", "PlayerState",
    "StoryArc", "StoryStage",
    "WorldEvent", "ResourceNode", "Skill", "Recipe", "Talent", "reputation_title", "Home", "Pet",
    "PlayerDomain",
    "Dungeon", "DungeonFloor", "DungeonRoom",
    "WorldBoss", "BossForm", "Place",
    "Commodity", "StockMarket",
    "AuctionLot", "AuctionEvent",
    "SceneLog", "SceneEntry",
    "SceneImageState",
    "CombatSnapshot", "AttackResult", "XpResult",
]