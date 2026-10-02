"""世界模拟地图对话框（[P7f] 图形化 + [P12d] 面板化重做 + [2026-08-23] 图片格）。

布局：左侧 QGraphicsView 网格地图（纯视觉标记）+ 右侧详情/操作面板。
- 地图格子显示地点场景图（loc.background 封面式裁剪入格），格内不堆文字——
  名字/详情全部走右侧面板（点击后展示，格内文字重复且挤）；
  状态用边框色表达：[蓝框]当前  [绿框+绿点]可前往  [灰蓝框]已探索  [暗框]已发现  [虚线+?]迷雾；
  无场景图的地点回退旧占位（当前地点显名字/其余中央圆点）。
- 点击格子 -> 右侧面板显示详情（区域/势力/危险/描述），操作也在面板：
  相邻地点「前往」按钮（selected = loc_id 后 accept 返回调用方）；
  迷雾块「向X探索」按钮（MapExpandWorker LLM 生成新地点），完成后刷新地图。
无限增殖沙盒：每次拓展生成 batch_size 个新地点，沿方向接入连通图。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPen, QBrush, QColor, QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsTextItem,
    QGraphicsEllipseItem, QGraphicsPixmapItem, QGraphicsLineItem,
)

from src.config import paths
from src.models import World, Location
from src.ui.dialogs.map_expand_worker import MapExpandWorker

_CELL_W = 150

# [P40] 势力版图着色调色板（按 faction 在世界中的序取色，确定性；格底色条 + 图例）
_FACTION_PALETTE = ["#7aa2f7", "#e0a860", "#9ece6a", "#bb9af7",
                    "#e05555", "#7dcfff", "#ff9e64", "#73daca"]
_CELL_H = 150  # [2026-08-27] 与宽度一致：地点背景是正方形图，格子同正方形防压扁

# [地图连线化 2026-08-24] 节点间距（格子不粘连）：cell 像素位置 = 网格坐标 x 步距。
# 步距 = 格子尺寸 + _GAP，留出空隙画连接线（节点图风格，非紧贴网格）。
_GAP = 26


def _px(gx: int) -> int:
    """网格列 -> 像素 x（含间距）。"""
    return gx * (_CELL_W + _GAP)


def _py(gy: int) -> int:
    """网格行 -> 像素 y（含间距）。"""
    return gy * (_CELL_H + _GAP)

_SEL_PEN = QPen(QColor("#e0af68"), 2)          # 选中高亮（金黄）

# 地图格场景图缓存（带 mtime 作废，仿 card_grid._PIX_CACHE 口径——文件覆写自动失效；
# 调用方只读。地图每次拓展/刷新全量重建格子，无缓存会每次重读磁盘重缩放全部地点图）。
_MAP_PIX_CACHE: dict[tuple, QPixmap] = {}
_MAP_PIX_CACHE_MAX = 256


def _cell_pixmap(bg_filename: str, w: int, h: int):
    """地点背景图封面式裁剪到格子尺寸（KeepAspectRatioByExpanding + 居中 copy）。
    失败/无图返回 None，调用方回退圆点占位。"""
    if not bg_filename:
        return None
    path = os.path.join(paths.world_images_dir(), bg_filename)
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size, w, h)
    if key in _MAP_PIX_CACHE:
        return _MAP_PIX_CACHE[key]
    pm = QPixmap(path)
    if pm.isNull():
        return None
    pm = pm.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    if pm.width() > w or pm.height() > h:
        x = max(0, (pm.width() - w) // 2)
        y = max(0, (pm.height() - h) // 2)
        pm = pm.copy(x, y, min(w, pm.width()), min(h, pm.height()))
    if len(_MAP_PIX_CACHE) >= _MAP_PIX_CACHE_MAX:
        _MAP_PIX_CACHE.clear()
    _MAP_PIX_CACHE[key] = pm
    return pm


class _MapCell(QGraphicsRectItem):
    """地图单元格（地点/迷雾），处理点击；保留基础边框供选中态恢复。

    [!] 位置用 setPos（item 原点=格子左上角），rect 固定 (0,0,w,h)：
    QGraphicsRectItem(x,y,w,h) 构造只把 (x,y) 写进 rect，item 原点仍是 (0,0)，
    子项（场景图/圆点/名字）坐标相对原点——不 setPos 的话所有格子的内容会
    全部画到同一位置叠在一起（历史「文字叠成一团」的真正根源）。
    """

    def __init__(self, x, y, w, h, kind, loc_id, direction, on_click):
        super().__init__(0, 0, w, h)
        self.setPos(x, y)
        self._kind = kind            # "loc" / "fog"
        self._loc_id = loc_id
        self._direction = direction  # (dx,dy) 或 None
        self._on_click = on_click
        self.base_pen = None         # 状态边框（选中态切换后恢复用）
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setCursor(Qt.PointingHandCursor)

    def set_base_pen(self, pen: QPen):
        self.base_pen = pen
        self.setPen(pen)

    def set_selected_look(self, on: bool):
        self.setPen(_SEL_PEN if on else (self.base_pen or self.pen()))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._on_click is not None:
            self._on_click(self._kind, self._loc_id, self._direction)
        super().mousePressEvent(event)


class WorldMapDialog(QDialog):
    """图形化世界地图：左视觉网格 + 右详情面板（前往/拓展）。"""

    def __init__(self, world: World, current_loc: Location, adjacent: list,
                 svc=None, preset=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("世界地图")
        self.resize(1080, 680)
        self.world = world
        self.current_loc = current_loc
        self.adjacent = adjacent
        self.svc = svc
        self.preset = preset
        self.selected: str | None = None
        self._expand_worker = None
        self._adj_ids = {l.id for l in adjacent}
        # 选中态（(kind, loc_id, direction) 元组）；cell 注册表用于高亮切换
        self._sel_key = None
        self._cells: dict[tuple, _MapCell] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("世界地图")
        title.setObjectName("bannerName")
        top.addWidget(title)
        top.addStretch()
        self.status_lbl = QLabel("点击地图地点查看详情；绿色格可前往，「?」迷雾格可探索。")
        self.status_lbl.setStyleSheet("color:#e0c48f; font-size:12px;")
        top.addWidget(self.status_lbl)
        outer.addLayout(top)

        # ---- 主体：左地图 + 右详情面板 ----
        body = QHBoxLayout()
        body.setSpacing(10)

        self.view = QGraphicsView()
        # [深改 2026-10-01] 画布暖深底（旧冷蓝 #11131c 与暗金主题割裂）
        self.view.setBackgroundBrush(QColor("#15130e"))
        self.view.setMinimumSize(640, 480)
        body.addWidget(self.view, 1)

        body.addWidget(self._build_panel())

        outer.addLayout(body, 1)

        btn_row = QHBoxLayout()
        legend = QLabel("图例：[蓝框]当前  [绿框+绿点]可前往  [灰蓝框]已探索  [暗框]已发现  "
                        "[虚线?]迷雾  [底色条]控制势力（无场景图的格子回退圆点占位）")
        legend.setStyleSheet("color:#9aa5ce; font-size:11px;")
        btn_row.addWidget(legend)
        # [P40] 势力版图图例（按当前世界势力动态生成色块行）
        self.faction_legend = QLabel("")
        self.faction_legend.setStyleSheet("color:#9aa5ce; font-size:11px;")
        btn_row.addWidget(self.faction_legend)
        btn_row.addStretch()
        cancel_btn = QPushButton("关闭")
        cancel_btn.setObjectName("primaryBtn")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        outer.addLayout(btn_row)

        self._rebuild_map()

    # ============ 右侧详情面板 ============
    def _build_panel(self) -> QFrame:
        """[深改 2026-10-01] 暖金卡（旧冷蓝底），区头金字、大名金字。"""
        panel = QFrame()
        panel.setFixedWidth(286)
        panel.setStyleSheet(
            "QFrame{background:#211d15; border:1px solid #57482a; border-radius:10px;}"
        )
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(14, 12, 14, 12)
        pl.setSpacing(8)

        head = QLabel("地点详情")
        head.setStyleSheet("color:#e0c48f; font-weight:bold; font-size:13px;")
        pl.addWidget(head)

        self.panel_title = QLabel("（点击地图地点）")
        self.panel_title.setWordWrap(True)
        self.panel_title.setStyleSheet("color:#e0c48f; font-size:15px; font-weight:bold;")
        pl.addWidget(self.panel_title)

        self.panel_tag = QLabel("")
        self.panel_tag.setStyleSheet("color:#c9a86a; font-size:12px;")
        pl.addWidget(self.panel_tag)

        self.panel_info = QLabel("")
        self.panel_info.setWordWrap(True)
        self.panel_info.setStyleSheet("color:#9aa5ce; font-size:12px;")
        self.panel_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pl.addWidget(self.panel_info)

        self.panel_desc = QLabel("")
        self.panel_desc.setWordWrap(True)
        self.panel_desc.setStyleSheet("color:#c0caf5; font-size:12px;")
        self.panel_desc.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pl.addWidget(self.panel_desc, 1)

        self.panel_btn = QPushButton("前往")
        self.panel_btn.setObjectName("primaryBtn")
        self.panel_btn.hide()
        self.panel_btn.clicked.connect(self._on_panel_action)
        pl.addWidget(self.panel_btn)

        return panel

    # ============ 地图渲染 ============
    def _rebuild_map(self):
        """重建地图场景：地点节点 + 迷雾块。"""
        scene = QGraphicsScene(self)
        self._cells.clear()
        self._sel_key = None
        # 拓展后相邻集合可能变化（新地点接入），重算
        if self.svc is not None and self.current_loc is not None:
            self._adj_ids = {l.id for l in self.svc._adjacent_locations(self.world, self.current_loc)}
        expand_on = (self.preset is not None and getattr(self.preset, "map_expansion_enabled", True)
                     and self.svc is not None)
        mist = self.svc.mist_name(self.world) if self.svc is not None else "未知"
        # [地图连线化] 先画连接线（低层，后加的 cell 盖在线上），再画节点格。
        self._draw_connections(scene)
        for loc in self.world.locations:
            if not loc.discovered:
                continue
            self._add_loc_cell(scene, loc, expand_on, mist)
        self.view.setScene(scene)
        # 居中到当前地点
        if self.current_loc is not None:
            cx = _px(self.current_loc.x) + _CELL_W / 2
            cy = _py(self.current_loc.y) + _CELL_H / 2
            self.view.centerOn(cx, cy)
        self._refresh_faction_legend()
        self._update_panel()

    def _draw_connections(self, scene):
        """[地图连线化] 节点间连接线：遍历 Location.connections 去重(A<B)画直线。

        线画在 cell 之下（先添加），两端取节点中心。仅已发现地点间画线。"""
        by_id = {loc.id: loc for loc in self.world.locations if loc.discovered}
        pen = QPen(QColor("#5a4a2e"), 2)   # [深改] 连线暖金调（旧冷蓝）
        drawn = set()
        for loc in by_id.values():
            for cid in (getattr(loc, "connections", None) or []):
                if cid not in by_id:
                    continue
                key = (loc.id, cid) if loc.id < cid else (cid, loc.id)
                if key in drawn:
                    continue
                drawn.add(key)
                a, b = by_id[key[0]], by_id[key[1]]
                line = QGraphicsLineItem(
                    _px(a.x) + _CELL_W / 2, _py(a.y) + _CELL_H / 2,
                    _px(b.x) + _CELL_W / 2, _py(b.y) + _CELL_H / 2)
                line.setPen(pen)
                line.setZValue(-1)
                scene.addItem(line)

    def _faction_color(self, fac_id: str) -> "QColor":
        """势力确定性取色（按 world.factions 顺序落调色板）。"""
        ids = [f.id for f in (getattr(self.world, "factions", None) or [])]
        try:
            idx = ids.index(fac_id) if fac_id in ids else int(fac_id) % len(_FACTION_PALETTE)
        except (TypeError, ValueError):
            idx = 0
        return QColor(_FACTION_PALETTE[idx % len(_FACTION_PALETTE)])

    def _refresh_faction_legend(self):
        """[P40] 底栏势力图例：色块 + 势力名（有归属地的势力才显示）。"""
        owned = []
        for loc in (getattr(self.world, "locations", None) or []):
            fid = str(getattr(loc, "owner_faction_id", "") or ""
                      or getattr(loc, "faction_id", "") or "")
            if fid and fid not in [o[0] for o in owned]:
                owned.append((fid, loc))
        parts = []
        for fid, _loc in owned[:6]:
            name = next((f.name for f in (self.world.factions or []) if f.id == fid), "未知势力")
            c = self._faction_color(fid).name()
            parts.append(f"<span style='color:{c};'>■</span>{name}")
        self.faction_legend.setText("　".join(parts) if parts else "")

    def _add_loc_cell(self, scene, loc: Location, expand_on: bool, mist: str):
        x, y = _px(loc.x), _py(loc.y)
        is_cur = self.current_loc is not None and loc.id == self.current_loc.id
        is_adj = loc.id in self._adj_ids
        cell = _MapCell(x, y, _CELL_W, _CELL_H, "loc", loc.id, None, self._on_cell_click)
        # [深改 2026-10-01] 格子暖金调（当前=金框暖底；可前往绿/已探索灰蓝调性保留语义）
        if is_cur:
            cell.setBrush(QBrush(QColor("#2a2418")))
            cell.set_base_pen(QPen(QColor("#c9a86a"), 3))
        elif is_adj:
            cell.setBrush(QBrush(QColor("#1c2416")))
            cell.set_base_pen(QPen(QColor("#9ece6a"), 2))
        elif loc.explored:
            cell.setBrush(QBrush(QColor("#1d1a14")))
            cell.set_base_pen(QPen(QColor("#4a4232"), 1))
        else:
            cell.setBrush(QBrush(QColor("#14120c")))
            cell.set_base_pen(QPen(QColor("#322c20"), 1))
        scene.addItem(cell)
        self._cells[("loc", loc.id, None)] = cell
        cell.setToolTip(loc.name)

        # [2026-08-23] 图片格：优先显示地点场景图（封面式裁剪），名字/详情全部走右侧面板。
        # 无场景图（生图关闭/失败）回退旧占位：当前地点显名字，其余中央圆点表达状态。
        pix = _cell_pixmap(getattr(loc, "background", "") or "", _CELL_W - 4, _CELL_H - 4)
        if pix is not None:
            pic = QGraphicsPixmapItem(pix, cell)
            pic.setPos(2, 2)   # 内缩 2px 留边框可见（[!] 子项坐标相对 item 原点）
            pic.setAcceptedMouseButtons(Qt.NoButton)
            if is_adj:
                # 可前往格在图上叠加绿点强化 affordance（边框已是绿色，点是第二信号）
                dot = QGraphicsEllipseItem(
                    _CELL_W / 2 - 7, _CELL_H / 2 - 7, 14, 14, cell)
                dot.setBrush(QBrush(QColor("#9ece6a")))
                dot.setPen(QPen(QColor("#11131c"), 1))
                dot.setAcceptedMouseButtons(Qt.NoButton)
        elif is_cur:
            txt = QGraphicsTextItem()
            txt.setHtml(
                f"<div style='text-align:center;'>"
                f"<span style='font-size:10pt; font-weight:bold; color:#e6d5a8;'>{loc.name}</span>"
                f"</div>"
            )
            txt.setParentItem(cell)
            txt.setTextWidth(_CELL_W - 10)
            txt.setPos(5, (_CELL_H - int(txt.boundingRect().height())) // 2)
            txt.setAcceptedMouseButtons(Qt.NoButton)
        else:
            dot_c = QColor("#9ece6a") if is_adj else (QColor("#4a4232") if loc.explored else QColor("#322c20"))
            dot_r = 7 if is_adj else 5
            dot = QGraphicsEllipseItem(
                _CELL_W / 2 - dot_r, _CELL_H / 2 - dot_r, dot_r * 2, dot_r * 2, cell)
            dot.setBrush(QBrush(dot_c))
            dot.setPen(QPen(QColor("#15130e"), 1))
            dot.setAcceptedMouseButtons(Qt.NoButton)

        # [P40] 势力版图底色条（owner_faction_id 动态归属，势力战胜负实时变色）
        # [!] 须在图片/圆点之后添加（同 z 值子项按添加序绘制，后加的盖在上面），
        # 否则场景图 pixmap 会把底边色条整个遮住。
        fac_id = str(getattr(loc, "owner_faction_id", "") or ""
                     or getattr(loc, "faction_id", "") or "")
        if fac_id:
            strip = QGraphicsRectItem(2, _CELL_H - 7, _CELL_W - 4, 5, cell)
            strip.setBrush(QBrush(self._faction_color(fac_id)))
            strip.setPen(QPen(QColor("#11131c"), 1))
            strip.setAcceptedMouseButtons(Qt.NoButton)

        # 迷雾块（仅已探索地点的 4 正方向空格）
        if expand_on and loc.explored:
            for dname, dx, dy in self.svc.fog_directions(self.world, loc):
                self._add_fog_cell(scene, loc, dx, dy, dname, mist)

    def _add_fog_cell(self, scene, loc: Location, dx: int, dy: int, dname: str, mist: str):
        fx, fy = _px(loc.x + dx), _py(loc.y + dy)
        cell = _MapCell(fx, fy, _CELL_W, _CELL_H, "fog", loc.id, (dx, dy), self._on_cell_click)
        cell.setBrush(QBrush(QColor(46, 40, 26, 150)))
        cell.set_base_pen(QPen(QColor("#57482a"), 1, Qt.DashLine))
        scene.addItem(cell)
        self._cells[("fog", loc.id, (dx, dy))] = cell
        dir_zh = {"north": "北", "south": "南", "east": "东", "west": "西"}.get(dname, "?")
        cell.setToolTip(f"向{dir_zh}探索")
        txt = QGraphicsTextItem()
        txt.setHtml(
            "<div style='text-align:center;'>"
            "<span style='font-size:12pt; font-weight:bold; color:#e0af68;'>?</span>"
            "</div>"
        )
        txt.setParentItem(cell)
        txt.setTextWidth(_CELL_W - 10)
        txt.setPos(5, (_CELL_H - int(txt.boundingRect().height())) // 2)
        txt.setAcceptedMouseButtons(Qt.NoButton)

    # ============ 选中与面板 ============
    def _on_cell_click(self, kind: str, loc_id: str, direction):
        if self._expand_worker is not None:
            return  # 拓展中禁用交互
        key = (kind, loc_id, tuple(direction) if direction else None)
        # 切换选中高亮
        if self._sel_key is not None and self._sel_key in self._cells:
            self._cells[self._sel_key].set_selected_look(False)
        self._sel_key = key if key != self._sel_key else None
        if self._sel_key is not None and self._sel_key in self._cells:
            self._cells[self._sel_key].set_selected_look(True)
        self._update_panel()

    def _update_panel(self):
        self.panel_btn.hide()
        key = self._sel_key
        if key is None:
            self.panel_title.setText("（点击地图地点）")
            self.panel_tag.setText("")
            self.panel_info.setText("")
            self.panel_desc.setText("")
            return
        kind, loc_id, direction = key
        if kind == "loc":
            loc = next((l for l in self.world.locations if l.id == loc_id), None)
            if loc is None:
                return
            is_cur = self.current_loc is not None and loc.id == self.current_loc.id
            is_adj = loc.id in self._adj_ids
            fac = next((f.name for f in self.world.factions if f.id == loc.faction_id), "无")
            self.panel_title.setText(loc.name)
            if is_cur:
                self.panel_tag.setText("当前所在")
                self.panel_tag.setStyleSheet("color:#c9a86a; font-size:12px; font-weight:bold;")
            elif is_adj:
                self.panel_tag.setText("可前往（与当前地点相邻）")
                self.panel_tag.setStyleSheet("color:#9ece6a; font-size:12px; font-weight:bold;")
            elif loc.explored:
                self.panel_tag.setText("已探索")
                self.panel_tag.setStyleSheet("color:#9aa5ce; font-size:12px;")
            else:
                self.panel_tag.setText("已发现（未探索）")
                self.panel_tag.setStyleSheet("color:#565f89; font-size:12px;")
            # [P25a] 秘境入口标记：此地有秘境（详情面板加一行）
            dg_line = ""
            try:
                from src.services import dungeon_engine as _dge
                _dg = _dge.dungeon_at(self.world, loc)
                if _dg is not None:
                    dg_line = ("\n秘境：✦ " + _dg.name +
                               ("（封印中）" if _dg.status != "open" else "（可探索）"))
            except Exception:
                pass
            # [P34e] 聚落规模 + 预留场所（city 型列 venue 名）
            ss_line = ""
            ss = getattr(loc, "settlement_size", "") or ""
            if loc.kind == "settlement":
                ss_zh = {"city": "城市", "town": "城镇", "village": "村庄"}.get(ss, "")
                if ss_zh:
                    ss_line = f"\n聚落规模：{ss_zh}"
                # city 型列预留场所名（venue place 的 type 在核心 3 类时显示）
                if ss == "city" and loc.places:
                    venue_names = [p.name for p in loc.places
                                   if p.type in ("auction", "weapon", "alchemy")]
                    if venue_names:
                        ss_line += f"\n场所：{'、'.join(venue_names[:5])}"
            self.panel_info.setText(
                f"区域：{loc.region or '未知'}\n势力：{fac}\n危险度：{loc.danger}\n"
                f"连通地点：{len(loc.connections)} 处\n在场 NPC：{len(loc.npc_ids)} 人"
                + dg_line + ss_line
            )
            self.panel_desc.setText(loc.desc or "")
            if is_adj:
                self.panel_btn.setText(f"前往 {loc.name}")
                self.panel_btn.show()
            elif not is_cur:
                self.status_lbl.setText(f"「{loc.name}」不与当前地点相邻，须先走到相邻地点。")
        elif kind == "fog":
            loc = next((l for l in self.world.locations if l.id == loc_id), None)
            dir_zh = {"north": "北", "south": "南", "east": "东", "west": "西"}.get(
                next((k for k, v in {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}.items()
                      if (v[0], v[1]) == tuple(direction)), ""), "?")
            mist = self.svc.mist_name(self.world) if self.svc is not None else "未知"
            self.panel_title.setText(f"{mist}（未探明）")
            self.panel_tag.setText("可探索区域")
            self.panel_tag.setStyleSheet("color:#e0af68; font-size:12px; font-weight:bold;")
            base = loc.name if loc else "此地"
            self.panel_info.setText(f"从「{base}」向{dir_zh}拓展\n（LLM 生成新地点，接入连通图）")
            self.panel_desc.setText("")
            if self.svc is not None and self.preset is not None:
                self.panel_btn.setText(f"向{dir_zh}探索")
                self.panel_btn.show()

    def _on_panel_action(self):
        if self._sel_key is None:
            return
        kind, loc_id, direction = self._sel_key
        if kind == "loc":
            cur_id = self.current_loc.id if self.current_loc else ""
            if loc_id != cur_id and loc_id in self._adj_ids:
                self.selected = loc_id
                self.accept()
        elif kind == "fog":
            if self.svc and self.preset:
                self._start_expand(loc_id, direction)

    # ============ 迷雾拓展（LLM）============
    def _start_expand(self, from_loc_id: str, direction):
        if self._expand_worker is not None:
            return
        self.status_lbl.setText("向迷雾探索中…（LLM 生成新地点，请稍候）")
        self._expand_worker = MapExpandWorker(
            self.svc, self.world, from_loc_id, direction, self.preset, parent=None)
        self._expand_worker.finished_signal.connect(self._on_expand_done)
        self._expand_worker.start()

    def _on_expand_done(self, ok: bool, msg: str, new_ids: list):
        w = self._expand_worker
        self._expand_worker = None
        if w is not None:
            try:
                w.disconnect()
            except (RuntimeError, TypeError):
                pass
            w.wait(2000)
            self._safe_delete_worker(w)
        if ok and new_ids:
            self.status_lbl.setText(f"{msg}（地图已刷新，新地点若与当前相邻即可前往）")
            self._rebuild_map()
        else:
            self.status_lbl.setText(msg or "拓展失败")

    @staticmethod
    def _safe_delete_worker(w):
        """[!] 守 §15：wait 超时后线程可能仍在跑（MapExpand 经 comfyui.generate
        阻塞不可取消出图），此时 deleteLater 会在 QThread 析构时 abort
        （Destroyed while running -> 0xC0000409）。未结束则把 deleteLater 挂到
        finished 信号，让线程自然收尾后再销毁。"""
        if w is None:
            return
        if w.isFinished():
            w.deleteLater()
        else:
            try:
                w.finished.connect(w.deleteLater)
            except (RuntimeError, TypeError):
                w.deleteLater()

    def _cleanup_worker(self):
        if self._expand_worker is not None:
            w = self._expand_worker
            self._expand_worker = None
            try:
                w.finished_signal.disconnect(self._on_expand_done)
            except (RuntimeError, TypeError):
                pass
            w.cancel()
            w.wait(5000)
            self._safe_delete_worker(w)

    def closeEvent(self, event):
        self._cleanup_worker()
        super().closeEvent(event)

    def reject(self):
        self._cleanup_worker()
        super().reject()

    def get_selected(self) -> str | None:
        return self.selected
