"""世界模拟场景交互页（P2 场景交互循环，§21）。

WorldSceneView 两栏：
  左栏（固定宽）：地点背景图 + 地点名/描述/危险度 + [前往其他地点] + 在场 NPC 头像横排（点击开交互面板）+ 玩家状态卡。
  右栏（stretch）：NarrationLog（旁白流，复用 MessageBubble）+ 底部行动栏（选项按钮 + 自由输入 + 发送/停止）。

NarrationLog 复用 MessageBubble + 瞬态 Message（零 DB 耦合，ChatView/MessageBubble 从不碰 storage）：
  - 玩家行动 = Message(role="user") 右泡
  - 旁白 = Message(role="assistant", character_name="旁白") 左泡
  - 流式：start_streaming 占位泡 -> append_streaming(chunk) -> finalize_streaming(full)
  - 配色走 §13 render_to_document 契约（AutoHeightTextBrowser + markup/markdown/auto）

worker 生命周期（守 §15）：场景页持有 SceneWorker，back/close/切页 disconnect+cancel+wait。
"""
from __future__ import annotations

import hashlib
import os

from PySide6.QtCore import Qt, Signal, QTimer, QPoint, QRect, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QFrame, QMessageBox, QProgressBar, QGridLayout,
    QLayout, QInputDialog,
    QMenu,
)

from src.config import paths
from src.models import World, SceneLog, Message, WorldSimPreset
from src.models.world_sim_preset import GenreText
from src.ui.widgets.message_bubble import MessageBubble
from src.ui.widgets.card_grid import make_card_pixmap
from src.ui.dialogs.scene_worker import SceneWorker
from src.ui.dialogs.world_map_dialog import WorldMapDialog
from src.ui.dialogs.npc_interaction_dialog import NpcInteractionDialog
from src.ui.dialogs.dice_check_overlay import DiceCheckOverlay
from src.services import talent_engine as te
from src.services.world_sim_service import fmt_time_weather_badge


# ==================== [开局 30 分钟改造包 2026-09-26] 引导层（纯函数，测试勿构造重型视图） ====================

_INPUT_PLACEHOLDER_DEFAULT = "自由输入行动…（回车发送）"

# 首回合示例行动池：key = config_overlay.attribute_template_id（6 内置题材），
# 值按动作类别分组（observe 看四周 / shop 逛店 / talk 打听 / gather 采集）。
# [!] 示例必须是**当场真能做成的事**（2026-09-26 用户指出）：ResourceNode 只挂野外点，
# 出生聚落没有资源点——采集类示例照抄必受阻，所以类别是否进候选由
# example_action_hint 按当前位置实际内容门控，本表只存文案不决定可用性。
# 读取处 `.get(gid) or _GENRE_EXAMPLE_ACTIONS["western_fantasy"]` 兜底（守 §23）。
_GENRE_EXAMPLE_ACTIONS: dict[str, dict[str, str]] = {
    "western_fantasy": {
        "observe": "看看四周有什么",
        "shop": "去商铺逛逛买点补给",
        "talk": "向酒馆老板打听附近的消息",
        "gather": "采些草药",
    },
    "xianxia": {
        "observe": "看看四周有什么",
        "shop": "去集市逛逛买点干粮",
        "talk": "向路人打听附近的传闻",
        "gather": "采几株药草",
    },
    "wuxia": {
        "observe": "看看四周有什么",
        "shop": "去镇上的铺子买些干粮",
        "talk": "向店家打听江湖近况",
        "gather": "采些草药",
    },
    "modern": {
        "observe": "看看四周有什么",
        "shop": "去附近的便利店买点吃的",
        "talk": "跟旁边的人聊几句近况",
        "gather": "捡些能用的材料",
    },
    "scifi": {
        "observe": "检查一下随身装备",
        "shop": "去补给站换些物资",
        "talk": "向站台的技术员打听消息",
        "gather": "收集些可用的材料",
    },
    "apocalypse": {
        "observe": "观察四周有没有危险的动静",
        "shop": "用物资换些补给",
        "talk": "向幸存者打听外面的情况",
        "gather": "搜刮些能用的材料",
    },
}


def example_action_hint(world: World) -> str:
    """首回合输入框 ghost text：按当前位置**实际可执行**的行动挑示例（md5 锚 world.id
    确定性，同世界同现场恒同一条不闪变）。

    候选类别按世界现状门控：商店/在场 NPC/资源点存在才给对应示例；万能项
    「看看四周」恒可选，「去相邻地点看看」有相邻地点时进候选（移动永远可行）。
    自定义题材回退西幻文案池（守 §23 回退规则）。"""
    ov = getattr(world, "config_overlay", None) or {}
    gid = str(ov.get("attribute_template_id", "") or "") if isinstance(ov, dict) else ""
    pool = _GENRE_EXAMPLE_ACTIONS.get(gid) or _GENRE_EXAMPLE_ACTIONS["western_fantasy"]
    fb = _GENRE_EXAMPLE_ACTIONS["western_fantasy"]

    player = getattr(world, "player", None)
    locs = list(getattr(world, "locations", None) or [])
    loc = next((l for l in locs if l.id == getattr(player, "location_id", "")), None)
    cands = ["observe"]
    adj = []
    if loc is not None:
        if any(getattr(s, "location_id", "") == loc.id for s in (getattr(world, "shops", None) or [])):
            cands.append("shop")
        if any(getattr(n, "alive", True) and getattr(n, "location_id", "") == loc.id
               for n in (getattr(world, "npcs", None) or [])):
            cands.append("talk")
        if getattr(loc, "resource_nodes", None):
            cands.append("gather")
        adj_ids = set(getattr(loc, "connections", None) or [])
        adj = [l for l in locs if l.id in adj_ids]
        if adj:
            cands.append("move")
    pick = cands[int(hashlib.md5(str(getattr(world, "id", "")).encode()).hexdigest(), 16) % len(cands)]
    if pick == "move" and adj:
        text = f"去「{adj[0].name}」看看"
    else:
        text = pool.get(pick) or fb.get(pick) or fb["observe"]
    return f"试试直接输入：「{text}」— 你可以自由行动（回车发送）"


def scene_is_first_turn(scene: SceneLog) -> bool:
    """玩家还没走过一步（无 player 条目）= 首回合引导期。intro 旁白只写 narrator 条目，
    不算玩家行动。scene 为 None 视为已过引导期（保守不显示）。"""
    if scene is None:
        return False
    return not any(e.role == "player" for e in scene.log)


# 受阻行动「说人话」规则表：按 reason 关键词命中（顺序敏感，specific 在前）。
# 引擎固定短语是模板文案（world_sim_service 的 summary["reason"]），LLM 编的 reason
# 落不到表里就走 unresolved 兜底；resolved=True 的成功回合 reason 不给提示（防噪音）。
_GUIDANCE_RULES: list[tuple[str, str]] = [
    ("已封印", "这座秘境已通关封印，过些天再去也进不来了；地图上若有别的秘境入口可以换一处。"),
    ("冷却中", "这里的资源还没长回来——游戏里过些天会再生。先去别的资源点，或到商店买现成的。"),
    ("枯竭", "这里的资源采完了，过些天会再生。换一处资源点，或去商店买现成的。"),
    ("没有可采集的资源", "资源点都在野外。打开「地图」找带资源标记的野外格过去，或点「探测」碰碰运气。"),
    ("无法采集", "资源点都在野外。打开「地图」找带资源标记的野外格过去，或点「探测」碰碰运气。"),
    ("不相邻", "只能去与当前地点相邻的地方。打开「地图」看看自己的位置，沿路一段段走过去。"),
    ("无法抵达", "只能去与当前地点相邻的地方。打开「地图」看看自己的位置，沿路一段段走过去。"),
    ("不在当前地点", "对方现在不在场。左栏【在场 NPC】是能直接互动的人；去对方所在的地点才能找到他。"),
    ("不在你身边", "对方现在不在当前场所。同地点就点左侧场所名称过去；若在别的地点，打开「地图」前往再交谈。"),
    ("送不到手上", "对方现在不在场，东西送不到。先去对方所在的地点（「地图」可查）。"),
    ("找不到名叫", "没找到这个人。左栏【在场 NPC】列出了当前地点能互动的人，名字照着它说。"),
    ("找不到可交谈的", "没有找到能交谈的这个人。先看左栏【在场 NPC】，也可以去其他场所或地点寻找。"),
    ("找不到「", "没找到这个东西或人。照着左栏在场名单或背包里的名字说，更容易被认出来。"),
    ("并非商人", "不是人人都能做买卖。左栏场所行里的商铺/集市（或带 ◈ 的游商条目）才能交易。"),
    ("没有可收购的商店", "这里没有愿意收这种货的店。去更大的聚落（城镇/城市）碰碰运气。"),
    ("都不出售", "这家店没货。换一家店看看；材料类东西也可以去野外采集或自己锻造。"),
    ("该商店不出售", "这家店没货。换一家店看看；材料类东西也可以去野外采集或自己锻造。"),
    ("背包里没有", "背包里没这件东西。打开「背包」核对名字；可以在商店买、野外采集或战斗掉落获得。"),
    ("你并没有", "你身上没有这件东西。打开「背包」核对名字；可以在商店买、野外采集或战斗掉落获得。"),
    ("不是敌对目标", "对方与你无敌对关系，不能直接动手。想切磋可以先跟对方攀谈，或去野外挑战怪物。"),
    ("已经倒下", "目标已经倒下了，战利品在战斗胜利时已结算；想拿遗物可以直接说「搜刮」。"),
    ("此处没有你的住宅", "这里不是你家。住宅要建在聚落：去聚落地点点「住宅」按钮购房，之后在那里才能休憩。"),
    ("交易意图缺少物品名", "把要买卖的东西说清楚些，比如「买一把铁剑」「把这袋药材卖给他」。"),
    ("未知交易方式", "把买卖说得更直白些：买 / 卖 / 用物换物，比如「买一袋米」「把这批皮甲卖了」。"),
    ("余额不足", "身上的钱不够。卖掉战利品或采集品换钱，也可以接任务赚赏金（「议程」里有线索）。"),
    ("背包已满", "背包装不下了。把杂物卖掉或存进据点仓库，腾出格子再来。"),
    ("已售罄", "这件刚卖完了。等商店补货，或换一家店看看。"),
    ("背包中没有", "你身上没有这件东西。打开「背包」核对名字；可以在商店买、野外采集或战斗掉落获得。"),
    ("送礼须说清", "把话说完整：送给谁、送什么（背包里的东西名），比如「把止血草送给王大夫」。"),
    ("未指定攻击目标", "先说明要攻击谁。左栏【在场 NPC】列出了当前地点的人物。"),
    ("未指定目标地点", "说明你想去哪儿。打开「地图」能看到相邻的地点名。"),
    ("无当前地点", "你现在不在任何地点。回主界面刷新，或重进世界。"),
]


def blocked_guidance(intent: dict | None, reason: str) -> str:
    """行动受阻时给玩家的「怎么办」提示（UI 层引导，不入场景日志/LLM 上下文）。

    规则表按引擎固定短语匹配；LLM 自判受阻（resolved=False）走通用兜底——
    引导层只教玩家「下一步可以做什么」，不改写引擎结论（守结算真相）。
    """
    reason = (reason or "").strip()
    if not reason:
        return ""
    for kw, hint in _GUIDANCE_RULES:
        if kw in reason:
            return hint
    # 「无法与」同时会出现在「无法与他当面相见」和「无法与某人交易」中；
    # 必须结合结算意图分流，不能把异地交谈误引到商铺。
    if "无法与" in reason:
        if intent and intent.get("intent_type") == "trade":
            return "对方没法跟你做成这笔交易。左栏场所行里的商铺/集市才能买卖。"
        return ("现在没法当面见到对方。同地点就点左侧场所名称过去；"
                "若在别的地点，打开「地图」前往，或托人传话。")
    if not intent or not intent.get("resolved", True):
        return ("这一步没有成功。试试把行动拆得更具体——去哪里 / 找谁 / 做什么；"
                "也可以输入「看看四周」观察环境，或点「议程」看看手头等着做的事。")
    return ""


class FlowLayout(QLayout):
    """流式换行布局：子件按可用宽度自动换行（Qt 无内建 flow layout）。

    左栏固定宽 + 横向滚动条 AlwaysOff 的约束下，单行 HBox 的徽章行/场所芯片行
    一旦总宽超栏宽就会被裁切——流式换行让内容永远收在栏宽内（标准 Qt 示例移植）。
    """

    def __init__(self, parent=None, margin: int = 0, h_spacing: int = 4, v_spacing: int = 4):
        super().__init__(parent)
        if margin >= 0:
            self.setContentsMargins(margin, margin, margin, margin)
        self._h_space = h_spacing
        self._v_space = v_spacing
        self._items: list = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect, test_only):
        x = rect.x()
        y = rect.y()
        line_height = 0
        for item in self._items:
            w = item.sizeHint().width()
            h = item.sizeHint().height()
            next_x = x + w + self._h_space
            if next_x - self._h_space > rect.right() and line_height > 0:
                x = rect.x()
                y += line_height + self._v_space
                next_x = x + w + self._h_space
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(w, h)))
            x = next_x
            line_height = max(line_height, h)
        return y + line_height - rect.y()


class NarrationLog(QScrollArea):
    """旁白流日志（复用 MessageBubble + 瞬态 Message）。"""

    # [P23] 右键旁白气泡：emit (entry_id, global_pos)；entry_id 为 None 时不响应（图片/分隔/占位期）
    entry_context_menu = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bubbles: list[MessageBubble] = []
        self._entry_bubbles: dict[str, MessageBubble] = {}   # [P23] entry_id -> bubble
        self._entry_image_bubbles: dict[str, list[MessageBubble]] = {}
        self._max_bubble_width = 600
        self._streaming_bubble: MessageBubble | None = None
        # 定时器归属控件：场景页销毁时一并取消，避免 singleShot 回调访问已销毁的 C++ 对象。
        self._scroll_now = QTimer(self)
        self._scroll_now.setSingleShot(True)
        self._scroll_now.timeout.connect(self._do_scroll)
        self._scroll_late = QTimer(self)
        self._scroll_late.setSingleShot(True)
        self._scroll_late.timeout.connect(self._do_scroll)

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(16, 12, 16, 12)
        self._layout.setSpacing(10)
        self._layout.addStretch()
        self.setWidget(self._container)

    def set_max_bubble_width(self, width: int):
        # 0.86：宽屏下减少气泡右侧死区（旧 0.78 在 1920 屏留 ~300px 空白），窄屏仍可读
        w = max(300, int(width * 0.86))
        if w == self._max_bubble_width:
            return
        self._max_bubble_width = w
        for b in self._bubbles:
            b.set_max_width(w)

    def clear_messages(self):
        # 移除 stretch 之前的所有 widget
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._bubbles.clear()
        self._entry_bubbles.clear()
        self._entry_image_bubbles.clear()
        self._streaming_bubble = None

    def _connect_entry_menu(self, bubble: MessageBubble, entry_id: str | None):
        """[P23] 连接气泡右键菜单信号：entry_id 非 None 时 emit 给外层建菜单。"""
        bubble._entry_id = entry_id
        def _on_menu(_msg, pos):
            eid = getattr(bubble, "_entry_id", None)
            if eid:
                self.entry_context_menu.emit(eid, pos)
        bubble.context_menu_requested.connect(_on_menu)

    def add_entry(self, role: str, content: str, name: str = "", entry_id: str | None = None):
        """追加一条已完成的日志条目。entry_id 非 None 时登记映射 + 连右键菜单。"""
        msg = self._make_msg(role, content, name)
        bubble = MessageBubble(msg, max_width=self._max_bubble_width)
        self._bubbles.append(bubble)
        self._connect_entry_menu(bubble, entry_id)
        if entry_id:
            self._entry_bubbles[entry_id] = bubble
        self._layout.insertWidget(self._layout.count() - 1, bubble)
        self.scroll_to_bottom()

    def add_image(self, image_path: str, caption: str = "", entry_id: str | None = None):
        """追加一条纯图片气泡（场景事件生图，仿 ChatView.add_image）。
        复用 MessageBubble 的 image_path 自动渲染（message_bubble.py:233），零侵入。
        不入场景日志（场景日志只记对话流，图片关联保存在 SceneImageState）。
        """
        if not image_path:
            return
        msg = self._make_msg("assistant", caption, "旁白")
        msg.image_path = image_path
        msg.is_image_only = True
        bubble = MessageBubble(msg, max_width=self._max_bubble_width)
        self._bubbles.append(bubble)
        if entry_id:
            self._entry_image_bubbles.setdefault(entry_id, []).append(bubble)
        self._layout.insertWidget(self._layout.count() - 1, bubble)
        self.scroll_to_bottom()

    def start_streaming(self, name: str = "旁白"):
        """开始一个流式旁白气泡（占位，等待 chunk）。占位期 _entry_id=None 不响应右键删除。"""
        msg = self._make_msg("assistant", "", name)
        bubble = MessageBubble(msg, max_width=self._max_bubble_width)
        self._bubbles.append(bubble)
        self._connect_entry_menu(bubble, None)
        self._streaming_bubble = bubble
        self._layout.insertWidget(self._layout.count() - 1, bubble)
        self.scroll_to_bottom()

    def attach_streaming_entry(self, entry_id: str):
        """[P23] 流式旁白定稿后回填 entry_id：登记映射并启用右键删除。
        scene.append 返回 entry 后调用（占位泡已存在，此时才与 SceneEntry 绑定）。"""
        if self._streaming_bubble is None or not entry_id:
            return
        self._streaming_bubble._entry_id = entry_id
        self._entry_bubbles[entry_id] = self._streaming_bubble

    def remove_entry_bubble(self, entry_id: str):
        """[P23] 按 entry_id 移除单条气泡（右键删除用，复用 _remove_bubble）。"""
        bubble = self._entry_bubbles.pop(entry_id, None)
        if bubble is not None:
            self._remove_bubble(bubble)
        for image_bubble in self._entry_image_bubbles.pop(entry_id, []):
            self._remove_bubble(image_bubble)

    def append_streaming(self, chunk: str):
        if self._streaming_bubble is not None:
            self._streaming_bubble.append_content(chunk)
            self.scroll_to_bottom()

    def finalize_streaming(self, full_text: str):
        """流式结束：用完整文本最终渲染（保证闭合正确）。

        [!] 空文本时移除占位泡（防空气泡残影，守审查 A1/A3）。
        """
        if self._streaming_bubble is None:
            return
        if full_text:
            self._streaming_bubble.update_content(full_text)
        else:
            # 空旁白：移除占位泡，不留空气泡
            self._remove_bubble(self._streaming_bubble)
        self._streaming_bubble = None
        self.scroll_to_bottom()

    def cancel_streaming_keep(self):
        """取消但保留已收旁白（守 §5）。

        [!] 占位泡若有内容（已 append 过 chunk）保留；若仍为空则移除（防空气泡残影）。
        """
        b = self._streaming_bubble
        self._streaming_bubble = None
        if b is not None and not (getattr(b, "_stream_text", "") or "").strip():
            # 占位泡从未收到 chunk，移除防空气泡
            self._remove_bubble(b)

    def _remove_bubble(self, bubble: MessageBubble):
        """从布局与列表移除一个气泡并 deleteLater。"""
        try:
            self._bubbles.remove(bubble)
        except ValueError:
            pass
        self._layout.removeWidget(bubble)
        bubble.setParent(None)
        bubble.deleteLater()

    @staticmethod
    def _make_msg(role: str, content: str, name: str) -> Message:
        return Message(
            role=role,
            content=content,
            character_name=name or ("你" if role == "user" else "旁白"),
            tokens=0,
        )

    def scroll_to_bottom(self, delay: bool = True):
        if delay:
            self._scroll_now.start(0)
            self._scroll_late.start(50)
        else:
            self._do_scroll()

    def _do_scroll(self):
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def showEvent(self, event):
        super().showEvent(event)
        # [!] 重新可见时补滚到底（切回世界模拟 tab / 再次进入场景）：
        # SceneWorker 不随主 tab 切换取消，隐藏期间旁白气泡继续追加，
        # 但隐藏时布局与滚动条 range 冻结，追加时的 scroll_to_bottom 定时器
        # 取到的 maximum 是旧值，滚不到新消息；重新可见后补一次（同
        # main_window._on_tab_changed 会话页 idx 2 的补滚口径）。
        self.scroll_to_bottom()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 仿 ChatView 宽度算法：self.width() - 滚动条宽（viewport 在滚动条显隐时跳变）
        self.set_max_bubble_width(self.width() - self.verticalScrollBar().width() - 4)


