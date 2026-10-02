"""[P7h] 回合制战斗对话框（玩家可操作：攻击/技能/物品/防御/逃跑）。

完全代码驱动（combat_engine CombatSession + 敌人 AI），不用 LLM 打架（守 Req4）。
CombatDialog 内部跑多回合循环：玩家选指令 -> player_action -> [P10] ally_turn -> enemy_turn -> 检查结束。
战斗结束 finish_combat 写回 hp/mp/掉落/经验，返回 summary 供叙事 LLM 描写。

接入：NpcInteractionDialog 给敌对 NPC 加「战斗」按钮 -> 开 CombatDialog -> 结束后
scene_tab save_world + 起叙事回合（描写已发生战斗）。

[P10] 遭遇判定（多敌 + 助战）：
- 打开对话框时先起 _JudgeWorker（QThread）调 svc.judge_encounter：LLM 判定敌人数量/
  随从命名/哪些在场同伴助战（失败回退确定性 roll）。判定期间指令区显示「判断遭遇中…」。
- 判定完成 -> svc.start_combat(allies, minion_specs) 构建会话（含随从/同伴单位），
  敌人区渲染多个可点击血条（点击切换攻击目标），同伴区渲染助战同伴血条。
- 回合顺序：玩家 -> 同伴 -> 敌方（主敌 + 随从）。守 §15 worker 生命周期（reject 时
  disconnect + cancel + wait）。

[战场化 2026-08-31]（参考 demo_combat_anim.py）：
- 敌我双方用头像立绘（UnitSprite：NPC.avatar/怪物图缓存，无图占位绘制）替换纯血条列表；
  2-1-2 前后排布局（slot 0/1 前排左右、2 中轴、3/4 后排左右），每次战斗引擎随机布局。
- 前排门控：对方前排未清空时后排立绘置灰不可选（引擎 unit_targetable 同口径）。
- 攻击/技能时显示部位骰子（引擎已掷的 body_part + 弱点/暴击结果经日志与飘字呈现）；
  受击闪白、伤害飘字、死亡淡出全走 QPropertyAnimation 纯表现层（不影响数值）。
"""
from __future__ import annotations

import math
import os

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QSize, QPoint, QPropertyAnimation, \
    QEasingCurve, QAbstractAnimation, QSequentialAnimationGroup, QParallelAnimationGroup, \
    QVariantAnimation
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QTextEdit, QFrame, QScrollArea, QWidget, QGraphicsOpacityEffect, QGridLayout,
    QTabWidget,
)
from PySide6.QtGui import QColor, QPixmap

from src.config import paths
from src.services import combat_engine as ce
from src.services.combat_engine import ELEMENT_ZH
from src.ui.dialogs.combat_sprite import UnitSprite
from src.utils.rng import SeededRng

# [战场化] 2-1-2 阵型像素坐标（战场 1240x560；[修 2026-09-05] 放大一档更大气）。
# [修 2026-09-05 用户指示] 左右半场各一张固定槽表（此前敌我共用一表镜像：前排
# 两侧仅隔 ~14px，同伴/随从一多就叠在战场中线——真人测试 4 同伴群战全糊）。
# 固定槽位 + 空位留白：单位数量不影响任何位置，战场永不变形。
# row 约定不变：slot 0/1 -> row=1（前排），2/3/4 -> row=2（后排）；敌我同构。
# 玩家独立于槽表（居半场中线列：前排位/后排位按 player_row），永不与同伴槽重叠。
# 每横排纵向预算 = 立绘高 100 + 血条 14 + 名字 16；上排头顶留 70px 飘字空间。
_BATTLE_W, _BATTLE_H = 1240, 560
_LANE_Y = (70, 195, 325)         # 上/中/下三横排立绘 y
_AVATAR_S = 100
# 左半场（玩家/同伴）列位：front 靠中线、axis 中轴、back 靠左缘
_ALLY_FRONT_X, _ALLY_AXIS_X, _ALLY_BACK_X = 470, 300, 90
# 右半场（敌方）列位：镜像同列宽，与左前排间隔 100px 防贴脸
_ENEMY_FRONT_X = _BATTLE_W - _ALLY_FRONT_X - _AVATAR_S     # 670
_ENEMY_AXIS_X = _BATTLE_W - _ALLY_AXIS_X - _AVATAR_S       # 840
_ENEMY_BACK_X = _BATTLE_W - _ALLY_BACK_X - _AVATAR_S       # 1050


def _slot_geom(slot: int, enemy: bool):
    """slot(0-4) + 阵营 -> (x, y, w, h)。未知 slot 回退前排上（防脏数据越界）。

    [修 2026-09-05 第二轮] 同伴侧 slot 9 = 第 5 同伴溢出位（陪玩家中线列前排 x，
    4 同伴 + 宠物满编时用；敌方不用）。"""
    fx = _ENEMY_FRONT_X if enemy else _ALLY_FRONT_X
    ax = _ENEMY_AXIS_X if enemy else _ALLY_AXIS_X
    bx = _ENEMY_BACK_X if enemy else _ALLY_BACK_X
    table = {
        0: (fx, _LANE_Y[0]),   # 前排上
        1: (fx, _LANE_Y[2]),   # 前排下
        2: (ax, _LANE_Y[1]),   # 中轴（2-1-2 的「1」——玩家专属位）
        3: (bx, _LANE_Y[0]),   # 后排上
        4: (bx, _LANE_Y[2]),   # 后排下
    }
    if slot == 9 and not enemy:
        return _ALLY_FRONT_X, _LANE_Y[1], _AVATAR_S, _AVATAR_S
    x, y = table.get(int(slot), table[0])
    return x, y, _AVATAR_S, _AVATAR_S

# [P30] 状态显示名查询（题材化）。combat_engine 已提供 condition_display；对话框据
# world.config_overlay 的题材 id 取显示名，缺省回退 western_fantasy。


