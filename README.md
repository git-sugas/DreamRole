# DreamRole

> 一款本地运行的 AI 角色扮演 / 语擦桌面客户端，定位类似简化版的 SillyTavern。接入你自己的大模型 API（OpenAI 兼容），即可创建角色、聊天、生成图片，并让 AI 跨会话长期记住你的剧情。

## ✨ 核心特性

- **角色卡系统**：自定义名字、头像、人设、外貌、对话示例；可一键自动生成角色头像。
- **单聊 & 群聊**：单聊与一个角色私聊；群聊支持「自动导演选角」与「点头像手动指定下一个发言者」两种模式。
- **文生图**：AI 回复中的 `[img:中文描述]` 会自动召回 Danbooru tag、经 LLM 加工成英文 tag，再走 ComfyUI 出图插入聊天；默认引导双视角出图（第一人称近景 + 第三人称全景）。
- **长期记忆**：跨会话沉淀角色自己经历过的事，支持 summary 增量总结 与 embedding_hybrid 三路召回两种模式。
- **上文总结 & 折叠楼层**：自动总结较早的历史并折叠原文，节省上下文 token。
- **世界书**：关键词触发的资料条目，常驻或按触发条件注入上下文。
- **气泡分色渲染**：台词 / 旁白 / 心声 / 符号可用正则自定义分色着色，仅影响显示，不改动发给 API 的原文。
- **统计计费**：按 API 设置输入 / 输出 / 缓存三档费率，状态栏常显命中率与累计费用，支持按新费率重算历史。
- **会话存档**：导出 / 导入整个会话（含角色与世界书，同名合并去重）。

## 🚀 快速上手

### 1. 环境要求

- Python 3.10+
- Windows（以 Windows 为主平台，其它桌面平台理论可用但未充分测试）

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

> 文生图功能依赖本地运行的 [ComfyUI](https://github.com/comfyanonymous/ComfyUI) 服务，以及一个 OpenAI 兼容的 Embedding 接口用于 Danbooru tag 召回。

### 3. 运行

```bash
python main.py
```

### 4. 配置流程

1. **配置 API**：菜单「API 与预设」-> 填入兼容 OpenAI 的 `base_url` / `api_key` / 模型名；
   - 可点「拉取模型」从 `/v1/models` 自动加载；
   - 可点「测试连接」先验证。
2. **创建角色卡**：菜单「角色卡管理」-> 新建，填人设 / 外貌，可选自动生成头像。
3. **新建会话**：选单角色（单聊）或多角色（群聊），可选是否要开场白。
4. **开聊**：底部输入框 `Enter` 发送（中文输入法组合态 `Enter` 仅确认候选词，不发送），AI 流式回复。

## 🧱 技术栈

| 维度    | 选型                                                                  |
| ------- | --------------------------------------------------------------------- |
| GUI     | PySide6（QSS 主题，Tokyo Night 风格）                                 |
| HTTP    | httpx（同步，跑在 QThread，不卡 UI）                                 |
| Token   | tiktoken                                                              |
| 存储    | SQLite（消息 / 统计 / FTS5）+ JSON（配置）+ ChromaDB（向量记忆 / Danbooru） |
| 文生图  | ComfyUI + Danbooru tag 两段式 RAG（中文 -> 英文 tag）                 |
| 打包    | PyInstaller（单 exe，产物名 `DreamRole`）                            |

## 📁 项目结构

```
DreamRole/
+--- main.py / app.py            # 入口 + QApplication / 主题 / 服务初始化
+--- assets/                      # 应用图标与预览图
+--- tools/make_icon.py           # 图标生成脚本
+--- data/                        # 运行时用户数据（不入库）
+--- src/
    +--- config/paths.py          # 数据目录管理
    +--- models/                  # dataclass，均带 to_dict/from_dict
    +--- services/                # LLM / 上下文 / 编排 / 记忆 / 总结 / Danbooru / ComfyUI / 存储
    +--- ui/                      # 主窗口 / 聊天视图 / 角色面板 + dialogs + widgets + theme.qss
    +--- utils/                   # tokenizer / 渲染 / 调试日志
```

## ⚙️ 可选开关

- **神秘小开关**：菜单「设置」**默认关闭**，内容由用户自负责任，本应用不审查内容。
- **调试日志**：把 `src/utils/debug.py` 里的 `DEBUG = True` 改掉后重启，即可看到 `[API-DEBUG]` 详细入参日志（API / ComfyUI / Embedding / 记忆 / 总结 / Danbooru 全链路）。

## 📝 已知限制

- 会话存档导入暂未迁移 `user_id`，跨机迁移可能丢用户实体。
- ComfyUI 采样器 / 调度器列表目前硬编码，未来可从 `/object_info` 拉取。
- 续写暂不能「先改已停止的文本再续」，只能以原文作 prefill。
- 记忆页 embedding 模式条目上千时可能卡顿。

## 📜 免责声明

本项目开发目的仅为方便用户使用 LLM 进行正常的互动聊天。用户自行使用其于非法 / 色情 / 暴力等行为，由用户自负全部责任，与开发者无关。不同模型的破限效果不同，部分模型可能拒绝或效果有限。

## 📬 联系方式

作者 QQ：`1965699077`

## 📄 许可

本项目仅供学习交流使用。商用或其他用途请联系作者。
