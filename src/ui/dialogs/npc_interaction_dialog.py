"""世界模拟 NPC 交互对话框：展示 NPC 信息 + 快捷行动（P2 场景交互循环）。

点击快捷行动按钮 accept 返回行动文本（如「与{npc.name}交谈」），SceneView 据此起一轮叙事。
[P10] 增加交情显示 + 加好友/私聊入口（回调由场景页注入：on_friend(world,npc)/on_chat(npc)）。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QProgressBar,
)

from src.config import paths
from src.models import NPC, WorldSimPreset
from src.ui.widgets.card_grid import make_card_pixmap
from src.services import npc_reaction_engine as nre
from src.services import relation_engine as reng
from src.services import quest_engine as qe


class NpcInteractionDialog(QDialog):
    """NPC 交互面板：展示信息 + 快捷行动按钮。"""

    def __init__(self, npc: NPC, location_name: str = "", on_shop=None, on_profile=None,
                 on_combat=None, world=None, on_friend=None, on_chat=None,
                 on_companion=None, on_gift=None, on_sworn=None, on_marry=None,
                 on_quest=None, preset=None, parent=None, on_treat=None):
        super().__init__(parent)
        self.npc = npc
        self.on_shop = on_shop      # [P6] 商售 NPC 交易回调：on_shop(npc) -> 开 ShopDialog
        self.on_profile = on_profile  # [P6c] 查看档案回调：on_profile(npc) -> 开 NpcDetailDialog
        self.on_combat = on_combat  # [P7h] 敌对 NPC 战斗回调：on_combat(npc) -> 开 CombatDialog
        self.world = world          # [P10] 传入才能显示交情/好友（None 兼容旧调用）
        self.on_friend = on_friend  # [P10] 加好友回调：on_friend(npc)（内部完成加好友 + save）
        self.on_chat = on_chat      # [P10] 私聊回调：on_chat(npc)
        self.on_companion = on_companion  # [P10b] 邀请同行/退队回调：on_companion(npc)（按当前队伍状态分流）
        self.on_gift = on_gift      # [P16] 玩家送礼回调：on_gift(npc)（选背包物品赠送，交情提升）
        self.on_sworn = on_sworn    # [P26a] 结义回调：on_sworn(npc)（条件满足时显示）
        self.on_marry = on_marry    # [P26a] 求婚回调：on_marry(npc)（条件满足时显示）
        self.on_quest = on_quest    # [任务改造] 任务接取/领奖回调：on_quest(npc)（save+refresh 后重建面板）
        self.on_treat = on_treat    # [G01/R3] 求医回调：on_treat(npc)（医者+玩家带伤时显示）
        # [P26a] preset 用于 can_sworn/can_propose 门控（relations_enabled）；None 兜底默认开
        self.preset = preset if preset is not None else WorldSimPreset()
        self.selected_action: str | None = None
        self._location_name = location_name or ""
        self.setWindowTitle(f"与 {npc.name} 互动")
        self.resize(460, 500)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)
        self._build(outer)

    # ---- [修 2026-09-13 用户报告] 面板重建 ----
    # 接取/领奖/加好友/同行/送礼/结义/求婚/私聊/战斗等回调会改 NPC/任务状态，但面板
    # 只在 __init__ 渲染一次——注释承诺「save+refresh 后重建面板」却从未实现，任务卡
    # 接取后仍显示「接取任务」、好友按钮不变、交情条 stale。改为：内容全部进 _build，
    # 状态变更回调经 _wrap 包装（回调完成后整面板重建）；_clear_layout 递归清嵌套布局。

    def _build(self, outer: QVBoxLayout):
        """构建面板内容（__init__ 与 _rebuild 共用；状态从 self.npc/self.world 现读）。"""
        npc = self.npc
        location_name = self._location_name

        # 头像 + 名字/身份
        head = QHBoxLayout()
        head.setSpacing(12)
        av = QLabel()
        av.setFixedSize(64, 64)
        av.setPixmap(make_card_pixmap(npc.name, npc.avatar, paths.world_images_dir(), 64))
        av.setStyleSheet("border-radius:32px; border:2px solid #6b5a35; background:#1d1e2a;")
        head.addWidget(av)
        info = QVBoxLayout()
        info.setSpacing(3)
        # [UI 改造 2026-10-01] 头部强化（全游戏最高频入口）：大金字大名 + 金色描边头像
        name = QLabel(npc.name)
        name.setObjectName("bannerName")
        info.addWidget(name)
        from src.ui.widgets.game_widgets import card_sub, game_badge
        from PySide6.QtWidgets import QWidget as _W, QHBoxLayout as _H
        badge_row = _W()
        bh = _H(badge_row)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.setSpacing(6)
        bh.addWidget(game_badge(npc.role or "平民", "info"))
        if location_name:
            bh.addWidget(game_badge(f"在 {location_name}", "warn"))
        if not getattr(npc, "alive", True):
            bh.addWidget(game_badge("已倒下", "danger"))
        elif getattr(npc, "hostile", False):
            bh.addWidget(game_badge("敌对", "danger"))
        if getattr(npc, "is_key_npc", False):
            bh.addWidget(game_badge("要角", "gold"))
        bh.addStretch()
        info.addWidget(badge_row)
        # [P10] 交情条（好友加星标）
        if self.world is not None:
            aff = int(getattr(npc, "affinity", 0))
            friend = nre.is_friend(self.world, npc)
            bar = QProgressBar()
            bar.setObjectName("hpBar")
            bar.setTextVisible(True)
            bar.setFixedHeight(12)
            bar.setRange(0, 100)
            bar.setValue(aff)
            bar.setFormat(("★好友 " if friend else "") + f"交情 {nre.affinity_level(aff)} {aff}/100")
            bar.setProperty("hpLevel", "low" if aff < 40 else "mid" if aff < 70 else "high")
            bar.style().unpolish(bar)
            bar.style().polish(bar)
            info.addWidget(bar)
        head.addLayout(info, 1)
        outer.addLayout(head)

        # 详情卡
        box = QFrame()
        box.setObjectName("gameCard")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(12, 10, 12, 10)
        bl.setSpacing(6)
        if npc.personality:
            bl.addWidget(self._line("性格", npc.personality))
        # [A+B 2026-09-06] 察言观色：此刻念头/今日目标直接可见（用户拍板无门槛直读）
        _th = (getattr(npc, "current_thought", "") or "").strip()
        if _th:
            bl.addWidget(self._line("念头", _th))
        _dg = ((getattr(npc, "current_goal", "") or "").strip()
               or (getattr(npc, "daily_goal", "") or "").strip())
        if _dg:
            bl.addWidget(self._line("今日目标", _dg))
        if npc.goal:
            bl.addWidget(self._line("长期目标", npc.goal))
        if npc.appearance:
            bl.addWidget(self._line("外貌", npc.appearance))
        if npc.desc:
            bl.addWidget(self._line("备注", npc.desc))
        outer.addWidget(box)

        outer.addStretch()

        # [P6c] 查看档案入口（任何 NPC 都可看档案）
        if self.on_profile is not None:
            profile_btn = QPushButton("查看档案")
            profile_btn.clicked.connect(lambda _=False: self.on_profile(self.npc))
            outer.addWidget(profile_btn)

        # [P10] 好友入口：未加好友 -> 加好友（交情是否达标由回调侧校验提示）；已是好友 -> 私聊
        if self.world is not None and self.on_friend is not None:
            if nre.is_friend(self.world, npc):
                if self.on_chat is not None:
                    chat_btn = QPushButton("私聊")
                    chat_btn.setObjectName("primaryBtn")
                    chat_btn.clicked.connect(self._wrap(self.on_chat))
                    outer.addWidget(chat_btn)
            else:
                add_btn = QPushButton("加为好友")
                add_btn.clicked.connect(self._wrap(self.on_friend))
                outer.addWidget(add_btn)

        # [P10b] 同行入口：已在队中 -> 道别退队；未在队中 -> 邀请同行（交情/队伍满由回调侧校验提示）
        if (self.world is not None and self.on_companion is not None
                and not getattr(npc, "hostile", False)):
            if nre.is_companion(self.world, npc):
                part_btn = QPushButton("道别（退队）")
                part_btn.clicked.connect(self._wrap(self.on_companion))
                outer.addWidget(part_btn)
            else:
                follow_btn = QPushButton("邀请同行")
                follow_btn.clicked.connect(self._wrap(self.on_companion))
                outer.addWidget(follow_btn)

        # [P16] 玩家送礼入口：非敌对存活 NPC（背包空/选物品/校验由回调侧处理提示）
        if (self.world is not None and self.on_gift is not None
                and not getattr(npc, "hostile", False) and getattr(npc, "alive", True)):
            gift_btn = QPushButton("送礼")
            gift_btn.setToolTip("从背包挑一件物品送给对方：品级越高、越合其爱好（看档案的爱好栏），交情涨得越多。")
            gift_btn.clicked.connect(self._wrap(self.on_gift))
            outer.addWidget(gift_btn)

        # [P26a] 结义入口：relation_engine.can_sworn 通过时显示（交情>=80 + 并肩作战>=3 + 未结义）
        if (self.world is not None and self.on_sworn is not None
                and not getattr(npc, "hostile", False) and getattr(npc, "alive", True)):
            ok_sworn, _ = reng.can_sworn(self.world, npc, self.preset)
            if ok_sworn:
                sworn_btn = QPushButton("结义")
                sworn_btn.setToolTip("与对方义结金兰：此后并肩作战时，彼此普攻伤害加成。需交情>=80 且曾并肩作战 3 次以上。")
                sworn_btn.clicked.connect(self._wrap(self.on_sworn))
                outer.addWidget(sworn_btn)

        # [P26a] 求婚入口：relation_engine.can_propose 通过时显示（恋人阶段 + 交情>=90 + 节日当天 + 拥有住宅）
        if (self.world is not None and self.on_marry is not None
                and not getattr(npc, "hostile", False) and getattr(npc, "alive", True)):
            ok_marry, _ = reng.can_propose(self.world, npc, self.preset)
            if ok_marry:
                marry_btn = QPushButton("求婚")
                marry_btn.setObjectName("primaryBtn")
                marry_btn.setToolTip("向恋人求婚（需拥有住宅）：结为配偶后对方将常伴左右（同行位豁免），离屏时也更易带话寄礼。")
                marry_btn.clicked.connect(self._wrap(self.on_marry))
                outer.addWidget(marry_btn)

        # [G01/R3 2026-09-30] 求医入口：医者（role/desc 关键词）+ 玩家有未愈伤情才显示；
        # 点击走 on_treat 回调（引擎真结算：诊金守恒入医者钱包、伤期 -3 天，HP 不动）
        if (self.world is not None and self.on_treat is not None
                and getattr(npc, "alive", True) and not getattr(npc, "hostile", False)):
            from src.services.combat_engine import _is_doctor
            _day = max(1, int(getattr(self.world, "day_count", 1) or 1))
            _inj = [it for it in (getattr(self.world.player, "injuries", None) or [])
                    if isinstance(it, dict) and int(it.get("until_day", 0) or 0) > _day]
            if _is_doctor(npc) and _inj:
                treat_btn = QPushButton("求医（处理伤情）")
                treat_btn.setToolTip("花诊金让对方为你敷药施针：一处伤的将养期缩短 3 天"
                                     "（气血须服药将养，治疗不回血）。诊金 = 20 + 10x伤处数。")
                treat_btn.clicked.connect(self._wrap(self.on_treat))
                outer.addWidget(treat_btn)

        # [P6] 商售 NPC 交易入口：点「查看商品/交易」开 ShopDialog（代码交易，不走叙事回合）
        if npc.is_merchant and self.on_shop is not None:
            shop_btn = QPushButton("查看商品 / 交易")
            shop_btn.setObjectName("primaryBtn")
            shop_btn.clicked.connect(lambda _=False: self.on_shop(self.npc))
            outer.addWidget(shop_btn)

        # [P7h] 敌对 NPC 战斗入口：点「战斗」开 CombatDialog（回合制，玩家可操作）
        if npc.hostile and npc.hp > 0 and self.on_combat is not None:
            fight_btn = QPushButton(f"与 {npc.name} 战斗")
            fight_btn.setStyleSheet("QPushButton{background:#5a2a3a; color:#ffb4ab; border-radius:6px; padding:8px; font-weight:bold;}")
            fight_btn.clicked.connect(self._wrap(self.on_combat))
            outer.addWidget(fight_btn)
        elif npc.hostile and npc.hp <= 0:
            # [修 2026-09-05] 去掉「可描述搜刮」误导：战利品在战斗胜利时已自动结算，
            # 场景层无二次搜刮（守结算真相契约）；左栏已过滤阵亡者，此分支仅兜底
            down_lbl = QLabel(f"{npc.name} 已经倒下（战利品已在战斗胜利时结算）")
            down_lbl.setStyleSheet("color:#e0af68;")
            outer.addWidget(down_lbl)

        # [任务改造] 该 NPC 发布的任务入口：available 可接取 / completed 可领奖 / active 显示进度
        if self.world is not None:
            from src.services import quest_engine as qe
            npc_quests = [q for q in (getattr(self.world, "quests", []) or [])
                          if str(getattr(q, "giver_npc_id", "") or "") == npc.id
                          and getattr(q, "status", "") in ("available", "active", "completed")]
            if npc_quests:
                qhdr = QLabel("委托任务")
                qhdr.setStyleSheet("color:#e0af68; font-weight:bold; margin-top:6px;")
                outer.addWidget(qhdr)
                for q in npc_quests:
                    qcard = QFrame()
                    qcard.setObjectName("gameCard")
                    ql = QVBoxLayout(qcard)
                    ql.setContentsMargins(8, 6, 8, 6)
                    ql.setSpacing(3)
                    chain_tag = "【主线】" if getattr(q, "chain", "side") == "main" else ""
                    qlbl = QLabel(f"{chain_tag}【{q.title}】")
                    qlbl.setStyleSheet("color:#c0caf5; font-weight:bold;")
                    qlbl.setWordWrap(True)
                    ql.addWidget(qlbl)
                    if getattr(q, "objective", ""):
                        ol = QLabel(f"目标：{q.objective}")
                        ol.setStyleSheet("color:#9aa5ce; font-size:12px;")
                        ol.setWordWrap(True)
                        ql.addWidget(ol)
                    st = getattr(q, "status", "")
                    if st == "available":
                        ab = QPushButton("接取任务")
                        ab.setObjectName("primaryBtn")
                        ab.clicked.connect(lambda _=False, _q=q: self._accept_quest(_q))
                        ql.addWidget(ab)
                    elif st == "active":
                        # 显示进度
                        pct = qe.progress_percent(q)
                        al = QLabel(f"进行中（{pct}%）—— 完成目标后回来领奖")
                        al.setStyleSheet("color:#7dcfff; font-size:12px;")
                        al.setWordWrap(True)
                        ql.addWidget(al)
                    elif st == "completed":
                        cb = QPushButton("领取奖励")
                        cb.setStyleSheet("QPushButton{background:#1f3a2e; color:#9ece6a; border-radius:6px; padding:6px; font-weight:bold;}")
                        cb.clicked.connect(lambda _=False, _q=q: self._claim_quest(_q))
                        ql.addWidget(cb)
                    outer.addWidget(qcard)

        # 快捷行动按钮
        actions = [
            (f"与 {npc.name} 交谈", f"与{npc.name}交谈"),
            (f"观察 {npc.name}", f"观察{npc.name}的言行举止"),
            (f"靠近 {npc.name}", f"靠近{npc.name}"),
        ]
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        for label, action in actions:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, a=action: self._pick(a))
            btn_row.addWidget(b)
        outer.addLayout(btn_row)

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        outer.addWidget(cancel_btn)

    def _rebuild(self):
        """清空并重建面板（状态变更回调完成后调用；deleteLater 延迟销毁防崩溃）。"""
        lay = self.layout()
        if lay is None:
            return
        self._clear_layout(lay)
        self._build(lay)

    @staticmethod
    def _clear_layout(lay):
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            sub = it.layout()
            lay.removeItem(it)
            if sub is not None:
                NpcInteractionDialog._clear_layout(sub)
                sub.deleteLater()

    def _wrap(self, cb):
        """状态变更回调包装：回调完成后重建面板。"""
        def _call(*_a):
            try:
                cb(self.npc)
            finally:
                self._rebuild()
        return _call

    @staticmethod
    def _line(k: str, v: str) -> QLabel:
        lbl = QLabel(f"{k}：{v}")
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#c0caf5;")
        return lbl

    def _pick(self, action: str):
        self.selected_action = action
        self.accept()

    def _accept_quest(self, q):
        """[任务改造] 在 NPC 面板接取该 NPC 发布的任务（giver 在场自然通过校验）。"""
        from src.services import quest_engine as qe
        from PySide6.QtWidgets import QMessageBox
        ok = qe.accept_quest(self.world, q, int(getattr(self.world, "tick_count", 0) or 0))
        if ok:
            QMessageBox.information(self, "接取任务", f"已接取任务「{q.title}」。")
        else:
            QMessageBox.information(self, "无法接取", "无法接取此任务。")
        if self.on_quest is not None:
            self.on_quest(self.npc)
        self._rebuild()   # [修 2026-09-13] 任务卡状态实时刷新（接取后按钮即变）

    def _claim_quest(self, q):
        """[任务改造] 在 NPC 面板领取该 NPC 发布的任务奖励。"""
        from src.services import quest_engine as qe
        from src.services import combat_engine as ce
        from PySide6.QtWidgets import QMessageBox
        result = qe.claim_reward(self.world, q,
                                 int(getattr(self.world, "tick_count", 0) or 0),
                                 gain_xp_fn=ce.gain_xp)
        if isinstance(result, dict) and result.get("error") == "need_giver":
            QMessageBox.information(self, "无法领奖", "需要找到任务发布者才能领取奖励。")
        elif result:
            parts = []
            if result.get("gold"):
                parts.append(f"{result['gold']} {qe._currency(self.world)}")
            if result.get("xp"):
                parts.append(f"{result['xp']} 经验")
            # [任务奖励物品] 只列这次真入包的（granted_names），否则会出现
            # 「领取奖励：朱果」紧跟「（已持有：朱果）」的自相矛盾
            _got = result.get("granted_names")
            if _got is None:
                _got = result.get("item_names") or result["items"]
            if _got:
                parts.append("、".join(_got))
            msg = f"领取奖励：{'、'.join(parts)}" if parts else "已领取奖励。"
            if result.get("bag_full"):
                # [B01-F10] 物品不再被吞：滞留件保留在任务上，任务日志条目可补领
                msg += "\n（背包已满，部分物品保留在任务奖励中——任务日志该条目上可随时补领）"
            if result.get("already_owned"):
                # [审核 P1] 与「背包满」分开报：已持有的蓝图 id 不是满包
                msg += "\n（已持有：" + "、".join(result["already_owned"]) + "）"
            if result.get("unlocked_next"):
                msg += f"\n任务链推进：下一环「{result['unlocked_next']}」已解锁。"
            if result.get("faction_power"):
                # [P45->2026-09-10] 势力任务反馈可见：领奖养势力（引擎 note 自带
                # 「实力+X 财富+Y」，直显整句不再 split 解析——格式解耦防再回归）。
                msg += f"\n{str(result['faction_power'])}。"
            QMessageBox.information(self, "领取奖励", msg)
        if self.on_quest is not None:
            self.on_quest(self.npc)
        self._rebuild()   # [修 2026-09-13] 领奖后任务卡即消失/转已完成

    def get_action(self) -> str | None:
        return self.selected_action