class _JudgeWorker(QThread):
    """[P10] 遭遇判定后台任务（LLM 判定敌数 + 助战人选；失败回退确定性 roll）。"""

    finished_signal = Signal(object)   # 判定结果 dict（或 None=取消/异常）

    def __init__(self, svc, world, target, preset, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.world = world
        self.target = target
        self.preset = preset
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            result = self.svc.judge_encounter(
                self.world, self.target, self.preset,
                cancel_check=lambda: self._cancelled,
            )
            self.finished_signal.emit(result if not self._cancelled else None)
        except Exception:
            self.finished_signal.emit(None)


class CombatDialog(QDialog):
    """回合制战斗对话框。"""

    def __init__(self, world_sim_service, world, target_npc, preset, parent=None,
                 skip_judge_with=None, spectate_session=None):
        super().__init__(parent)
        # [敌袭观战 2026-09-06 用户指示] spectate_session 非空 = 观战模式（据点守卫 vs
        # 来敌，玩家不参战不操作）：直接注入预构 session（svc.build_domain_siege_session），
        # 指令区隐藏、双方自动打满全场、结束不走玩家结算（缴获/繁荣由据点结算函数管）。
        self._spectate = spectate_session is not None
        self._spectate_timer = None
        self.svc = world_sim_service
        self.world = world
        self.target = target_npc
        self.preset = preset
        self.session = None          # 判定 worker 完成后才构建
        self.result_summary = None
        self._consumed_ids: list[str] = []  # 本战已扣消耗品（中途退战回滚用）
        self._judge_worker: _JudgeWorker | None = None
        self._target_unit = None     # [P10] 当前攻击目标（None=主敌）
        # [2026-08-21 用户指示] 敌方行动延迟（交互节奏）：玩家行动后敌人经 QTimer 停顿再出手；
        # 期间锁指令防连点。0=同步结算（测试桩缺字段也走此路径，保旧行为）。
        self._enemy_timer: QTimer | None = None
        self._waiting_enemy = False
        # [战场化] 立绘控件登记 {CombatUnit.id() -> sprite}；_sprites 按 unit 对象 id 索引
        self._sprites: dict[int, UnitSprite] = {}
        self._hp_bars: dict[int, QProgressBar] = {}
        self._name_labels: dict[int, QLabel] = {}
        self._ally_visual: dict[int, int] = {}   # [修 2026-09-05] 同伴视觉槽（id -> 0/1/3/4/9）
        self._player_sprite: UnitSprite | None = None
        self.setWindowTitle(f"战斗：{target_npc.name}")
        self._build_ui()
        # [2026-08-21] 怪物图经 start_combat 传给敌方单位 avatar（游玩中不现场渲染）。
        # [P25d] skip_judge_with：世界 Boss 等预设战斗跳过 LLM 判定，直接喂入
        # {allies, minion_specs, note}（确定性小弟 + 声望援护），不显示「判断态势」占位。
        if self._spectate:
            self.setWindowTitle("观战：据点守卫战")
            self.session = spectate_session
            atk_names = "、".join(u.name for u in (spectate_session.enemy_units or []))
            dfd_names = "、".join([spectate_session.player_label]
                                  + [u.name for u in (spectate_session.ally_units or [])])
            self._append_log(f"【观战】敌袭「{atk_names}」来犯，守军「{dfd_names}」列阵迎战——"
                             f"你在一旁观战，战斗自动进行。")
            self.judging_lbl.hide()
            self._refresh()
        elif skip_judge_with is not None:
            self._append_log(f"遭遇 {target_npc.name}（{target_npc.role or '敌人'}）！")
            self._on_judge_done(skip_judge_with)
        else:
            self._append_log(f"遭遇 {target_npc.name}（{target_npc.role or '敌人'}）！正在判断战场态势…")
            self._start_judge()

    # ---------- UI ----------
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        self.resize(1272, 1000)

        # [战场化] 战场画布（敌我立绘 2-1-2 布局容器）
        # [深改 2026-10-01] 暖色暗金「戏台」底 + 金细边框（旧蓝紫渐变与暗金古卷
        # 全局风格割裂，且无外框像浮贴片）；有地点底图时被覆盖，仅作无图回退。
        self.scene = QWidget()
        self.scene.setFixedSize(_BATTLE_W, _BATTLE_H)
        self.scene.setStyleSheet(
            "QWidget{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "stop:0 #1a1712, stop:1 #261d14); "
            "border:1px solid #57482a; border-radius:8px;}")
        outer.addWidget(self.scene, 0, Qt.AlignHCenter)
        # [战场背景 2026-09-06] 当前地点场景图作战场底图（用户指示）：无图/加载失败回退
        # 上面的渐变。封面式缩放 + 居中裁剪（与场景页 refresh_state 同口径）+ 半透明
        # 压暗层保立绘/血条/飘字可读。先建背景/压暗层（同父控件按创建序叠放——立绘
        # _battle_rebuild 后建、flash raise_() 置顶，恒在其上）；两者都放行鼠标点击
        # （不吞 UnitSprite 的点击选目标）。
        self._bg_dim = None
        try:
            _loc = self.svc._current_location(self.world) if self.svc else None
            _bg_name = getattr(_loc, "background", "") or ""
        except Exception:
            _bg_name = ""
        if _bg_name:
            _pm = QPixmap(os.path.join(paths.world_images_dir(), _bg_name))
            if not _pm.isNull():
                _scaled = _pm.scaled(_BATTLE_W, _BATTLE_H, Qt.KeepAspectRatioByExpanding,
                                     Qt.SmoothTransformation)
                if _scaled.width() > _BATTLE_W or _scaled.height() > _BATTLE_H:
                    _cx = max(0, (_scaled.width() - _BATTLE_W) // 2)
                    _cy = max(0, (_scaled.height() - _BATTLE_H) // 2)
                    _scaled = _scaled.copy(_cx, _cy, _BATTLE_W, _BATTLE_H)
                _bg = QLabel(self.scene)
                _bg.setGeometry(0, 0, _BATTLE_W, _BATTLE_H)
                _bg.setPixmap(_scaled)
                _bg.setStyleSheet("border-radius:8px;")
                _bg.setAttribute(Qt.WA_TransparentForMouseEvents)
                self._bg_dim = QLabel(self.scene)
                self._bg_dim.setGeometry(0, 0, _BATTLE_W, _BATTLE_H)
                self._bg_dim.setStyleSheet("background:rgba(20,16,10,175); border-radius:8px;")
                self._bg_dim.setAttribute(Qt.WA_TransparentForMouseEvents)
        # [特效包] 全屏闪白遮罩（技能/借势用；透明度 0 平时不可见、不挡鼠标）。
        # [!] 立绘在 _battle_rebuild 动态创建会盖在遮罩上 -> 每次重建后 raise_() 置顶。
        self._flash_overlay = QLabel(self.scene)
        self._flash_overlay.setGeometry(0, 0, _BATTLE_W, _BATTLE_H)
        self._flash_overlay.setStyleSheet("background:#ffffff; border-radius:8px;")
        self._flash_overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._flash_eff = QGraphicsOpacityEffect(self._flash_overlay)
        self._flash_eff.setOpacity(0.0)
        self._flash_overlay.setGraphicsEffect(self._flash_eff)
        # 震屏基准位（scene 布局位，抖动后复位；只记一次防连震漂移）
        self._shake_base = None

        # 战斗日志（[深改 2026-10-01] 暖深底 + 羊皮纸色文字 + 暗金细边，融入暗金古卷）
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setStyleSheet("QTextEdit{background:#15130e; color:#cfc6ae; "
                                    "border:1px solid #3f3722; border-radius:6px;}")
        outer.addWidget(self.log_edit, 1)

        # [修 2026-09-05 用户指示] 队伍状态条：玩家 + 最多 4 同伴，横排固定宽格子
        # （原玩家独占一长条 HP/MP，同伴血量只能去战场立绘下方看）。空位隐藏、
        # 每格固定宽——多人也不挤不变形。玩家格带 MP + 状态标签；同伴格倒下置灰。
        # [深改 2026-10-01] gameCard 化（旧蓝灰底 #1f2a3a 突兀）+ 名字金字。
        party_box = QFrame()
        party_box.setObjectName("gameCard")
        pb_lay = QHBoxLayout(party_box)
        pb_lay.setContentsMargins(8, 6, 8, 6)
        pb_lay.setSpacing(8)
        pb_lay.addStretch()
        self._party_cells = []      # [(frame, name_lbl, hp_bar, mp_bar|None, cond_lbl|None)]
        for _i in range(5):
            cell = QFrame()
            cell.setFixedWidth(226)
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(6, 4, 6, 4)
            cl.setSpacing(2)
            name_lbl = QLabel("")
            name_lbl.setStyleSheet("color:#e0c48f; font-weight:bold; font-size:12px;")
            hp_bar = self._make_bar()
            cl.addWidget(name_lbl)
            cl.addWidget(hp_bar)
            mp_bar = None
            cond_lbl = None
            if _i == 0:     # 玩家格：MP 条 + 状态标签
                mp_bar = self._make_bar()
                cl.addWidget(mp_bar)
                cond_lbl = QLabel("")
                cond_lbl.setStyleSheet("color:#e0af68; font-size:10px;")
                cl.addWidget(cond_lbl)
            pb_lay.addWidget(cell)
            self._party_cells.append((cell, name_lbl, hp_bar, mp_bar, cond_lbl))
        pb_lay.addStretch()
        outer.addWidget(party_box)
        # 兼容旧引用（_refresh 曾直写这些控件；现统一走 _party_cells[0]）
        self.player_name, self.player_hp = self._party_cells[0][1], self._party_cells[0][2]
        self.player_mp, self.player_conds = self._party_cells[0][3], self._party_cells[0][4]

        # 指令区（主指令行常驻 + 技能/物品/同伴指令页签）
        # [修 2026-09-05 用户指示] 传统 RPG 页签化：此前技能/同伴指令/物品三段
        # 纵向堆叠，人多时挤作一团；改 QTabWidget 三页切换，每页独立滚动不互相挤。
        cmd_scroll = QScrollArea()
        cmd_scroll.setWidgetResizable(True)
        cmd_scroll.setFrameShape(QFrame.NoFrame)
        cmd_scroll.setFixedHeight(190)
        cmd_inner = QWidget()
        self.cmd_lay = QVBoxLayout(cmd_inner)
        self.cmd_lay.setContentsMargins(0, 0, 0, 0)
        self.cmd_lay.setSpacing(4)
        # 判定中提示
        self.judging_lbl = QLabel("正在判断遭遇规模与同伴动向…")
        self.judging_lbl.setStyleSheet("color:#e0af68;")
        self.cmd_lay.addWidget(self.judging_lbl)
        # 主指令行
        main_row = QHBoxLayout()
        self.attack_btn = QPushButton("攻击")
        # [深改 2026-10-01] 主行动金按钮（攻击是最高频指令，primaryBtn 强调）
        self.attack_btn.setObjectName("primaryBtn")
        self.attack_btn.clicked.connect(lambda: self._player_act("attack"))
        self.defend_btn = QPushButton("防御")
        self.defend_btn.clicked.connect(lambda: self._player_act("defend"))
        self.flee_btn = QPushButton("逃跑")
        self.flee_btn.clicked.connect(lambda: self._player_act("flee"))
        # [P40] 借势环境按钮（战场险地一次性指令：悬崖/油火/藤蔓/落石，题材化名随敌刷新）
        self.env_btn = QPushButton("借势")
        self.env_btn.setObjectName("optionBtn")
        self.env_btn.setEnabled(False)
        self.env_btn.setToolTip("借势战场险地发动一次特殊攻击（悬崖逼落/引燃油火/藤蔓困敌/落石砸阵），每场战斗限一次")
        self.env_btn.clicked.connect(lambda: self._player_act("env"))
        for b in (self.attack_btn, self.defend_btn, self.flee_btn, self.env_btn):
            b.setObjectName("optionBtn")
            b.setEnabled(False)
            main_row.addWidget(b)
        self.confirm_btn = QPushButton("战斗结束，确认")
        self.confirm_btn.setObjectName("optionBtn")
        self.confirm_btn.clicked.connect(self.accept)
        self.confirm_btn.hide()
        main_row.addWidget(self.confirm_btn)
        main_row.addStretch()
        self.cmd_lay.addLayout(main_row)
        # 页签：技能 / 物品 / 同伴指令（各页一行横排按钮；空内容隐藏页签）
        self.tabs = QTabWidget()
        self.tabs.setObjectName("cmdTabs")

        def _mk_tab(row_spacing=6):
            page = QWidget()
            lay = QHBoxLayout(page)
            lay.setContentsMargins(4, 6, 4, 6)
            lay.setSpacing(row_spacing)
            lay.addStretch()
            return page, lay

        skill_page, self.skill_row = _mk_tab()
        item_page, self.item_row = _mk_tab()
        orders_page, self.orders_row = _mk_tab()
        self._tab_skill = self.tabs.addTab(skill_page, "技能")
        self._tab_item = self.tabs.addTab(item_page, "物品")
        self._tab_orders = self.tabs.addTab(orders_page, "同伴指令")
        self.tabs.setFixedHeight(96)
        self.cmd_lay.addWidget(self.tabs)
        cmd_scroll.setWidget(cmd_inner)
        outer.addWidget(cmd_scroll)
        # [敌袭观战] 观战模式指令区整块隐藏（玩家不参战不操作），改显一行观战提示
        if self._spectate:
            cmd_scroll.hide()
            spec_lbl = QLabel("观战中——守军与来敌自动交战，战斗结束自动结算")
            spec_lbl.setStyleSheet("color:#e0af68; font-weight:bold;")
            spec_lbl.setAlignment(Qt.AlignCenter)
            outer.addWidget(spec_lbl)

    def _make_bar(self) -> QProgressBar:
        bar = QProgressBar()
        bar.setObjectName("hpBar")
        bar.setTextVisible(True)
        bar.setFixedHeight(16)
        return bar

    # ---------- [P10] 遭遇判定 ----------
    def _start_judge(self):
        self._judge_worker = _JudgeWorker(self.svc, self.world, self.target, self.preset, parent=None)
        self._judge_worker.finished_signal.connect(self._on_judge_done)
        self._judge_worker.finished.connect(self._on_judge_worker_done)
        self._judge_worker.start()

    def _on_judge_worker_done(self):
        w = self._judge_worker
        self._judge_worker = None
        if w is not None:
            w.deleteLater()

    def _on_image_done(self, fname):
        """[战场化 2026-08-31] 旧怪物大图位已移除：头像统一走单位立绘（UnitSprite.avatar）。"""
        return

    def _cleanup_judge_worker(self):
        """守 §15：disconnect + cancel + wait + 安全回收。"""
        w = self._judge_worker
        if w is None:
            return
        try:
            w.finished_signal.disconnect()
            w.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        w.cancel()
        w.wait(5000)
        self._judge_worker = None
        # [!] wait 超时线程可能仍在跑（LLM 流式中段卡住偶发），未结束则挂 deleteLater
        # 到 finished 信号让线程自然收尾后再销毁，避免 Destroyed while running 崩溃。
        if w.isFinished():
            w.deleteLater()
        else:
            try:
                w.finished.connect(w.deleteLater)
            except (RuntimeError, TypeError):
                w.deleteLater()

    def _on_judge_done(self, result):
        if result is None:
            result = {"enemy_count": 1, "minion_specs": [], "allies": [], "note": ""}
        allies = result.get("allies") or []
        minion_specs = result.get("minion_specs") or []
        note = str(result.get("note", "") or "")
        # 构建战斗会话（含随从/同伴单位）
        self.session = self.svc.start_combat(self.world, self.target, self.preset,
                                             allies=allies, minion_specs=minion_specs)
        self.judging_lbl.hide()
        intro = f"战斗开始！你 对阵 {self.target.name}（{self.target.role or '敌人'}）"
        # [P9] 敌人元素构成提示（供玩家选克制技能；火克冰、雷克金木等）
        _els = [e for e in (getattr(self.target, "elements", None) or []) if e in ELEMENT_ZH]
        if _els:
            intro += f"［元素：{'、'.join(ELEMENT_ZH[e] for e in _els)}］"
        if minion_specs:
            names = "、".join(u["name"] for u in minion_specs if isinstance(u, dict))
            intro += f"，另有增援：{names}"
        if allies:
            intro += "。同伴：" + "、".join(n.name for n in allies) + " 与你并肩作战！"
        if note:
            intro += f"（{note}）"
        self._append_log(intro)
        self._refresh()

    # ---------- [战场化] 战场立绘构建（2-1-2 布局）----------
    def _battle_rebuild(self):
        """按 session 单位（含布局 row/slot）重建战场立绘。每次 _refresh 复用已建控件。

        [修 2026-09-05] 增量建绘：原实现 `if not self._sprites` 仅首建——战斗中途
        入场的单位（召唤潮爪牙 boss_mechanics_tick / P40 狂卫 _check_boss_phase）
        永远拿不到立绘/血条/名字：隐形单位活着战斗不结束、还会攻击玩家（真人测试
        「敌人全倒下不自动结束」根因）。改为每次刷新给缺失单位补建；已建控件复用。
        """
        s = self.session
        if s is None:
            return
        if s.enemy_units:
            for unit in s.enemy_units:
                if id(unit) not in self._sprites:
                    self._mk_field_unit(unit, enemy=True)
        elif not self._sprites:
            # legacy 单敌会话：all_enemy_combatants 每次临时包装主敌（新对象，id 每次不同），
            # 增量口径会重复建绘——仅首建一次
            for unit in ce.all_enemy_combatants(s):
                self._mk_field_unit(unit, enemy=True)
        # [修 2026-09-05 第二轮 用户指示] 同伴视觉槽重映射：轴位(2)是玩家专属
        # （玩家当 2-1-2 的「1」，此前同伴占轴位+玩家站前排 -> 视觉变 3-1-2）。
        # 同伴只占四角；slot2/占角冲突 -> 挪到空角（优先后排角保 row=2 语义）；
        # 四角全满（4 同伴 + 宠物）-> 第 5 人落溢出位 9（陪玩家中线列）。
        self._ally_visual = {}
        _taken: set[int] = set()
        for u in (s.ally_units or []):
            vs = int(getattr(u, "slot", 0) or 0)
            if vs not in (0, 1, 3, 4) or vs in _taken:
                vs = next((c for c in (4, 3, 1, 0) if c not in _taken), 9)
            _taken.add(vs)
            self._ally_visual[id(u)] = vs
        for unit in (s.ally_units or []):
            if id(unit) not in self._sprites:
                self._mk_field_unit(unit, enemy=False)
        if self._player_sprite is None:
            self._mk_player_sprite()
        # [审查跟进 2026-09-05] 中途移除的单位（撤退指令 remove 出 ally_units）：立绘隐藏
        # （dict 登记保留，防 id 复用误判；死亡单位仍在 enemy_units 尸体照常显示）
        _present = {id(u) for u in (s.enemy_units or [])} | {id(u) for u in (s.ally_units or [])}
        for uid, sp in self._sprites.items():
            sp.setVisible(uid in _present)
        # 刷新各立绘位置（布局不变，但倒下单位需要 down 动画态——由结算路径触发）
        self._refresh_field_units()
        # [特效包] 立绘重建后把闪白遮罩重新置顶（后建者盖上）
        self._flash_overlay.raise_()

    def _mk_player_sprite(self):
        """玩家立绘：固定站左半场中轴位（2-1-2 的「1」）。

        [修 2026-09-05 第二轮 用户指示] 玩家此前按 player_row 站前排/后包 x，
        与中轴槽位的同伴同处中线列 -> 视觉变 3-1-2。现玩家恒占轴位，同伴只占
        四角（见 _battle_rebuild 的视觉槽重映射）；player_row 只作引擎门控语义。"""
        avatar = str(getattr(self.world.player, "avatar", "") or "")
        sp = UnitSprite("你", avatar, enemy=False, parent=self.scene)
        sp.setGeometry(_ALLY_AXIS_X, _LANE_Y[1], _AVATAR_S, _AVATAR_S)
        sp.show()
        self._player_sprite = sp

    @staticmethod
    def _elide_name(name: str, max_w: int) -> str:
        """[修 2026-09-01] 长名省略号截断（QFontMetrics 量宽，超宽尾部 …）。"""
        try:
            from PySide6.QtGui import QFontMetrics, QFont
            fm = QFontMetrics(QFont(None, 11))
            if fm.horizontalAdvance(name) <= max_w:
                return name
            while name and fm.horizontalAdvance(name + "…") > max_w:
                name = name[:-1]
            return name + "…"
        except Exception:
            return name

    def _mk_field_unit(self, unit, enemy: bool):
        """为单位建立绘 + 血条（立绘正下方）+ 名字标签（血条下方）。

        [修 2026-09-05] 槽位经 _slot_geom（左右半场各自固定 2-1-2 表，不再镜像共用）。
        同伴侧经 _ally_visual 重映射后的视觉槽（轴位留给玩家）。"""
        s = self.session
        uid = id(unit)
        sp = UnitSprite(unit.name, getattr(unit, "avatar", "") or "", enemy, parent=self.scene)
        slot = int(getattr(unit, "slot", 0) or 0)
        if not enemy:
            slot = self._ally_visual.get(uid, slot)
        gx, gy, gw, gh = _slot_geom(slot, enemy)
        sp.setGeometry(gx, gy, gw, gh)
        sp.show()
        if enemy:
            sp.clicked.connect(lambda _=False, u=unit: self._on_enemy_clicked(u))
        # 血条 + 名字（立绘正下方；名字条加宽 60px + 居中，超宽用省略号防截断）
        bar = self._make_bar()
        bar.setParent(self.scene)
        bar.setGeometry(sp.x(), sp.y() + sp.height() + 2, sp.width(), 14)
        bar.show()
        nm = QLabel(self._elide_name(unit.name, sp.width() + 60), self.scene)
        nm.setStyleSheet(("color:#f7768e;" if enemy else "color:#9ece6a;")
                         + "font-size:11px; font-weight:bold; background:transparent;")
        nm.setAlignment(Qt.AlignCenter)
        nm.setGeometry(sp.x() - 30, sp.y() + sp.height() + 17, sp.width() + 60, 16)
        nm.setToolTip(unit.name)
        nm.show()
        self._sprites[uid] = sp
        self._hp_bars[uid] = bar
        self._name_labels[uid] = nm

    def _on_enemy_clicked(self, unit):
        """点敌方立绘选目标（不可选单位提示；可选即锁定——含主敌包装，同排都能选）。"""
        s = self.session
        if s is None or s.state != "active" or self._waiting_enemy:
            return
        if not unit.alive:
            return
        if not ce.unit_targetable(s, unit, "enemy"):
            self._append_log("　对方前排未清，暂时打不到后排目标。")
            return
        # [修 2026-09-01] 主敌包装也作为显式目标（原 None=回退主敌会让「多敌同排时
        # 点主敌像没选上」）；None 才表示引擎自动选（首个存活前排）。
        self._target_unit = unit
        sp = self._sprites.get(id(unit))
        if sp is not None:
            self._ring_pulse(sp)   # 选中即金圈脉冲反馈
        self._refresh()

    def _refresh_field_units(self):
        """刷新战场单位态：血条值/名字（当前目标标记/弱点提示）/后排置灰/倒下淡出。"""
        s = self.session
        for unit in ce.all_enemy_combatants(s):
            self._update_field_unit(unit, enemy=True)
        for unit in (s.ally_units or []):
            self._update_field_unit(unit, enemy=False)

    def _update_field_unit(self, unit, enemy: bool):
        s = self.session
        uid = id(unit)
        sp = self._sprites.get(uid)
        bar = self._hp_bars.get(uid)
        nm = self._name_labels.get(uid)
        if sp is None or bar is None or nm is None:
            return
        self._set_bar(bar, unit.snapshot.hp, unit.snapshot.hp_max, "HP")
        base_txt = self._unit_label_text(unit, enemy)
        # 前排门控视觉：后排不可选时置灰 + 提示 tooltip
        targetable = ce.unit_targetable(s, unit, "enemy") if enemy else True
        sp.setAttribute(Qt.WA_TransparentForMouseEvents, not (targetable and unit.alive))
        if unit.alive and not targetable:
            # [修 2026-09-06 真机] 「后排·受保护」挪出文本进 tooltip——后排立绘已置灰，
            # 文本标注只加长名条挤遮等级文字；置灰 + tooltip 双通道信息不丢
            # （tooltip 行在下方 tip_lines 全量重建处统一追加）
            nm.setText(base_txt)
        elif not unit.alive:
            nm.setText(f"{base_txt}（倒下）")
            if getattr(sp, "get_down", lambda: 0.0)() < 0.5:
                self._run(sp, b"down", 0.0, 1.0, 450, QEasingCurve.InCubic)
        else:
            # 当前攻击目标前缀 » + 常亮金圈（点选反馈；无显式目标时主敌为默认目标）
            cur = self._target_unit
            is_cur = (cur is None and unit.snapshot is s.enemy) or (cur is unit)
            nm.setText(("» " if (enemy and is_cur) else "") + base_txt)
            if enemy:
                sp.set_highlight(1.0 if is_cur else 0.0)
        # tooltip 每次全量重建（[修 2026-09-01] 原实现每回合往上拼接，悬停文字无限叠加）
        tip_lines = []
        if enemy and unit.alive and not targetable:
            tip_lines.append("后排·受保护：须先清空其前排才能选中")
        if enemy:
            wp = str(getattr(unit, "weakpoint", "") or "")
            if wp:
                tip_lines.append(f"弱点部位：{ce.part_display(wp)}（命中弱点伤害 x1.5 且可暴击）")
        cond_lbl = self._make_cond_label(self._conds_for_unit(unit))
        if cond_lbl.text():
            tip_lines.append(cond_lbl.text())
        nm.setToolTip("\n".join(tip_lines))

    # ---------- 动画原语（纯表现层，不影响数值）----------
    def _anim(self, target, prop, start, end, ms, curve=QEasingCurve.OutQuad):
        """创建属性动画（不 start——供动画组编排；组会接管子动画生命周期）。

        [!] 勿在此 start：加入 QSequentialAnimationGroup 前已 start 的子动画会失效
        （曾致冲刺/闪白/飘字组全部不动——2026-09-01 修）。独立播放用 _run。
        """
        a = QPropertyAnimation(target, prop, self)
        a.setStartValue(start)
        a.setEndValue(end)
        a.setDuration(ms)
        a.setEasingCurve(curve)
        return a

    def _run(self, target, prop, start, end, ms, curve=QEasingCurve.OutQuad):
        """创建并立即播放（独立动画，非组编排）。"""
        a = self._anim(target, prop, start, end, ms, curve)
        a.start(QAbstractAnimation.DeleteWhenStopped)
        return a

    def _lunge(self, widget, dx: int, ms_out=110, ms_back=150):
        """冲刺：向 dx 方向位移后回原位。"""
        if widget is None:
            return
        base = widget.pos()
        mid = base + QPoint(dx, 0)
        g = QSequentialAnimationGroup(self)
        g.addAnimation(self._anim(widget, b"pos", base, mid, ms_out, QEasingCurve.OutQuad))
        g.addAnimation(self._anim(widget, b"pos", mid, base, ms_back, QEasingCurve.InOutQuad))
        g.start(QAbstractAnimation.DeleteWhenStopped)

    def _lunge_to(self, widget, target_sprite, stop_gap=60, ms_out=160, ms_back=220):
        """[战场化 2026-09-01] 向目标冲刺：dx 按双方立绘真实距离算（停在目标前 stop_gap）。

        目标缺失/距离过近回退短冲刺（+80）；纵向也带 40% 位移靠拢（攻击轨迹自然）。
        """
        if widget is None:
            return
        dx = 80
        dy = 0
        if target_sprite is not None:
            tx = target_sprite.x() + target_sprite.width() // 2
            ty = target_sprite.y() + target_sprite.height() // 2
            mx = widget.x() + widget.width() // 2
            my = widget.y() + widget.height() // 2
            ddx = tx - mx - stop_gap if tx > mx else tx - mx + stop_gap
            if abs(ddx) > 24:
                dx = ddx
            dy = int((ty - my) * 0.4)
        base = widget.pos()
        mid = base + QPoint(dx, dy)
        g = QSequentialAnimationGroup(self)
        g.addAnimation(self._anim(widget, b"pos", base, mid, ms_out, QEasingCurve.OutQuad))
        g.addAnimation(self._anim(widget, b"pos", mid, base, ms_back, QEasingCurve.InOutQuad))
        g.start(QAbstractAnimation.DeleteWhenStopped)

    def _strike(self, widget, target_sprite, ms_out=260, ms_back=460):
        """[撞击感 2026-09-01] 打击式冲刺：急冲贴脸（快出）- 回弹半程（撞击停顿）- 归位。

        与 _lunge_to 的区别：冲到目标「身前贴住」（stop_gap=8）且出招段用 InQuad 先慢后快
        （蓄力->突进），回弹段先急退 40% 再缓归位——命中瞬间的挤压感就是撞击。
        纵向 60% 靠拢（比 _lunge_to 更贴）。
        """
        if widget is None:
            return
        dx = 90
        dy = 0
        if target_sprite is not None:
            tx = target_sprite.x() + target_sprite.width() // 2
            ty = target_sprite.y() + target_sprite.height() // 2
            mx = widget.x() + widget.width() // 2
            my = widget.y() + widget.height() // 2
            ddx = tx - mx - 8 if tx > mx else tx - mx + 8
            if abs(ddx) > 16:
                dx = ddx
            dy = int((ty - my) * 0.6)
        base = widget.pos()
        mid = base + QPoint(dx, dy)
        # 撞击点：略越过停点 6px（打进目标身位），回弹先退到 60% 处再缓归位
        hit = mid + QPoint(8 if dx > 0 else -8, 0)
        back = base + QPoint(int(dx * 0.4), int(dy * 0.4))
        g = QSequentialAnimationGroup(self)
        # 出招：蓄力前段（前 30% 距离慢慢起步）+ 突进段（InQuad 加速撞上去）
        wind = base + QPoint(int(dx * 0.3), int(dy * 0.3))
        g.addAnimation(self._anim(widget, b"pos", base, wind, int(ms_out * 0.35),
                                  QEasingCurve.OutQuad))
        g.addAnimation(self._anim(widget, b"pos", wind, hit, int(ms_out * 0.65),
                                  QEasingCurve.InQuad))
        # 撞击回弹：急退到 40% 处（挤压感）再缓归位
        g.addAnimation(self._anim(widget, b"pos", hit, back, int(ms_back * 0.3),
                                  QEasingCurve.OutQuad))
        g.addAnimation(self._anim(widget, b"pos", back, base, int(ms_back * 0.7),
                                  QEasingCurve.InOutQuad))
        g.start(QAbstractAnimation.DeleteWhenStopped)

    def _flash(self, sprite, ms=180):
        if sprite is None:
            return
        g = QSequentialAnimationGroup(self)
        g.addAnimation(self._anim(sprite, b"flash", 0.0, 1.0, ms // 2))
        g.addAnimation(self._anim(sprite, b"flash", 1.0, 0.0, ms // 2))
        g.start(QAbstractAnimation.DeleteWhenStopped)

    def _shake(self, magnitude=8, ms=260):
        """[特效包] 震屏：正弦衰减抖动战场画布（暴击/技能重击；结束后复位）。

        基准位只记一次（_shake_base），连震也回到同一基准，防叠加漂移。
        """
        if self._shake_base is None:
            self._shake_base = self.scene.pos()
        base = self._shake_base
        anim = QVariantAnimation(self)
        anim.setDuration(ms)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)

        def on_val(t):
            decay = 1.0 - t
            off = magnitude * decay * math.sin(t * math.pi * 6)
            self.scene.move(base.x() + int(off), base.y() + int(off * 0.6))

        anim.valueChanged.connect(on_val)
        anim.finished.connect(lambda: self.scene.move(base))
        anim.start(QAbstractAnimation.DeleteWhenStopped)

    def _wobble(self, widget, ms=420, amp=6):
        """[特效包] 原地抖动（敌方蓄势）：正弦摆动后回原位。"""
        if widget is None:
            return
        base = widget.pos()
        anim = QVariantAnimation(self)
        anim.setDuration(ms)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)

        def on_val(t):
            widget.move(base.x() + int(amp * math.sin(t * math.pi * 8)), base.y())

        anim.valueChanged.connect(on_val)
        anim.finished.connect(lambda: widget.move(base))
        anim.start(QAbstractAnimation.DeleteWhenStopped)

    def _screen_flash(self, peak=0.45, ms=160):
        """[特效包] 全屏闪白（技能/借势）：遮罩透明度 peak -> 0。"""
        self._run(self._flash_eff, b"opacity", peak, 0.0, ms)

    def _ring_pulse(self, sprite, ms=380):
        """[特效包] 金圈脉冲（弱点命中）：highlight 0 -> 1 -> 0 扩散一圈。"""
        if sprite is None:
            return
        g = QSequentialAnimationGroup(self)
        g.addAnimation(self._anim(sprite, b"highlight", 0.0, 1.0, int(ms * 0.35)))
        g.addAnimation(self._anim(sprite, b"highlight", 1.0, 0.0, int(ms * 0.65)))
        g.start(QAbstractAnimation.DeleteWhenStopped)

    def _pop_number(self, text: str, sprite, color="#ff5577", big=False,
                    dx: int = 0, delay_ms: int = 0):
        """飘字（伤害数字/回复）：慢速上浮 + 后半程才淡出。

        [修 2026-09-01] dx 横向偏移 / delay_ms 延迟出场——同一击多个飘字（伤害+部位
        +对撞条）错开位置与时机，不再叠成一团挡住立绘。
        """
        if sprite is None:
            return
        if delay_ms > 0:
            QTimer.singleShot(delay_ms,
                              lambda: self._pop_number(text, sprite, color, big, dx, 0))
            return
        lbl = QLabel(text, self.scene)
        lbl.setStyleSheet(f"color:{color}; font-size:{30 if big else 20}px; font-weight:900;"
                          "background:transparent;")
        lbl.adjustSize()
        px = sprite.x() + sprite.width() // 2 - lbl.width() // 2 + dx
        py = sprite.y() - 6
        # [修 2026-09-01] 钳在战场内（上排立绘头顶空间不足时不再被顶边切掉）
        px = max(4, min(_BATTLE_W - lbl.width() - 4, px))
        py = max(2, py)
        lbl.move(px, py)
        lbl.show()
        eff = QGraphicsOpacityEffect(lbl)
        lbl.setGraphicsEffect(eff)
        g = QParallelAnimationGroup(self)
        g.addAnimation(self._anim(lbl, b"pos", lbl.pos(), lbl.pos() + QPoint(0, -min(34, py)), 2000,
                                  QEasingCurve.OutCubic))
        # 淡出分两段：前 70% 保持不透明，后 30% 渐隐（数字停留可读）
        seq = QSequentialAnimationGroup(self)
        seq.addAnimation(self._anim(eff, b"opacity", 1.0, 1.0, 1400))
        seq.addAnimation(self._anim(eff, b"opacity", 1.0, 0.0, 600, QEasingCurve.OutQuad))
        g.addAnimation(seq)
        g.finished.connect(lbl.deleteLater)
        g.start(QAbstractAnimation.DeleteWhenStopped)

    def _part_roll_anim(self, sprite, part_txt: str):
        """[部位 roll 动画 2026-09-01] 攻击命中时目标头顶跳骰：部位名快速轮换
        （OutCubic 逐渐减速）后定格实际 roll 结果（弱点金字），停一拍再上浮淡出。

        纯表现层：结果由引擎日志解析（_part_dice_text），动画只是把结果「演」出来。
        位置在目标右上角（与伤害数字错开）。
        """
        if sprite is None or not part_txt:
            return
        parts = [ce.part_display(k) for k in ce.BODY_PART_KEYS]
        weak = "弱点" in part_txt
        lbl = QLabel("部位…", self.scene)
        lbl.setStyleSheet("color:#c0caf5; font-size:16px; font-weight:900; background:transparent;")
        lbl.adjustSize()
        # [修 2026-09-01] 钳在战场内（右上角位不越界）
        px = max(4, min(_BATTLE_W - lbl.width() - 4, sprite.x() + sprite.width() - 14))
        py = max(2, sprite.y() - 4)
        lbl.move(px, py)
        lbl.show()
        anim = QVariantAnimation(self)
        anim.setDuration(1300)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)   # 越转越慢的减速感

        def on_val(t):
            # 轮换下标随进度推进（减速 easing -> 后段切换变慢）
            idx = int(t * 16) % len(parts)
            lbl.setText(f"部位·{parts[idx]}")
            lbl.adjustSize()

        def on_fin():
            lbl.setText(part_txt)
            lbl.setStyleSheet(("color:#e0af68;" if weak else "color:#c0caf5;")
                              + "font-size:17px; font-weight:900; background:transparent;")
            lbl.adjustSize()
            QTimer.singleShot(800, lambda: self._float_out(lbl, 1200))

        anim.valueChanged.connect(on_val)
        anim.finished.connect(on_fin)
        anim.start(QAbstractAnimation.DeleteWhenStopped)

    def _part_dice_text(self, log_text: str) -> str:
        """从回合日志提取部位骰子信息（部位名 + 弱点标记），供飘字展示。"""
        import re
        m = re.search(r"部位：([^）]+?)(·弱点！)?（?暴击", log_text) or \
            re.search(r"部位：([^）]+?)(·弱点！)?）", log_text)
        if not m:
            return ""
        part = m.group(1)
        return f"{part}{'·弱点!' if m.group(2) else ''}"

    # ---------- [对撞 2026-09-01] 骰子对撞条（战场内嵌，不打断节奏）----------
    def _clash_info(self, log_text: str):
        """解析对撞日志 -> (攻方骰面, 守方骰面, 结局标签, 攻方名, 守方名)。失配返回 None。

        日志格式（引擎 clash_result_log）：（对撞：A【3-3-3-4-5·三同】18 vs D【...·散牌】13 -> 破防！）
        [修 2026-09-05] 多回攻/守名（组1/组5）供立绘归属（敌方回合对撞不再硬绑主敌头像）。
        """
        import re
        m = re.search(r"对撞：(.+?)【([0-9\-]+)·([^\]]+)】(\d+)\s*vs\s*(.+?)【([0-9\-]+)·([^\]]+)】(\d+)\s*->\s*([^）]+)）",
                      log_text or "")
        if not m:
            return None
        a_dice = [int(x) for x in m.group(2).split("-")]
        d_dice = [int(x) for x in m.group(6).split("-")]
        outcome = m.group(9).strip()
        return a_dice, d_dice, outcome, m.group(1).strip(), m.group(5).strip()

    def _clash_bar(self, clash_info, attacker_sprite, defender_sprite):
        """对撞条：攻守双方立绘之间浮出对撞展示（双方 5 骰面 + 牌型胜负横幅），淡出。

        clash_info = _clash_info() 的解析结果；精灵缺失时退化为单侧展示。
        纯表现层：QLabel 组合 + 上浮淡出动画，1400ms 自动消散。
        """
        if not clash_info:
            return
        a_dice, d_dice, outcome = clash_info
        a_txt = " ".join(f"[{d}]" for d in a_dice)
        d_txt = " ".join(f"[{d}]" for d in d_dice)
        a_hand = ce.clash_hand(a_dice)[0]
        d_hand = ce.clash_hand(d_dice)[0]
        a_won = outcome.startswith(("破防",))
        # 结果横幅（攻方胜金 / 守方胜蓝 / 完全格挡紫）
        if "完全格挡" in outcome:
            color = "#9d7cd8"
        elif a_won:
            color = "#e0af68"
        else:
            color = "#7aa2f7"
        banner = QLabel(outcome, self.scene)
        banner.setStyleSheet(f"color:{color}; font-size:16px; font-weight:900; background:transparent;")
        banner.adjustSize()
        # 摆位：攻方骰面贴攻方头顶、守方贴守方头顶、横幅居两者中间上方
        if attacker_sprite is not None:
            la = QLabel(a_txt + f" {ce.CLASH_HAND_ZH.get(a_hand, '')}", self.scene)
            la.setStyleSheet("color:#f7768e; font-size:14px; font-weight:900; background:transparent;")
            la.adjustSize()
            la.move(attacker_sprite.x() + attacker_sprite.width() // 2 - la.width() // 2,
                    max(2, attacker_sprite.y() - 22))   # [审查跟进] y 钳制防截顶（同 _pop_number 口径）
            self._float_out(la, 1400)
        if defender_sprite is not None:
            ld = QLabel(d_txt + f" {ce.CLASH_HAND_ZH.get(d_hand, '')}", self.scene)
            ld.setStyleSheet("color:#7aa2f7; font-size:14px; font-weight:900; background:transparent;")
            ld.adjustSize()
            ld.move(defender_sprite.x() + defender_sprite.width() // 2 - ld.width() // 2,
                    max(2, defender_sprite.y() - 22))
            self._float_out(ld, 1400)
        # 横幅居中战场上方
        banner.move(self.scene.width() // 2 - banner.width() // 2, 12)
        self._float_out(banner, 1600)
        # 音效级强调：豹子/完全格挡震屏
        if "豹子" in outcome or "完全格挡" in outcome:
            self._shake(12, 340)

    def _float_out(self, lbl, ms):
        """[对撞] 通用上浮淡出消散（复用 _pop_number 的骨架，无数字格式约束）。"""
        lbl.show()
        eff = QGraphicsOpacityEffect(lbl)
        lbl.setGraphicsEffect(eff)
        g = QParallelAnimationGroup(self)
        g.addAnimation(self._anim(lbl, b"pos", lbl.pos(), lbl.pos() + QPoint(0, -30), ms,
                                  QEasingCurve.OutCubic))
        g.addAnimation(self._anim(eff, b"opacity", 1.0, 0.0, ms, QEasingCurve.OutCubic))
        g.finished.connect(lbl.deleteLater)
        g.start(QAbstractAnimation.DeleteWhenStopped)

    def _sprite_for_actor(self, name: str):
        """[修 2026-09-05] 名字 -> 立绘（「你」=玩家；「敌人」/敌方单位名=对应敌立绘；
        同伴名=同伴立绘）。解析不到返回 None（调用方回退传入立绘）。"""
        s = self.session
        if s is None or not name:
            return None
        if name == "你":
            return self._player_sprite
        for u in (s.enemy_units or []):
            if name == "敌人" or name == getattr(u, "name", ""):
                return self._sprites.get(id(u))
        for u in (s.ally_units or []):
            if name == getattr(u, "name", ""):
                return self._sprites.get(id(u))
        return None

    def _play_clash_from_log(self, log_text: str, a_sprite, d_sprite):
        """从回合日志尝试解析并播放对撞条（无对撞信息静默）。

        [修 2026-09-05] 攻/守立绘按对撞行名字归属（原实现敌方回合硬传主敌立绘：
        主敌倒下后随从打出的对撞会显示尸体头像）；名字解析不到回退调用方立绘。
        """
        info = self._clash_info(log_text)
        if info is None:
            return
        a_dice, d_dice, outcome, a_name, d_name = info
        a_sprite = self._sprite_for_actor(a_name) or a_sprite
        d_sprite = self._sprite_for_actor(d_name) or d_sprite
        self._clash_bar((a_dice, d_dice, outcome), a_sprite, d_sprite)

    # ---------- 刷新 ----------
    def _refresh(self):
        if self.session is None:
            return
        s = self.session
        # [P47-C2 敌情预告] 回合推进时规划并播报敌方锁定目标（独立 salt 流；
        # 措辞用「锁定/预计」，禁「攻击/防御」——防 _enemy_action_anims 误归属动画）
        if (s.state == "active" and not self._spectate
                and getattr(self.preset, "combat_intents_enabled", True)):
            try:
                ce.plan_enemy_intents(s, self.world.id, self.world.tick_count)
            except Exception:
                pass
            if (s.enemy_intents
                    and s.enemy_intents_round != getattr(self, "_intents_shown_round", -1)):
                self._intents_shown_round = s.enemy_intents_round
                for it in s.enemy_intents:
                    # [P49] 完整意图：动作标签（锁定/酝酿/防御）+ 伤害区间（预告=事实，
                    # 执行段已做动作覆写）。措辞禁「攻击/防御」起头（动画归属红线）。
                    self._append_log(f"【敌情】{it['unit_name']} {it.get('label', '蓄势待发')}"
                                     "——想好怎么应对")
        self._battle_rebuild()
        # 名字（观战：玩家位=守方队长，不写「你」）
        if self._spectate:
            self.player_name.setText(f"{s.player_label}  {s.player.level}级（守将）")
        else:
            self.player_name.setText(f"你  {self.world.player.level}级")
        # 玩家 HP/MP 条
        self._set_bar(self.player_hp, s.player.hp, s.player.hp_max, "HP")
        self._set_bar(self.player_mp, s.player_mp, max(1, self.world.player.mp_max), "MP")
        # [P30] 玩家状态标签
        cond_lbl = self._make_cond_label(s.player_conditions)
        self.player_conds.setText(cond_lbl.text())
        self.player_conds.setToolTip(cond_lbl.toolTip())
        # [修 2026-09-05] 同伴格 1-4（固定宽，空位隐藏；倒下置灰）
        allies = list(s.ally_units or [])[:4]
        for idx, (cell, name_lbl, hp_bar, _mp, _cond) in enumerate(self._party_cells[1:], start=1):
            u = allies[idx - 1] if idx - 1 < len(allies) else None
            if u is None:
                cell.setVisible(False)
                continue
            cell.setVisible(True)
            base = self._unit_label_text(u, enemy=False)
            name_lbl.setText(f"{base}（倒下）" if not u.alive else base)
            name_lbl.setStyleSheet(
                "color:#5c6a8a; font-weight:bold; font-size:12px;" if not u.alive
                else "color:#9ece6a; font-weight:bold; font-size:12px;")
            self._set_bar(hp_bar, u.snapshot.hp, u.snapshot.hp_max, "HP")
        # 战斗结束 -> 禁用指令，显示确认
        if s.state != "active":
            self._clear_layout(self.skill_row)
            self._clear_layout(self.item_row)
            for b in (self.attack_btn, self.defend_btn, self.flee_btn, self.env_btn):
                b.setEnabled(False)
            self.env_btn.setVisible(False)   # [审查跟进] env 可用期间战斗结束不再残留灰按钮
            self._update_cmd_tabs(False, False, False)
            self.confirm_btn.show()
            return
        self.confirm_btn.hide()
        # [2026-08-21] 敌方蓄势期间锁全部指令（防连点/提前出招）
        for b in (self.attack_btn, self.defend_btn, self.flee_btn):
            b.setEnabled(not self._waiting_enemy)
        # [P40] 借势环境：战场有险地且未用过才可用（文本带题材化名，用完隐藏）
        env_ready = bool(s.environment) and not s.environment_used
        self.env_btn.setVisible(env_ready)
        if env_ready:
            self.env_btn.setText(
                f"借势·{ce.env_display_name(s.environment, s.genre_id, seed=s.round)}")
        self.env_btn.setEnabled(env_ready and not self._waiting_enemy)
        # 技能按钮（冷却/mp 不足标灰）
        self._clear_layout(self.skill_row)
        has_skill = bool(s.player_skills)
        if has_skill:
            for sk in s.player_skills:
                if not isinstance(sk, dict):
                    continue
                sid = str(sk.get("id", sk.get("name", "")))
                cost = int(sk.get("cost_mp", 0) or 0)
                cd = s.player_cd.get(sid, 0)
                usable = (cd <= 0) and (cost <= s.player_mp)
                label = sk.get("name", "技能")
                # [P12] 技能成长：Lv2 起显示等级（威力每级 +15%，施展积熟练度升级）
                lv = ce.skill_level(sk)
                if lv > 1:
                    label += f"·{lv}级"
                # [P30] AOE 技能标「群」提示
                pattern = str(sk.get("target_pattern", "single") or "single")
                if pattern != "single":
                    label = f"群·{label}"
                if cd > 0:
                    label += f"(冷却{cd})"
                elif cost > 0:
                    label += f"({cost}蓝)"
                btn = QPushButton(label)
                # [技能图标] 有预生成图用图，无图用类型色 + 首字兜底（skill_brief 统一口径，
                # 同物品 full_item_pixmap；svc 缓存键自查 + 本地内容键双查防键漂移）
                try:
                    from src.ui.widgets.skill_brief import skill_icon_pixmap
                    from PySide6.QtGui import QIcon as _QIcon
                    icon_file = (self.svc.cached_skill_icon(sk)
                                 if getattr(self.svc, "cached_skill_icon", None) else None)
                    btn.setIcon(_QIcon(skill_icon_pixmap(sk, 26, icon_file)))
                    btn.setIconSize(QSize(26, 26))
                except Exception:
                    pass
                tip = f"威力随熟练度成长（当前 {lv} 级，每级 +15%，施展 +20 熟练度）"
                # [P30] inflicts / target_pattern 提示
                if pattern != "single":
                    tip += f"；目标模式：{pattern}"
                raw_inf = sk.get("inflicts") if isinstance(sk, dict) else None
                if isinstance(raw_inf, list) and raw_inf:
                    gid = self._genre_id()
                    inf_names = []
                    for inf in raw_inf:
                        if isinstance(inf, dict):
                            inf_names.append(ce.condition_display(str(inf.get("condition", "") or ""), gid))
                    if inf_names:
                        tip += f"；附带状态：{'、'.join(inf_names)}"
                btn.setToolTip(tip)
                btn.setObjectName("optionBtn")
                btn.setEnabled(usable and not self._waiting_enemy)
                btn.clicked.connect(lambda checked, _sk=sk: self._player_act("skill", skill=_sk))
                self.skill_row.addWidget(btn)
        self.skill_row.addStretch()
        # 物品按钮（背包消耗品）
        self._clear_layout(self.item_row)
        consumables = []
        seen = set()
        for iid in (self.world.player.inventory or []):
            if iid in seen:
                continue
            it = next((i for i in self.world.items if i.id == iid), None)
            if it and it.type == "consumable":
                # 所有消耗品都可见；无法在战斗快照内结算的物品置灰并标明战斗外使用。
                # revive 类倒下时自动生效，不进手动列表。
                _eff = getattr(it, "consume_effect", None)
                if isinstance(_eff, dict) and str(_eff.get("type", "") or "") == "revive":
                    continue
                consumables.append(it)
                seen.add(iid)
        if not consumables:
            pass
        else:
            for it in consumables:
                _eff = getattr(it, "consume_effect", None) or {}
                _kind = str(_eff.get("type", "") or "") if isinstance(_eff, dict) else ""
                usable_item = ce.combat_item_usable(it)
                if not usable_item:
                    effect_txt = "战斗外使用"
                elif _kind == "heal_mp":
                    effect_txt = f"+{_eff.get('amount', 0)}%灵力"
                elif _kind == "cure":
                    effect_txt = "解除异常"
                elif _kind == "heal_full" and _eff.get("amount"):
                    effect_txt = f"+{_eff['amount']}%HP"
                else:
                    effect_txt = f"+{it.heal_pct}%HP" if it.heal_pct > 0 else f"+{it.heal_amount}HP"
                btn = QPushButton(f"{it.name}({effect_txt})")
                btn.setObjectName("optionBtn")
                btn.setEnabled(usable_item and s.state == "active" and not self._waiting_enemy)
                if not usable_item:
                    btn.setToolTip("此物品的效果不能在战斗中结算")
                btn.clicked.connect(lambda checked, _it=it: self._player_act("item", item=_it))
                self.item_row.addWidget(btn)
        self.item_row.addStretch()
        # [P25b 战场化] 同伴指令行重建（每同伴一组；宠物 npc_id 空不受控）
        self._clear_layout(self.orders_row)
        _orderable = [u for u in (s.ally_units or []) if getattr(u, "npc_id", "")]
        if _orderable:
            for unit in _orderable:
                self._add_order_buttons(self.orders_row, unit)
            self.orders_row.addStretch()
        # [修 2026-09-05] 页签显隐（原三段标题行的等价替换：空内容隐藏页签）
        self._update_cmd_tabs(has_skill, bool(consumables), bool(_orderable))
        # [敌袭观战] 自动回合：观战态每帧刷新后（active 且无阶段锁）排下一手
        if self._spectate:
            self._spectate_schedule()

    def _spectate_schedule(self, delay_ms: int = 700):
        """[敌袭观战] 排定守方自动行动（防重入：单定时器；战斗结束/阶段锁时不再排）。"""
        if getattr(self, "_spectate_timer", None) is not None:
            return
        s = self.session
        if s is None or s.state != "active" or self._waiting_enemy:
            return
        t = QTimer(self)
        t.setSingleShot(True)

        def _go():
            self._spectate_timer = None
            self._spectate_step()

        t.timeout.connect(_go)
        self._spectate_timer = t
        t.start(delay_ms)

    def _spectate_step(self):
        """[敌袭观战] 守方一手：队长（玩家位）活着 -> AI 普攻（打手无技能，普攻即主战法）；
        队长倒下 -> 跳过玩家阶段直接同伴阶段（其余守卫继续打，引擎观战判定不提前判负）。"""
        s = self.session
        if s is None or s.state != "active" or self._waiting_enemy:
            return
        if s.player.hp > 0:
            self._player_act("attack")
        else:
            self._append_log(f"【第{s.round}回合】{s.player_label}已倒下，其余守军继续迎战")
            self._ally_phase(s.round)

    def _update_cmd_tabs(self, has_skill: bool, has_item: bool, has_orders: bool):
        """[修 2026-09-05] 指令页签显隐（原 skill_hdr/item_hdr/orders_hdr 的 show/hide
        等价替换）。当前页被隐藏时切到首个可见页（仿 P34d 背包 tab 口径）；
        全空收起整条页签防占空高。"""
        self.tabs.setTabVisible(self._tab_skill, has_skill)
        self.tabs.setTabVisible(self._tab_item, has_item)
        self.tabs.setTabVisible(self._tab_orders, has_orders)
        any_tab = has_skill or has_item or has_orders
        self.tabs.setVisible(any_tab)
        if not any_tab:
            return
        if not self.tabs.isTabVisible(self.tabs.currentIndex()):
            for i in range(self.tabs.count()):
                if self.tabs.isTabVisible(i):
                    self.tabs.setCurrentIndex(i)
                    break

    def _add_order_buttons(self, row, unit):
        """[P25b] 同伴指令组（战场化：挂在指令区「同伴指令」行）：自由/集火/守护/撤退。"""
        orders = (("free", "自由"), ("focus", "集火"), ("defend", "守护"), ("retreat", "撤退"))
        cur = str((self.session.ally_orders or {}).get(unit.npc_id, "free") or "free")
        tag = QLabel(f"{unit.name}:")
        tag.setStyleSheet("color:#9ece6a; font-size:11px;")
        row.addWidget(tag)
        for key, zh in orders:
            b = QPushButton(zh)
            b.setFixedHeight(22)
            b.setToolTip({"free": "自主作战（默认 AI）",
                          "focus": "集火你本回合攻击的目标",
                          "defend": "守护姿态：不攻击不治疗，概率替你挡刀（挡下的伤害减半）",
                          "retreat": "撤出战斗不再行动（战后不结算）"}[key])
            if key == cur:
                b.setStyleSheet("QPushButton{background:#2d4a2d; color:#9ece6a; "
                                "font-weight:bold; border:1px solid #9ece6a; border-radius:4px;}")
            def _set_order(checked=False, k=key, u=unit):
                self.session.ally_orders[u.npc_id] = k
                self._refresh()

            b.clicked.connect(_set_order)
            row.addWidget(b)

    def _genre_id(self) -> str:
        """[P30] 当前世界题材 id（状态显示名题材化；缺省 western_fantasy）。"""
        try:
            return str(getattr(self.world, "config_overlay", {}).get("attribute_template_id", "")
                       or "western_fantasy")
        except Exception:
            return "western_fantasy"

    def _conds_for_unit(self, unit) -> list:
        """[P30] 取某单位的当前状态列表（玩家/主敌是 bare CombatSnapshot，状态挂 session）。

        主敌包装 unit 的 snapshot is session.enemy -> enemy_conditions；玩家无单位行（状态单独渲染）。
        """
        s = self.session
        if s is None:
            return []
        snap = getattr(unit, "snapshot", None)
        if snap is s.enemy:
            return s.enemy_conditions
        if snap is s.player:
            return s.player_conditions
        return getattr(unit, "conditions", []) or []

    def _make_cond_label(self, conds) -> QLabel:
        """[P30] 据状态列表构建一个 QLabel（题材化名 + duration tooltip）。无状态返回空 label。"""
        gid = self._genre_id()
        if not conds:
            lbl = QLabel("")
            return lbl
        parts = []
        tips = []
        for c in conds:
            if not isinstance(c, dict):
                continue
            key = str(c.get("key", "") or "")
            if not key:
                continue
            disp = ce.condition_display(key, gid)
            dur = int(c.get("duration", 0) or 0)
            parts.append(disp)
            tips.append(f"{disp}（剩余 {dur} 回合）")
        lbl = QLabel("　".join(parts) if parts else "")
        if parts:
            lbl.setStyleSheet("color:#e0af68; font-size:11px;")
            lbl.setToolTip("；".join(tips))
        return lbl

    def _unit_label_text(self, unit, enemy: bool) -> str:
        """[战场化] 单位名字标签文本（含主敌机制提示，血条/名字标签共用）。

        [修 2026-09-06 真机] 名条宽有限——职能标注降噪：纯「随从」省略（主敌外皆是，
        无信息量），「随从·迅捷/重装」只留职能词；等级文字不再被挤遮挡。"""
        s = self.session
        role = str(unit.role_label or "")
        if role and role != "主敌":
            if role == "随从":
                role = ""                       # 纯随从：省略
            elif role.startswith("随从·"):
                role = role.split("·", 1)[1]    # 随从·重装 -> 重装
        elif role == "主敌":
            role = ""                           # 主敌不标注（原口径保留：默认目标已示 »）
        label_txt = f"{unit.name}" + (f"（{role}）" if role else "")
        lvl = getattr(unit.snapshot, "level", 0) or 0
        if lvl:
            label_txt = f"{label_txt} {lvl}级"
        if enemy and unit.snapshot is getattr(s, "enemy", None):
            mechs = getattr(s, "boss_mechanics", None) or []
            tips = []
            if "shield" in mechs and int(getattr(s, "boss_shield", 0) or 0) > 0:
                tips.append(f"护盾{s.boss_shield}(普攻)")
            if ("enrage_timer" in mechs and not s.boss_enrage_active
                    and int(getattr(s, "boss_enrage_round", 0) or 0) > 0
                    and s.enemy.hp > 0):   # [修 2026-09-05] 倒下主敌不挂「N 回合后狂暴」
                left = max(0, s.boss_enrage_round - s.round)
                tips.append(f"{left} 回合后狂暴")
            if tips:
                label_txt += "［" + "｜".join(tips) + "］"
        return label_txt

    def _set_bar(self, bar, cur, mx, tag):
        mx = max(1, int(mx))
        cur = max(0, min(int(cur), mx))
        bar.setRange(0, mx)
        # [特效包] 血条值平滑过渡（仅可见时；offscreen 测试不可见 -> 瞬时设值保断言口径）
        old = bar.value()
        if self.isVisible() and 0 <= old <= mx and old != cur:
            self._run(bar, b"value", old, cur, 420, QEasingCurve.OutCubic)
        else:
            bar.setValue(cur)
        bar.setFormat(f"{tag} {cur}/{mx}")
        ratio = cur / mx
        bar.setProperty("hpLevel", "low" if ratio < 0.3 else "mid" if ratio < 0.6 else "high")
        bar.style().unpolish(bar)
        bar.style().polish(bar)

    def _clear_layout(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    # ---------- 战斗流程 ----------
    # ---------- [回合流水线 2026-09-01] 每阶段结算->播动画->等动画走完才进下一阶段 ----------
    # 阶段基础时长（毫秒）：结算值先算好，动画只是把这段时长「演」出来。
    # [修 2026-09-01] 攻击/技能类再放慢（用户反馈仍偏快）；喝药/防御等无打击动画的
    # 操作走短阶段（飘字起效即可，别让"喝水站 900ms"拖节奏）。
    _PHASE_MS_PLAYER = 1250     # 攻击/技能（冲刺 + 部位 roll + 飘字全读完）
    _PHASE_MS_LIGHT = 600       # 喝药/防御/逃跑等无打击动画操作
    _PHASE_MS_ALLY = 950        # 同伴出手
    _PHASE_MS_ENEMY = 1100      # 敌方出手
    # [修 2026-09-05] 部位骰先行拍时长：骰子 1300ms 减速轮换，1150ms 时出手落地
    # （定格前一瞬命中，骰子锁死即结果揭晓）
    _PART_LEAD_MS = 1150

    def _player_act(self, action: str, skill=None, item=None):
        s = self.session
        if s is None or s.state != "active" or self._waiting_enemy:
            return
        rng = SeededRng.seed_from(self.world.id, self.world.tick_count, f"combat_pr{s.round}")
        _hp_before = {id(u): u.snapshot.hp for u in ce.all_enemy_combatants(s)}
        _p_hp_before = s.player.hp
        if action == "env":
            # [P40] 借势环境（一次性指令，消耗玩家回合；结算含结束判定）
            r = ce.use_environment(s, rng)
            log = r.get("desc") or ""
        elif action == "skill" and skill is not None:
            log = ce.player_action(s, "skill", rng, skill=skill, target=self._target_unit)
        elif action == "item" and item is not None:
            if (getattr(item, "type", "") != "consumable"
                    or item.id not in self.world.player.inventory
                    or not ce.combat_item_usable(item)):
                return
            # 引擎会让眩晕/冰封者跳过行动、让恐惧者改为逃跑；这些情况没有用药。
            item_blocked = any(isinstance(c, dict) and c.get("key") in
                               ("stunned", "frozen", "feared")
                               for c in s.player_conditions)
            # [百分比回血 2026-09-01] 传 Item 本体（engine 按 heal_pct 优先结算）
            log = ce.player_action(s, "item", rng, item_heal=item)
            if not item_blocked and item.id in self.world.player.inventory:
                self.world.player.inventory.remove(item.id)
                # [!] 记录本战已消耗：中途关窗（reject 不走 _finish 结算）时回滚，
                # 否则物品从活 world 上白丢且随后被场景页落盘。
                self._consumed_ids.append(item.id)
        else:
            log = ce.player_action(s, action, rng, target=self._target_unit)
        if log:
            self._append_log(f"【第{s.round}回合】{log}")
        # [修 2026-09-05 第三轮] 同步实际打击目标：引擎结算时若目标已死会自动改打
        # 存活者并写 session.player_target——UI 选中不跟随会把部位骰/冲刺打到旧尸体上
        # （真人测试：击杀 A 后打 B，部位骰仍播在 A 头上）。
        if getattr(s, "player_target", None) is not None:
            self._target_unit = s.player_target
        # [回合流水线] 出手瞬间先锁指令（仅异步游玩态；同步测试模式由 _phase_after 直通）
        if self._enemy_delay_ms() > 0:
            self._waiting_enemy = True
            for b in (self.attack_btn, self.defend_btn, self.flee_btn):
                b.setEnabled(False)
        # [修 2026-09-01] 行动结算完立即刷新血条/按钮（喝药回血、敌人掉血都要即时可见，
        # 不能等敌方回合才更新——用户实测「喝了药要对面攻击我才加血」）
        # [修 2026-09-05 第三轮] 两拍路径例外：血条刷新随打击一起延到第二拍——
        # 结算即刷会让血扣跑在冲刺前面 1.15s，骰子还没定格敌人血就掉了。
        # [修 2026-09-05 第三轮] 击杀回合不再提前收场：致命一击同样走两拍
        # （骰子 -> 出手 -> 命中倒地 -> 胜利结算），_play_impact_phase 内判 finish。
        _sk_type = str((skill or {}).get("type", "") or "") if isinstance(skill, dict) else ""
        phase_ms = (self._PHASE_MS_LIGHT
                    if action not in ("attack", "skill", "env")
                    or (action == "skill" and _sk_type in ("heal", "buff"))
                    else self._PHASE_MS_PLAYER)
        _part_txt = (self._part_dice_text(log or "")
                     if action in ("attack", "skill", "env") else "")
        _part_sp = self._anim_target_sprite() if _part_txt else None
        if _part_sp is None:
            self._refresh()
        if _part_sp is not None:
            self._part_roll_anim(_part_sp, _part_txt)
            self._phase_after(
                self._PART_LEAD_MS,
                lambda: self._play_impact_phase(action, log, _hp_before,
                                                _p_hp_before, phase_ms, skill))
            return
        self._play_impact_phase(action, log, _hp_before, _p_hp_before, phase_ms, skill)

    def _play_impact_phase(self, action, log, hp_before, p_hp_before, phase_ms, skill):
        """[修 2026-09-05] 第二拍：打击动画 + 受击飘字 -> 阶段流转 -> 血条结算刷新。

        刷新放在阶段流转之后：异步态 _phase_after/_finish_after 会重置 _waiting_enemy，
        先流转再刷新可避免按钮在动画中被短暂解锁。"""
        s = self.session
        if s is None:
            return
        self._play_player_anim(action, log, hp_before, p_hp_before, skill=skill)
        if s.state != "active":
            self._finish_after(phase_ms)
            self._refresh()
            return
        self._phase_after(phase_ms, lambda: self._ally_phase(s.round))
        self._refresh()

    def _ally_phase(self, round_no: int):
        """同伴阶段：结算 + 动画 + 等待，再进敌方阶段。"""
        s = self.session
        if s is None or s.state != "active":
            self._maybe_finish()
            return
        rng_a = SeededRng.seed_from(self.world.id, self.world.tick_count, f"combat_al{round_no}")
        _a_before = {id(u): u.snapshot.hp for u in ce.all_enemy_combatants(s)}
        _pa_p_before = s.player.hp
        alog = ce.ally_turn(s, rng_a)
        if alog:
            self._append_log(f"  {alog}")
        # [战场化] 同伴行动表现：出手同伴冲刺 + 被打敌方单位闪白/飘字
        self._play_side_anim(hp_before_map=_a_before, side="ally", log=alog or "",
                             p_hp_before=_pa_p_before)
        if s.state != "active":
            self._finish_after(self._PHASE_MS_ALLY)
            return
        # 同伴动画演完 -> 敌方阶段
        self._phase_after(self._PHASE_MS_ALLY, self._enemy_phase)

    def _enemy_phase(self):
        """敌方阶段：蓄势等待（已有延迟节奏）-> 出手。"""
        s = self.session
        if s is None or s.state != "active":
            self._maybe_finish()
            return
        delay = self._enemy_delay_ms()
        if delay > 0:
            self._begin_enemy_wait(delay)
            return
        self._run_enemy_turn()

    def _phase_after(self, ms: int, fn):
        """[回合流水线] 等 ms 毫秒（动画时长）后进下一阶段。

        [!] delay=0 的测试/同步模式（preset 缺字段桩）跳过等待直接同步结算，
        保住 2459 存量测试的同步断言口径；游玩态（默认 600ms）走真等待。
        """
        if ms <= 0 or self._enemy_delay_ms() <= 0:
            fn()
            return
        self._waiting_enemy = True      # 阶段间锁指令（防连点跳阶段）
        t = QTimer(self)
        t.setSingleShot(True)

        def _step():
            self._waiting_enemy = False
            self._enemy_timer = None
            fn()

        t.timeout.connect(_step)
        self._enemy_timer = t
        t.start(ms)

    def _finish_after(self, ms: int):
        """终局也等动画演完再结算关场（[!] 同步模式立即结算保测试口径）。"""
        if ms <= 0 or self._enemy_delay_ms() <= 0:
            self._finish()
            return
        self._waiting_enemy = True
        t = QTimer(self)
        t.setSingleShot(True)

        def _fin():
            self._waiting_enemy = False
            self._enemy_timer = None
            self._finish()

        t.timeout.connect(_fin)
        self._enemy_timer = t
        t.start(ms)

    def _maybe_finish(self):
        s = self.session
        if s is None:
            return
        if s.state != "active":
            self._finish()
        else:
            self._refresh()

    def _anim_target_sprite(self):
        """当前行动目标的立绘（选中目标优先，回退主敌）。无返回 None。"""
        s = self.session
        if s is None:
            return None
        tgt = self._target_unit
        tgt_sprite = self._sprites.get(id(tgt)) if tgt is not None else None
        main_sp = self._sprites.get(id((s.enemy_units or [None])[0])) if s.enemy_units else None
        return tgt_sprite or main_sp

    def _play_player_anim(self, action: str, log: str, hp_before: dict, p_hp_before: int,
                          skill=None):
        """[战场化] 玩家行动表现层：按操作差异化特效（全纯表现，不碰数值）。

        普攻=冲刺+白闪+红字；打击技能=全屏闪白+震屏+更远冲刺；借势=闪白+重震屏；
        暴击=橙金大字+目标震退；弱点=金字+金圈脉冲；喝药=绿字回复；防御=护盾光圈。
        [修 2026-09-05] 治疗/增益技能不吃打击三件套（原实现「技能=攻击表现」一刀切，
        回春诀给自己回血也冲脸震屏）；治疗改绿字回复飘字（满血则「气血已满」），增益
        改紫字提示。[修 2026-09-05 第二轮] 部位骰动画移到第一拍（_player_act 拆两拍
        先骰后打），此处不再播部位 roll。
        """
        s = self.session
        if s is None:
            return
        log = log or ""
        # 目标单位（当前选中/主敌）
        tgt_sprite = self._anim_target_sprite()
        # 按操作差异化出手动作（打击式冲刺朝目标真实方向；撞击感=贴脸快出+回弹）
        if action == "attack":
            self._strike(self._player_sprite, tgt_sprite)
        elif action == "skill":
            _stype = str((skill or {}).get("type", "") or "") if isinstance(skill, dict) else ""
            if _stype == "heal":
                gain = max(0, s.player.hp - p_hp_before)
                self._pop_number(f"+{gain}" if gain else "气血已满",
                                 self._player_sprite,
                                 color="#9ece6a" if gain else "#9aa5ce", big=bool(gain))
            elif _stype == "buff":
                self._pop_number("增益", self._player_sprite, color="#9d7cd8")
            else:
                self._strike(self._player_sprite, tgt_sprite, ms_out=330, ms_back=540)
                self._screen_flash()
                self._shake(10, 300)
        elif action == "env":
            self._screen_flash(0.5, 200)
            self._shake(12, 340)
        elif action == "item" and s.player.hp > p_hp_before:
            self._pop_number(f"+{s.player.hp - p_hp_before}", self._player_sprite,
                             color="#9ece6a", big=True)
        elif action == "item":
            # 满血喝药（回复 0）也给出反馈，避免「点了没反应」
            self._pop_number("气血已满", self._player_sprite, color="#9aa5ce")
        elif action == "defend":
            if self._player_sprite is not None:
                # [!] 先停在满值再动画（同步敌方回合会在 player_action 内立即跑完，
                # 随后 _run_enemy_turn 的收场淡出从 1.0 起；若此处还挂 0->1 的
                # 200ms 动画会与淡出竞写同属性，最终值被本动画钉死在 1.0）
                self._player_sprite.set_shield(1.0)
            self._pop_number("防御", self._player_sprite, color="#7aa2f7")
        # 受击方闪白 + 飘字（按 hp 差值找被打单位--普攻/单体技能/AOE 全覆盖）
        for uid, hp0 in hp_before.items():
            unit = next((u for u in ce.all_enemy_combatants(s) if id(u) == uid), None)
            if unit is None:
                continue
            delta = hp0 - unit.snapshot.hp
            sp = self._sprites.get(uid)
            if delta > 0 and sp is not None:
                self._flash(sp)
                crit = "暴击" in log
                weak = "弱点" in log
                dmg_color = "#e0af68" if weak else ("#ff9e64" if crit else "#ff5577")
                # 多目标命中时错开横位（AOE 全体同帧飘字不叠）
                off = 0 if sp is tgt_sprite else (34 if (uid % 2) else -34)
                self._pop_number(f"-{delta}", sp, color=dmg_color, big=(crit or weak), dx=off)
                if crit:
                    self._lunge(sp, +45, 90, 140)   # 被暴击震退
        # 暴击补震屏（普攻暴击也有；技能已震过则更重一档）
        if "暴击" in log:
            self._shake(12, 320)
        # 弱点命中：目标金圈脉冲
        if "弱点" in log and tgt_sprite is not None:
            self._ring_pulse(tgt_sprite)
        # 玩家受击（反击等场景）
        if s.player.hp < p_hp_before:
            self._flash(self._player_sprite)
            self._pop_number(f"-{p_hp_before - s.player.hp}", self._player_sprite, color="#7aa2f7")
        # [对撞 2026-09-01] 敌方防御被玩家普攻命中 -> 播对撞条（攻=玩家立绘，守=目标立绘）
        self._play_clash_from_log(log, self._player_sprite, tgt_sprite)

    def _play_side_anim(self, hp_before_map: dict, side: str, log: str, p_hp_before: int = -1):
        """[战场化] 一方行动后的表现层：被打单位闪白 + 伤害飘字 + 出手方冲刺。

        hp_before_map：行动前的 {unit_id -> hp} 快照；与当前 hp 差值 > 0 即被打。
        side="ally"：同伴打敌方（打人方在左，冲刺向右）；"enemy"：敌方打玩家方。
        p_hp_before >= 0 时检测同伴治疗玩家（绿字 +回复）。
        """
        s = self.session
        if s is None:
            return
        log = log or ""
        for uid, hp0 in hp_before_map.items():
            unit = next((u for u in ce.all_enemy_combatants(s) if id(u) == uid), None)
            if unit is None:
                continue
            delta = hp0 - unit.snapshot.hp
            sp = self._sprites.get(uid)
            if delta > 0 and sp is not None:
                self._flash(sp)
                crit = "暴击" in log
                weak = "弱点" in log
                color = "#e0af68" if weak else ("#ff9e64" if crit else "#ff5577")
                self._pop_number(f"-{delta}", sp, color=color, big=(crit or weak))
                if crit:
                    self._lunge(sp, +45, 90, 140)
        if "暴击" in log:
            self._shake(10, 300)
        # 出手方冲刺（ally 侧：第一个存活同伴）；打击类才有动作
        # [修 2026-09-05] 收紧为打击口径（「施展」须带「，对」打击段）：同伴治疗/增益
        # 施展不再冲向敌群（与 _enemy_action_anims 同口径）
        if side == "ally" and ("攻击" in log or ("施展" in log and "，对" in log)):
            for u in (s.ally_units or []):
                if u.alive:
                    sp = self._sprites.get(id(u))
                    if sp is not None:
                        self._lunge(sp, +70)
                    break
        # 同伴治疗玩家（绿字 +回复飘字）
        if side == "ally" and p_hp_before >= 0 and s.player.hp > p_hp_before:
            self._pop_number(f"+{s.player.hp - p_hp_before}", self._player_sprite,
                             color="#9ece6a", big=True)
        # [对撞 2026-09-01] 同伴普攻打防御中的敌方 -> 播对撞条（攻=同伴立绘，守=被打敌立绘）
        if side == "ally" and "对撞" in log:
            a_sp = None
            for u in (s.ally_units or []):
                if u.alive:
                    a_sp = self._sprites.get(id(u))
                    break
            d_sp = None
            for uid, hp0 in hp_before_map.items():
                u = next((x for x in ce.all_enemy_combatants(s) if id(x) == uid), None)
                if u is not None and hp0 > u.snapshot.hp:
                    d_sp = self._sprites.get(uid)
                    break
            self._play_clash_from_log(log, a_sp, d_sp)

    def _enemy_delay_ms(self) -> int:
        """敌方行动延迟（毫秒，钳 0-5000）。preset 缺字段（测试桩）回退 0=同步。"""
        try:
            v = int(getattr(self.preset, "combat_enemy_delay_ms", 0) or 0)
        except (TypeError, ValueError):
            v = 0
        return max(0, min(5000, v))

    def _begin_enemy_wait(self, delay_ms: int):
        """进入敌方蓄势态：锁指令 + 日志提示 + QTimer 定时出手。

        [修 2026-09-01] 蓄势表现挂「存活」敌方单位：主敌已倒（hp=0 淡出中）但随从
        尚存时，原实现让尸体抖动+飘蓄势字+日志写死者名字，随后随从攻击命中——
        用户视角即「死人蓄势后攻击我」。主敌死 -> 首个存活随从蓄势；全灭不播。
        """
        self._waiting_enemy = True
        s = self.session
        actor = None
        for u in ce.all_enemy_combatants(s) if s else []:
            if u.alive:
                actor = u
                break
        actor_sp = self._sprites.get(id(actor)) if actor is not None else None
        if actor_sp is not None:
            self._wobble(actor_sp, ms=min(600, max(300, delay_ms)))
            self._pop_number("…蓄势", actor_sp, color="#e0af68")
        if actor is not None:
            self._append_log(f"　{actor.name} 蓄势待发…")
        self._refresh()
        self._enemy_timer = QTimer(self)
        self._enemy_timer.setSingleShot(True)
        self._enemy_timer.timeout.connect(self._on_enemy_timer)
        self._enemy_timer.start(delay_ms)

    def _on_enemy_timer(self):
        t = self._enemy_timer
        self._enemy_timer = None
        if t is not None:
            t.deleteLater()          # 已触发的单发 timer 立即回收（防长战累积）
        if not self._waiting_enemy:
            return
        self._waiting_enemy = False
        self._run_enemy_turn()

    def _enemy_action_anims(self, elog: str) -> list:
        """[修 2026-09-05] 敌方回合日志 -> [(出手立绘, "attack"|"skill", 目标立绘)]。

        原实现把敌方动画无条件归给主敌立绘（enemy_units[0]）：主敌被击败后随从
        仍在打 -> 倒下的主敌（压暗+「倒下」标）每回合「诈尸」冲刺攻击玩家——真人
        测试误报「倒下还能攻击」。引擎侧有守卫（_main_enemy_act hp<=0 跳过 / 随从
        循环 unit.alive 过滤），纯表现层归属错误；与 _begin_enemy_wait 的「存活
        优先」同思路。引擎无结构化回传通道（对撞条同款口径：按日志文本解析，
        引擎日志格式变更须同步此处）。归属规则：随从行以自身名字开头（长名优先
        匹配，防「XX的爪牙」含主敌名子串）；主敌行以「敌人」开头；「你举盾迎击，」
        对撞前缀先剥掉；挡刀行以守护同伴名开头、出手者藏在「挡下<名>的攻击」里。
        已倒下单位的行不动画；自疗/增益施展（无「，对」打击段）不冲刺。
        """
        s = self.session
        out = []
        if s is None or not elog:
            return out
        units = [u for u in (s.enemy_units or []) if isinstance(u, ce.CombatUnit)]
        by_len = sorted((u for u in units if getattr(u, "name", "")),
                        key=lambda x: -len(x.name))
        for raw in elog.split("\n"):
            line = raw.strip()
            # [P47-C2] 【敌情】预告行直接跳过（防「锁定/酝酿」被误归属冲刺动画）
            if line.startswith("【敌情】"):
                continue
            if line.startswith("你举盾迎击，"):
                line = line[len("你举盾迎击，"):]
            kind = ("attack" if "攻击" in line
                    else ("skill" if "施展" in line else None))
            if kind is None:
                continue
            if kind == "skill" and "，对" not in line:
                continue  # 自疗/增益/无目标施展：无打击动作不冲刺
            actor = next((u for u in by_len if line.startswith(u.name)), None)
            if actor is None:
                if line.startswith("敌人") or "挡下敌人的攻击" in line \
                        or "挡下了敌人的攻击" in line:
                    actor = units[0] if units else None
                else:
                    # 挡刀行出手者藏在「挡下<名>的攻击」里（未命中分支多一个「了」）
                    actor = next(
                        (u for u in by_len
                         if f"挡下{u.name}的攻击" in line
                         or f"挡下了{u.name}的攻击" in line), None)
            if actor is None or not actor.alive:
                continue
            # 目标立绘：打同伴（行内出现同伴名/「同伴XX」）冲向该同伴，否则冲玩家
            t_sp = self._player_sprite
            for a in (s.ally_units or []):
                an = getattr(a, "name", "")
                if not an:
                    continue
                if an in line or f"{getattr(a, 'role_label', '') or '同伴'}{an}" in line:
                    _sp = self._sprites.get(id(a))
                    if _sp is not None:
                        t_sp = _sp
                    break
            a_sp = self._sprites.get(id(actor))
            if a_sp is not None:
                out.append((a_sp, kind, t_sp))
        return out

    def _run_enemy_turn(self):
        """敌人回合（主敌 + 随从，逐单位演出）：每一手等到演出时机才结算。

        [依次出手 2026-10-02 用户指示] 旧实现一次结算整轮、全部冲刺/飘字同时上——
        双敌观感是「同时扑向玩家」。改为惰性分步：按 主敌->随从 逐手 next() 消费
        engine.enemy_acts（rng 消费顺序与旧单次口径一致，数值不变），每一手演完
        （敌方节奏等待）再结算下一手，最后一手后才回合收口（DoT/战意/回合推进）。"""
        s = self.session
        if s is None:
            return
        if s.state != "active":
            self._refresh()
            return
        rng2 = SeededRng.seed_from(self.world.id, self.world.tick_count, f"combat_er{s.round}")
        self._enemy_seq = {
            "gen": ce.enemy_acts(s, rng2),
            "snap": ce.enemy_turn_begin(s),
            "was_def": bool(getattr(s, "player_defending", False)),
        }
        self._play_next_enemy_act()

    def _play_next_enemy_act(self):
        s = self.session
        seq = getattr(self, "_enemy_seq", None)
        if s is None or seq is None:
            return
        if s.state != "active":
            # 玩家在上一手已倒下：跳过余手直接收口（同旧口径：acts 循环遇终局 break）
            self._finish_enemy_sequence(seq)
            return
        p0 = s.player.hp
        a0 = {id(u): u.snapshot.hp for u in (s.ally_units or [])}
        try:
            act = next(seq["gen"])
        except StopIteration:
            act = None
        if act:
            self._append_log(f"  {act}")
            self._play_one_enemy_act(act, p0, a0, seq["was_def"])
            # 演完这一手再打下一手（同步口径立即递归；游玩态按敌方节奏等待）
            self._phase_after(self._enemy_delay_ms(), self._play_next_enemy_act)
            return
        self._finish_enemy_sequence(seq)

    def _finish_enemy_sequence(self, seq):
        self._enemy_seq = None
        s = self.session
        if s is None:
            return
        # [特效包] 防御护盾收场：被打碎（快碎）/回合平安过去（缓收）
        if seq["was_def"] and self._player_sprite is not None \
                and self._player_sprite.get_shield() > 0.01:
            _p_hit = s.player.hp < seq["snap"][0]
            self._run(self._player_sprite, b"shield",
                      self._player_sprite.get_shield(), 0.0, 120 if _p_hit else 450)
        # 全部出手完毕 -> 回合收口（DoT/战意/CD/Boss/回合推进/承受累计）
        for line in ce.enemy_turn_finalize(s, seq["snap"]):
            self._append_log(f"  {line}")
        self._refresh()
        if s.state != "active":
            self._finish()

    def _play_one_enemy_act(self, act, p0, a0, was_def):
        """单个敌方单位的出手表现（冲刺/闪白/飘字/对撞条；解析自该单位日志行）。"""
        s = self.session
        # [战场化] 敌方行动表现：出手敌冲刺（朝真实目标方向）+ 闪白与飘字
        # [修 2026-09-05] 冲刺按日志行归属到真正出手的单位（原实现无条件归主敌立绘，
        # 主敌被击败后随从仍打 -> 尸体每回合冲刺攻击玩家，视觉即「倒下还能攻击」）。
        for _a_sp, _a_kind, _t_sp in self._enemy_action_anims(act):
            if _a_kind == "attack":
                self._lunge_to(_a_sp, _t_sp or self._player_sprite, stop_gap=60,
                               ms_out=240, ms_back=340)
            else:
                self._lunge_to(_a_sp, _t_sp or self._player_sprite, stop_gap=90,
                               ms_out=300, ms_back=400)
                # [特效包] 敌方放技能：全屏闪白 + 震屏（压迫感）
                self._screen_flash(0.35, 180)
                self._shake(10, 300)
        p_hit = s.player.hp < p0
        if p_hit and self._player_sprite is not None:
            self._flash(self._player_sprite)
            crit = "暴击" in act
            self._pop_number(f"-{p0 - s.player.hp}", self._player_sprite,
                             color="#7aa2f7", big=crit)
            self._shake(10 if crit else 6, 300 if crit else 220)
            if crit:
                self._lunge(self._player_sprite, -45, 90, 140)   # 被暴击震退
        for uid, hp0 in a0.items():
            u = next((x for x in (s.ally_units or []) if id(x) == uid), None)
            if u is None:
                continue
            delta = hp0 - u.snapshot.hp
            sp = self._sprites.get(uid)
            if delta > 0 and sp is not None:
                self._flash(sp)
                self._pop_number(f"-{delta}", sp, color="#7aa2f7")
        # [对撞 2026-09-01] 玩家防御被敌普攻命中 -> 播对撞条（攻/守立绘按对撞行
        # 名字归属，[修 2026-09-05] 主敌倒下后随从打出的对撞不再显示尸体头像）
        if was_def:
            self._play_clash_from_log(act, None, self._player_sprite)

    def _stop_enemy_timer(self):
        """停掉待发的敌方回合（关窗/退战时；未出手的回合直接丢弃，session 不写回）。"""
        self._waiting_enemy = False
        self._enemy_seq = None     # [依次出手] 未演完的敌方序列一并丢弃
        t = self._enemy_timer
        self._enemy_timer = None
        if t is not None:
            try:
                t.timeout.disconnect()
            except (RuntimeError, TypeError):
                pass
            t.stop()
            t.deleteLater()

    def _finish(self):
        # [敌袭观战] 玩家未参战：不走 finish_combat（无掉落/金币/经验结算），
        # 据点缴获/繁荣/负伤回写由场景页 settle_spectated_siege 管；此处只报战况收场。
        if self._spectate:
            if getattr(self, "_spectate_timer", None) is not None:
                self._spectate_timer.stop()
                self._spectate_timer = None
            s0 = self.session
            self.result_summary = {"spectate": True, "state": s0.state}
            txt = {"won": "守军击退来敌！缴获入据点资金池。",
                   "lost": "守军力战不支，据点失守……",
                   "fled": "来敌退走。"}.get(s0.state, "战斗结束")
            self._append_log(f"—— 观战结束：{txt} ——")
            self.confirm_btn.setText("战斗结束，确认")
            self.confirm_btn.show()
            self._refresh()
            return
        # [P47 修复 2026-09-25] 复活丹自动生效（最后一道保险）：判负结算前，若玩家倒下
        # 且背包有复活丹 -> 自动消耗并站起，战斗继续。物品页里的说明「倒下时自动生效」
        # 从此是真的（原实现只有文案没有机制，DeepSeek 评估 F-1）。
        if self.session is not None and self.session.state == "lost":
            _rev = ce.try_auto_revive(self.world, self.world.player,
                                      hp_target=self.session.player,
                                      consumed_ids=self._consumed_ids)
            if _rev:
                self.session.state = "active"
                self._append_log(f"「{_rev}」在你怀中碎裂，一股暖流托着你重新站起"
                                 f"（HP {self.session.player.hp}/{self.session.player.hp_max}）——战斗继续！")
                self._refresh()
                return
        # 正常结算路径：消耗已生效（finish_combat 写回），不再回滚
        self._consumed_ids.clear()
        self.result_summary = self.svc.finish_combat(
            self.world, self.session, self.target, self.preset)
        state_txt = {"won": "你获胜了！", "lost": "你被击败！", "fled": "双方脱战。"}.get(
            self.session.state, "战斗结束")
        self._append_log(f"===== {state_txt} =====")
        # [G01/R3 2026-09-30] 战后结算卡：拾取/遗留奖励/任务变化/伤情/世界变化
        # （纯读 summary+世界即时状态，来源可追；失败静默不打断战报）
        try:
            for _ln in self.svc.battle_settlement(self.world, self.result_summary):
                self._append_log(f"—— {_ln}")
        except Exception:
            pass
        self._refresh()

    def _append_log(self, text: str):
        self.log_edit.append(text)
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def get_summary(self):
        return self.result_summary

    # ---------- 生命周期（守 §15：判定 worker 退出时清理）----------
    def _rollback_consumed(self):
        """中途退战（X/Esc，reject 不走 _finish）：回滚本战已扣除的消耗品。

        HP/MP 只写在 session 快照（finish_combat 才写回 world），reject 即丢弃——
        物品同理不该白丢；不回滚的话场景页下一次 save_world 会把损失落盘。
        [!] 按消耗计数无条件补回（consume 1 颗 -> 补 1 颗），勿用「现有数」对比：
        原实现 n - have 补差在「包里还有同款剩余」时误判不用还（2 颗喝 1 剩 1 ->
        have=1 >= need=1 -> 白丢 1 颗；只有喝光才回滚）。
        """
        inv = self.world.player.inventory
        for iid in self._consumed_ids:
            inv.append(iid)
        self._consumed_ids.clear()

    def reject(self):
        # [敌袭观战] 中途关窗不允许（敌袭必须打完结算，pending 依赖终局）；ESC/右上角
        # 关闭在战斗进行中直接吞掉，结束时走 confirm_btn accept。
        if self._spectate:
            s0 = self.session
            if s0 is not None and s0.state == "active":
                return
        self._cleanup_judge_worker()
        self._stop_enemy_timer()
        self._settle_if_ended()
        self._rollback_consumed()
        super().reject()

    def accept(self):
        self._stop_enemy_timer()
        self._settle_if_ended()
        super().accept()

    def closeEvent(self, event):
        self._cleanup_judge_worker()
        self._stop_enemy_timer()
        self._settle_if_ended()
        self._rollback_consumed()
        super().closeEvent(event)

    def _settle_if_ended(self):
        """[修 2026-10-02 真机 P1·结算竞态] 终局动画窗口内点确认/关窗的兜底结算。

        背景：玩家回合击杀终局走 _finish_after（动画定时器延迟 _finish），而 _refresh
        在状态翻转后立刻亮出「战斗结束，确认」——真机 tick100 案：确认点击先于动画
        定时器，accept() 的 _stop_enemy_timer 把待发结算杀掉，_finish 永不执行 ->
        get_summary() 返回 None -> 场景页战后收尾整段跳过（无战报/无掉落/无经验/
        kill 进度不动/胜场不记；敌回合终局同步 _finish 故败北路径不受影响）。
        关窗/确认前兜底同步结算：state != active 且 result_summary 为空时补一次
        _finish（finish_combat 有 _settle_done 幂等闸 + 此处判重，重复调用无副作用）。
        """
        s = self.session
        if s is None or self.result_summary is not None:
            return
        if s.state != "active":
            self._finish()
