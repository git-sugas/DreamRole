"""世界生成对话框：填预设表单 -> 启动 WorldGenWorker -> 生成完成后开预览对话框。

仿 group_setup.py（滚动区表单 + QFormLayout + 固定底部按钮）+ tts_dialog.py（worker 启动 + closeEvent wait）。
表单字段对应 service.generate_world_skeleton 的 form dict。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QTextEdit,
    QPushButton, QCheckBox, QSlider, QComboBox,
    QScrollArea, QFrame, QProgressBar, QMessageBox, QWidget,
    QGridLayout,
)

from src.models import WorldSimPreset
from src.ui.dialogs.world_gen_worker import WorldGenWorker
from src.ui.widgets.avatar_grid_selector import AvatarGridSelector


_TONE_OPTIONS = ["轻松", "严肃", "黑暗", "荒诞", "热血", "日常"]
# [P12] 基础地点数翻倍（3/5/8 -> 6/10/16）；此常量已不被引用（下拉走 BUILTIN_SCALES），仅留档对齐
_SCALE_OPTIONS = [("small", "小（6地点5NPC）"), ("medium", "中（10地点10NPC）"), ("large", "大（16地点15NPC）")]

# [开局 30 分钟改造包 2026-09-26] 一键模板世界：每个内置题材一套现成开局配置，
# 点一下填表 + 直接生成（跳过「不知道世界观该写什么」的空白卡壳）。
# tags 填的是 default_genre_tags 的**显示标签**（复选框 key），premise 是可再编辑的起点
# 不是终点；scale 用 BUILTIN_SCALES 的 id。自定义题材用户仍走下方表单，互不影响。
_QUICK_TEMPLATES = [
    {
        "id": "xianxia", "name": "仙侠",
        "desc": "凡人问长生，宗门与秘宝",
        "premise": "你是青云山脚下的贫寒药童，替宗门上山送药时失足坠崖，醒来后发现掌心多了一枚会发烫的古印，从此被卷入修行界的争夺。",
        "tags": ["仙侠修真"], "tone": "热血", "magic": 85, "tech": 10, "scale": "medium",
    },
    {
        "id": "wuxia", "name": "武侠",
        "desc": "快意恩仇，江湖路远",
        "premise": "你是初入江湖的无名少年，替病重的老镖师接下最后一趟镖，护送一只谁也不许看的木箱去千里之外的府城，路上已经有人盯上了你。",
        "tags": ["武侠古代"], "tone": "热血", "magic": 5, "tech": 15, "scale": "medium",
    },
    {
        "id": "western_fantasy", "name": "西幻",
        "desc": "剑与魔法，王国风云",
        "premise": "你是边境小村的猎户之子，近来商队接连在黑松林失踪，领主悬赏彻查，你为了赏金和失踪的哥哥背起长弓进了林子。",
        "tags": ["西幻中世纪"], "tone": "严肃", "magic": 55, "tech": 20, "scale": "medium",
    },
    {
        "id": "modern", "name": "现代",
        "desc": "都市人情，平凡处起波澜",
        "premise": "你是刚到这座大城市打拼的普通人，租住的老小区里邻居们各怀心事，一封塞错门缝的信把你拖进了一桩没人敢提的旧事。",
        "tags": ["现代都市"], "tone": "日常", "magic": 0, "tech": 60, "scale": "medium",
    },
    {
        "id": "scifi", "name": "科幻",
        "desc": "星海远航，未知边境",
        "premise": "你是货运飞船「远潮号」上最年轻的轮机员，跃迁事故把船抛在一片图上没有的星域，舰长失踪、燃料减半，幸存者都看着你。",
        "tags": ["科幻未来"], "tone": "严肃", "magic": 0, "tech": 90, "scale": "medium",
    },
    {
        "id": "apocalypse", "name": "末日",
        "desc": "废土求生，人性冷暖",
        "premise": "你是七号避难所的新晋幸存者，储备只够撑十天，长老把最后一支步枪交给你，让你出去为全所人找回落在废墟里的净水芯片。",
        "tags": ["末日废土"], "tone": "黑暗", "magic": 0, "tech": 45, "scale": "medium",
    },
]



def _magic_word(v: int) -> str:
    """魔法等级滑块的语义词（告诉玩家这个数字意味着什么样的世界）。"""
    if v == 0:
        return "无魔世界"
    if v <= 30:
        return "低魔（法术罕见，凡人为主）"
    if v <= 70:
        return "中魔（修行者常见）"
    return "高魔（魔法即日常）"


def _tech_word(v: int) -> str:
    """科技等级滑块的语义词。"""
    if v <= 20:
        return "原始（冷兵器/农耕）"
    if v <= 40:
        return "近代（火器/机械）"
    if v <= 60:
        return "现代（信息时代）"
    if v <= 85:
        return "近未来（智能义体）"
    return "未来（星际/赛博）"


class WorldGenDialog(QDialog):
    """世界生成对话框。生成成功后由调用方（MainWindow）打开预览对话框。"""

    def __init__(self, world_sim_service, preset: WorldSimPreset, parent=None):
        super().__init__(parent)
        self.svc = world_sim_service
        self.preset = preset
        self.worker: WorldGenWorker | None = None
        self.generated_world = None  # 生成成功后存此处，调用方读取

        self.setWindowTitle("新建世界")
        self.resize(720, 680)
        self.setMinimumSize(640, 600)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 滚动表单
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        form = QVBoxLayout(content)
        form.setContentsMargins(24, 20, 24, 12)
        form.setSpacing(12)

        title = QLabel("创建一个新世界")
        # [深改 2026-10-01] 页名大字 + 金笔分隔（旧 titleLabel 无页头感）
        from src.ui.widgets.game_widgets import banner_rule
        title.setObjectName("bannerName")
        form.addWidget(title)
        form.addWidget(banner_rule())
        form.addSpacing(2)
        hint = QLabel("填写世界观与设定，LLM 将据此生成完整的世界（地点 / NPC / 势力 / 物品 / 任务）。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa5ce; font-size:12px;")
        form.addWidget(hint)

        # [开局 30 分钟改造包] 一键模板行：点模板 = 填表 + 立即生成，不想用照样走下面表单
        quick_w = QWidget()
        quick_g = QGridLayout(quick_w)
        quick_g.setContentsMargins(0, 0, 0, 0)
        quick_g.setSpacing(6)
        for i, tpl in enumerate(_QUICK_TEMPLATES):
            btn = QPushButton(tpl["name"])
            btn.setObjectName("optionBtn")   # [深改] 一键模板按钮统一款式
            # [修 2026-10-01 用户指示] 点模板只填表不生成（旧「点击后立即开始生成」
            # 会在用户没点生成时就把世界带跑）；生成一律由底部「生成」按钮触发
            btn.setToolTip(f"{tpl['desc']}\n\n{tpl['premise']}\n\n（点击填入下方表单，可修改后点「生成」）")
            btn.clicked.connect(lambda _=False, t=tpl: self._on_quick_template(t))
            quick_g.addWidget(btn, i // 3, i % 3)
        quick_hint = QLabel("快速填表：点模板把下方表单填好，确认或修改后点「生成」开始创建世界")
        quick_hint.setStyleSheet("color:#565f89; font-size:12px;")
        form.addWidget(quick_w)
        form.addWidget(quick_hint)

        fl = QFormLayout()
        fl.setSpacing(10)
        fl.setLabelAlignment(Qt.AlignRight)

        self.premise_edit = QTextEdit()
        self.premise_edit.setPlaceholderText("例：穿越到修仙世界，我是废柴外门弟子，意外获得上古传承…")
        self.premise_edit.setFixedHeight(70)
        self.premise_edit.setToolTip("世界观的灵魂一句话，LLM 据此生成整个世界骨架（地点/NPC/势力/任务）。必填。")
        fl.addRow("世界观一句话*:", self.premise_edit)

        self.extra_edit = QTextEdit()
        self.extra_edit.setPlaceholderText("可选：想要的元素 / 禁忌 / 特殊规则 / 关键设定…")
        self.extra_edit.setFixedHeight(60)
        self.extra_edit.setToolTip("可选：想要的元素 / 禁忌 / 特殊规则 / 关键角色设定。留空则 LLM 自由发挥。")
        fl.addRow("补充设定:", self.extra_edit)

        # [2026-08-23 用户卡绑定] 玩家卡（单选可空）：绑了则姓名/人设注入场景上下文与
        # NPC 记忆，NPC 按名字认识玩家（否则只知「那个男人/那个白衣年轻人」模糊指代）。
        self._users = []
        try:
            self._users = self.svc.storage.load_all_users() if self.svc else []
        except Exception:
            self._users = []
        self.user_grid = AvatarGridSelector(multi=False, columns=5, avatar_size=48)
        # [!] 单选约定：首项 (id="", "不绑定") 占位（守 avatar_grid_selector 口径），
        # 默认不选中任何项 = 不绑定；点了某卡后想改回点「不绑定」。
        self.user_grid.set_items([("", "不绑定", "")]
                                 + [(u.id, u.name or "未命名用户", u.avatar) for u in self._users])
        self.user_grid.setToolTip(
            "可选：绑定一张用户卡作为玩家化身。绑定后玩家姓名/人设会注入叙事与 NPC 记忆，"
            "NPC 将按名字认识你（不绑则 NPC 只能用「那位少侠」之类泛称）。")
        fl.addRow("玩家卡:", self.user_grid)
        if not self._users:
            user_hint = QLabel("（暂无用户卡，可在「会话 -> 用户管理」新建；不绑也能玩）")
            user_hint.setStyleSheet("color:#565f89; font-size:12px;")
            fl.addRow("", user_hint)

        # 题材标签多选（每行 4 个流式换行，标签多了不挤爆）
        tag_grid = QGridLayout()
        tag_grid.setContentsMargins(0, 0, 0, 0)
        tag_grid.setSpacing(2)
        self.tag_checks: dict[str, QCheckBox] = {}
        for i, tag in enumerate(preset.default_genre_tags):
            cb = QCheckBox(tag)
            self.tag_checks[tag] = cb
            tag_grid.addWidget(cb, i // 4, i % 4)
        tag_w = QWidget()
        tag_w.setLayout(tag_grid)
        tag_w.setToolTip("勾选的世界题材标签，LLM 据此决定画风/技能/势力名/物品调性。空=不限题材。")
        fl.addRow("题材标签:", tag_w)

        # 基调
        self.tone_combo = QComboBox()
        for t in _TONE_OPTIONS:
            self.tone_combo.addItem(t)
        self.tone_combo.setCurrentText("严肃")
        self.tone_combo.setToolTip("整体叙事基调：轻松=喜剧向；严肃=正剧；黑暗=残酷；荒诞=解构；热血=燃向；日常=慢生活。")
        fl.addRow("基调:", self.tone_combo)

        # 魔法/科技滑块
        magic_row = QHBoxLayout()
        self.magic_slider = QSlider(Qt.Horizontal)
        self.magic_slider.setRange(0, 100)
        self.magic_slider.setValue(60)
        self.magic_label = QLabel(f"60 · {_magic_word(60)}")
        self.magic_slider.valueChanged.connect(lambda v: self.magic_label.setText(f"{v} · {_magic_word(v)}"))
        magic_row.addWidget(self.magic_slider, 1)
        magic_row.addWidget(self.magic_label)
        mw = QWidget()
        mw.setLayout(magic_row)
        mw.setToolTip("魔法在这个世界有多普遍——决定 LLM 生成的世界规则、势力构成与技能风格：\n"
            "0=无魔世界（凡人搏杀/权谋）；30 上下=低魔（法术是传说，修行者稀少）；"
            "60 上下=中魔（门派/教会常见，超凡力量参与日常）；100=高魔（魔法像科技一样普及）。")
        fl.addRow("魔法等级:", mw)

        tech_row = QHBoxLayout()
        self.tech_slider = QSlider(Qt.Horizontal)
        self.tech_slider.setRange(0, 100)
        self.tech_slider.setValue(20)
        self.tech_label = QLabel(f"20 · {_tech_word(20)}")
        self.tech_slider.valueChanged.connect(lambda v: self.tech_label.setText(f"{v} · {_tech_word(v)}"))
        tech_row.addWidget(self.tech_slider, 1)
        tech_row.addWidget(self.tech_label)
        tw = QWidget()
        tw.setLayout(tech_row)
        tw.setToolTip("世界的科技树走到了哪——决定物品、场景与势力的形态：\n"
            "0-20=冷兵器农耕；40 上下=火器机械；60 上下=现代都市；85 上下=近未来义体智能；"
            "100=星际时代/全面赛博。")
        fl.addRow("科技等级:", tw)

        # 规模 — [P5b] 动态合并：内置三档 + 预设里的自定义档
        self.scale_combo = QComboBox()
        # 用辅助函数把内置 + 用户自定义按顺序合并
        scale_options = self._scale_options_for_preset()
        for sid, label in scale_options:
            self.scale_combo.addItem(label, sid)
        # 默认选 preset.default_world_scale
        default_idx = self.scale_combo.findData(preset.default_world_scale)
        self.scale_combo.setCurrentIndex(default_idx if default_idx >= 0 else min(1, len(scale_options) - 1))
        self.scale_combo.setToolTip("世界规模档：small/medium/large + 自定义档。决定地点数、NPC 数、势力数。")
        fl.addRow("规模:", self.scale_combo)

        # NSFW
        self.nsfw_chk = QCheckBox("启用 NSFW 内容（受全局破限开关门控）")
        self.nsfw_chk.setToolTip("允许 LLM 在叙事中生成成人内容。最终是否生成仍受全局 NSFW 破限开关门控（设置->通用里开启）。")
        fl.addRow("", self.nsfw_chk)

        # NPC 性别倾向（动态注入一句提示词；不限=当前行为，不注入）
        self.npc_gender_combo = QComboBox()
        self.npc_gender_combo.addItem("不限", "")
        self.npc_gender_combo.addItem("全女性", "female")
        self.npc_gender_combo.addItem("全男性", "male")
        self.npc_gender_combo.setToolTip(
            "生成世界时约束全部 NPC 的性别：不限=按世界观自然生成（默认）；"
            "全女性/全男性=向 LLM 注入一句提示词，所有 NPC 均为该性别（怪物/随从等临时单位不受约束）。")
        fl.addRow("NPC 性别:", self.npc_gender_combo)

        # 生图（[修 2026-09-05] 语义改为「保存后后台补图」——生成管线不再阻塞出图，
        # 勾选状态经 gen_images_enabled() 交给 main_window 保存后起后台任务）
        self.gen_images_chk = QCheckBox("保存后在后台生成世界图片（不阻塞进入世界）")
        self.gen_images_chk.setChecked(preset.image_enabled)
        self.gen_images_chk.setToolTip("勾选后：世界保存即可立即进入游玩，图片在后台批量生成、完成后自动刷新显示。取消=仅文字世界，以后可在详情页「补缺失图」。")
        # [P11c] image_skip_allowed=False 时锁定必生图（设置「创建时允许跳过生图」现在真实生效：
        # 关掉它 = 新建世界一律出图，生成界面不给跳过的机会）。
        if not preset.image_skip_allowed:
            self.gen_images_chk.setChecked(True)
            self.gen_images_chk.setEnabled(False)
            self.gen_images_chk.setToolTip(
                "全局设置已禁止创建时跳过生图（世界模拟设置 -> 生图 -> 「创建时允许跳过生图」），"
                "新建世界一律出图。")
        img_hint = QLabel(f"生图项可在「设置 -> 世界模拟设置」中勾选；当前总开关：{'开' if preset.image_enabled else '关'}")
        img_hint.setStyleSheet("color:#e0c48f; font-size:12px;")
        fl.addRow("", self.gen_images_chk)
        fl.addRow("", img_hint)

        form.addLayout(fl)
        form.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # 进度区（生成中显示）
        self.progress_frame = QFrame()
        pf_layout = QVBoxLayout(self.progress_frame)
        pf_layout.setContentsMargins(24, 8, 24, 8)
        self.stage_label = QLabel("")
        self.stage_label.setStyleSheet("color:#e0c48f;")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        pf_layout.addWidget(self.stage_label)
        pf_layout.addWidget(self.progress_bar)
        self.progress_frame.setVisible(False)
        outer.addWidget(self.progress_frame)

        # 底部按钮
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(24, 8, 24, 12)
        btn_row.addStretch()
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setToolTip("放弃本次新建世界，关闭对话框。")
        self.cancel_btn.clicked.connect(self.reject)
        self.gen_btn = QPushButton("生成世界")
        self.gen_btn.setObjectName("primaryBtn")
        self.gen_btn.setToolTip("开始生成世界（耗时主要取决于 LLM + 文生图，骨架生成一般 1-3 分钟）。")
        self.gen_btn.clicked.connect(self._on_generate)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.gen_btn)
        outer.addLayout(btn_row)

    def _apply_template(self, tpl: dict) -> None:
        """[开局 30 分钟改造包] 把模板写进表单字段（不触发生成，供测试/预填复用）。

        标签复选框按**显示标签名**勾选（default_genre_tags 之外的标签静默跳过——
        用户改过默认标签集时模板照常可用）；scale 找不到对应档时保持当前选择。
        """
        self.premise_edit.setPlainText(str(tpl.get("premise", "")))
        self.extra_edit.clear()
        for tag, cb in self.tag_checks.items():
            cb.setChecked(tag in (tpl.get("tags") or []))
        tone = str(tpl.get("tone", ""))
        if tone:
            self.tone_combo.setCurrentText(tone)
        self.magic_slider.setValue(int(tpl.get("magic", 60)))
        self.tech_slider.setValue(int(tpl.get("tech", 20)))
        idx = self.scale_combo.findData(tpl.get("scale"))
        if idx >= 0:
            self.scale_combo.setCurrentIndex(idx)

    def _on_quick_template(self, tpl: dict) -> None:
        """点模板卡片：只填表（[修 2026-10-01 用户指示] 旧版填表后立即生成——用户
        还没点生成就被带跑；现生成一律由底部「生成」按钮触发）。"""
        self._apply_template(tpl)

    def _scale_options_for_preset(self) -> list[tuple[str, str]]:
        """[P5b] 构造时下拉选项：内置三档 + 用户自定义档（按 preset.custom_world_scales 顺序）。"""
        from src.models.world_sim_preset import BUILTIN_SCALES
        opts: list[tuple[str, str]] = [(c["id"], str(c.get("label") or c["id"])) for c in BUILTIN_SCALES]
        for it in (self.preset.custom_world_scales or []):
            if isinstance(it, dict) and it.get("id"):
                opts.append((it["id"], str(it.get("label") or it["id"])))
        return opts

    def _build_form(self) -> dict:
        tags = [t for t, cb in self.tag_checks.items() if cb.isChecked()]
        return {
            "premise": self.premise_edit.toPlainText().strip(),
            "extra": self.extra_edit.toPlainText().strip(),
            "genre_tags": tags,
            "tone": self.tone_combo.currentText(),
            "magic_level": self.magic_slider.value(),
            "tech_level": self.tech_slider.value(),
            "scale": self.scale_combo.currentData(),
            "nsfw": self.nsfw_chk.isChecked(),
            "npc_gender": self.npc_gender_combo.currentData() or "",
        }

    def _on_generate(self):
        form = self._build_form()
        if not form["premise"]:
            QMessageBox.warning(self, "提示", "请填写世界观一句话。")
            return
        # [2026-08-23 用户卡绑定] 玩家卡不进生成管线（骨架/数值与之无关），
        # 暂存待生成成功后直接写到 World.user_id（_on_finished）。
        self._pending_user_id = self.user_grid.get_selected_id()
        # 进入生成态
        self.gen_btn.setEnabled(False)
        self.cancel_btn.setText("关闭")
        self.progress_frame.setVisible(True)
        self.progress_bar.setVisible(False)
        self.stage_label.setText("正在启动生成…")

        # [!] worker parent=None 自管（守 §15 同款教训）：parent=self 时 wait 超时后
        # dialog 销毁会级联析构仍在运行的 QThread（ComfyUI 生图阻塞不可取消）-> abort。
        self.worker = WorldGenWorker(self.svc, form, self.preset, None)
        self.worker.stage.connect(self._on_stage)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()

    def _on_stage(self, text: str):
        self.stage_label.setText(text)

    def _on_progress(self, done: int, total: int, label: str):
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(done)
        self.stage_label.setText(f"生成图片 {done}/{total}：{label}")

    def _on_finished(self, ok: bool, payload):
        w = self.worker
        self.worker = None
        # [!] 先取失败清单再 deleteLater（对象仍在，deleteLater 延迟到事件循环）
        failed = list(getattr(w, "failed_images", []) or []) if w is not None else []
        if w is not None:
            # finished 信号触发时线程已结束，deleteLater 安全（parent=None 须手动回收）
            w.deleteLater()
        self.gen_btn.setEnabled(True)
        self.cancel_btn.setText("取消")
        if ok:
            self.generated_world = payload
            # [2026-08-23 用户卡绑定] 把选中的玩家卡写到世界（随预览保存落盘）
            if payload is not None:
                payload.user_id = getattr(self, "_pending_user_id", "") or ""
            self.stage_label.setText("生成完成！")
            # [!] 部分图片生成失败时告知用户（旧版静默吞失败，用户只见进度走满却有个别 NPC
            # 头像为空，误以为「没存下来」——实为单张生成失败被跳过）。提示可在保存后补图。
            if failed:
                shown = failed[:20]
                msg = (f"世界已生成，但 {len(failed)} 张图片生成失败（已跳过，不影响世界保存）：\n  "
                       + "\n  ".join(shown)
                       + ("\n  …" if len(failed) > len(shown) else "")
                       + "\n\n保存世界后，可在对应 NPC / 地点详情里点「重新生成」补图。")
                QMessageBox.information(self, "部分图片未生成", msg)
            self.accept()
        else:
            self.progress_bar.setVisible(False)
            self.stage_label.setText("")
            QMessageBox.warning(self, "生成失败", str(payload))

    def closeEvent(self, event):
        # [!] 守 §15：先 disconnect 全信号（含 stage/progress）防 wait 超时后 dialog 销毁、
        # worker 后续 emit 投递到死槽致 0xC0000409 崩溃（世界生成是长任务，wait 易超时）。
        # worker parent=None：超时未结束时保留引用并把 deleteLater 挂到 finished，
        # 线程自然收尾后自回收（置 None 会丢引用致泄漏，强删会析构运行中线程）。
        if self.worker is not None:
            w = self.worker
            for sig in (w.stage, w.progress, w.finished_signal):
                try:
                    sig.disconnect()
                except (TypeError, RuntimeError):
                    pass
            try:
                if w.isRunning():
                    w.cancel()
                    w.wait(10000)  # LLM + 文生图可能长跑，给足时间
                self.worker = None
                if w.isFinished():
                    w.deleteLater()
                else:
                    w.finished.connect(w.deleteLater)
            except RuntimeError:
                pass
        super().closeEvent(event)

    def reject(self):
        # [!] 生成中点"关闭"/按 Esc 会走 reject 而非 closeEvent，必须先停 worker
        # 否则 QThread 仍在跑 LLM，dialog 销毁时引用丢失/线程泄漏。
        if self.worker is not None:
            w = self.worker
            for sig in (w.stage, w.progress, w.finished_signal):
                try:
                    sig.disconnect()
                except (TypeError, RuntimeError):
                    pass
            try:
                if w.isRunning():
                    w.cancel()
                    w.wait(10000)
                self.worker = None
                if w.isFinished():
                    w.deleteLater()
                else:
                    w.finished.connect(w.deleteLater)
            except RuntimeError:
                pass
        super().reject()

    def gen_images_enabled(self) -> bool:
        """[修 2026-09-05] 是否保存后后台补图（原「生成管线内出图」勾选的新语义）。"""
        return self.gen_images_chk.isChecked()

    def get_world(self):
        """生成成功后取 World 实体。"""
        return self.generated_world