def _npc_log_entries(w, limit: int = 50) -> list:
    """[动态栏过滤 2026-09-11] NPC 动态原始条目（倒序）：[(文本行, 相关 npc_id 列表)]。

    与 _life_log_rows 共用同一过滤/排序/截断口径；这里额外带出 WorldEvent.npcs，
    供「按人物过滤」做结构化匹配（不靠标题文本猜人名）。"""
    evs = list(getattr(w, "event_log", None) or [])
    out = []
    for e in reversed([e for e in evs if getattr(e, "category", "") == "npc"][-limit:]):
        row = (f"[第{getattr(e, 'tick', 0)}回合] {getattr(e, 'title', '')}："
               f"{(getattr(e, 'desc', '') or '')[:60]}")
        ids = [str(i) for i in (getattr(e, "npcs", None) or []) if i]
        out.append((row, ids))
    return out


def _life_log_rows(w, limit: int = 50) -> tuple:
    """[P36c] 右侧动态日志栏数据：world.event_log 按 category 过滤最近 N 条，
    倒序返回 (npc_rows, faction_rows) 文本行。纯函数可单测。"""
    npc_rows = [row for row, _ids in _npc_log_entries(w, limit)]
    evs = list(getattr(w, "event_log", None) or [])
    fac_rows = []
    for e in reversed([e for e in evs if getattr(e, "category", "") == "faction_war"][-limit:]):
        fac_rows.append(f"[第{getattr(e, 'tick', 0)}回合] {getattr(e, 'title', '')}："
                        f"{(getattr(e, 'desc', '') or '')[:60]}")
    return npc_rows, fac_rows


def _combat_log_rows(w, limit: int = 30) -> tuple:
    """[战报 2026-08-28] 战斗历史行（world.combat_history 倒序）+ 汇总行。

    返回 (rows, summary_line)。行格式：
    [第3天·回合12] 胜 · 德雷克 Lv13 · 8回合 · 输出412/承受156 · 得45经验 28<本题材币种>
    掉落与助战同伴在行内续行。汇总行：总场次/胜率/累计输出与承受。纯函数可单测。"""
    hist = [h for h in (getattr(w, "combat_history", None) or []) if isinstance(h, dict)]
    if not hist:
        return [], ""
    currency = GenreText(getattr(w, "config_overlay", None)).currency
    rows = []
    for h in reversed(hist[-limit:]):
        state = {"won": "胜", "lost": "败", "fled": "逃"}.get(str(h.get("state", "")), "?")
        head = (f"[第{h.get('day', 1)}天·回合{h.get('tick', 0)}] {state} · "
                f"{h.get('enemy', '?')} Lv{h.get('level', 1)} · "
                f"{h.get('rounds', 0)}回合 · 输出{h.get('dmg_dealt', 0)}/承受{h.get('dmg_taken', 0)}")
        tail = []
        xp = int(h.get("xp", 0) or 0)
        gold = int(h.get("gold", 0) or 0)
        if xp or gold:
            tail.append(f"得{xp}经验{' ' + str(gold) + currency if gold else ''}".strip())
        loot = str(h.get("loot", "") or "")
        if loot:
            tail.append(f"掉落：{loot}")
        # [P47 后果层] 败北被夺读数（引擎在 finish_combat/_resolve_combat 写 "lost"）
        lost = str(h.get("lost", "") or "")
        if lost:
            tail.append(lost)
        allies = h.get("allies") or []
        if allies:
            tail.append(f"同伴：{'、'.join(str(a) for a in allies)}")
        line = head + (" · " + " · ".join(tail) if tail else "")
        rows.append(line)
    n = len(hist)
    wins = sum(1 for h in hist if h.get("state") == "won")
    dealt = sum(int(h.get("dmg_dealt", 0) or 0) for h in hist)
    taken = sum(int(h.get("dmg_taken", 0) or 0) for h in hist)
    summary = f"共 {n} 战 · 胜率 {int(wins * 100 / n)}% · 累计输出 {dealt} / 承受 {taken}"
    return rows, summary


class WorldSceneView(QWidget):
    """场景交互页：左栏场景信息 + 右栏旁白流与行动栏。"""

    back_requested = Signal()
    world_changed = Signal()   # 玩家移动后世界状态变了，通知外部可选刷新
    # [P7k8] 永久死亡：玩家 HP 归 0 战斗失败 + permadeath 开启 -> 删世界回首页
    permadeath = Signal(str)

    def __init__(self, world_sim_service, storage, parent=None):
        super().__init__(parent)
        self.svc = world_sim_service
        self.storage = storage
        self._world: World | None = None
        self._scene: SceneLog | None = None
        self._scene_image_state = None
        self._pending_images: list[str] = []
        self._preset: WorldSimPreset | None = None
        self._worker: SceneWorker | None = None
        self._shop_worker = None  # [P6] 商店换货 worker（ShopWorker）
        self._shop_dialog = None  # [游商店实时回显 2026-09-10] 打开中的 ShopDialog 引用（补货完成后重渲染货架）
        self._pending_player_entry = None  # [P29] 待回填 talk_to 的玩家条目（worker 产出 intent 后回填 meta）
        # [断网韧性 P45(3)] 回合事务性：快照（启动 worker 前拍摄）+ 失败回合 stash（右键重试）
        self._turn_snapshot = None        # (world.to_dict(), scene.to_dict()) 或 None
        self._last_turn = None            # {"player_action", "preset_intent", "entry_id"} 本回合信息
        self._failed_turn = None          # 失败未重试的回合（rolled_back 后 stash，成功/新回合清除）
        self._busy = False
        self._back_when_stopped = False
        self._permadeath_handled_id = None
        # [动态栏过滤 2026-09-11] 按人物过滤 + 未读红点：显式状态位（勿依赖 isVisible）
        self._log_open = False
        self._log_seen_tick = None      # None=尚未载入（首次刷新不标红点）
        self._log_filter_npc = ""       # "" = 全部人物；否则按 npc_id 过滤
        # [P6] 墙钟定时器：每 N 分钟给过期商店 LLM 换货（世界「活起来」，Req 1）
        self._shop_timer = QTimer(self)
        self._shop_timer.timeout.connect(self._on_shop_timer)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- 左栏 ----
        self._left = self._build_left()
        outer.addWidget(self._left, 0)

        # ---- 右栏 ----
        self._right = self._build_right()
        outer.addWidget(self._right, 1)

        # ---- [P36c] 右侧动态日志栏（NPC 动态 / 势力日志，默认收起，边条钮切换）----
        self._log_toggle = QPushButton("◀\n动\n态")
        self._log_toggle.setFixedWidth(26)
        self._log_toggle.setToolTip("展开/收起右侧动态栏：NPC 行为日志与势力动向")
        self._log_toggle.clicked.connect(self._toggle_log_panel)
        outer.addWidget(self._log_toggle, 0)
        self._log_panel = self._build_log_panel()
        self._log_panel.hide()
        outer.addWidget(self._log_panel, 0)

    # ============ [P36c] 右侧动态日志栏 ============
    def _build_log_panel(self) -> QWidget:
        from PySide6.QtWidgets import QTabWidget, QListWidget, QListWidgetItem, QComboBox
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 6, 4, 4)
        lay.setSpacing(4)
        self._log_tabs = QTabWidget()
        self._log_npc = QListWidget()
        self._log_npc.setStyleSheet("QListWidget{background:#11131c; color:#c0caf5; "
                                    "border-radius:6px; font-size:12px;}")
        # [!] 条目换行 + 禁横向滚动：单行时「[第N回合] 标题：描述」超出栏宽被裁切不可读
        self._log_npc.setWordWrap(True)
        self._log_npc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._log_faction = QListWidget()
        self._log_faction.setStyleSheet(self._log_npc.styleSheet())
        self._log_faction.setWordWrap(True)
        self._log_faction.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._log_combat = QListWidget()
        self._log_combat.setStyleSheet(self._log_npc.styleSheet())
        self._log_combat.setWordWrap(True)
        self._log_combat.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # [动态栏过滤 2026-09-11] NPC 动态页顶部加「按人物过滤」下拉（数据源 _npc_log_entries）
        npc_page = QWidget()
        npc_lay = QVBoxLayout(npc_page)
        npc_lay.setContentsMargins(0, 0, 0, 0)
        npc_lay.setSpacing(3)
        self._log_filter = QComboBox()
        self._log_filter.setToolTip("只看某个 NPC 的动态（按事件关联的人物过滤）")
        self._log_filter.currentIndexChanged.connect(self._on_log_filter_changed)
        npc_lay.addWidget(self._log_filter)
        npc_lay.addWidget(self._log_npc, 1)
        self._log_tabs.addTab(npc_page, "NPC 动态")
        self._log_tabs.addTab(self._log_faction, "势力日志")
        self._log_tabs.addTab(self._log_combat, "战报")   # [战报] 战斗历史（数值可感性）
        lay.addWidget(self._log_tabs, 1)
        w.setFixedWidth(360)   # 268->360：换行 + 加宽后默认无需横向拖动（用户反馈 300 仍显示不全）
        return w

    def _toggle_log_panel(self):
        # [!] 显式状态位判展开——isVisible() 在父级未 show（启动瞬间/offscreen）时恒 False，
        # 连点两次会双双变成「展开」收不回去
        self._log_open = not getattr(self, "_log_open", False)
        self._log_panel.setVisible(self._log_open)
        if self._log_open:
            w = getattr(self, "_world", None)
            if w is not None:
                self._log_seen_tick = self._log_marker(w)   # 展开即清未读
        self._log_toggle.setText("动\n态\n▶" if self._log_open else "◀\n动\n态")

    def _log_marker(self, w):
        """未读水位 = 面板实际展示的两类事件的 (最大 tick, 行数)。

        [!] 只统计 npc/faction_war（面板展示的），否则 economy/event 等不产生新行的
        事件会把水位抬高 -> 假红点；用 (tick, 行数) 二元组，同 tick 内新增多条也能察觉；
        event_log 头部裁剪致行数回落时小于旧值，不会误报。"""
        evs = [e for e in (getattr(w, "event_log", None) or [])
               if getattr(e, "category", "") in ("npc", "faction_war")]
        if not evs:
            return (0, 0)
        return (max(int(getattr(e, "tick", 0) or 0) for e in evs), len(evs))

    def _populate_log_filter(self, w, entries) -> None:
        """按当前 NPC 动态条目重建「全部人物 + 命中人物」下拉，并保持原选中项。"""
        name_by_id = {n.id: n.name for n in (getattr(w, "npcs", None) or [])}
        ids = []
        for _row, eids in entries:
            for i in eids:
                if i not in ids:
                    ids.append(i)
        keep = getattr(self, "_log_filter_npc", "")
        self._log_filter.blockSignals(True)
        self._log_filter.clear()
        self._log_filter.addItem("全部人物", "")
        for i in ids:
            self._log_filter.addItem(name_by_id.get(i, i), i)
        idx = self._log_filter.findData(keep)
        self._log_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self._log_filter.blockSignals(False)
        self._log_filter_npc = self._log_filter.currentData() or ""

    def _on_log_filter_changed(self, _idx: int) -> None:
        self._log_filter_npc = (self._log_filter.currentData() or "")
        self.refresh_log_panel()

    def refresh_log_panel(self):
        """从 world.event_log 过滤最近 50 条（NPC 动态=category npc；势力=faction_war）。
        [动态栏过滤 2026-09-11] NPC 页支持按人物过滤；收起时若有比已读水位更新的事件 ->
        边条钮标红点（首次载入不标，展开即清）。
        [!] world 为空时静默；过滤逻辑在 _npc_log_entries/_life_log_rows 纯函数（可单测）。"""
        w = getattr(self, "_world", None)
        if w is None or not getattr(self, "_log_panel", None):
            return
        try:
            entries = _npc_log_entries(w)
            self._populate_log_filter(w, entries)
            sel = getattr(self, "_log_filter_npc", "")
            npc_rows = [row for row, ids in entries if not sel or sel in ids]
            _, fac_rows = _life_log_rows(w)
            cb_rows, cb_summary = _combat_log_rows(w)
            from PySide6.QtWidgets import QListWidgetItem
            self._log_npc.clear()
            self._log_faction.clear()
            self._log_combat.clear()
            for txt in npc_rows:
                self._log_npc.addItem(QListWidgetItem(txt))
            for txt in fac_rows:
                self._log_faction.addItem(QListWidgetItem(txt))
            if cb_summary:
                self._log_combat.addItem(QListWidgetItem(cb_summary))
            for txt in cb_rows:
                self._log_combat.addItem(QListWidgetItem(txt))
            # 未读红点：首次载入不标；展开期间可见即已读（避免收起后弹假红点）
            marker = self._log_marker(w)
            seen = getattr(self, "_log_seen_tick", None)
            if seen is None or getattr(self, "_log_open", False):
                self._log_seen_tick = marker
            elif marker > seen:
                self._log_toggle.setText("●\n动\n态")
            else:
                self._log_toggle.setText("◀\n动\n态")
        except Exception:
            pass

    # ============ 左栏 ============
    def _build_left(self) -> QWidget:
        w = QScrollArea()
        w.setWidgetResizable(True)
        w.setFrameShape(QFrame.NoFrame)
        w.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 420：380 装不下「场所标签+3 芯片」单行与 4 列带标记头像（内容被裁切）；
        # 加宽后徽章/芯片仍走流式换行兜底，窄内容不浪费
        w.setFixedWidth(420)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(16, 14, 12, 14)
        lay.setSpacing(10)

        # 顶栏：返回
        top = QHBoxLayout()
        self.back_btn = QPushButton("← 返回")
        self.back_btn.clicked.connect(self._on_back)
        top.addWidget(self.back_btn)
        top.addStretch()
        lay.addLayout(top)

        # [P5] 氛围徽章行：节日 / 时间天气 / 回合数 / 危险度 / 战斗模式（沉浸感增强）
        # 流式换行：历法日期徽章较长，单行 HBox 在窄栏会溢出裁切
        self.atmosphere_row = QWidget()
        ar_l = FlowLayout(self.atmosphere_row, 0, 6, 4)
        # [P24b] 节日徽章：今日节日时显示（金色 warn 档），无节日隐藏
        # [P18] 时间天气徽章：历法日期（第X年春二月十五）· 昼夜相位 · 天气
        self.atm_time = QLabel("第1天 · 白昼 · 晴朗")
        self.atm_time.setObjectName("atmosphereBadge")
        self.atm_time.setProperty("badgeLevel", "info")
        self.atm_round = QLabel("回合 0")
        self.atm_round.setObjectName("atmosphereBadge")
        self.atm_round.setProperty("badgeLevel", "info")
        self.atm_danger = QLabel("危险度 -")
        self.atm_danger.setObjectName("atmosphereBadge")
        self.atm_danger.setProperty("badgeLevel", "info")
        self.atm_combat = QLabel("模式 -")
        self.atm_combat.setObjectName("atmosphereBadge")
        self.atm_combat.setProperty("badgeLevel", "info")
        for b in (self.atm_time, self.atm_round, self.atm_danger, self.atm_combat):
            ar_l.addWidget(b)
        lay.addWidget(self.atmosphere_row)

        # [UI 深改 2026-10-01] 当前地点卡（背景图+地点名+描述+场所条 包一张卡，消孤岛断层）
        self.loc_card = QFrame()
        self.loc_card.setObjectName("gameCard")
        loc_cl = QVBoxLayout(self.loc_card)
        loc_cl.setContentsMargins(12, 10, 12, 10)
        loc_cl.setSpacing(6)
        lay.addWidget(self.loc_card)
        # 地点背景图
        self.bg_label = QLabel()
        self.bg_label.setAlignment(Qt.AlignCenter)
        self.bg_label.setFixedHeight(150)
        self.bg_label.setStyleSheet("background:#1a1b26; border-radius:8px; color:#565f89;")
        self.bg_label.setText("（无场景图）")
        loc_cl.addWidget(self.bg_label)

        # 地点信息
        self.loc_title = QLabel("")
        self.loc_title.setObjectName("bannerName")
        loc_cl.addWidget(self.loc_title)
        self.loc_meta = QLabel("")
        self.loc_meta.setWordWrap(True)
        self.loc_meta.setStyleSheet("color:#9aa5ce; font-size:12px;")
        loc_cl.addWidget(self.loc_meta)
        self.loc_desc = QLabel("")
        self.loc_desc.setWordWrap(True)
        self.loc_desc.setStyleSheet("color:#c0caf5;")
        loc_cl.addWidget(self.loc_desc)

        # [P27] 场所信息（面包屑 + 切换条）：当前场所名 + 可点击的相邻场所按钮
        # 芯片走流式换行（独立一行）：旧版与标签同单行 HBox，相邻场所多时溢出栏宽被裁
        self.place_bar = QWidget()
        self.place_bar.setVisible(False)
        pbl = QVBoxLayout(self.place_bar)
        pbl.setContentsMargins(0, 4, 0, 4)
        pbl.setSpacing(4)
        label_row = QHBoxLayout()
        self.place_label = QLabel("")
        self.place_label.setStyleSheet("color:#7dcfff; font-size:12px; font-weight:bold;")
        label_row.addWidget(self.place_label)
        label_row.addStretch()
        pbl.addLayout(label_row)
        self.place_switch_lay = FlowLayout(None, 0, 4, 4)
        pbl.addLayout(self.place_switch_lay)
        loc_cl.addWidget(self.place_bar)

        # 前往其他地点
        self.map_btn = QPushButton("前往其他地点")
        self.map_btn.clicked.connect(self._on_map)
        lay.addWidget(self.map_btn)

        # [P7g] 可采集资源区（动态填充当前地点资源点：名 + 丰度条 + 采集按钮）
        # [UI 深改 2026-10-01] 区块卡片化：金色卡题 + 设计系统卡
        self.resource_header = QLabel("可采集资源")
        self.resource_header.setObjectName("gameCardTitle")
        lay.addWidget(self.resource_header)
        self.resource_container = QFrame()
        self.resource_container.setObjectName("gameCard")
        self.resource_lay = QVBoxLayout(self.resource_container)
        self.resource_lay.setContentsMargins(12, 8, 12, 8)
        self.resource_lay.setSpacing(6)
        lay.addWidget(self.resource_container)

        # 在场 NPC
        self.npc_header = QLabel("在场人物")
        self.npc_header.setObjectName("gameCardTitle")
        lay.addWidget(self.npc_header)
        self.npc_row = QFrame()
        self.npc_row.setObjectName("gameCard")
        # [!] 头像流式换行（QGridLayout 每行 4 个）：单行 HBox 放 10 个 56px 头像必挤爆
        self.npc_row_lay = QGridLayout(self.npc_row)
        self.npc_row_lay.setContentsMargins(10, 8, 10, 8)
        self.npc_row_lay.setSpacing(8)
        self._NPC_COLS = 4
        lay.addWidget(self.npc_row)

        lay.addStretch()

        # 玩家状态卡
        self.player_box = QFrame()
        self.player_box.setObjectName("gameCard")
        pl = QVBoxLayout(self.player_box)
        pl.setContentsMargins(12, 10, 12, 10)
        pl.setSpacing(3)
        _pt = QLabel("你的状态")
        _pt.setObjectName("gameCardTitle")
        pl.addWidget(_pt)
        # P3 HP 进度条 + 数值（P5: objectName 限定 + hpLevel 动态属性切渐变,
        # 不再手写 setStyleSheet，渐变由 theme.qss #hpBar 接管）
        self.hp_bar = QProgressBar()
        self.hp_bar.setObjectName("hpBar")
        self.hp_bar.setTextVisible(True)
        self.hp_bar.setFixedHeight(16)
        pl.addWidget(self.hp_bar)
        # [修 2026-08-28] MP 条：技能耗蓝可视化（用户报缺）
        self.mp_bar = QProgressBar()
        self.mp_bar.setObjectName("mpBar")
        self.mp_bar.setTextVisible(True)
        self.mp_bar.setFixedHeight(14)
        pl.addWidget(self.mp_bar)
        # [遗留#7 2026-08-29] XP 条：经验进度可视化（此前只有文字数字，MP 已有条 XP 没有）
        self.xp_bar = QProgressBar()
        self.xp_bar.setObjectName("xpBar")
        self.xp_bar.setTextVisible(True)
        self.xp_bar.setFixedHeight(12)
        pl.addWidget(self.xp_bar)
        # [饱食度 2026-09-06] 饱食度条（hunger_enabled 的世界才显示；低于 30 变红警示）
        self.injury_lbl = QLabel("")
        self.injury_lbl.setObjectName("injuryLbl")
        self.injury_lbl.setWordWrap(True)
        self.injury_lbl.setStyleSheet("color:#e0688a; font-size:11px;")
        self.injury_lbl.setVisible(False)
        self.hunger_bar = QProgressBar()
        self.hunger_bar.setObjectName("hungerBar")
        self.hunger_bar.setTextVisible(True)
        self.hunger_bar.setFixedHeight(12)
        self.hunger_bar.setFormat("饱食 %p%")
        self.hunger_bar.setVisible(False)
        pl.addWidget(self.hunger_bar)
        pl.addWidget(self.injury_lbl)
        self.player_info = QLabel("")
        self.player_info.setWordWrap(True)
        self.player_info.setStyleSheet("color:#c0caf5;")
        pl.addWidget(self.player_info)
        # 背包按钮
        self.inv_btn = QPushButton("背包")
        self.inv_btn.setToolTip("背包 / 装备")
        self.inv_btn.clicked.connect(self._on_inventory)
        # [P7i] 任务日志按钮
        self.quest_btn = QPushButton("任务日志")
        self.quest_btn.clicked.connect(self._on_quest_log)
        # [P7k6] 图鉴按钮
        self.codex_btn = QPushButton("图鉴")
        self.codex_btn.clicked.connect(self._on_codex)
        # [住宅网格 2026-08-24] 合成/器物按钮移除：锻造/炼丹/洗练功能迁入住宅建筑
        # （右栏功能面板），左栏不再提供独立入口（_on_refine 仍供背包右键链式调用）。
        # [P8] 探测/搜索按钮（手动触发奇遇：intent_type=adventure，消耗 1 回合，概率较低但可重复）
        self.probe_btn = QPushButton("探测")
        self.probe_btn.setToolTip("在此地点搜寻机缘（消耗 1 回合，幸运越高越易触发奇遇）")
        self.probe_btn.clicked.connect(self._on_probe)
        # [P9] 天赋按钮（查看玩家已选天赋详情 + effects）
        self.talent_btn = QPushButton("天赋")
        self.talent_btn.setToolTip("查看已选天赋与加成效果")
        self.talent_btn.clicked.connect(self._on_talent)
        # [技能页 2026-08-28 用户定稿] 技能按钮：已学列表/熟练度/3 装备栏/研读技能书
        self.skill_btn = QPushButton("技能")
        self.skill_btn.setToolTip("技能：查看已学技能与熟练度（施展升级，满 5 级）、装备出战技能栏（3 槽）、研读背包里的技能书")
        self.skill_btn.clicked.connect(self._on_skills)
        # [P10] 好友按钮（好友列表 + 添加好友 + 私聊入口）
        self.friends_btn = QPushButton("好友")
        self.friends_btn.setToolTip("查看好友列表；交情达标的 NPC 可添加好友并私聊")
        self.friends_btn.clicked.connect(self._on_friends)
        # [P7d2] 加点按钮（stat_points > 0 时高亮提示）
        self.attr_btn = QPushButton("加点")
        self.attr_btn.clicked.connect(self._on_attribute)
        # [P24a] 住宅按钮（购宅/仓库/陈列/家具/休憩入口；聚落内置宅，宅中可休憩跳时）
        self.home_btn = QPushButton("住宅")
        self.home_btn.setToolTip("宅邸：在聚落购宅，仓库/陈列/家具/休憩（在宅可睡到次日清晨）")
        self.home_btn.clicked.connect(self._on_home)
        # [P24d] 宠物按钮（驯服的伙伴：出战切换/投食养成入口）
        self.pet_btn = QPushButton("宠物")
        self.pet_btn.setToolTip("宠物：驯服的伙伴跟随冒险，出战自动参战（不占同伴位），投食养亲和")
        self.pet_btn.clicked.connect(self._on_pet)
        # [P25a] 秘境按钮（入口进入/内部深入/封印倒计时；仅当前地点有秘境或在其内部可用）
        self.dungeon_btn = QPushButton("秘境")
        self.dungeon_btn.setToolTip("秘境：逐层探索（战斗/机关/宝藏/陷阱），击败最深处首领通关，"
                                    "封印数日后重生且难度提升")
        self.dungeon_btn.clicked.connect(self._on_dungeon)
        # [P25d] 讨伐按钮（世界 Boss：仅当前地点有未击败在窗 Boss 时可用；多形态递进讨伐）
        self.world_boss_btn = QPushButton("讨伐")
        self.world_boss_btn.setToolTip("世界 Boss：盘踞此地的世界级威胁，多形态递进讨伐；"
                                       "击杀最终形态得传说级掉落 + 全势力声望变动；窗口到期则离去")
        self.world_boss_btn.clicked.connect(self._on_world_boss)
        # [P34f] 股市按钮（城市交易所 venue 入口，每日 LLM 定价题材化大宗商品）
        self.stock_btn = QPushButton("股市")
        self.stock_btn.setToolTip("交易所行情：买卖题材化大宗商品（灵石/精铁/粮食等），"
                                   "价格每日据世界事件/节日/经济变动，可信息套利。仅城市交易所可访问")
        self.stock_btn.clicked.connect(self._on_stock)
        # [P34g] 拍卖会按钮（城市交易所 venue，拍卖会 active 时进入竞价）
        self.auction_btn = QPushButton("拍卖会")
        self.auction_btn.setToolTip("拍卖会：限时竞价珍奇拍品（高 level 未鉴定物品），"
                                     "押金模式出价即扣，被超出退还。仅本城拍卖会进行中可访问")
        self.auction_btn.clicked.connect(self._on_auction)
        # [P39b] 委托订单按钮（聚落订单板：NPC 收购单，生产交付赚钱/交情/声望）
        self.commission_btn = QPushButton("订单")
        self.commission_btn.setToolTip("委托订单板：NPC 发布的收购单（补货/节庆/急缺三类），"
                                       "生产或收集对应物品后到发布聚落当面交付，赚取出价"
                                       "（高于商店收价）+ 交情 + 声望。订单按日过期")
        self.commission_btn.clicked.connect(self._on_commission)
        # [D1 2026-08-29] 据点按钮（购地开辟/资金池存取；野外之地当面购地）
        self.domain_btn = QPushButton("据点")
        self.domain_btn.setToolTip("据点：亲至野外之地斥巨资购地开辟私人聚落（营门+广场），"
                                   "资金池存取供建设与经营周转")
        self.domain_btn.clicked.connect(self._on_domain)
        # [P57 NPC 上门 2026-09-26] 信使按钮：NPC 主动上门的信件/悬赏/战书/求助
        # （引擎每日 roll + 旱涝保底生成，过期不候——pending 有红点提示）
        self.messenger_btn = QPushButton("信使")
        self.messenger_btn.setToolTip("信使：NPC 托人带给你的信件、悬赏、战书与求助。\n"
                                      "世界隔几天就会有人找你——过期不候，尽快处理")
        self.messenger_btn.clicked.connect(self._on_messenger)
        # [P55 今日议程 2026-09-25 三 PM 共识] 聚合「玩家手头的事」：治空白输入框恐惧——
        # 打开即答「现在有什么等着你」（零 LLM，读任务/委托/产线/Boss/伤情/记恨状态）
        self.agenda_btn = QPushButton("议程")
        self.agenda_btn.setToolTip("今日议程：任务进度、剧情线、委托、产线、Boss 窗口、伤情——"
                                   "你手头所有等着处理的事，一眼看完。")
        self.agenda_btn.clicked.connect(self._on_agenda)
        # [饱食度 2026-09-08 用户指示] 用餐按钮已删：恢复饱食度一律走食物店买食物吃
        # （背包「使用」）；食堂堂食/dine 意图同步下线，NPC 改去商店买食物。
        # [!] 功能按钮 16 个 8 行 2 列网格：竖排占高把左栏挤长，
        # 网格化减半高度且列宽统一（按钮文字均 <=4 字，两列在左栏放得下）
        btn_grid = QGridLayout()
        btn_grid.setContentsMargins(0, 6, 0, 0)
        btn_grid.setSpacing(4)
        for i, b in enumerate((self.agenda_btn, self.inv_btn, self.quest_btn, self.codex_btn,
                               self.probe_btn, self.talent_btn, self.skill_btn, self.friends_btn, self.attr_btn,
                               self.home_btn, self.pet_btn, self.dungeon_btn, self.world_boss_btn,
                               self.stock_btn, self.auction_btn,
                               self.commission_btn, self.domain_btn, self.messenger_btn)):
            btn_grid.addWidget(b, i // 2, i % 2)
        pl.addLayout(btn_grid)
        lay.addWidget(self.player_box)

        w.setWidget(inner)
        return w

    # ============ 右栏 ============
    def _build_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 14, 16, 14)
        lay.setSpacing(10)

        # 旁白流
        self.log = NarrationLog()
        self.log.entry_context_menu.connect(self._on_entry_context_menu)   # [P23] 右键删除旁白
        lay.addWidget(self.log, 1)

        # 自由输入 + 发送/停止
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.input = QLineEdit()
        # [UI 深改 2026-10-01] 行动输入框样式化（NarrationLog 属世界模拟自有视图，
        # 非 §21 主链 ChatView——背景槽感+金边聚焦，与设计系统统一）
        self.input.setStyleSheet(
            "QLineEdit{background:#191b26; border:1px solid #2a2d40;"
            " border-radius:8px; padding:8px 12px; color:#d4d9f0; font-size:14px;}"
            "QLineEdit:focus{border:1px solid #c9a86a;}")
        self.input.setPlaceholderText(_INPUT_PLACEHOLDER_DEFAULT)
        self.input.returnPressed.connect(self._on_send_free)
        action_row.addWidget(self.input, 1)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("primaryBtn")
        self.send_btn.clicked.connect(self._on_send_free)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("dangerBtn")
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.hide()
        action_row.addWidget(self.send_btn)
        action_row.addWidget(self.stop_btn)
        lay.addLayout(action_row)

        # 状态提示
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#565f89; font-size:12px;")
        lay.addWidget(self.status_label)

        return w

    # ============ 加载/刷新 ============
    def load_world(self, world: World):
        self._world = world
        self._permadeath_handled_id = None
        self._back_when_stopped = False
        # [动态栏过滤 2026-09-11] 视图跨世界复用：重置未读水位与人物筛选，
        # 否则「首次载入不标红点」只对第一个世界成立（旧世界 tick 更高会长期压制红点）
        self._log_seen_tick = None
        self._log_filter_npc = ""
        self._preset = self.storage.load_world_sim_preset()
        # [!] 旧存档自愈：玩家所在地点必然已发现+已探索（世界生成早期版本没标出生地，
        # 导致地图上出生地永远显示"已发现(暗格)"、周围无迷雾块）。检测到才落盘。
        try:
            dirty = False
            # 旧档天赋耐力/智力已参与战斗，但资源上限仍按裸值存档时补齐。
            from src.services import combat_engine as _ce
            if _ce.sync_player_talent_resources(world.player):
                dirty = True
            cur = next((l for l in world.locations if l.id == world.player.location_id), None)
            if cur is not None and (not cur.discovered or not cur.explored):
                cur.discovered = True
                cur.explored = True
                dirty = True
            # 相邻自动揭示（新拓展区域走近发现）
            if self.svc.reveal_adjacent(world):
                dirty = True
            # [P10c] 补全单向连接的反向边（老存档可能存在不可往返的单向边，复活/移动后卡死）
            if self.svc.ensure_bidirectional_connections(world):
                dirty = True
            # [修 2026-09-13] 建店缺口自愈（旧档 town 有商店类场所零店 -> 场景页无游商）：
            # 补建游商店 + 即时目录备货（零 LLM 不阻塞）；先于 generation_gaps——补出的店
            # 才能承接 collect 材料保底上架；后随 _ensure_shop_places 绑定门面。
            try:
                if self.svc.heal_missing_vendor_shops(world, self._preset):
                    dirty = True
            except Exception:
                pass
            # [修 2026-09-13] 生成一致性自愈（旧档）：聚落 danger 归零 / 零野外兜底 /
            # 怪物池 danger 对齐 / kill 目标空串回填 / collect 材料保底上架。幂等，
            # 无缺口返回 False 不落盘。
            try:
                if self.svc.heal_generation_gaps(world, self._preset):
                    dirty = True
            except Exception:
                pass
            # [修 2026-09-06] 商店-场所配对自愈（旧世界占位店无场所两张皮；幂等）
            if self.svc._ensure_shop_places(world):
                dirty = True
            # [修 2026-09-06 审计] 食物自愈（后开 hunger 的旧世界没跑过 _init_foods，
            # 全图无食物——非城市 NPC 买食路径无物可买；幂等注入物品+货架）
            try:
                if self.svc._ensure_food_supply(world, self._preset):
                    dirty = True
            except Exception:
                pass
            if dirty:
                self.storage.save_world(world)
        except Exception:
            pass
        # 加载或新建场景日志
        self._scene = self.storage.load_scene(world.id)
        if self._scene is None:
            self._scene = SceneLog(world_id=world.id, tick=world.tick_count)
        try:
            self._scene_image_state = self.storage.load_scene_image_state(world.id)
        except (AttributeError, OSError, ValueError):
            self._scene_image_state = None
        self._pending_images.clear()
        # [P36c] 右侧动态日志栏
        self.refresh_log_panel()
        # 渲染已有日志
        self.log.clear_messages()
        # [P15a1] 已压缩的历史以「前情摘要」气泡呈现（原文滚入摘要删除，重开不丢脉络）
        if getattr(self._scene, "summary", ""):
            self.log.add_entry("system", "【前情摘要】" + self._scene.summary, "系统")
        for e in self._scene.log:
            name = {"player": "你", "narrator": "旁白", "system": "系统"}.get(e.role, "")
            self.log.add_entry(e.role, e.content, name, entry_id=e.id)   # [P23] 重建时建映射
            if e.role == "narrator" and self._scene_image_state is not None:
                for filename in self._scene_image_state.images_by_entry.get(e.id, []):
                    image_path = os.path.join(paths.world_images_dir(), filename)
                    if os.path.isfile(image_path):
                        self.log.add_image(image_path, entry_id=e.id)
        self.refresh_state()
        # [P6] 启动商店墙钟补货定时器（世界活起来，Req 1）
        self._start_shop_timer()

        # [开局 30 分钟改造包] 首回合 ghost text：玩家还没走过一步时，输入框显示一条
        # 题材化示例行动（教「这里可以随便打字」）；发出第一个行动后回落默认占位。
        if scene_is_first_turn(self._scene) and self._world is not None:
            self.input.setPlaceholderText(example_action_hint(self._world))
        else:
            self.input.setPlaceholderText(_INPUT_PLACEHOLDER_DEFAULT)

        # 若场景为空，自动起首回合开场
        if not self._scene.log and not self._busy:
            self._start_worker(player_action=None)
        else:
            self._set_busy(False)

    # ============ [P6] 商店墙钟补货 ============
    def _start_shop_timer(self):
        self._shop_timer.stop()
        if not self._world or not self._preset:
            return
        minutes = int(self.svc._per_world(self._world, "shops_wallclock_restock_minutes",
                                          self._preset.shops_wallclock_restock_minutes, self._preset)
                      ) if self.svc else 0
        # 总开关 / 墙钟关 / 无商店 -> 不启动
        enabled = bool(self.svc._per_world(self._world, "shops_enabled",
                                           self._preset.shops_enabled, self._preset)) if self.svc else False
        if not enabled or minutes <= 0 or not self._world.shops:
            return
        self._shop_timer.start(minutes * 60 * 1000)

    def _stop_shop_timer(self):
        self._shop_timer.stop()

    def _on_shop_timer(self):
        """墙钟到期：所有商店换货（LLM，后台 ShopWorker）。

        [P6] 墙钟定时器是「时间驱动」而非 tick 驱动：每 N 分钟到点即给所有商店换货，
        让玩家即使静止探索世界也在变化（Req 1 活世界）。tick 驱动的确定性补货另由 tick_shops 负责。
        仿 §15 worker 生命周期：叙事回合中 / 已有换货任务 -> 跳过本轮。
        """
        if self._busy or self._shop_worker is not None:
            return
        if not self._world or not self._preset or not self._world.shops:
            return
        if not self.svc._per_world(self._world, "shops_llm_restock_enabled",
                                   self._preset.shops_llm_restock_enabled, self._preset):
            return
        stale_ids = [s.id for s in self._world.shops]
        from src.ui.dialogs.shop_worker import ShopWorker
        self._shop_worker = ShopWorker(self.svc, self._world, self._preset, stale_ids, parent=None)
        self._shop_worker.finished_signal.connect(self._on_shop_regen_done)
        self._shop_worker.finished.connect(self._on_shop_worker_done)
        self._shop_worker.start()

    def refresh_state(self, *, allow_encounters: bool = True):
        """刷新左栏场景信息（地点/NPC/玩家）。移动后调用。"""
        if not self._world:
            return
        # [敌袭观战] 读档残留的未打敌袭：非忙时补弹观战（_on_finished 正常路径已即时弹）
        if (allow_encounters and getattr(self._world, "pending_domain_siege", None)
                and not self._busy and not getattr(self, "_siege_dialog_open", False)):
            self._spectate_domain_siege()
            return
        self.refresh_log_panel()          # [P36c] 右侧动态日志栏同步刷新
        w = self._world
        loc = self.svc._current_location(w)
        # [修 2026-09-13 用户报告] 同地点刷新（如游商买卖 on_changed）会重建 NPC 栏/
        # 场所条，QScrollArea 内容瞬时缩致滚动位置被钳回顶部。地点未变时保存/恢复
        # 滚动位置；换地点（move/传送）仍回顶看新场景。
        _sb = self._left.verticalScrollBar()
        _same_loc = getattr(self, "_last_refresh_loc_id", None) == (loc.id if loc else "")
        _scroll_y = _sb.value()
        self._last_refresh_loc_id = loc.id if loc else ""
        # 背景图
        if loc and loc.background:
            pm = QPixmap(os.path.join(paths.world_images_dir(), loc.background))
            if not pm.isNull():
                self.bg_label.clear()
                self.bg_label.setStyleSheet("background:#1a1b26; border-radius:8px;")
                # [!] 封面式缩放 + 居中裁剪到 (视口宽-边距, 150)：
                # 1) 不可用 bg_label.width()——首次刷新时布局未完成，返回默认 640；
                # 2) KeepAspectRatioByExpanding 直接 setPixmap 会让 QLabel 的
                #    minimumSizeHint 跟随 pixmap 实际尺寸（可宽于视口），横向滚动条
                #    又被关死 -> 整个左栏内容右侧被裁掉（"右边不显示"根因）。
                bg_h = 150
                bg_w = max(240, self._left.viewport().width() - 28)
                scaled = pm.scaled(bg_w, bg_h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                if scaled.width() > bg_w or scaled.height() > bg_h:
                    cx = max(0, (scaled.width() - bg_w) // 2)
                    cy = max(0, (scaled.height() - bg_h) // 2)
                    scaled = scaled.copy(cx, cy, min(bg_w, scaled.width()), min(bg_h, scaled.height()))
                self.bg_label.setPixmap(scaled)
            else:
                self.bg_label.setText("（场景图加载失败）")
        else:
            self.bg_label.setText("（无场景图）")
            self.bg_label.setStyleSheet("background:#1a1b26; border-radius:8px; color:#565f89;")
        # 地点信息
        if loc:
            fac = next((f.name for f in w.factions if f.id == loc.faction_id), "无")
            self.loc_title.setText(loc.name)
            self.loc_meta.setText(f"区域:{loc.region or '未知'} | 势力:{fac} | 危险度:{loc.danger}")
            self.loc_desc.setText(loc.desc or "")
        else:
            self.loc_title.setText("未知地点")
            self.loc_meta.setText("")
            self.loc_desc.setText("")
        # [P27] 场所信息：当前场所面包屑 + 相邻场所切换按钮
        self._refresh_place_bar(loc)
        # 在场 NPC
        self._refresh_npcs(loc)
        self._refresh_resources(loc)
        # 玩家
        p = w.player
        ploc = loc.name if loc else "未知"
        # P3 数值显示（crpg 模式）
        combat_system = "crpg"
        if self._preset:
            combat_system = self.svc._per_world(w, "combat_system", self._preset.combat_system, self._preset)
        if combat_system == "crpg":
            hp_max = max(1, p.hp_max)
            hp_pct = max(0, min(100, int(p.hp / hp_max * 100)))
            self.hp_bar.setRange(0, hp_max)
            self.hp_bar.setValue(max(0, p.hp))
            self.hp_bar.setFormat(f"{self.svc._genre_text(w).hp} {max(0, p.hp)}/{max(1, hp_max)}  ({hp_pct}%)")
            # [修 2026-08-28] MP 条刷新（narrative 模式隐藏——无蓝概念）
            mp_max = max(1, int(getattr(p, "mp_max", 0) or 0))
            self.mp_bar.setRange(0, mp_max)
            self.mp_bar.setValue(max(0, int(getattr(p, "mp", 0) or 0)))
            self.mp_bar.setFormat(f"{self.svc._genre_text(w).mp} {max(0, int(getattr(p, 'mp', 0) or 0))}/{mp_max}")
            # [遗留#7] XP 条刷新（narrative 模式同隐藏——无经验概念）
            xp_next = max(1, int(getattr(p, "xp_next", 1) or 1))
            self.xp_bar.setRange(0, xp_next)
            self.xp_bar.setValue(max(0, min(xp_next, int(getattr(p, "xp", 0) or 0))))
            self.xp_bar.setFormat(
                f"XP {max(0, int(getattr(p, 'xp', 0) or 0))}/{xp_next}")
            # [饱食度 2026-09-06] 饱食度条（hunger_enabled 的世界才显示；
            # 低于 30 饥饿线红字警示——此时全属性减半；[2026-09-08] 用餐按钮已删，
            # 恢复饱食度走食物店买食物吃）
            hunger_on = bool(self.svc._per_world(w, "hunger_enabled",
                                                 getattr(self._preset, "hunger_enabled", False),
                                                 self._preset)) if self.svc is not None else False
            self.hunger_bar.setVisible(hunger_on)
            if hunger_on:
                # [修 2026-09-06] 饱食 0 是合法值——`x or 100` 会把 0 当缺省翻成 100，
                # 饿到 0 的玩家反而显示满饱食（真机验收第 34 回合抓到）
                _raw = getattr(p, "hunger", None)
                hg = max(0, min(100, int(_raw if _raw is not None else 100)))
                self.hunger_bar.setValue(hg)
                self.hunger_bar.setFormat(f"饱食 {hg}%{'（饿了！全属性减半）' if hg < 30 else ''}")
                # [修 2026-09-06] 低饱食红条走 hungerLevel 动态属性切 theme.qss 渐变
                # （与 hpLevel 同构；不再内联 setStyleSheet——内联只盖了 ::chunk，
                #  groove 落回原生立体槽样式，12px 高度下与 XP 条视觉重叠）
                self.hunger_bar.setProperty("hungerLevel", "low" if hg < 30 else "ok")
                self.hunger_bar.style().unpolish(self.hunger_bar)
                self.hunger_bar.style().polish(self.hunger_bar)
            # [P52 伤疤] 伤情行（有未愈伤才显示；战败/惨胜结算产生，按天痊愈）
            inj = [i for i in (getattr(p, "injuries", None) or []) if isinstance(i, dict)]
            inj = [i for i in inj if int(i.get("until_day", 0) or 0) > int(getattr(w, "day_count", 1) or 1)]
            self.injury_lbl.setVisible(bool(inj))
            if inj:
                names = "、".join(str(i.get("name", "?")) for i in inj[:2])
                self.injury_lbl.setText(f"伤情：{names}（{len(inj)} 处；悬停查看影响）")
                injury_effect = {
                    "arm": "战斗力量 ×0.9",
                    "legs": "战斗敏捷 ×0.85",
                    "head": "受暴击率 +5%",
                    "torso": "获得治疗 ×0.8",
                }
                day = int(getattr(w, "day_count", 1) or 1)
                self.injury_lbl.setToolTip("\n".join(
                    f"{i.get('name', '?')}：{injury_effect.get(str(i.get('part', '')), '养伤中')}"
                    f"（约 {max(0, int(i.get('until_day', day) or day) - day)} 天后痊愈）"
                    for i in inj
                ))
            else:
                self.injury_lbl.setToolTip("")
            # 战意（P51）：战斗外无意义，仅存值供战斗 UI 读取——场景页不显示
            # [P5] HP 颜色档：动态属性 hpLevel 切 theme.qss 三色渐变
            # （不再手写 setStyleSheet，#hpBar::chunk 接管；属性变化 Qt 重渲染）
            if hp_pct < 30:
                self.hp_bar.setProperty("hpLevel", "low")
            elif hp_pct < 60:
                self.hp_bar.setProperty("hpLevel", "mid")
            else:
                self.hp_bar.setProperty("hpLevel", "high")
            # 重新应用 QSS 让动态属性生效
            self.hp_bar.style().unpolish(self.hp_bar)
            self.hp_bar.style().polish(self.hp_bar)
            # [P5c] 题材化属性名：从 _per_world 读 stat_display_names，缺省回退西幻默认（力/敏/智/耐/运）
            sdn = {"str": "力", "dex": "敏", "int": "智", "vit": "耐", "luk": "运"}
            if self.svc is not None and self._preset is not None:
                got = self.svc._per_world(w, "stat_display_names", sdn, self._preset)
                if isinstance(got, dict):
                    sdn = got
            # 装备计数（已装备 X / 8 槽）
            equipped_count = sum(1 for v in (p.equipped.values() if isinstance(p.equipped, dict) else []) if v)
            # [P6] 题材化货币单位（金币/灵石/信用点...），底层仍是 PlayerState.gold。
            gt = self.svc._genre_text(w) if self.svc is not None else None
            cur = gt.currency if gt is not None else "金币"
            realm = gt.level_name(p.level) if gt is not None else f"Lv{p.level}"
            # [P10b] 同行 NPC（邀请跟随的同伴）；[P25b] 疲劳中标注（休整）
            comp_ids = p.companion_npc_ids or []
            from src.services import npc_reaction_engine as _nre
            comp_names = ("、".join(
                next((n.name + ("(休整)" if _nre.is_fatigued(w, n) else "")
                      for n in w.npcs if n.id == cid), "?")
                for cid in comp_ids)) if comp_ids else "无"
            # [P24d] 出战宠物（跟随参战，不占同伴位）
            from src.services import pet_engine as _pex
            _ap = _pex.active_pet(w)
            pet_txt = f" | 宠物:{_ap.name}(Lv{_ap.level})" if _ap is not None else ""
            # 五维常态值含装备、天赋、固有加成和词缀，括号标来源。
            from src.services import combat_engine as _ce
            def _st(key: str, base: int) -> str:
                return _ce.primary_stat_display(w, p, key)
            self.player_info.setText(
                f"{realm}（{p.level}级） | {p.class_name or '职业未定'}\n"
                f"经验 {p.xp}/{p.xp_next} | {cur}:{p.gold} | 装备 {equipped_count}/8\n"
                f"{sdn.get('str','力')}{_st('str', p.stat_str)} {sdn.get('dex','敏')}{_st('dex', p.stat_dex)} "
                f"{sdn.get('int','智')}{_st('int', p.stat_int)} {sdn.get('vit','耐')}{_st('vit', p.stat_vit)} "
                f"{sdn.get('luk','运')}{_st('luk', p.stat_luk)}\n"
                f"地点:{ploc} | 同行:{comp_names}{pet_txt}\n"
                f"天赋:{te.talent_summary(p) or '无'}"
            )
            # [P9] 天赋后天不可购买：剩余天赋点是开局选择剩下的死值，不再展示；新天赋只经奇遇觉醒
            self.player_info.setToolTip(
                "五维括号：装=装备、赋=天赋、固=固有、词=词缀；显示常态值，饥饿和伤势在战斗时另计。\n"
                "天赋开局选定后无法后天购买；新的天赋只能通过奇遇（参悟秘籍/奇人传承/异变灵泉等）概率觉醒。")
            if p.hp <= 0:
                self.player_info.setText(self.player_info.text() + "\n【你已倒下！】")
            # [P7d2] 可分配属性点提示（attr_btn 高亮显示待加点数）
            sp = int(getattr(p, "stat_points", 0) or 0)
            if sp > 0:
                self.attr_btn.setText(f"加点({sp})")
                self.attr_btn.setStyleSheet("QPushButton{background:#5a3a1f; color:#e0af68; font-weight:bold;}")
            else:
                self.attr_btn.setText("加点")
                self.attr_btn.setStyleSheet("")
            self.hp_bar.show()
            self.mp_bar.show()
            self.xp_bar.show()
            self.inv_btn.show()
        else:
            # narrative 模式：隐藏数值面板，回 P2 显示
            self.hp_bar.hide()
            self.mp_bar.hide()
            self.xp_bar.hide()
            self.inv_btn.hide()
            self.player_info.setText(
                f"职业:{p.class_name or '未定'}\n身世:{p.background or '未定'}\n"
                f"当前地点:{ploc}\n回合:{w.tick_count}"
            )
        # [P3 修 2026-10-01 真机验收] 门控按钮基础 tooltip 一次性缓存：
        # 禁用态在基础文案后追加「当前不可用原因 + 怎么解锁」，新手不用猜按钮为什么是灰的。
        for _btn, _attr in ((self.dungeon_btn, "_tip_dungeon"),
                            (self.world_boss_btn, "_tip_wboss"),
                            (self.stock_btn, "_tip_stock"),
                            (self.auction_btn, "_tip_auction")):
            if not hasattr(self, _attr):
                setattr(self, _attr, _btn.toolTip())

        # [P25a] 秘境按钮启用态：当前地点有秘境入口或在秘境内部才可用
        try:
            from src.services import dungeon_engine as _dge
            _loc = self.svc._current_location(w)
            _has_dg = (_dge.interior_dungeon(w, _loc) is not None
                       or _dge.dungeon_at(w, _loc) is not None)
        except Exception:
            _has_dg = False
        self.dungeon_btn.setEnabled(_has_dg and not self._busy)
        self.dungeon_btn.setToolTip(
            self._tip_dungeon if _has_dg else
            self._tip_dungeon + "\n[当前不可用] 此地没有秘境入口。入口挂在特定地点——"
            "探索迷雾地点、留意传闻与「动态」可能发现")
        # [P25d] 讨伐按钮启用态：当前地点有未击败在窗世界 Boss 才可用
        try:
            from src.services import world_boss_engine as _wbe
            _loc2 = self.svc._current_location(w)
            _has_wb = _wbe.boss_at(w, _loc2) is not None
        except Exception:
            _has_wb = False
        self.world_boss_btn.setEnabled(_has_wb and not self._busy)
        self.world_boss_btn.setToolTip(
            self._tip_wboss if _has_wb else
            self._tip_wboss + "\n[当前不可用] 此地没有活动窗口内的世界 Boss；"
            "Boss 随危机事件盘踞高危险地点，限期在窗（留意「动态」与传闻）")
        # [P34f] 股市按钮启用态：当前地点是城市且有交易所(auction shop) + 股市开关开
        try:
            _loc3 = self.svc._current_location(w)
            _is_city = getattr(_loc3, "settlement_size", "") == "city"
            _has_auction = _is_city and any(
                s.shop_type == "auction"
                for s in self.svc.shops_at_location(w, _loc3.id))
            _stock_on = bool(self.svc._per_world(
                w, "stock_market_enabled", self._preset.stock_market_enabled, self._preset))
        except Exception:
            _is_city = False
            _has_auction = False
            _stock_on = False
        _stock_ok = _has_auction and _stock_on
        self.stock_btn.setEnabled(_stock_ok and not self._busy)
        self.stock_btn.setToolTip(
            self._tip_stock if _stock_ok else
            self._tip_stock + "\n[当前不可用] "
            + ("股市只在大城市开放，当前地点不是大城市（地图上找城市型聚落）" if not _is_city
               else "本城没有交易所场所，换一座更大的城市" if not _has_auction
               else "本世界股市开关未开启（世界模拟设置可改）"))
        # [P34g] 拍卖会按钮启用态：当前城市有进行中的拍卖会 + 拍卖会开关开
        try:
            from src.services import auction_engine as _aue
            _loc4 = self.svc._current_location(w)
            _has_active_auc = (_aue.active_auction_at(w, _loc4.id) is not None)
            _auction_on = bool(self.svc._per_world(
                w, "auction_enabled", self._preset.auction_enabled, self._preset))
        except Exception:
            _has_active_auc = False
            _auction_on = False
        _auc_ok = _has_active_auc and _auction_on
        self.auction_btn.setEnabled(_auc_ok and not self._busy)
        self.auction_btn.setToolTip(
            self._tip_auction if _auc_ok else
            self._tip_auction + "\n[当前不可用] "
            + ("本世界拍卖会开关未开启（世界模拟设置可改）" if not _auction_on
               else "本城当前没有进行中的拍卖会；拍卖会周期性开拍，留意「动态」与议程"))
        # [P57] 信使按钮：pending 有红点；回合进行中禁开（防与 worker 并发改世界）
        try:
            _pend = any(getattr(o, "state", "") == "pending"
                        for o in (getattr(w, "outreaches", None) or []))
        except Exception:
            _pend = False
        self.messenger_btn.setText("信使●" if _pend else "信使")
        self.messenger_btn.setEnabled(not self._busy)

        # [P5] 氛围徽章行刷新（时间天气 / 回合数 / 危险度 / 战斗模式）
        # [定版裁剪 2026-09-05] 节日徽章随节日系统移除
        self.atm_time.setText(fmt_time_weather_badge(w))
        self.atm_round.setText(f"回合 {w.tick_count}")
        self.atm_round.setProperty("badgeLevel", "info")
        self.atm_round.style().unpolish(self.atm_round)
        self.atm_round.style().polish(self.atm_round)
        # 危险度（loc.danger 0-10 档位）
        if loc:
            danger = int(getattr(loc, "danger", 0) or 0)
            if danger >= 7:
                self.atm_danger.setText(f"危险度 {danger}")
                self.atm_danger.setProperty("badgeLevel", "danger")
            elif danger >= 4:
                self.atm_danger.setText(f"危险度 {danger}")
                self.atm_danger.setProperty("badgeLevel", "warn")
            else:
                self.atm_danger.setText(f"危险度 {danger}")
                self.atm_danger.setProperty("badgeLevel", "success")
        else:
            self.atm_danger.setText("危险度 -")
            self.atm_danger.setProperty("badgeLevel", "info")
        self.atm_danger.style().unpolish(self.atm_danger)
        self.atm_danger.style().polish(self.atm_danger)
        # 战斗模式
        self.atm_combat.setText("CRPG 数值" if combat_system == "crpg" else "叙事模式")
        self.atm_combat.setProperty("badgeLevel", "info")
        self.atm_combat.style().unpolish(self.atm_combat)
        self.atm_combat.style().polish(self.atm_combat)
        # [修 2026-09-13] 布局重算完成后恢复滚动位置（singleShot 0 落到下一轮事件循环，
        # 此时内容高度已稳定；直接 setValue 会被随后的布局失效再次钳回）
        if _same_loc:
            QTimer.singleShot(0, lambda: self._left.verticalScrollBar().setValue(_scroll_y))

    def _refresh_place_bar(self, loc):
        """[P27] 刷新场所信息条：面包屑（地点 > 场所）+ 相邻场所切换按钮。

        无场所化的地点（places 空）隐藏场所条，退化单层兼容。
        """
        # 清旧切换按钮
        while self.place_switch_lay.count():
            item = self.place_switch_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if not self._world or not loc or not getattr(loc, "places", None):
            self.place_bar.setVisible(False)
            return
        cur_place = self.svc._current_place(self._world)
        if cur_place is None:
            self.place_bar.setVisible(False)
            return
        self.place_bar.setVisible(True)
        self.place_label.setText(f"场所：{cur_place.name}")
        # [用户定稿 2026-08-28] 本地点全部场所可点（地点内自由到达，不管场所连通图；
        # 当前场所以★标在【场所列表】——切走后原场所也回到切换条）
        all_places = [x for x in loc.places
                      if getattr(x, "id", "") and x.id != cur_place.id]
        for p in all_places:
            btn = QPushButton(p.name)
            btn.setStyleSheet("QPushButton{background:#1f2335; color:#7dcfff; border-radius:4px; padding:3px 8px; font-size:11px;}"
                              "QPushButton:hover{background:#2a2e44;}")
            btn.setToolTip(f"前往「{p.name}」（{self._place_type_label(p)}）")
            btn.clicked.connect(lambda _=False, pn=p.name: self._on_switch_place(pn))
            self.place_switch_lay.addWidget(btn)

    def _place_type_label(self, p) -> str:
        """[场所悬浮中文化] place.type 只有两类值：LLM 中文类型（酒馆/铁匠铺/渡口…
        原样透传）与 city venue 英文 key（weapon/alchemy/auction…，_VENUE_SHOP_TYPES
        口径）——后者走 GenreText.shop_type 转当前题材中文名（铁匠铺/药铺/
        英雄会黑市…）；空回退「场所」；旧档食堂 canteen 显示「食堂（已歇业）」。
        GenreText 读 world.config_overlay（build 已写入题材字段），缺省西幻默认。
        """
        raw = str(getattr(p, "type", "") or "").strip()
        if not raw:
            return "场所"
        if raw == "canteen":
            return "食堂（已歇业）"
        if raw in ("general", "weapon", "armor", "alchemy",
                   "consumable", "material", "magic", "auction"):
            from src.models.world_sim_preset import GenreText
            try:
                ov = self._world.config_overlay if self._world is not None else {}
                if not isinstance(ov, dict):
                    ov = {}
                return GenreText(ov).shop_type(raw)
            except Exception:
                pass
        return raw

    def _on_switch_place(self, place_name: str):
        """[P27] 场所切换按钮：起叙事回合 move 到当前地点内的指定场所。"""
        if self._busy or not self._world or not self._scene:
            return
        loc = self.svc._current_location(self._world)
        if not loc:
            return
        # move_to 格式 = 当前地点名/场所名（apply_intent move 分支按此解析场所移动）
        self._start_worker(player_action=f"前往{place_name}",
                           preset_intent={"intent_type": "move", "resolved": True,
                                          "move_to": f"{loc.name}/{place_name}",
                                          "narration_hint": f"玩家前往{place_name}"})

    def _refresh_npcs(self, loc):
        # 清旧（QGridLayout 无尾 stretch，count==0 即空）；重建期间关重绘防闪烁
        self.npc_row.setUpdatesEnabled(False)
        try:
            while self.npc_row_lay.count():
                item = self.npc_row_lay.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            if not self._world or not loc:
                return
            # [P27] 场所级在场 NPC（有场所时按场所过滤，无场所回退地点级）
            # [修 2026-09-05 用户指示] 阵亡 NPC 不进左栏（不可选不可交互——此前尸体仍
            # 可点击开交互面板，还带「可搜刮」提示误导玩家去场景层搜刮；战利品已在
            # 战斗胜利时自动结算）。叙事上下文【在场 NPC】仍保留尸体（已倒下标注）。
            cur_place = self.svc._current_place(self._world)
            npcs = [n for n in self.svc._npcs_at_place(self._world, loc, cur_place)
                    if getattr(n, "alive", True)]
            idx = 0
            for npc in npcs:
                cell = self._make_npc_cell(npc)
                self.npc_row_lay.addWidget(cell, idx // self._NPC_COLS, idx % self._NPC_COLS)
                idx += 1
            # [游商 2026-09-08 用户指示] 在场商店的游商条目（假 NPC，只交易）：
            # 商店挂地点（地点级），游商常驻在场；点击直开 ShopDialog，不进任何 NPC
            # 逻辑（无档案/私聊/交情/战斗/记忆）。旧档已绑真商人的店不加条目（走 NPC 交互）。
            if self.svc is not None:
                for shop in (self.svc.shops_at_location(self._world, loc.id) or []):
                    if str(getattr(shop, "merchant_npc_id", "") or ""):
                        continue
                    cell = self._make_vendor_cell(shop)
                    self.npc_row_lay.addWidget(cell, idx // self._NPC_COLS, idx % self._NPC_COLS)
                    idx += 1
        finally:
            self.npc_row.setUpdatesEnabled(True)

    def _make_vendor_cell(self, shop) -> QWidget:
        """[游商] 商店游商条目 cell（与 NPC cell 同网格混排，视觉区分：金框 + 铜币标记）。

        点击直开 ShopDialog（代码交易，不走叙事回合）；悬浮只显示店名/店型/库存数。
        [!] 名字超长省略：游商显示名「{店名}·{称谓}」可达 10+ 字，QLabel 默认
        sizeHint 按全文算宽撑大 cell -> QGridLayout 列宽被撑大 -> 整栏 inner
        变宽 -> 左栏 QScrollArea（横滚 AlwaysOff）右侧被裁。名字 QLabel 定宽
        86px + 省略号，长悬浮 tooltip 看全名（与 _make_npc_cell 同口径）。
        """
        from src.services.world_sim_service import trade_vendor_name
        vname = trade_vendor_name(self._world, shop)
        cell = QWidget()
        cell.setCursor(Qt.PointingHandCursor)
        cl = QVBoxLayout(cell)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(2)
        av = QLabel()
        av.setFixedSize(56, 56)
        av.setPixmap(make_card_pixmap(vname, "", paths.world_images_dir(), 56))
        av.setStyleSheet("border-radius:28px; border:2px solid #b58900;")
        cl.addWidget(av, alignment=Qt.AlignCenter)
        nm = QLabel("◈" + vname)
        nm.setStyleSheet("color:#e0af68; font-size:11px;")
        nm.setAlignment(Qt.AlignCenter)
        nm.setFixedWidth(86)
        nm.setWordWrap(False)
        _fm = nm.fontMetrics()
        nm.setText(_fm.elidedText("◈" + vname, Qt.ElideRight, 86))
        nm.setToolTip(vname)
        cl.addWidget(nm, alignment=Qt.AlignCenter)
        stk = len(getattr(shop, "stock", None) or [])
        cell.setToolTip(f"{vname}\n{getattr(shop, 'name', '') or '商店'}（游商，只交易）"
                        + (f"\n在售 {stk} 件商品" if stk else "\n（暂无商品）"))
        cell.mousePressEvent = lambda e, s=shop: self._on_vendor_click(s)
        return cell

    def _on_vendor_click(self, shop):
        """[游商] 游商条目点击：直开该店 ShopDialog（与 _open_shop_for_npc 同参数口径）。"""
        if self._busy or not self._world or shop is None:
            return
        from src.ui.dialogs.shop_dialog import ShopDialog
        dlg = ShopDialog(self._world, shop, self.storage,
                         on_changed=self.refresh_state,
                         on_regen_request=self._request_shop_regen, parent=self,
                         world_sim_service=self.svc, preset=self._preset)
        self._shop_dialog = dlg
        dlg.exec()
        self._shop_dialog = None

    def _make_npc_cell(self, npc) -> QWidget:
        from src.services import npc_reaction_engine as nre
        cell = QWidget()
        cell.setCursor(Qt.PointingHandCursor)
        cl = QVBoxLayout(cell)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(2)
        av = QLabel()
        av.setFixedSize(56, 56)
        av.setPixmap(make_card_pixmap(npc.name, npc.avatar, paths.world_images_dir(), 56))
        av.setStyleSheet("border-radius:28px; border:1px solid #2a2e44;")
        cl.addWidget(av, alignment=Qt.AlignCenter)
        aff = int(getattr(npc, "affinity", 0))
        friend = nre.is_friend(self._world, npc) if self._world else False
        companion = nre.is_companion(self._world, npc) if self._world else False
        fatigued = nre.is_fatigued(self._world, npc) if self._world else False
        nm_txt = ("◈" if companion else ("★" if friend else "")) + npc.name + ("⏳" if fatigued else "")
        nm = QLabel()
        if companion:
            nm.setStyleSheet("color:#9ece6a; font-size:11px; font-weight:bold;")
        else:
            nm.setStyleSheet("color:#c0caf5; font-size:11px;")
        nm.setAlignment(Qt.AlignCenter)
        # [!] 名字超长省略（与 _make_vendor_cell 同因）：长名 QLabel 默认 sizeHint
        # 撑大 grid 列宽 -> 整栏 inner 变宽 -> 左栏横滚关死右侧被裁。定宽 86 + 省略号。
        nm.setFixedWidth(86)
        nm.setWordWrap(False)
        _fm = nm.fontMetrics()
        nm.setText(_fm.elidedText(nm_txt, Qt.ElideRight, 86))
        nm.setToolTip(npc.name)
        cl.addWidget(nm, alignment=Qt.AlignCenter)
        # 生活模拟已经给 NPC 写入当前行动，场景里直接露出一行，玩家不用逐个悬浮猜谁在做事。
        current_action = str(getattr(npc, "current_action", "") or "").strip()
        if current_action:
            action_label = QLabel()
            action_label.setObjectName("npcCurrentAction")
            action_label.setFixedWidth(86)
            action_label.setAlignment(Qt.AlignCenter)
            action_label.setStyleSheet("color:#7dcfff; font-size:10px;")
            action_label.setText(action_label.fontMetrics().elidedText(current_action, Qt.ElideRight, 86))
            action_label.setToolTip(current_action)
            cl.addWidget(action_label, alignment=Qt.AlignCenter)
        # [P10/P10b] 悬浮提示：交情档位 + 好友/同行标记（点击开交互面板）；[P25b] 疲劳标注
        # [A+B 2026-09-06] 察言观色：悬浮直读此刻念头/今日目标（用户拍板无门槛）
        _th = (getattr(npc, "current_thought", "") or "").strip()
        _dg = ((getattr(npc, "current_goal", "") or "").strip()
               or (getattr(npc, "daily_goal", "") or "").strip())
        cell.setToolTip(f"{npc.name}（{npc.role or '未知'}）\n交情：{nre.affinity_level(aff)} {aff}/100"
                        + ("（好友）" if friend else "") + ("（同行中）" if companion else "")
                        + ("（战斗疲劳休整中，暂不可同行/助战）" if fatigued else "")
                        + (f"\n正在：{current_action}" if current_action else "")
                        + (f"\n念头：{_th}" if _th else "")
                        + (f"\n今日目标：{_dg}" if _dg else ""))
        cell.mousePressEvent = lambda e, n=npc: self._on_npc_click(n)
        return cell

    # ============ [P7g] 可采集资源 ============
    def _refresh_resources(self, loc):
        """刷新可采集资源区：每资源点一行（名 + 描述 + 丰度条 + 采集按钮）。"""
        # 清旧
        while self.resource_lay.count():
            item = self.resource_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        # 无世界/无地点/无资源点/采集关闭 -> 隐藏整区
        if (not self._world or not loc or not loc.resource_nodes
                or not getattr(self._preset, "gathering_enabled", True)):
            self.resource_header.hide()
            self.resource_container.hide()
            return
        self.resource_header.show()
        self.resource_container.show()
        tick = self._world.tick_count
        for rn in loc.resource_nodes:
            self.resource_lay.addWidget(self._make_resource_row(rn, tick))

    def _make_resource_row(self, rn, tick) -> QWidget:
        row = QFrame()
        row.setStyleSheet("QFrame{background:#1f2335; border-radius:6px; padding:6px;}")
        rl = QVBoxLayout(row)
        rl.setContentsMargins(8, 6, 8, 6)
        rl.setSpacing(3)
        # 名 + 类型
        name_lbl = QLabel(f"{rn.name}（{rn.type}）")
        name_lbl.setStyleSheet("color:#c0caf5; font-weight:bold;")
        rl.addWidget(name_lbl)
        # 状态判断
        can = True
        status_txt = ""
        if rn.richness <= 0:
            can = False
            status_txt = "已枯竭" + ("（再生中）" if rn.regenerates else "（永久耗尽）")
        elif rn.cooldown_tick > tick:
            can = False
            status_txt = f"冷却中（约{rn.cooldown_tick - tick}回合后可采）"
        desc_lbl = QLabel(rn.desc + (f"  {status_txt}" if status_txt else ""))
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet("color:#9aa5ce; font-size:11px;")
        rl.addWidget(desc_lbl)
        # 丰度条（复用 hpBar 渐变样式 + hpLevel 档位）
        bar = QProgressBar()
        bar.setFixedHeight(10)
        bar.setTextVisible(False)
        bar.setRange(0, max(1, rn.richness_max))
        bar.setValue(max(0, min(rn.richness, rn.richness_max)))
        bar.setObjectName("hpBar")
        if rn.richness < 30:
            bar.setProperty("hpLevel", "low")
        elif rn.richness < 60:
            bar.setProperty("hpLevel", "mid")
        else:
            bar.setProperty("hpLevel", "high")
        rl.addWidget(bar)
        # 采集按钮
        verb = self.svc._gather_verb(self._world) if self._world else "采集"
        btn = QPushButton(f"{verb}" if can else (status_txt or "不可采集"))
        btn.setObjectName("optionBtn")
        btn.setEnabled(can and not self._busy)
        if can:
            btn.clicked.connect(lambda checked, n=rn.name: self._on_gather(n))
        rl.addWidget(btn)
        return row

    def _on_gather(self, node_name: str):
        """触发采集回合：先弹三骰判定 overlay，再把预掷结果塞进 preset_intent 走 SceneWorker。"""
        if self._busy or not self._world:
            return
        chance, _node = self.svc.gather_preview(self._world, node_name, self._preset)
        if chance is None:
            return
        overlay = DiceCheckOverlay(chance, parent=self)
        overlay.exec()
        if overlay.result is None:
            return
        verb = self.svc._gather_verb(self._world)
        action_text = f"{verb}{node_name}"
        preset_intent = {
            "intent_type": "gather",
            "gather_node": node_name,
            "resolved": True,
            "narration_hint": "",
            "dice": overlay.result,
        }
        self._start_worker(action_text, preset_intent=preset_intent)

    def _on_probe(self):
        """[P8] 手动探测奇遇：主线程预生成 + 弹三骰判定，再把 enc/dice 透传走 SceneWorker。"""
        if self._busy or not self._world:
            return
        loc = self.svc._current_location(self._world)
        if loc is None:
            return
        preview = self.svc.encounter_preview(self._world, loc, mode="probe")
        loc_name = loc.name
        action_text = f"在「{loc_name}」探测机缘"
        preset_intent = {
            "intent_type": "adventure",
            "resolved": True,
            "narration_hint": "",
            "encounter_preview": preview,
        }
        if preview.get("triggered") and preview.get("chance") is not None:
            overlay = DiceCheckOverlay(preview["chance"], parent=self)
            overlay.exec()
            if overlay.result is None:
                return
            preset_intent["dice"] = overlay.result
        self._start_worker(action_text, preset_intent=preset_intent)

    def _on_talent(self):
        """[P9] 打开天赋详情对话框（只读，展示玩家已选天赋 + effects + 剩余点数）。"""
        if not self._world:
            return
        from src.ui.dialogs.talent_view_dialog import TalentViewDialog
        dlg = TalentViewDialog(self._world, parent=self)
        dlg.exec()

    def _on_skills(self):
        """[技能页 2026-08-28] 打开技能对话框（熟练度/装备栏/研读技能书），改动落盘。"""
        if not self._world:
            return
        from src.ui.dialogs.skill_dialog import SkillDialog

        def _on_changed():
            if self.storage is not None:
                self.storage.save_world(self._world)
            if getattr(self, "on_world_changed", None):
                try:
                    self.on_world_changed()
                except Exception:
                    pass
            self.refresh_state()
        dlg = SkillDialog(self._world, on_changed=_on_changed, parent=self)
        dlg.exec()

    # ============ [P10] 好友 + 私聊 ============
    def _on_friends(self):
        """[P10] 打开好友页（好友列表/添加好友/私聊入口）。"""
        if not self._world or not self._preset:
            return
        from src.ui.dialogs.friends_dialog import FriendsDialog
        dlg = FriendsDialog(self._world, self.svc, self.storage, self._preset, parent=self)
        dlg.changed.connect(self._on_friends_changed)
        dlg.exec()

    def _on_friends_changed(self):
        """好友增删/私聊后刷新场景页（交情变了，NPC 提示与上下文随之更新）。"""
        if not self._world:
            return
        self.refresh_state()
        self.world_changed.emit()

    # ============ [P24a] 住宅（购宅/仓库/陈列/家具/休憩）============
    def _on_home(self):
        """[P24a] 打开住宅页（生成中禁开：仓库/休憩改世界状态，与回合并发会撕裂）。"""
        if self._busy or not self._world or not self._preset:
            return
        from src.ui.dialogs.home_dialog import HomeDialog
        dlg = HomeDialog(self._world, self.svc, self.storage, self._preset, parent=self)
        dlg.changed.connect(self._on_home_changed)
        dlg.rest_requested.connect(self._on_home_rest)
        dlg.travel_requested.connect(self._on_home_travel)
        dlg.exec()

    def _on_home_changed(self):
        """[P24a] 仓库/陈列/家具/购宅变更：落盘已由对话框做，这里刷新场景页。"""
        if not self._world:
            return
        self.refresh_state()
        self.world_changed.emit()

    def _on_home_rest(self, home_name: str):
        """[P24a] 宅中休憩：起 rest 叙事回合（preset_intent 跳 settle；apply_intent
        跳时到次日晨 -> narrate -> tick_world 一次，见 world_sim_service.apply_intent）。"""
        if self._busy or not self._world or not self._scene:
            return
        preset_intent = {
            "intent_type": "rest",
            "resolved": True,
            "narration_hint": "",
        }
        self._start_worker(f"回到「{home_name}」休憩", preset_intent=preset_intent)

    def _on_home_travel(self, home_id: str):
        """[P24a] 归宅快捷：起 go_home 叙事回合（preset_intent 跳 settle；apply_intent
        把玩家移到宅所在聚落，不受相邻限制，narrate + tick 一次，同 rest 快捷口径）。"""
        if self._busy or not self._world or not self._scene:
            return
        home = next((h for h in self._world.homes if h.id == home_id), None)
        if home is None:
            return
        preset_intent = {
            "intent_type": "go_home",
            "resolved": True,
            "home_id": home_id,
            "narration_hint": "",
        }
        self._start_worker(f"回到「{home.name}」", preset_intent=preset_intent)

    # ============ [D1] 据点（购地开辟/资金池存取/归返）============
    def _set_life_goal(self):
        """立/改毕生所愿：选执念类型 + 三档目标（引擎进度判定，零 LLM）。"""
        svc = self.svc
        w = self._world
        kinds = [("wealth", "富甲一方（攒钱）"), ("mastery", "登峰造极（修为）"),
                 ("slay", "百战之人（战绩）"), ("explore", "踏遍山河（探索）"),
                 ("fame", "名动一方（声望）")]
        names = [n for _, n in kinds]
        sel, ok = QInputDialog.getItem(self, "毕生所愿", "选一个你毕生想达成的事：",
                                       names, 0, False)
        if not ok:
            return
        kind = kinds[names.index(sel)][0]
        tiers = svc.life_goal_tiers(w, kind)
        tsel, ok2 = QInputDialog.getItem(
            self, "毕生所愿", "立多大的誓？",
            [f"小愿（目标 {tiers[0]}）", f"宏愿（目标 {tiers[1]}）", f"毕生大愿（目标 {tiers[2]}）"],
            1, False)
        if not ok2:
            return
        tier = ["小", "宏", "毕"].index(tsel[:1]) if tsel[:1] in ("小", "宏", "毕") else 1
        ok3, err = svc.set_life_goal(w, kind, tier)
        if not ok3:
            QMessageBox.warning(self, "立愿失败", err)
        else:
            QMessageBox.information(self, "执念已立",
                                    "从今往后，你有了在这世界非做不可的事。\n"
                                    "达成时会公告天下。")

    def _on_agenda(self):
        """[P55 今日议程] 弹出「玩家手头的事」清单（零 LLM 聚合，治空白输入框恐惧）。"""
        if not self._world:
            return
        # [修 2026-09-30 真机] 属性名是 _preset（下方裸 self.preset 在「未立愿+点Yes」
        # 路径炸 AttributeError——53 回合档已立愿从不走此分支，新世界首发即崩）
        _preset = getattr(self, "_preset", None)
        items = self.svc.agenda_items(self._world, _preset)
        goal = getattr(self._world.player, "life_goal", None) or {}
        if not goal.get("kind"):
            if QMessageBox.question(
                    self, "毕生所愿",
                    "你还没有立下毕生所愿。\n\n"
                    "富甲一方 / 登峰造极 / 百战之人 / 踏遍山河 / 名动一方——"
                    "选一个作为你在这世界的执念吗？",
                    QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
                self._set_life_goal()
                items = self.svc.agenda_items(self._world, _preset)
        # [U01 完整版 2026-09-30] 可滚动议程视图 + 每项「前往」导航（F12：点一件事
        # 直达对应页面；导航只查看，不消耗时间/不结算/不改存档——空态也进视图说人话）
        from src.ui.dialogs.agenda_dialog import AgendaDialog
        # [G01/R3 2026-09-30] 战备读数（纯读）进议程顶卡：出发前看得见自己缺什么
        _ready = []
        try:
            _ready = self.svc.expedition_readiness(self._world).get("lines") or []
        except Exception:
            pass
        dlg = AgendaDialog(
            items,
            nav_callbacks={"quests": self._on_quest_log,
                           "outreach": self._on_messenger,
                           "dungeon": self._on_dungeon,
                           "commissions": self._on_commission,
                           "domain": self._on_domain,
                           "bosses": self._on_world_boss},
            parent=self, readiness_lines=_ready,
            initial_filter=getattr(self, "_agenda_filter", "全部"),
            on_filter_changed=lambda t: setattr(self, "_agenda_filter", t))
        dlg.exec()

    def _on_messenger(self):
        """[P57 NPC 上门] 打开信使对话框处理 pending 上门（过期不候）。

        约战 = 引擎纯结算的切磋（点到即止非致死——finish_combat 胜利必杀 NPC /
        一击制拒非敌对，正式战斗两条通路都不适配比武），结算后起叙事回合写场面
        （结义同款「先改状态 -> save -> preset_intent custom」口径）。"""
        if not self._world or self._busy:
            return
        from src.ui.dialogs.outreach_dialog import OutreachDialog
        dlg = OutreachDialog(self.svc, self._world, parent=self)
        dlg.exec()
        if not dlg.changed:
            return
        self.storage.save_world(self._world)
        self.refresh_state()
        self.world_changed.emit()
        if dlg.duel_npc_id:
            npc = next((n for n in self._world.npcs if n.id == dlg.duel_npc_id), None)
            if npc is not None and self.svc.outreach_move_for_duel(self._world, npc.id):
                res = self.svc.outreach_settle_duel(self._world, npc)
                self.storage.save_world(self._world)
                self.log.add_entry("system", f"{npc.name} 登门赴约——切磋开始！", "系统")
                self._start_worker(
                    player_action=f"与{npc.name}切磋一场",
                    preset_intent={"intent_type": "custom", "resolved": True,
                                   "narration_hint": str(res.get("hint", ""))})

    def _on_domain(self):
        """[D1] 打开据点页（生成中禁开：购地/存取款改世界状态，防与回合并发撕裂）。"""
        if self._busy or not self._world or not self._preset:
            return
        from src.ui.dialogs.domain_dialog import DomainDialog
        dlg = DomainDialog(self._world, self.svc, self.storage, self._preset, parent=self)
        dlg.changed.connect(self._on_domain_changed)
        dlg.travel_requested.connect(self._on_domain_travel)
        dlg.exec()

    def _on_domain_changed(self):
        """[D1] 购地/存取款变更：落盘已由对话框做，这里刷新场景页。"""
        if not self._world:
            return
        self.refresh_state()
        self.world_changed.emit()

    def _on_domain_travel(self, domain_location_id: str):
        """[D1] 归返据点快捷：起 go_domain 叙事回合（preset_intent 跳 settle；apply_intent
        把玩家移到据点地点（不受相邻限制）并落到营门，narrate + tick 一次，同 go_home 口径）。"""
        if self._busy or not self._world or not self._scene:
            return
        loc = next((l for l in self._world.locations if l.id == domain_location_id), None)
        if loc is None or not getattr(loc, "player_owned", False):
            return
        preset_intent = {
            "intent_type": "go_domain",
            "resolved": True,
            "domain_location_id": domain_location_id,
            "narration_hint": "",
        }
        self._start_worker(f"回到据点「{loc.name}」", preset_intent=preset_intent)

    # ============ [P24d] 宠物（出战切换/投食养成）============
    def _on_pet(self):
        """[P24d] 打开宠物页（生成中禁开：出战切换/投食改世界状态，防与回合并发撕裂）。"""
        if self._busy or not self._world or not self._preset:
            return
        from src.ui.dialogs.pet_dialog import PetDialog
        dlg = PetDialog(self._world, self.storage, parent=self)
        dlg.changed.connect(self._on_pet_changed)
        dlg.exec()

    def _on_pet_changed(self):
        """[P24d] 出战/投食变更：落盘已由对话框做，这里刷新场景页。"""
        if not self._world:
            return
        self.refresh_state()
        self.world_changed.emit()

    # ============ [P25a] 秘境（进入/深入/离开）============
    def _on_dungeon(self):
        """[P25a] 打开秘境页（生成中禁开：进出/推进改世界状态，防与回合并发撕裂）。"""
        if self._busy or not self._world or not self._preset:
            return
        from src.services import dungeon_engine as dge
        loc = self.svc._current_location(self._world)
        if dge.interior_dungeon(self._world, loc) is None and dge.dungeon_at(self._world, loc) is None:
            return
        from src.ui.dialogs.dungeon_dialog import DungeonDialog
        dlg = DungeonDialog(self._world, self.svc, self.storage, parent=self)
        # 进入走预设移动回合（包含入口陷阱）；撤出已改状态后起叙事回合。
        dlg.enter_requested.connect(self._on_dungeon_enter_round)
        dlg.exit_requested.connect(self._on_dungeon_exit_round)
        dlg.changed.connect(self._on_dungeon_changed)
        dlg.explore_requested.connect(self._on_dungeon_explore)
        dlg.interaction_requested.connect(self._on_dungeon_interaction)
        dlg.exec()

    def _on_dungeon_changed(self):
        """[P25a] 进入/离开秘境：落盘已由对话框做，这里刷新场景页。"""
        if not self._world:
            return
        self.refresh_state()
        self.world_changed.emit()

    def _on_dungeon_enter_round(self, intent: dict):
        """进入由回合结算；未结算前不改位置，入口陷阱/守卫沿用正常战斗与倒下分流。"""
        if self._busy or not self._world or not self._scene:
            return
        nm = intent.get("move_to") or "秘境"
        self._start_worker(f"踏入「{nm}」", preset_intent=dict(intent))

    def _on_dungeon_exit_round(self, msg: str):
        """[F14 2026-09-30] 撤出秘境的叙事回合（同上；msg 为服务返回文案）。"""
        if self._busy or not self._world or not self._scene:
            return
        nm = str(msg or "").strip("撤出「」") or "秘境"
        self._start_worker(f"撤出「{nm}」", preset_intent={
            "intent_type": "custom", "resolved": True,
            "narration_hint": f"你撤出「{nm}」，回到入口的光亮之下",
            "reason": "", "effects": []})

    def _on_dungeon_explore(self, dungeon_name: str):
        """[P25a] 深入探索：起 move=深入 叙事回合（apply_intent 秘境特判 -> 引擎推进房间）。"""
        if self._busy or not self._world or not self._scene:
            return
        preset_intent = {
            "intent_type": "move",
            "move_to": "深入",
            "resolved": True,
            "narration_hint": "",
        }
        self._start_worker(f"深入「{dungeon_name}」探索", preset_intent=preset_intent)

    def _on_dungeon_interaction(self, intent: dict):
        if self._busy or not self._world or not self._scene:
            return
        text = (f"前往「{intent.get('move_to', '')}」" if intent.get("dungeon_action") == "move"
                else f"使用「{intent.get('target', '')}」探索房间" if intent.get("dungeon_action") == "tool"
                else "沿安全主路绕过当前机关或陷阱")
        self._start_worker(text, preset_intent=dict(intent))

    def _on_world_boss(self):
        """[P25d] 打开世界 Boss 讨伐页（Boss 信息/形态进度/援护/窗口；挑战起预设战斗）。"""
        if self._busy or not self._world or not self._preset:
            return
        from src.services import world_boss_engine as wbe
        from src.ui.dialogs.world_boss_dialog import WorldBossDialog
        loc = self.svc._current_location(self._world)
        boss = wbe.boss_at(self._world, loc)
        if boss is None:
            return
        dlg = WorldBossDialog(self._world, boss, self.svc, parent=self)
        dlg.challenge_requested.connect(self._fight_world_boss_form)
        dlg.exec()

    def _fight_world_boss_form(self, boss):
        """[P25d] 起世界 Boss 当前形态战斗（CombatDialog skip_judge 喂预设单位 + 战后收尾）。

        镜像 _fight_npc_dialog：预设战斗跳过 LLM 判定（prepare_world_boss_combat 喂入 Boss
        临时单位 + 确定性小弟 + 声望援护）；战后收尾含存档/刷新/永久死亡/叙事回合
        （narration_hint 含变身/胜利文案，由 on_combat_victory 写）。形态间玩家回到场景
        可休整回血再战下一形态（current_form_index 已推进）。
        """
        from src.ui.dialogs.combat_dialog import CombatDialog
        prep = self.svc.prepare_world_boss_combat(self._world, boss, self._preset)
        if prep is None:
            return
        dlg = CombatDialog(self.svc, self._world, prep["target_npc"], self._preset, parent=self,
                           skip_judge_with={"allies": prep.get("allies") or [],
                                            "minion_specs": prep.get("minion_specs") or [],
                                            "note": prep.get("note", "")})
        dlg.exec()
        summary = dlg.get_summary()
        if not summary:
            return
        # [P7k8] 永久死亡：玩家被击败 + permadeath 开启 -> 删世界回首页
        permadeath = self.svc._per_world(self._world, "permadeath_enabled",
                                         self._preset.permadeath_enabled, self._preset)
        if summary.get("player_defeated") and permadeath:
            QMessageBox.warning(self, "永久死亡",
                                f"你在讨伐{boss.name}时倒下，再也没能站起来……\n"
                                f"（永久死亡模式：这个世界已被永久抹去。）")
            wid = self._world.id
            try:
                if self.svc is not None:
                    self.svc.npc_memory().clear_world_memory(self._world)
            except Exception:
                pass
            try:
                self.storage.delete_world(wid)
            except Exception:
                pass
            self.permadeath.emit(wid)
            return
        # Boss 每一形态的普通掉落与终形态保底奖励都由战后格子拾取。
        if summary.get("combat_state") == "won" \
                and (summary.get("loot_grid") or {}).get("entries"):
            from src.ui.dialogs.loot_grid_dialog import LootGridDialog
            ldlg = LootGridDialog(self.svc, self._world, summary["loot_grid"], parent=self)
            ldlg.exec()
            for line in (ldlg.result_lines or []):
                if self._scene is not None:
                    entry = self._scene.append("system", line,
                                               tick=self._world.tick_count if self._world else 0)
                    self.log.add_entry("system", line, "系统", entry_id=entry.id)
                else:
                    self.log.add_entry("system", line, "系统")
        self.storage.save_world(self._world)
        self.refresh_state()
        self.world_changed.emit()
        # 叙事回合描写变身/胜利（on_combat_victory 已写 narration_hint；post_combat 防连环强制战斗）
        hint = summary.get("narration_hint", "")
        self._start_worker(player_action=f"讨伐{boss.name}",
                           preset_intent={"intent_type": "custom", "resolved": True,
                                          "narration_hint": hint, "post_combat": True})

    def _on_add_friend_npc(self, npc):
        """[P10] NPC 交互面板「加为好友」：校验交情达标后加入好友列表。"""
        if not self._world or not self._preset:
            return
        from src.services import npc_reaction_engine as nre
        threshold = int(self.svc._per_world(self._world, "friend_affinity_threshold",
                                            self._preset.friend_affinity_threshold, self._preset))
        if not nre.can_add_friend(self._world, npc, threshold):
            from PySide6.QtWidgets import QMessageBox
            aff = int(getattr(npc, "affinity", 0))
            QMessageBox.information(
                self, "还不能成为好友",
                f"与{npc.name}的交情还不够（当前 {nre.affinity_level(aff)} {aff}/{threshold}）。\n"
                f"多交谈、私聊或并肩作战可以拉近距离。")
            return
        if npc.id not in self._world.player.friend_npc_ids:
            self._world.player.friend_npc_ids.append(npc.id)
        self.storage.save_world(self._world)
        self.refresh_state()
        self.world_changed.emit()

    def _on_chat_npc(self, npc):
        """[P10] NPC 交互面板「私聊」：打开私聊对话框（仅好友；走类级单例）。"""
        if not self._world or not self._preset:
            return
        from src.ui.dialogs.npc_chat_dialog import NpcChatDialog
        dlg, _new = NpcChatDialog.open_for(
            self._world, npc, self.svc, self.storage, self._preset, parent=self)
        dlg.exec()
        # 私聊会 +交情，刷新显示
        self.refresh_state()
        self.world_changed.emit()

    def _on_gift_npc(self, npc):
        """[P16] NPC 交互面板「送礼」：从背包挑物品送给 NPC（纯代码结算交情 + 物品转移）。"""
        if not self._world or self._busy:
            return
        from src.services import npc_reaction_engine as nre
        from src.ui.widgets.item_brief import item_tooltip, kind_label
        p = self._world.player
        # 候选：背包物品排除 key 类（任务关键道具不可送）；去重（背包本身 id 唯一）
        cands = []
        for iid in (p.inventory or []):
            it = next((i for i in self._world.items if i.id == iid), None)
            if it is not None and it.type != "key":
                cands.append(it)
        if not cands:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "没有可送的物品", "背包里没有可赠送的物品（关键道具不可送人）。")
            return
        # 简易选择器：列表 + 全属性 tooltip；确认后引擎结算
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QListWidget, QListWidgetItem, QDialogButtonBox, QMessageBox,
        )
        gt = self.svc._genre_text(self._world)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"送礼给 {npc.name}")
        dlg.resize(420, 460)
        lay = QVBoxLayout(dlg)
        hint = QLabel(f"挑一件送给{npc.name}：品级越高、越合对方爱好（档案-爱好栏），交情涨得越多。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa5ce; font-size:12px;")
        lay.addWidget(hint)
        if npc.hobbies:
            hb = QLabel("对方爱好：" + "、".join(npc.hobbies))
            hb.setWordWrap(True)
            hb.setStyleSheet("color:#7dcfff; font-size:12px;")
            lay.addWidget(hb)
        lst = QListWidget()
        for it in cands:
            row = QListWidgetItem(f"{it.name}（{gt.rarity(it.rarity)} · {kind_label(self._world, it, gt)}）")
            row.setData(Qt.ItemDataRole.UserRole, it.id)
            row.setToolTip(item_tooltip(self._world, it))
            row.setToolTipDuration(10000)
            lst.addItem(row)
        if lst.count() > 0:
            lst.setCurrentRow(0)   # 默认选中首件，防「送出」无选中静默返回
        lay.addWidget(lst, 1)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("送出")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        row = lst.currentItem()
        if row is None:
            QMessageBox.information(self, "未选择物品", "请先在列表中选中要送出的物品。")
            return
        it = next((i for i in self._world.items if i.id == row.data(Qt.ItemDataRole.UserRole)), None)
        result = nre.apply_player_gift(self._world, npc, it)
        if not result.get("ok"):
            QMessageBox.information(self, "无法赠送", result.get("reason", "赠送失败。"))
            return
        self.storage.save_world(self._world)
        self.refresh_state()
        self.world_changed.emit()
        QMessageBox.information(self, "送礼成功", result.get("narration_hint", "对方收下了礼物。"))

    # ============ [P10b] 同行（邀请跟随 / 道别退队）============
    def _on_companion_npc(self, npc):
        """[P10b] NPC 交互面板「邀请同行/道别退队」：按当前队伍状态分流。"""
        if not self._world or not self._preset:
            return
        from src.services import npc_reaction_engine as nre
        from PySide6.QtWidgets import QMessageBox
        if nre.is_companion(self._world, npc):
            nre.dismiss_companion(self._world, npc)
            self.storage.save_world(self._world)
            self.refresh_state()
            self.world_changed.emit()
            QMessageBox.information(self, "退队", f"{npc.name}向你道别，留在了当前地点。")
            return
        # 邀请：校验开关/交情/队伍上限/同地点
        if not self.svc._per_world(self._world, "companions_enabled",
                                   self._preset.companions_enabled, self._preset):
            QMessageBox.information(self, "同行", "这个世界不支持邀请同行。")
            return
        threshold = int(self.svc._per_world(self._world, "companion_affinity_threshold",
                                            self._preset.companion_affinity_threshold, self._preset))
        max_followers = int(self.svc._per_world(self._world, "companion_max_followers",
                                                self._preset.companion_max_followers, self._preset))
        comp_ids = self._world.player.companion_npc_ids or []
        if len(comp_ids) >= max_followers:
            names = "、".join(next((n.name for n in self._world.npcs if n.id == cid), cid)
                              for cid in comp_ids) or "无"
            QMessageBox.information(
                self, "同行", f"同行人数已满（{len(comp_ids)}/{max_followers}）：{names}。\n"
                              f"先与某位同伴道别再邀请{npc.name}。")
            return
        if nre.is_fatigued(self._world, npc):   # [P25b] 战斗疲劳休整中
            left = max(0, int(getattr(npc, "companion_fatigue_until_tick", 0) or 0)
                       - int(self._world.tick_count or 0))
            QMessageBox.information(
                self, "同行", f"{npc.name}刚经历过一场恶战，正在休整（约 {left} 个回合），"
                              "暂时无法同行或助战。")
            return
        if not nre.can_invite_companion(self._world, npc, threshold, max_followers):
            aff = int(getattr(npc, "affinity", 0))
            same_loc = npc.location_id == self._world.player.location_id
            if not same_loc:
                QMessageBox.information(self, "同行", f"{npc.name}不在你当前地点，无法邀请同行。")
            else:
                QMessageBox.information(
                    self, "同行",
                    f"与{npc.name}的交情还不够（当前 {nre.affinity_level(aff)} {aff}/{threshold}）。\n"
                    f"多交谈、私聊或并肩作战可以拉近距离。")
            return
        nre.invite_companion(self._world, npc)
        self.storage.save_world(self._world)
        self.refresh_state()
        self.world_changed.emit()
        QMessageBox.information(self, "同行", f"{npc.name}加入了你的旅程，将随你一同移动。")

    # ============ [P26a] 结义 / 求婚 ============
    def _on_treat_npc(self, npc):
        """[G01/R3 2026-09-30] NPC 面板「求医」：ce.treat_injury 真结算 + 叙事回合写场面。

        [!] 回满 HP 不等于部位伤痊愈——治疗只缩伤期，气血靠服药；诊金守恒入医者钱包。
        结义同款「先改状态 -> save -> preset_intent custom 叙事」口径。"""
        if self._busy or not self._world:
            return
        from src.services import combat_engine as _ce
        from PySide6.QtWidgets import QMessageBox
        ok, msg = _ce.treat_injury(self._world, npc)
        if not ok:
            QMessageBox.information(self, "求医", msg or "此刻无法诊治。")
            return
        self.storage.save_world(self._world)
        self.refresh_state()
        self.world_changed.emit()
        if not self._scene:
            return
        self._start_worker(f"请{npc.name}诊治伤势", preset_intent={
            "intent_type": "custom", "resolved": True,
            "narration_hint": f"药师{npc.name}为你敷药施针（{msg}）",
            "reason": "", "effects": []})

    def _on_sworn_npc(self, npc):
        """[P26a] NPC 交互面板「结义」：引擎写 sworn 阶段 + 起叙事回合描写结义仪式。

        按钮可见性已在 NpcInteractionDialog 由 can_sworn 门控；这里再校验一次防
        状态漂移（tick/NPC 死亡等），失败则提示原因。仪式走 preset_intent custom
        叙事回合（narration_hint 含题材化结义文案），不改 settle 提示词。
        """
        if self._busy or not self._world or not self._preset:
            return
        from src.services import relation_engine as reng
        from PySide6.QtWidgets import QMessageBox
        ok, reason = reng.can_sworn(self._world, npc, self._preset)
        if not ok:
            QMessageBox.information(self, "结义", reason or "此刻无法结义。")
            return
        hint = reng.make_sworn(self._world, npc)
        self.storage.save_world(self._world)
        self.refresh_state()
        self.world_changed.emit()
        self._start_worker(
            player_action=f"与{npc.name}结义",
            preset_intent={"intent_type": "custom", "resolved": True, "narration_hint": hint})

    def _on_marry_npc(self, npc):
        """[P26a] NPC 交互面板「求婚」：引擎写 spouse 阶段 + 落 major 婚礼事件 +
        自动邀请配偶同行（豁免位限制）+ 起叙事回合描写婚礼。

        按钮可见性已在 NpcInteractionDialog 由 can_propose 门控；这里再校验一次。
        make_spouse 内部已落 WorldEvent 并自动 invite_companion。
        """
        if self._busy or not self._world or not self._preset:
            return
        from src.services import relation_engine as reng
        from PySide6.QtWidgets import QMessageBox
        ok, reason = reng.can_propose(self._world, npc, self._preset)
        if not ok:
            QMessageBox.information(self, "求婚", reason or "此刻无法求婚。")
            return
        hint = reng.make_spouse(self._world, npc)
        self.storage.save_world(self._world)
        self.refresh_state()
        self.world_changed.emit()
        self._start_worker(
            player_action=f"与{npc.name}举行婚礼",
            preset_intent={"intent_type": "custom", "resolved": True, "narration_hint": hint})

    # ============ 行动 ============
    def _set_busy(self, busy: bool):
        self._busy = busy
        self.send_btn.setEnabled(not busy)
        self.input.setEnabled(not busy)
        if busy:
            self.stop_btn.show()
            self.send_btn.hide()
        else:
            self.stop_btn.hide()
            self.send_btn.show()

    def _on_send_free(self):
        if self._busy or not self._world:
            return
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        # [开局 30 分钟改造包] 首个行动已发出，ghost text 退场回落默认占位（不再打扰）
        self.input.setPlaceholderText(_INPUT_PLACEHOLDER_DEFAULT)
        self._start_worker(player_action=text)

    def _on_stop(self):
        if self._worker is not None:
            self._worker.cancel()
            self.status_label.setText("正在停止…")

    def _on_map(self):
        if self._busy or not self._world:
            return
        w = self._world
        cur = self.svc._current_location(w)
        if not cur:
            return
        adj = self.svc._adjacent_locations(w, cur)
        dlg = WorldMapDialog(w, cur, adj, self.svc, self._preset, self)
        dlg.exec()
        # [P7f] 拓展（迷雾块点击）可能改了 world（新增地点），无论是否前往都 save + refresh
        self.storage.save_world(w)
        self.refresh_state()
        self.world_changed.emit()
        if dlg.get_selected():
            target_id = dlg.get_selected()
            target_name = next((l.name for l in w.locations if l.id == target_id), '新地点')
            # [!] 不预移动（守引擎契约）：旧逻辑先 move_player 再起叙事回合，导致结算 LLM
            # 看到「当前地点=目标、相邻列表无目标」误判 resolved=false（行动受阻）。
            # 现在只起叙事回合，让 settle→apply_intent move 分支完成移动（玩家仍在原地点，
            # 相邻列表含目标，settle 正常判 resolved=true）。move_player 的 discovered/
            # explored/奇遇/任务钩子/同伴跟随/reveal_adjacent apply_intent 已有或已补。
            self._start_worker(player_action=f"前往{target_name}")

    def _on_npc_click(self, npc):
        if self._busy or not self._world:
            return
        # [P7k6] 图鉴：标记 NPC 已遇到
        if npc.id not in self._world.player.codex_npcs:
            self._world.player.codex_npcs.append(npc.id)
            self.storage.save_world(self._world)
        loc = self.svc._current_location(self._world)
        # [P6] 商售 NPC 传 on_shop 回调 -> 开 ShopDialog（代码交易，不走叙事回合）
        # [P6c] 任何 NPC 传 on_profile 回调 -> 开 NpcDetailDialog（只读档案）
        # [P7h] 敌对 NPC 传 on_combat 回调 -> 开 CombatDialog（回合制，玩家可操作）
        # [P10] 传 world + on_friend/on_chat -> 交情显示 + 加好友/私聊入口
        dlg = NpcInteractionDialog(npc, loc.name if loc else "",
                                   on_shop=self._open_shop_for_npc,
                                   on_profile=self._open_npc_profile,
                                   on_combat=self._on_npc_combat,
                                   world=self._world,
                                   on_friend=self._on_add_friend_npc,
                                   on_chat=self._on_chat_npc,
                                   on_companion=self._on_companion_npc,
                                   on_gift=self._on_gift_npc,
                                   on_sworn=self._on_sworn_npc,
                                   on_marry=self._on_marry_npc,
                                   on_quest=self._on_quest_npc,
                                   on_treat=self._on_treat_npc,
                                   preset=self._preset, parent=self)
        if dlg.exec() and dlg.get_action():
            self._start_worker(player_action=dlg.get_action())

    def _on_npc_combat(self, npc):
        """[P7h] 与敌对 NPC 战斗：combat_player_controlled 开 CombatDialog 多回合，否则走场景回合一击制。"""
        if self._busy or not self._world:
            return
        controlled = self.svc._per_world(self._world, "combat_player_controlled",
                                         self._preset.combat_player_controlled, self._preset)
        if not controlled:
            # 兼容老一击制：combat preset_intent 走 SceneWorker（apply_intent combat 分支）
            self._start_worker(player_action=f"攻击{npc.name}",
                               preset_intent={"intent_type": "combat", "talk_to": npc.name,
                                              "resolved": True, "narration_hint": ""})
            return
        self._fight_npc_dialog(npc)

    def _on_quest_npc(self, npc):
        """[任务改造] NPC 面板接取/领奖后：存档 + 刷新世界状态（任务卡状态已变）。"""
        if not self._world:
            return
        self.storage.save_world(self._world)
        self.world_changed.emit()

    def _spectate_domain_siege(self):
        """[敌袭观战 2026-09-06 用户指示] 据点敌袭观战：组装守军 vs 来敌会话 ->
        CombatDialog 观战模式（双方自动交战，演出与正常战斗相同）-> 结束按 D7 口径
        结算（缴获/繁荣/负伤回写）+ 落盘刷新。读档残留的 pending 也会在 refresh 时
        经此补打（战斗没打完退出不丢敌袭）。"""
        if getattr(self, "_siege_dialog_open", False):
            return
        if self._world is None or self.svc is None:
            return
        session, domain = self.svc.build_domain_siege_session(self._world, self._preset)
        if session is None or domain is None:
            # [审核修复 2026-09-13] 残留 pending 必须清掉再返回：据点已删除/已易主/
            # 守方阵容已空时会话建不起来，而 refresh_state 见到 pending 又会回来调本函数，
            # 两边互相调用（本函数末尾也调 refresh_state）。清掉后走正常刷新路径。
            try:
                self._world.pending_domain_siege = {}
                self.storage.save_world(self._world)
            except Exception:
                pass
            self.refresh_state()
            return
        self._siege_dialog_open = True
        try:
            from src.ui.dialogs.combat_dialog import CombatDialog
            # target 参数观战模式仅作占位（不参与构建/结算）
            from src.models import NPC as _N
            placeholder = _N(name="据点敌袭", role="观战", hostile=False)
            dlg = CombatDialog(self.svc, self._world, placeholder, self._preset,
                               parent=self, spectate_session=session)
            dlg.exec()
            evts = self.svc.settle_spectated_siege(self._world, domain, session)
            for e in evts or []:
                try:
                    self._world.event_log.append(e)
                except Exception:
                    pass
        finally:
            self._siege_dialog_open = False
        try:
            self.storage.save_world(self._world)
        except Exception:
            pass
        self.refresh_state()
        self.world_changed.emit()

    def _fight_npc_dialog(self, npc):
        """[P7h/P12] 拉起回合制战斗 + 战后收尾（存档/刷新/永久死亡/叙事回合）。

        NPC 战斗与野外怪强制战斗共用：npc 可以是 world.npcs 实体，也可以是
        wilderness_engine 产出的临时怪物 NPC（战后丢弃不入库）。
        """
        from src.ui.dialogs.combat_dialog import CombatDialog
        dlg = CombatDialog(self.svc, self._world, npc, self._preset, parent=self)
        dlg.exec()
        summary = dlg.get_summary()
        if summary:
            # [P7k8] 永久死亡：玩家被击败 + permadeath 开启 -> 删世界回首页
            permadeath = self.svc._per_world(self._world, "permadeath_enabled",
                                             self._preset.permadeath_enabled, self._preset)
            if summary.get("player_defeated") and permadeath:
                QMessageBox.warning(self, "永久死亡",
                                    f"你在与{npc.name}的战斗中倒下，再也没能站起来……\n"
                                    f"（永久死亡模式：这个世界已被永久抹去。）")
                wid = self._world.id
                try:
                    # [!] 必须传 world_id（str）：传 World 对象会拼出非法路径，删除整体 no-op。
                    # 级联清理对称 world_sim_tab._delete_world：先清 NPC 记忆 Chroma 库再删文件。
                    if self.svc is not None:
                        self.svc.npc_memory().clear_world_memory(self._world)
                except Exception:
                    pass
                try:
                    self.storage.delete_world(wid)
                except Exception:
                    pass
                self.permadeath.emit(wid)
                return
            # [P58 摸格子] 战利品格子（won 且有掉落）：玩家取舍 -> loot_grid_settle 守恒收口，
            # 与战斗结算一起在下方 save_world 落盘。永久死亡删世界路径不弹（世界已没了）。
            if summary.get("combat_state") == "won" \
                    and (summary.get("loot_grid") or {}).get("entries"):
                from src.ui.dialogs.loot_grid_dialog import LootGridDialog
                ldlg = LootGridDialog(self.svc, self._world, summary["loot_grid"], parent=self)
                ldlg.exec()
                # [P47 因果可见性] 夺回/下落不明/留尸事实双写：当场可见 + 进场景日志
                # （存档/LLM 上下文可感知，替代原 finish_combat 内 parts 生成）
                for line in (ldlg.result_lines or []):
                    if self._scene is not None:
                        e = self._scene.append("system", line,
                                               tick=self._world.tick_count if self._world else 0)
                        self.log.add_entry("system", line, "系统", entry_id=e.id)
                    else:
                        self.log.add_entry("system", line, "系统")
            self.storage.save_world(self._world)
            self.refresh_state()
            self.world_changed.emit()
            # 战斗已由 CombatDialog 结算（hp/掉落/经验），叙事回合用 custom intent + narration_hint
            # 描写已发生战斗（apply_intent custom 不再重复结算数值）
            hint = summary.get("narration_hint", "")
            # [P12] post_combat 打标：战后叙事回合不再 roll 野外怪（防连环强制战斗）
            self._start_worker(player_action=f"与{npc.name}战斗",
                               preset_intent={"intent_type": "custom", "resolved": True,
                                              "narration_hint": hint, "post_combat": True})

    def _open_npc_profile(self, npc):
        """[P6c] 打开 NPC 档案（固定外貌可编辑，记忆默认隐藏）。"""
        if not self._world:
            return
        from src.ui.dialogs.npc_detail_dialog import NpcDetailDialog
        dlg = NpcDetailDialog(self._world, npc, world_sim_service=self.svc, parent=self,
                              on_changed=self.refresh_state)
        dlg.exec()

    def _open_shop_for_npc(self, npc):
        """[P6] 打开 NPC 的商店对话框（代码交易 + 按需 LLM 换货）。"""
        if not self._world:
            return
        shop = self.svc.shop_for_npc(self._world, npc.id)
        if shop is None:
            return
        from src.ui.dialogs.shop_dialog import ShopDialog
        dlg = ShopDialog(self._world, shop, self.storage,
                         on_changed=self.refresh_state,
                         on_regen_request=self._request_shop_regen, parent=self,
                         world_sim_service=self.svc, preset=self._preset)
        self._shop_dialog = dlg
        dlg.exec()
        self._shop_dialog = None

    def _request_shop_regen(self, shop_id: str):
        """[P6 -> 游商手动刷新 2026-09-09] 玩家在商店点「刷新商品」按店类型分流。

        - 游商店（无真商人 NPC）：调 svc.manual_vendor_refresh 确定性本地补货
          （同步瞬时完成，零 LLM），直接 save + 刷新场景页；
        - 真商人店：保留原来走 ShopWorker LLM 换货（后台，可能数十秒）。
        """
        if self._shop_worker is not None or not self._world or not self._preset:
            return
        shop = next((s for s in self._world.shops if s.id == shop_id), None)
        merchant = None
        if (shop is not None and self.svc is not None
                and getattr(self.svc, "_shop_merchant", None) is not None):
            try:
                merchant = self.svc._shop_merchant(self._world, shop)
            except Exception:
                merchant = None
        if shop is not None and merchant is None and self.svc is not None:
            # —— 游商：本地补货同步完成（manual_vendor_refresh 返回 ""=未处理时
            # 回退 LLM 链，防新口径漏店导致按钮点空）——
            name = self.svc.manual_vendor_refresh(self._world, shop_id, self._preset)
            if name:
                if not self._busy:
                    self.storage.save_world(self._world)
                    self.refresh_state()
                    self.world_changed.emit()
                    self._refresh_open_shop_dialog()
                self.status_label.setText(f"已补货：{name}")
                return
        from src.ui.dialogs.shop_worker import ShopWorker
        self._shop_worker = ShopWorker(self.svc, self._world, self._preset,
                                       [shop_id], parent=None)
        self._shop_worker.finished_signal.connect(self._on_shop_regen_done)
        self._shop_worker.finished.connect(self._on_shop_worker_done)
        self.status_label.setText("店主正在重新进货…")
        self._shop_worker.start()

    def _on_shop_regen_done(self, ok, payload):
        if not self._world:
            return
        # [!] 回合进行中不落盘：主线程 to_dict 与 SceneWorker 对 world 的结构性修改
        # 并发交错会撕裂快照；换货结果已在 world 对象上，回合结束统一 save。
        if self._busy:
            return
        if ok:
            self.storage.save_world(self._world)
            self.refresh_state()
            self.world_changed.emit()
            self._refresh_open_shop_dialog()
            names = payload if isinstance(payload, list) else []
            self.status_label.setText("已进货：" + "、".join(names) if names else "已进货")

    def _refresh_open_shop_dialog(self):
        """[游商店实时回显 2026-09-10] 补货/换货完成后让打开中的 ShopDialog 重渲染。

        ShopWorker（后台）/ manual_vendor_refresh（主线程）改的都是对话框持有的
        同一 world/shop 对象——数据已变，只补 UI 重渲染这一步（此前左侧货架要
        退出去重进才更新）。exec 返回即清引用，模态期内引用恒有效。
        [!] 前提：模态 exec 的嵌套事件循环照常派发 timer 与 queued 信号——墙钟
        自动换货与 ShopWorker finished 都是在对话框开着时送达本槽的，勿改坏。"""
        dlg = self._shop_dialog
        if dlg is None:
            return
        try:
            dlg.refresh_view()
        except RuntimeError:
            self._shop_dialog = None   # C++ 对话框已销毁（防御；正常路径 exec 返回即清）

    def _on_shop_worker_done(self):
        w = self._shop_worker
        self._shop_worker = None
        if w is not None:
            w.deleteLater()

    def _on_inventory(self):
        """打开背包/装备对话框（P3）。装备变更后刷新场景页。

        [P34d] 传入 open_refine/open_home 回调供背包右键菜单「鉴定·洗练」「入仓」
        链式打开器物台/住宅页（避免模态对话框嵌套）。
        """
        if not self._world:
            return
        from src.ui.dialogs.inventory_dialog import InventoryDialog
        dlg = InventoryDialog(
            self._world, self.storage, on_changed=self.refresh_state,
            open_refine=self._on_refine, open_home=self._on_home, parent=self)
        dlg.exec()

    def _on_quest_log(self):
        """[P7i] 打开任务日志（接取/追踪/领奖）。操作后 save_world + refresh。"""
        if not self._world:
            return

        def _on_changed():
            self.storage.save_world(self._world)
            self.refresh_state()
            self.world_changed.emit()

        from src.ui.dialogs.quest_log_dialog import QuestLogDialog
        dlg = QuestLogDialog(self._world, on_changed=_on_changed, parent=self)
        dlg.exec()

    def _on_codex(self):
        """[P7k6] 打开图鉴（地点/NPC/怪物/物品，只读展示）。"""
        if not self._world:
            return
        from src.ui.dialogs.codex_dialog import CodexDialog
        dlg = CodexDialog(self._world, parent=self, preset=self._preset)
        dlg.exec()

    def _on_refine(self):
        """[P34b] 打开器物台（鉴定/洗练装备 + 宠物资质洗练，纯引擎不调 LLM）。"""
        if not self._world:
            return

        def _on_changed():
            self.storage.save_world(self._world)
            self.refresh_state()
            self.world_changed.emit()

        from src.ui.dialogs.refine_dialog import RefineDialog
        dlg = RefineDialog(self._world, on_changed=_on_changed, parent=self)
        dlg.exec()

    def _on_stock(self):
        """[P34f] 打开交易所股市（买卖题材化大宗商品，纯引擎结算无 LLM）。"""
        if not self._world:
            return

        def _on_changed():
            self.storage.save_world(self._world)
            self.refresh_state()
            self.world_changed.emit()

        from src.ui.dialogs.stock_dialog import StockDialog
        dlg = StockDialog(self._world, storage=self.storage,
                          on_changed=_on_changed, parent=self)
        dlg.exec()

    def _on_auction(self):
        """[P34g] 打开拍卖会竞价（限时拍品，押金模式竞价，纯引擎结算）。"""
        if not self._world:
            return

        def _on_changed():
            self.storage.save_world(self._world)
            self.refresh_state()
            self.world_changed.emit()

        from src.services import auction_engine as _aue
        _loc = self.svc._current_location(self._world)
        auction = _aue.active_auction_at(self._world, _loc.id)
        if auction is None:
            return
        from src.ui.dialogs.auction_dialog import AuctionDialog
        dlg = AuctionDialog(self._world, auction, storage=self.storage,
                            on_changed=_on_changed, parent=self)
        dlg.exec()

    def _on_commission(self):
        """[P39b] 打开委托订单板（NPC 收购单，当面交付结算）。"""
        if not self._world:
            return

        def _on_changed():
            self.storage.save_world(self._world)
            self.refresh_state()
            self.world_changed.emit()

        from src.ui.dialogs.commission_dialog import CommissionDialog
        dlg = CommissionDialog(self._world, storage=self.storage,
                               on_changed=_on_changed, parent=self)
        dlg.exec()

    def _on_attribute(self):
        """[P7d2] 打开属性加点面板（分配升级获得的属性点）。"""
        if not self._world:
            return

        def _on_changed():
            self.storage.save_world(self._world)
            self.refresh_state()
            self.world_changed.emit()

        from src.ui.dialogs.attribute_dialog import AttributeDialog
        dlg = AttributeDialog(self._world, on_changed=_on_changed, parent=self)
        dlg.exec()

    # ============ worker ============
    def _start_worker(self, player_action, preset_intent=None, reuse_entry=None):
        if self._busy or not self._world or not self._scene:
            return
        # [!] 防重入：若旧 worker 仍在跑（load_world 重入/快速切页），先清理（守审查 D2）
        if self._worker is not None:
            if not self._cleanup_worker():
                return
        # [!] 商店换货 worker 在跑时不可并发：ShopWorker 与 SceneWorker 会同时改
        # 同一个 World 对象（regen_shops 清空/重填 shops vs apply_intent/tick_world
        # 遍历修改），主线程 to_dict 落盘与 worker 结构性修改交错可撕裂快照。
        # 换货是 best-effort（下个墙钟周期会再来），直接中断让位给玩家回合。
        if self._shop_worker is not None:
            self._cleanup_shop_worker()
        # 玩家行动先入旁白流 + 场景日志（旁白随后流式追加）
        # [断网韧性] reuse_entry 非空 = 重试失败回合：玩家条目已在 scene（快照含它），
        # 不重复 append，只重新 stash 待回填引用
        entry_id = None
        if reuse_entry is not None:
            self._pending_player_entry = reuse_entry
            entry_id = getattr(reuse_entry, "id", "")
        elif player_action is not None:
            # [P23] 先 append 拿到 entry（含 id），再 add_entry 传 id 建立映射（右键删除用）
            e = self._scene.append("player", player_action, tick=self._world.tick_count)
            self.log.add_entry("player", player_action, "你", entry_id=e.id)
            self.storage.save_scene(self._scene)
            # [P29] stash 玩家条目引用：talk_to 在 worker settle 后才产出，_on_finished 回填 meta
            self._pending_player_entry = e
            entry_id = e.id
        # [断网韧性 P45(3)] 回合事务性快照：玩家条目已入 scene 后拍摄（失败回滚保留
        # 玩家行动可见——「你的行动还在，旁白生成失败，右键重试」）；拍摄失败退化
        # 为旧行为（失败不回滚，靠 _tick_before 漂移落盘兜底）
        self._turn_snapshot = None
        try:
            self._turn_snapshot = (self._world.to_dict(), self._scene.to_dict())
        except Exception:
            self._turn_snapshot = None
        self._last_turn = {"player_action": player_action,
                           "preset_intent": preset_intent, "entry_id": entry_id}
        self._failed_turn = None    # 新回合开始，旧失败 stash 作废
        # 流式旁白占位
        self.log.start_streaming("旁白")
        self._set_busy(True)
        self.status_label.setText("结算中…")
        # [!] 记录回合前 tick：worker 后段（滴答/生图）失败时 ok=False，但引擎结算
        # （tick 推进/入包/战斗）已在子线程发生——不落盘会让内存与磁盘永久漂移。
        # （快照回滚路径不依赖此值；快照拍摄失败的退化路径仍用它兜底。）
        self._tick_before = int(self._world.tick_count)

        self._worker = SceneWorker(
            self.svc, self._world, self._scene, self._preset,
            player_action=player_action, preset_intent=preset_intent,
            turn_snapshot=self._turn_snapshot, parent=None,
        )
        self._pending_images.clear()
        self._worker.stage.connect(self._on_stage)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.usage.connect(self._on_usage)
        self._worker.error.connect(self._on_error)
        # 场景事件生图信号（§21c 独立铁律，独立于 ChatOrchestrator.on_image）
        self._worker.image_generated.connect(self._on_image_generated)
        self._worker.finished_signal.connect(self._on_finished)
        # finished -> deleteLater（守 §15 worker 生命周期）
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    def _on_stage(self, text: str):
        self.status_label.setText(text)

    def _on_chunk(self, text: str):
        self.log.append_streaming(text)

    def _on_image_generated(self, image_filename: str):
        """暂存出图，等回合成功且旁白条目落盘后再绑定并显示。"""
        origin = self.sender()
        if origin is not None and origin is not self._worker:
            return  # 旧 worker 的排队信号，不能附到新世界/新回合
        if not image_filename or not self._world:
            return
        if ("/" in image_filename or "\\" in image_filename
                or not image_filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))):
            return
        if image_filename not in self._pending_images:
            self._pending_images.append(image_filename)


    def _on_usage(self, api_id: str, usage):
        # P2 不接统计页，仅日志
        pass

    def _on_error(self, text: str):
        self.status_label.setText(f"错误：{text}")

    # ============ [P23] 右键删除单条消息（player/narrator/system 均可删）============
    def _on_entry_context_menu(self, entry_id: str, pos):
        # 生成中禁删（流式占位泡/scene.log 正在变，守 §15 worker 生命周期）
        if self._busy or not self._world or not self._scene:
            return
        # 取预览文本（菜单提示用）
        entry = next((e for e in self._scene.log if e.id == entry_id), None)
        if entry is None:
            return
        preview = (entry.content or "").strip().replace("\n", " ")[:20]
        menu = QMenu(self)
        # [断网韧性 P45(3)] 失败回合重试：本条目是失败回合的玩家行动时提供
        # （菜单构建与 exec 分离，守 [P34d] offscreen 测试契约）
        retryable = bool(self._failed_turn
                         and self._failed_turn.get("entry_id") == entry_id)
        act_retry = None
        if retryable:
            act_retry = menu.addAction("↻ 重试本回合（上次生成失败已回滚）")
        act_del = menu.addAction("🗑 删除该条消息" + (f"（{preview}…）" if preview else ""))
        chosen = menu.exec(pos)
        if chosen is act_retry and act_retry is not None:
            self._retry_failed_turn()
        elif chosen is act_del:
            self._delete_scene_entry(entry_id)

    def _retry_failed_turn(self):
        """[断网韧性 P45(3)] 重试失败回合：复用 scene 里已有的玩家条目（不重复 append），
        重新走完整 settle -> narrate -> tick 链路（重拍快照）。"""
        info = self._failed_turn
        self._failed_turn = None
        if not info:
            return
        entry = next((e for e in (self._scene.log if self._scene else [])
                      if e.id == info.get("entry_id")), None)
        if entry is None:
            # 玩家条目已被手动删除：退化为普通回合（重新 append 行动文本）
            self._start_worker(info.get("player_action"),
                               preset_intent=info.get("preset_intent"))
            return
        self._start_worker(info.get("player_action"),
                           preset_intent=info.get("preset_intent"),
                           reuse_entry=entry)

    def _delete_scene_entry(self, entry_id: str):
        if not self._scene or not entry_id:
            return
        # 弹确认框（仿单聊 _on_delete_message，防误删）
        ret = QMessageBox.question(
            self, "删除消息",
            "确定删除该条消息？删除后该条不再进入后续叙事的历史上下文。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        if not self._scene.delete_entry(entry_id):
            return
        self.storage.save_scene(self._scene)
        if self._scene_image_state is not None and self._scene_image_state.forget(entry_id):
            self.storage.save_scene_image_state(self._scene_image_state)
        self.log.remove_entry_bubble(entry_id)
        self.status_label.setText("旁白已删除")

    def _on_finished(self, ok: bool, payload):
        origin = self.sender()
        if origin is not None and origin is not self._worker:
            return  # 取消清理后已排队的旧回合不能再次存档或更新另一份世界
        if not ok:
            self._pending_images.clear()
            # [P20] 错误路径 payload 现为 dict（含 msg + speech_updated），兼容旧字符串
            if isinstance(payload, dict):
                err_msg = str(payload.get("msg") or "场景交互失败")
                speech_updated = bool(payload.get("speech_updated"))
                rolled_back = bool(payload.get("rolled_back"))
            else:
                err_msg = str(payload or "场景交互失败")
                speech_updated = False
                rolled_back = False
            if rolled_back:
                # [断网韧性 P45(3)] 事务性回滚：worker 已原地恢复快照（settle 引擎修改
                # 撤销、tick 未跑），丢弃半截流式旁白 + 刷新 UI 回快照态 + stash 重试。
                # 不落盘（世界未变过）；玩家条目保留在 scene/日志（快照含它）。
                self.log.finalize_streaming("")    # 丢弃部分流式正文（重试会重新生成）
                self._set_busy(False)
                self.refresh_state()
                if self._last_turn is not None:
                    self._failed_turn = self._failed_turn or dict(self._last_turn)
                self.status_label.setText("回合失败已回滚（右键你的行动可重试本回合）")
                QMessageBox.warning(
                    self, "回合失败（已回滚）",
                    err_msg + "\n\n本回合未生效、世界未推进。在场景日志中右键你的行动条目"
                    "选择「重试本回合」，或直接继续其他行动。")
                return
            self.log.cancel_streaming_keep()
            self._set_busy(False)
            self.status_label.setText("")
            # [!] 引擎结算已在子线程发生（tick 推进/入包/战斗）时不能只弹框丢弃：
            # 检测 tick 变化则落盘 + 刷新，防内存与磁盘状态漂移（重开丢进度）。
            # （快照回滚路径已提前 return；此处是快照拍摄失败的退化兜底。）
            # [P20] 腔调补完也需落盘（ensure 在错误前已跑，补完的 speech_style 只存内存，
            # 重开世界读空会再触发补生成，违反幂等契约）。
            if (self._world is not None
                    and (int(self._world.tick_count) != int(getattr(self, "_tick_before", -1))
                         or speech_updated)):
                try:
                    self.storage.save_world(self._world)
                    self.refresh_state()
                except Exception:
                    pass
            self.refresh_log_panel()
            QMessageBox.warning(self, "场景交互失败", err_msg)
            return
        data = payload or {}
        if (data.get("settle_summary") or {}).get("player_defeated"):
            if self._handle_player_permadeath(defeated=True):
                return
        narration = data.get("narration", "")
        cancelled = data.get("cancelled", False)
        narration_entry_id = None
        # [P29] talk_to 归因：写入场景日志 meta 供折叠时批量整理记忆用（哪个 NPC 在场对话）
        _intent = data.get("intent") or {}
        _talk_to = str(_intent.get("talk_to", "") or "")
        # [P29] 回填玩家条目 meta.talk_to（player entry 在 worker 启动前 append，intent 当时未产出）
        if self._pending_player_entry is not None:
            self._pending_player_entry.meta["talk_to"] = _talk_to
            self._pending_player_entry = None
        # [P5] 回合分隔气泡：先 finalize 流式旁白定稿，再插入回合分隔，把本回合旁白
        # 视觉上「夹在」两条分隔线之间。但若先 insert 再 finalize 视觉上分隔在下。
        # 我们采用「先 finalize 本回合旁白，再 insert 分隔作为下一回合的开始标记」——
        # 也就是本回合结束后看到「回合 N · 地点」分隔，下一回合旁白继续往下。
        # 旁白落场景日志
        if self._scene is not None:
            if narration:
                e = self._scene.append("narrator", narration, tick=self._world.tick_count if self._world else 0,
                                       meta={"talk_to": _talk_to})
                narration_entry_id = e.id
                self.log.attach_streaming_entry(e.id)   # [P23] 流式占位泡回填 entry_id（启用右键删除）
            s = data.get("settle_summary") or {}
            if s.get("reason"):
                # P3 combat 结果带 meta 供日志结构化展示
                meta = {"combat": {k: s[k] for k in
                        ("damage_dealt", "damage_taken", "target_npc_id", "target_npc_defeated",
                         "xp_gained", "leveled_up", "new_level", "items_dropped")
                        if k in s}} if "damage_dealt" in s else None
                if meta is None:
                    meta = {}
                meta["talk_to"] = _talk_to  # [P29] 系统条目也带 talk_to 归因
                e = self._scene.append("system", s["reason"],
                                       tick=self._world.tick_count if self._world else 0, meta=meta)
                # [P0 修复 2026-09-25] 补屏幕落屏：原实现只写 SceneLog（存档），玩家在当前
                # 会话看不到「行动为什么没成」，要退出世界重进才显示。与 :2352 玩家条目的
                # append + add_entry 双写口径对齐（场景日志负责持久化，log 负责当场可见）。
                self.log.add_entry("system", s["reason"], "系统", entry_id=e.id)
                # [开局 30 分钟改造包] 受阻「说人话」引导：只进当场旁白流（不入 SceneLog，
                # 防引导文案进 LLM 上下文/存档；引擎 reason 原样保留守结算真相）。
                _hint = blocked_guidance(_intent, s["reason"])
                if _hint:
                    self.log.add_entry("assistant", _hint, "提示")
            self.storage.save_scene(self._scene)
            # P3 扩展保存触发：移动 / 战斗 / 使用物品 / 回血 都要 save_world + 刷新
            # P4：世界滴答改变了世界状态（event_log/势力/NPC）也需 save_world + 刷新
            tr = data.get("tick_report") or {}
            # [审核修复 2026-09-13] 阶段异常可见化：tick 里被跳过（异常）的子系统进主对话流。
            # 原实现只写 debug 日志，玩家与开发者都看不到「世界的某一块已经不转了」——
            # 本次 P0（剧情线规划链 NameError 被 _phase 静默吞掉）正是这样潜伏下来的。
            _skipped = tr.get("skipped_phases") or []
            if _skipped:
                _names = "、".join(str(x.get("phase", "?")) for x in _skipped)
                _err = str((_skipped[0] or {}).get("error", ""))[:80]
                _msg = f"[世界模拟] 本回合有 {len(_skipped)} 个子系统异常被跳过：{_names}（{_err}）"
                e = self._scene.append("system", _msg,
                                       tick=self._world.tick_count if self._world else 0)
                # [P0 修复 2026-09-25] §21 明文要求这条「写进主对话流」，原实现只写了存档
                # ——「世界的某一块已经不转了」在当前会话里看不见（因果可见性失效）。
                self.log.add_entry("system", _msg, "系统", entry_id=e.id)
            world_changed = bool(tr.get("events")) or bool(tr.get("faction_changes")) \
                or bool(tr.get("npc_changes")) or bool(_skipped)
            # [P7g] 采集（gather）也触发 save_world + refresh（背包/丰度/冷却都变了）
            # [P20] 腔调补生成也触发：纯旁白/聊天回合无上述事件，但本回合补完的 NPC
            # speech_style 必须落盘，否则重开世界后读空又触发补生成，违反「补完永不触发」幂等契约。
            speech_updated = bool(data.get("speech_updated"))
            if s.get("moved") or "damage_dealt" in s or "healed" in s or s.get("xp_gained") \
                    or s.get("world_changed") or "gathered" in s or world_changed \
                    or speech_updated:
                self.storage.save_world(self._world)
                self.refresh_state()
                self.world_changed.emit()
        # finalize 旁白流（先把流式旁白落定）
        self.log.finalize_streaming(narration)
        # 插图只绑定成功落盘的旁白；图片路径留在独立状态，不进入 LLM 场景上下文。
        if narration_entry_id and self._pending_images and self._world is not None:
            from src.models.scene_image_state import SceneImageState
            # worker 刚更新了同一状态文件里的限频计数；必须重读再绑定，避免旧快照覆写计数。
            try:
                state = self.storage.load_scene_image_state(self._world.id)
            except (AttributeError, OSError, ValueError):
                state = self._scene_image_state or SceneImageState(world_id=self._world.id)
            if state.attach(narration_entry_id, self._pending_images):
                self._scene_image_state = state
                try:
                    self.storage.save_scene_image_state(state)
                except (AttributeError, OSError, ValueError):
                    pass
            for filename in self._pending_images:
                image_path = os.path.join(paths.world_images_dir(), filename)
                if os.path.isfile(image_path):
                    self.log.add_image(image_path, entry_id=narration_entry_id)
        self._pending_images.clear()
        # [断网韧性] 回合成功（含取消路径——取消是玩家主动，不算失败）：清失败 stash
        self._failed_turn = None
        self._turn_snapshot = None
        # [P5] 插入回合分隔气泡（仅当 narration 非空时；首回合空旁白没必要分隔）
        if narration and not cancelled and self._world is not None:
            self._insert_round_divider(self._world.tick_count)
        self._set_busy(False)
        # [修 2026-09-06 真机 R42] 上方 save 块里的 refresh_state 是在 busy=True 时跑的，
        # 秘境/讨伐等按钮被 `and not self._busy` 钳成禁用后无人再刷 -> 玩家刚进秘境
        # 按钮就灰的（看似被困）。解锁后补一次刷新恢复按钮启用态。
        self.refresh_state(allow_encounters=not cancelled)
        self.status_label.setText("已停止" if cancelled else "")
        # [P12] 野外遇怪：引擎结算产出 forced_combat（wilderness_engine 临时怪物 NPC）
        # -> 强制拉起回合制战斗（monster 不入 world.npcs，战后丢弃）。
        s_all = data.get("settle_summary")
        monster = s_all.get("forced_combat") if isinstance(s_all, dict) else None
        if monster is not None and not cancelled and self._world is not None:
            self._fight_npc_dialog(monster)
        # [C-lite 2026-10-02 用户指示] 同地点敌对 NPC 主动袭击：引擎 roll 命中产出
        # npc_ambush（真实 NPC，区别于 forced_combat 临时怪）-> 同一战斗对话框链路
        # （胜败/掉落/kill 进度全真实记账；战后叙事回合自动接「与X战斗」）。
        ambusher = s_all.get("npc_ambush") if isinstance(s_all, dict) else None
        if ambusher is not None and not cancelled and self._world is not None:
            if not getattr(ambusher, "alive", True):
                self.storage.save_world(self._world)   # 极端时序：袭击者在结算后已倒下
            else:
                self._fight_npc_dialog(ambusher)
        # [P58 摸格子] 快速战斗（一击制）战后：结算摘要带 loot_grid -> 弹格子拾取
        # （同 forced_combat 先例：回合外主线程弹 UI，_busy 已解除）。
        _lg = s_all.get("loot_grid") if isinstance(s_all, dict) else None
        if _lg and _lg.get("entries") and not cancelled and self._world is not None:
            from src.ui.dialogs.loot_grid_dialog import LootGridDialog
            ldlg = LootGridDialog(self.svc, self._world, _lg, parent=self)
            ldlg.exec()
            # [P47 因果可见性] 结算事实双写（同正式战斗路径口径）
            for line in (ldlg.result_lines or []):
                if self._scene is not None:
                    _e = self._scene.append("system", line,
                                            tick=self._world.tick_count if self._world else 0)
                    self.log.add_entry("system", line, "系统", entry_id=_e.id)
                else:
                    self.log.add_entry("system", line, "系统")
            self.storage.save_world(self._world)
            self.refresh_state()
            self.world_changed.emit()
        # [敌袭观战 2026-09-06 用户指示] 本回合据点敌袭定格（玩家在据点时不再后台速算）
        # -> 弹观战战斗（守军 vs 来敌全自动演出），打完按 D7 口径结算回写。
        if not cancelled and self._world is not None                 and getattr(self._world, "pending_domain_siege", None):
            self._spectate_domain_siege()

    def _insert_round_divider(self, tick: int):
        """[P5] 在旁白流顶部插入「回合 N · 地点」分隔气泡，沉浸感节奏点。

        实现：直接构造一个 QFrame(对象名 roundDivider) + 文字标签，不走 MessageBubble
        （避免 system role 被渲染成普通 aiBubble）。样式由 theme.qss #roundDivider 接管。
        不入场景日志（仅视觉装饰）；取消/失败时不插入。
        """
        from PySide6.QtWidgets import QFrame as _F, QLabel, QVBoxLayout
        loc = self.svc._current_location(self._world) if self._world else None
        loc_name = loc.name if loc else "未知地点"
        frame = _F()
        frame.setObjectName("roundDivider")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(4, 6, 4, 6)
        fl.setSpacing(2)
        head = QLabel(f"回合 {tick} | 地点:{loc_name}")
        fl.addWidget(head)
        # 加到旁白流（NarrationLog 内部 _layout + stretch）
        self.log._layout.insertWidget(self.log._layout.count() - 1, frame)
        self.log.scroll_to_bottom()

    def _on_worker_done(self):
        w = self._worker
        self._worker = None
        if w is not None:
            w.deleteLater()
        # [P29] 兜底清空 stash：_on_finished 正常路径已回填+清空，此处防异常/取消漏清致下回合误回填
        self._pending_player_entry = None

    # ============ 生命周期 ============
    @staticmethod
    def _safe_delete_worker(w):
        """[!] wait 超时后线程可能仍在跑（ComfyUI 生图阻塞不可取消），此时直接
        deleteLater 会在 QThread 析构时 abort（Destroyed while running）。
        未结束则把 deleteLater 挂到 finished 信号，让线程自然收尾后再销毁。"""
        if w is None:
            return
        if w.isFinished():
            w.deleteLater()
        else:
            try:
                w.finished.connect(w.deleteLater)
            except (RuntimeError, TypeError):
                w.deleteLater()

    def _on_back(self):
        if self._worker is not None:
            if not self._cleanup_worker(mark_cancelled=True):
                if not self._back_when_stopped:
                    self._back_when_stopped = True
                    self._worker.finished.connect(self._resume_back)
                    if self._worker.isFinished():
                        QTimer.singleShot(0, self._resume_back)
                return
        if self._handle_player_permadeath():
            return
        self._back_when_stopped = False
        self.back_requested.emit()

    def _resume_back(self):
        if self._back_when_stopped:
            self._back_when_stopped = False
            self._on_back()

    def _handle_player_permadeath(self, defeated: bool = False) -> bool:
        """主线程单次收口；取消/返回同样不能撤销已结算的致命结果。"""
        if self._world is None:
            return False
        if self._permadeath_handled_id == self._world.id:
            return True
        if ((not defeated and self._world.player.hp > 0) or not self.svc._per_world(self._world, "permadeath_enabled",
                self._preset.permadeath_enabled if self._preset else False, self._preset)):
            return False
        self._permadeath_handled_id = self._world.id
        self._back_when_stopped = False
        self._pending_images.clear()
        self._set_busy(False)
        QMessageBox.warning(self, "永久死亡", "你倒下后再也没能站起来……\n（永久死亡模式：这个世界已被永久抹去。）")
        wid = self._world.id
        try:
            self.svc.npc_memory().clear_world_memory(self._world)
        except Exception:
            pass
        self.storage.delete_world(wid)
        self.permadeath.emit(wid)
        return True

    def _cleanup_shop_worker(self):
        """商店换货 worker 清理（守 §15；cleanup 与 _start_worker 让位时共用）。"""
        sw = self._shop_worker
        if sw is None:
            return
        try:
            sw.finished_signal.disconnect()
            sw.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        sw.cancel()
        sw.wait(5000)
        self._shop_worker = None
        self._safe_delete_worker(sw)

    def _cleanup_worker(self, mark_cancelled: bool = False):
        """守 §15：disconnect + cancel + wait。

        mark_cancelled=True（玩家点返回/切页离开，而非开新回合前的防重入清理）：
        回合被中断后补一条 system 收尾——玩家的行动条目早已落盘，而世界的回应因
        disconnect 永不落盘，不补的话场景日志会永久留一句「无回应的行动」。
        """
        w = self._worker
        if w is None:
            return True
        _was_running = not w.isFinished()
        # 先请求取消并确认线程停止。超时仍保留原 finished 槽和引用，不能边写边存或切回旧档。
        w.cancel()
        if not w.wait(5000):
            self.status_label.setText("正在停止当前行动，完成保存后即可离开…")
            return False
        try:
            w.stage.disconnect()
            w.chunk.disconnect()
            w.usage.disconnect()
            w.error.disconnect()
            # [!] P2 场景事件生图信号必须 disconnect：worker deleteLater 后若
            # image_generated 仍排队（取消后 ComfyUI 阻塞路径），不 disconnect 会致
            # _on_image_generated 槽访问已销毁对象 0xC0000409 崩溃。
            w.image_generated.disconnect()
            w.finished_signal.disconnect()
            w.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._pending_images.clear()
        self._worker = None
        self._safe_delete_worker(w)
        if mark_cancelled and self._handle_player_permadeath():
            return True
        if mark_cancelled and self._world is not None:
            self.storage.save_world(self._world)  # 取消保留已结算结果，tick 不变也必须保存
        if mark_cancelled and _was_running:
            self._mark_turn_cancelled()
        return True

    def _mark_turn_cancelled(self):
        """[审查修复 2026-09-25] 回合中途离开的收尾：把「世界没回应」写进场景日志，
        消掉「玩家说了一句话、下面什么都没有」的悬空记录。best-effort，失败不阻断退出。"""
        if self._scene is None:
            return
        try:
            _msg = "（本回合旁白已取消，已结算的行动结果保留）"
            e = self._scene.append("system", _msg,
                                   tick=self._world.tick_count if self._world else 0)
            self.storage.save_scene(self._scene)
            self.log.add_entry("system", _msg, "系统", entry_id=e.id)
        except Exception:
            pass

    def cleanup(self):
        """外部切页/关窗前调用，停止 worker。"""
        # [P6] 停商店墙钟定时器 + 清理商店换货 worker（守 §15）
        self._stop_shop_timer()
        if self._shop_worker is not None:
            self._cleanup_shop_worker()
        if self._worker is not None:
            return self._cleanup_worker(mark_cancelled=True)
        return True
