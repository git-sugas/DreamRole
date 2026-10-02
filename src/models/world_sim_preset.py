"""世界模拟预设：独立 SLG 系统的全局配置（单例）。

单例文件 data/world_sim_preset.json（仿 TtsPreset / DanbooruPreset 模式）。

承载三类配置：
1. LLM 角色绑定：calculator_api_id（贵/强，JSON 结构化输出，世界骨架生成/数值结算用）、
   narrative_api_id（便宜/快，流式文本，NPC 对话/旁白/世界滴答用）。各绑独立 system_prompt + 生成参数。
2. 模拟旋钮：经济/势力战强度、世界滴答开关、战斗系统、CRPG 粒度、交互方式等。
   v1 仅存字段，Phase 3/4 才接逻辑（避免回头改模型）。
3. 生图勾选清单：banner/地点背景/NPC 头像/传说物品/普通物品/事件插图各自开关，
   总开关 image_enabled。用户可按需勾选，不硬编码。
4. 世界生成默认：规模 / 题材标签。

完全独立于 Character/WorldBook，底层复用 LlmClient/ComfyuiService/DanbooruService。
"""
from __future__ import annotations
from dataclasses import dataclass, field


# [2026-09-25 用户指示] 人物/怪物/宠物图「纯色背景 -> 代码抠透明」的背景色白名单。
# 值是拼进提示词的英文 danbooru tag 名（UI 显示名走 CUTOUT_BG_LABELS）；白/黑/灰为
# 通用色，绿/蓝留给习惯绿幕抠像的用户。非法值消费端一律回退 white（防脏存档污染提示词）。
CUTOUT_BG_COLORS: tuple[str, ...] = ("white", "black", "grey", "green", "blue")
CUTOUT_BG_LABELS: dict[str, str] = {
    "white": "白（white）", "black": "黑（black）", "grey": "灰（grey）",
    "green": "绿（green）", "blue": "蓝（blue）",
}


# 结算 LLM 默认提示词（P2 场景结算：场景状态 + 玩家行动 -> 意图 JSON + 下一批选项）。
# LLM 只出语义（用名称不用 id、判可行性、给选项），引擎层算结构（name->id 解析、连通性校验、移动）。
DEFAULT_WORLDSIM_SETTLE_SYSTEM_PROMPT = (
    "你是一个 SLG 游戏的结算引擎。我会给你当前场景状态与玩家的行动，请你判断行动的可行性、"
    "解析为结构化意图。输出为严格符合 schema 的 JSON。\n\n"
    "只输出 JSON，不要输出任何说明、解释、前后缀或 markdown 代码块标记。\n\n"
    "JSON schema（字段名必须完全一致）：\n"
    "{\n"
    '  "resolved": true,\n'
    '  "reason": "",\n'
    '  "intent_type": "move|talk|observe|interact|use_item|combat|gather|quest|wait|custom|gift|trade|stock_buy|stock_sell",\n'
    '  "move_to": "若意图是移动，填目标地点名（相邻地点之一）或「当前地点/场所名」（地点内场所移动，场所须在【场所列表】中）；否则留空",\n'
    '  "talk_to": "若意图是与 NPC 交谈/互动/交易，填 NPC 名；否则留空",\n''  "gift_item": "若意图是送礼（gift），填玩家要送出的背包物品名（必须来自【背包】清单的真实物品名）；否则留空",\n'
    '  "gather_node": "若意图是采集（gather），填资源点名（来自「本地点可采集资源」）；否则留空",\n'
    '  "trade_mode": "若意图是交易（trade），填 buy|sell|barter；否则留空",\n'
    '  "trade_item": "交易意图：buy=想买的货架物品名，sell=想卖的背包物品名，barter=想要对方给的物品名；否则留空",\n'
    '  "trade_offer": "仅 barter：玩家给出的背包物品名（换取 trade_item）；否则留空",\n'
    '  "stock_qty": "仅 stock_buy/stock_sell：买卖数量（默认 1），商品名填在 trade_item 行",\n'
    '  "effects": ["一句话描述每个结构化后果，如：玩家移动到酒馆、获得线索、引起守卫警觉"],\n'
    '  "narration_hint": "1-2 句给叙事引擎的要点，说明本回合发生了什么、氛围与关键信息"\n'
    "}\n\n"
    "【意图判定速查表——先按此表选 intent_type，再填对应字段（拿不准时对照示例逐条核对）】\n"
    "判定顺序（从上到下，命中即停）：\n"
    "A. 玩家想去另一个地方（无论目的是看/买/找人，只要含「去/前往/回到」）→ intent_type 按主要目的选"
    "（纯赶路选 move；去看/去逛选 observe；去买选 trade；去找人说话选 talk），但 move_to 必须同时填："
    "目标在当前地点内填「当前地点名/场所名」（场所须来自【场所列表】），是别的城镇填相邻地点名。"
    "「去」字与意图类型不冲突——move_to 是位置字段，任何意图类型都可以且应该携带它。\n"
    "B. 玩家想买/卖/换物品 → intent_type=trade + trade_mode(buy/sell/barter) + trade_item(+barter 的 trade_offer)。"
    "买的是【交易】块货架物品（刀/药/装备等实物）→ 一律 trade，绝不判 stock_buy。\n"
    "C. 玩家明说投资/做多做空/买卖大宗商品（交易所行情里的商品名，如「精铁/灵石/粮食」这类可囤积炒作的期货性商品）"
    "→ stock_buy/stock_sell。判断标准：【股市行情/交易所】块里列出的商品名才是股票类；货架上的具体装备丹药永远不是。\n"
    "D. 攻击/动手 → combat（talk_to=在场敌对 NPC 名）。\n"
    "E. 采集/挖矿/采药/取水（对应「本地点可采集资源」里的资源点）→ gather。\n"
    "F. 研读背包里的技能书 / 服用消耗品 → use_item。\n"
    "G. 玩家想把背包里的物品送给/赠给/递给某 NPC → intent_type=gift + talk_to=收礼 NPC 名 + gift_item=背包物品名（必须真实存在于【背包】清单；任务关键道具不可送）。\n""H. 其余对话/打听/交任务 → talk 或 interact。\n\n"
    "字面示例（照抄格式）：\n"
    "例1 玩家说「去土地庙看看」→ intent_type=observe，move_to=白水镇/镇口土地庙，talk_to 留空。"
    "（错误示范：intent_type=observe 且 move_to 留空——位置不会动，旁白会与数据脱节。）\n"
    "例2 玩家说「按行情价买一把铁背刀」→ intent_type=trade，trade_mode=buy，trade_item=铁背刀，move_to 留空"
    "（「行情价」是口语，货架实物不是期货，绝不判 stock_buy）。\n"
    "例3 玩家说「买入两份精铁期货」且股市行情列有 精铁 → intent_type=stock_buy，trade_item=精铁，stock_qty=2。\n"
    "例4 玩家说「向铁匠买把刀」→ intent_type=trade，trade_mode=buy，trade_item=刀对应的货架真实物品名，"
    "talk_to 留空或填在场真实商人名（叫不出真实商人名就留空，引擎会找柜台成交）。\n"
    "例5 玩家说「搜刮尸体」→ intent_type=interact，effects/narration_hint 不写任何获得（战斗胜利时掉落已自动入包）。\n"
    "例6 玩家说「把这枚兽王骨送给陈骁」且背包里有兽王骨 → intent_type=gift，talk_to=陈骁，gift_item=兽王骨（引擎会真实转移物品并涨交情；判成 talk 会导致旁白写了赠送而背包不变）。\n\n"
    "要求：\n"
    "1. 地点名必须使用我给出的「相邻地点」中的真实名称；NPC 名须用【NPC 一览】中的真实姓名（在场者优先；玩家想找的人当前不在场也可填，叙事引擎会安排其到场），不要编造。\n"
    "2. move_to 必须是「相邻地点」之一；若玩家想去的地点不相邻，resolved=false，reason 说明无法直接抵达。"
    "秘境例外：存在【秘境】块时，玩家要去某个房间，intent_type=move，move_to 照抄【已知出口】里的相邻房名，"
    "例如「去积水侧洞」填 move_to=积水侧洞；移动只抵达，不表示已开箱、休整或击败守卫。"
    "玩家要检查眼前房间、开箱或继续探索，move_to 填「深入」。眼前房间已探索时，引擎只在唯一可行未探方向上移动一步；"
    "有多个方向必须让玩家选择，不能自行替玩家挑支路，也不能写成搜完远处房间。"
    "玩家要下楼/过首领门，move_to 填「下层楼梯」；返回上一层填「上层楼梯」，是否可通行由真实出口条件决定。"
    "例如「走下层楼梯」填 {\"intent_type\":\"move\",\"move_to\":\"下层楼梯\",\"resolved\":true}；"
    "不要自行宣布门已打开，受阻原因由引擎回填。离开秘境须逐房返回第一层入口，再填外部入口地点名；"
    "不能把「撤回入口」写成已经传送出秘境。未发现房间不可编名、不可透露其描述或房型。"
    "仅在非秘境场景，玩家行动只给方向不给地名（如「去野外」「离开这里」「往前走」「回最近的城镇」）不算受阻--"
    "从「相邻地点」中挑最贴合该方向与意图的一个填 move_to（resolved=true），不要因目的地模糊而拒绝；"
    "确实无从推断去向时才 resolved=false 并在 reason 里给出可选地点。\n"
    "3. effects 只写本回合确实发生的结构化后果；纯对话/观察/氛围不必写 effects，留空数组即可。"
    "[!] effects 与 narration_hint 一律不写物品/货币/经验/技能的获得——真实获得由引擎结算后回填到结算要点，你编造的获得引擎不会执行（玩家背包不会变化）。\n"
    "4. narration_hint 要点化，供叙事引擎展开成沉浸旁白，不要写成完整小说段落。\n"
    "5.（已移除：玩家行动选项生成——玩家自由键入行动，不生成 next_options；此条占位避免规则重编号。）\n"
    "6.（同上，已移除。）\n"
    "7. 不可行的行动（如攻击不存在的对象、去封锁区域）置 resolved=false，reason 说明原因；此时 effects 可照常给（描述受阻后的情形）。\n"
    "8. 战斗意图（intent_type=combat）：\n"
    "   - crpg 数值模式（默认）：仅指定 talk_to=攻击目标 NPC 名（须是在场且敌对的 NPC），引擎会纯 Python 结算伤害/暴击/掉落/经验并回填 narration_hint。你不需要也不应该输出具体伤害数值。\n"
    "   - narrative 叙事模式：combat 仅作语义标记，战斗过程由叙事 LLM 自由描写。\n"
    "9. 使用物品意图（intent_type=use_item）：玩家想用消耗品回血（引擎自动用背包里第一个消耗品），或想研读/学习背包里的技能书——玩家说「研读X/学习X/读书」且 X 是背包里的技能书（描述含「研读可习得」）时必须判 use_item。\n"
    "10. 采集意图（intent_type=gather）：玩家想采集资源点（采药/挖矿/翻找/取水/拾荒等，指「本地点可采集资源」中的真实资源点），gather_node 填其中的真实资源点名。引擎会纯 Python 结算成功率/产出/丰度递减/冷却并回填 narration_hint，你不输出具体产出数值。\n"
    "11. 内容遵循世界观的基调与 NSFW 设定。"
"12. 战斗意图（多敌遭遇与同伴助战）：你只须指定 talk_to=主攻击目标；"
    "遭遇规模（敌人数量）与哪些在场同伴会助战由系统自动判定，你不输出这些字段。"
    "结算要点 narration_hint 里若已给出「同伴X与你并肩作战」等信息，据实织入即可。\n"
    "13. NPC 的到场/离场由叙事引擎按旁白首行的移动命令统一同步，你不须也不要在 effects 里写「来到/出现」类调度声明；"
    "narration_hint 可自然描述 NPC 到来/离开的剧情（叙事 LLM 会据此在命令行声明并同步位置）。"
    "narration_hint 里描写某 NPC 的现场言行时，该 NPC 须在【在场 NPC】列表或剧情上即将到场（叙事会安排）；"
    "确实不在场也不能到场的 NPC 只能描述为「不在场传闻/来信/口信」等离场方式。\n"
    "14. 记忆整理时机由引擎按交互间隔确定，你不需要判断记忆标记；"
    "此条保留占位避免规则重编号。\n"
    "15. 若【场所列表】非空，玩家可在当前地点内的场所间移动：move_to 填「当前地点名/场所名」"
    "（如「小镇/酒馆」），引擎校验场所连通性。跨地点移动仍填相邻地点名。\n"
    "16. intent_type=trade 时：trade_mode 填 buy（向商人买）/sell（卖给商人）/barter（与任意 NPC 以物易物）；"
    "talk_to 填交易对象（buy/sell 须是【交易】块中有货架的商人；barter 是在场 NPC）。"
    "trade_item 填物品名（buy=货架物品，sell=背包物品，barter=对方随身物品），barter 另填 trade_offer=玩家给出的背包物品名。"
    "[!] trade 必须填 trade_mode 和 trade_item 两个字段，不得只说「先达成交易窗口/先报价/问价」等含糊表述——"
    "缺任一字段引擎会直接 resolved=false，交易作废。"
    "物品名必须用【交易】块中的真实名称，不要用旁白提及但【交易】块没有的物品（引擎按容器校验，编造物品交易会受阻）。"
    "引擎纯 Python 结算价格/库存/价值比/交情门控并回填 narration_hint，你不输出具体数值。"
    "关系不高时 NPC 像正经商人——不划算的以物易物会被拒绝（resolved=false，reason 说明），据实描写受阻即可。\n"
    "17. 夜晚时段（见【时间天气】相位）商店已打烊（交易所例外）：玩家坚持买时照常判 trade，"
    "引擎会自动加 1.5 倍敲门价--旁白里写出掌柜被敲门叫起、睡眼惺忪加价卖货的情景；玩家也可改等白昼再来。\n"
    "18. [!] 只有【交易】块中有货架的商人才是真实卖货人：结算意图不得涉及"
    "【交易】块之外任何人的售卖/推销/收购物品（街头摊贩、货担郎中、吆喝卖货的路人等一律不构造）--"
    "引擎不追认虚构卖货人，编造的交易会 resolved=false。玩家行动指向这类人时，把他当作普通 NPC 对话互动"
    "（intent_type=talk/interact），不判 trade；"
    "可改为「向商人（名）打听/购买」或逛【交易】块货架的引导选项。\n"
    "19. [B方案] 交易所股市自由文本：玩家说「投资/买入/看涨/做空/卖出 X 商品」且语境是交易所大宗商品"
    "（非商店货架商品）时判 stock_buy/stock_sell（trade_item 填商品名，stock_qty 填数量）；"
    "玩家说「看看行情/行情怎么样」仍走 observe。非城市地点时引擎会拒并指路，不必预判。\n"
    "20. [委托路由] 玩家说「跑腿/送货/交付委托/把 X 交给 Y」且语境匹配某张进行中或可交付的委托订单时，"
    "优先 intent_type=custom 并在 narration_hint 里写明对应委托订单名与目标（引擎与委托板 UI 共用数据）；"
    "玩家只是泛泛说「跑个腿赚点钱」时引导其打开场景页「订单」按钮查看在架委托。\n"
    "21. [!] 战利品/搜刮真相：战斗胜利时引擎已自动结算全部掉落（含搜刮）入包，场景层没有对遗骸的"
    "二次搜刮结算——玩家说「搜刮/翻检/搜索某已倒下 NPC 的遗骸」时判 interact 或 custom"
    "（不是 gather，遗骸不是资源点），effects 与 narration_hint 均不得写获得物品/金币"
    "（引擎不追认，背包不会变化），据实描述翻检无获或仅氛围即可。"
)


# 叙事 LLM 默认提示词（P2 场景旁白：场景 + 行动 + 意图要点 -> 流式旁白 prose）。
# 纯文本流式，不出 JSON/不出选项（选项由结算引擎单独生成）；不替玩家决定。
DEFAULT_WORLDSIM_NARRATIVE_SYSTEM_PROMPT = (
    "你是一个 SLG 游戏的叙事引擎。我会给你当前场景状态、玩家本回合的行动、以及结算引擎给出的"
    "意图要点，请你把它们展开成沉浸式的旁白：描写环境氛围、NPC 的言行神态、行动的过程与结果。\n\n"
    "流式输出纯文本，不要输出 JSON、不要输出选项（选项由结算引擎单独生成）。\n\n"
    "要求：\n"
    "1. 以第二人称（「你」）描写玩家的所见所闻所为；NPC 对话用引号包裹。\n"
    "2. 不要替玩家做决定或发言；玩家行动已在输入给出，你只负责展开其后果。\n"
    "3. 保持与世界观设定、基调、NSFW 一致；人物性格与目标要呼应。\n"
    "4. 叙事要承上启下：衔接上一回合的结果，为下一回合留出钩子。\n"
    "5. 长度适中（通常 2-5 段），每段旁白 300 字内（重大时刻可放宽到 500 字），不要流水账式罗列 effects，要写成有画面感的场景。\n"
    "6. 战斗场景：crpg 模式下具体伤害/掉落/经验数值已由引擎算好填入 narration_hint（如「造成 36 伤害」「获得 30 经验」），请据实描写动作与张力，不要编造或修改数字；narrative 模式下自由描写。\n"
    "7. 若结算引擎标记 resolved=false（行动受阻），据 reason 描写受阻的情境。\n"
    "8. 每个 NPC 有自己的身份、立场与口吻，台词须符合其人设；旁白不得用叙述者口吻替 NPC 说话——NPC 的话语要让读者听出「这是这个人在说话」。\n"
    "9. NPC 提及第三人时，称谓须符合该 NPC 与第三人的关系（如父亲称女儿用名字或「我儿」，绝不用「娘子」「夫人」这类配偶称谓称呼自己的女儿）；NPC 不掌握的信息就说不知道，不得全知全能。\n"
    "10. 玩家使用的称谓（如「娘子」「柳叔」「掌柜」）需先对应到具体人物再回答；若称谓指代不明，可让 NPC 自然反问（「你说的娘子是……？」），不要凭空猜测。\n"
    "11. 你的第一行输出必须是 NPC 移动命令行。格式与字符必须逐字照抄下面的范例"
    "（中括号内是大写字母 N-P-C，箭头是中文长箭头 →，多人用分号 ； 隔开）：\n"
    "    有人到场/离场时，第一行写：[NPC] 张三→白水酒肆；李四→临江府\n"
    "    本回合没有任何 NPC 移动时，第一行只写：[NPC] 无\n"
    "    禁止变体：不要写小写 [npc]，不要用 -> 或 => 代替 →，不要把命令行写进正文，"
    "不要在命令行前后加任何说明文字。命令行独占第一行，从第二行起才是正文。"
    "要让不在【在场 NPC】列表的 NPC 出现在玩家现场（归来、来访、传唤等），必须先在命令行写「名字→当前地点名」，再在正文描写其到场；要让现场 NPC 离场，写「名字→去处地点名」。"
    "命令行的人名必须出自【NPC 一览】、地名必须出自【地点列表】（完整抄写，勿加「门前/郊外」等自造后缀）；既不在【在场 NPC】列表、也未在命令行声明的 NPC 不得出现在现场描写（台词、动作、神态都不行）。命令行之后才是正文，正文不要再包含命令行。引擎按命令行同步 NPC 位置，保证旁白与在场列表一致。\n"
    "12. 【坊间热议】与【世界编年史】中的事件发生在玩家视线之外，仅作背景谈资，旁白最多一笔带过（如「你隐约听说……」「江湖传言……」），不要把其中的具体人物当作现场人物描写；但在场 NPC 若与热议事件相关（同地/同势力/当事人），可让其自然提起一两句（如酒桌闲谈、感慨近况），不必每回合都提。\n"
    "13. 旁白应适时调用感官细节（环境音/气味/温度触感等，优先非视觉），与【时间天气】呼应（风暴夜的雨腥味与灯笼摇晃、清晨的鸟鸣与草露）；不必每段都堆砌，自然融入行文即可，避免泛泛而谈。\n"
    "14. [!] 玩家伤势的唯一依据是【伤情】块（引擎仅在玩家气血不足五成时注入该块）：注入了【伤情】块时，动作与神态须体现伤患（步履迟缓/牵动伤处/呼吸滞重），濒死时行动明显受限、旁人可察觉其虚弱；未注入【伤情】块时，一律不得描写玩家伤势——【最近经历】、【前情摘要】、结算要点（narration_hint）中出现过的旧伤情一律不再延续（无论此前回合写过多么严重的伤），也不要无中生有地描写受伤。本回合玩家是否带伤，只看【伤情】块的有无。\n"
    "15. 人物离场须写离开动作（告辞/起身离去/脚步声远去/背影消失），不得凭空消失；到场不要求过渡描写——[NPC] 命令行已把到场者移动到位，旁白可自然揭示（先闻其声/推门撞见/抬眼即见），是否写过场由行文需要决定。\n"
    "16. 若输入含【同伴反应】块（引擎检定某同伴本回合有话要说时注入），仅该块点名的同伴可发表一句简短插话（呼应其 affinity 倾向与腔调，认同/中立/反对皆可）；插话须显式署名（如「柳叔插话道：「…」」），不得用无主语引号——避免记忆系统误归因给 talk_to 对象。无【同伴反应】块时同伴/路人不得主动发言（仅回答玩家 talk_to 的对象说话）。插话自然融入场景，不喧宾夺主。\n"
    "17. [!] 交易真相硬约束：买卖/以物易物是否成交、价格多少、物品归属是否转移，以结算引擎给出的结算要点为准，旁白无权虚构。结算要点给出交易结果时据实描写；结算要点为空或行动受阻时，付款、收货、拿走商品、对方收下钱物等完成动作一律不得写，最多描写议价、看货、试探等未成交过程。买卖类台词只出自【交易】块中有货架的商人；不得虚构街头摊贩、货担郎中等卖货人物。氛围性路人（摊贩/行人/叫卖声）只作背景点缀，不得与玩家发生物品、金钱或信息的实质交互。\n"
    "18. [!] 位置真相硬约束：玩家与所有 NPC 的所在位置以【当前地点】【当前场所】【在场 NPC】为准，旁白无权擅自移动任何人。行动受阻（如目标地点不相邻、卖家不在场）时，只能描写玩家在当前地点内的尝试与见闻——不得写玩家实际抵达了未移动到的地点（跨区逛街、进店看货等），不得写不在场的 NPC 出现在现场。要让远处可望而不可及：写边界、路途、传闻、方向，把「去不了」本身写成剧情，而不是写一场未获引擎结算的旅行。\n"
    "    秘境内的房间位置以【秘境】的「你此刻在」和本回合真实移动结果为准，同一个内部地点不代表玩家能瞬间走遍所有房间。"
    "例如结算只写「移步至积水侧洞」，本回合只能描写抵达和眼前景象，不得写已经开箱、回复状态或击败守卫；"
    "未发现房间的名称、类型、环境与首领不得提前揭示。未通过楼梯/首领门就不能写已到下层，撤离受阻就不能写已返回外部入口。\n"
    "19. [!] 结算真相硬约束：物品/货币/经验/技能的获得与数值变化，只能来自「结算要点/结构化后果」中引擎给出的确切事实（如「获得 30 灵石」「拾取回气丹」）；存在「行动受阻」块时一律不得描写任何获得（空手而归就写空手而归）。结算要点未提及的获得不得自行补充——翻检尸体/搜刮遗骸若无结算要点支持，只能写一无所获或仅氛围描写。\n"
)


# [场景事件生图] 叙事旁白 [img:...] 协议块（P2 场景交互循环补链：出图管线一直在
# process_scene_images，但叙事 LLM 从未被教过输出 [img:...] 标签，功能实际死链）。
# [!] 运行时按 narrative_image_event × image_enabled 双闸在 _build_narrate_messages
#     注入（前面要求块 + 末尾强制行），不落 preset 存储、不改 DEFAULT_*_PROMPT，
#     故不触发 PROMPT_DEFAULTS_REV 版本门。
# [!] 块内描述须点名【在场 NPC】的真实名字：process_scene_images 按「名字出现在
#     标签描述里」召回该 NPC 的 appearance_tags（最多 2 人），无名匹配走纯场景模式。
WORLDSIM_NARRATIVE_IMG_REQ = (
    "=== 图片标签要求（最高优先级，每次回复必须遵守）===\n"
    "旁白正文写完后，必须在末尾追加**一个** `[img:画面描述]` 标签，给出本回合最有"
    "画面感的一个关键画面，供文生图用。这是硬性格式要求，不可省略，不可输出多个。\n"
    "画面内容规则：\n"
    "1. 画面最多 2 个人。优先画本回合的关键人物：必须是【在场 NPC】列表里的真实人物，"
    "且描述中**写出其名字**（如「白芷」「铁蛟」，引擎按名字套用该人物的外貌设定）；"
    "画 2 人时两人都写名字与可见外貌；纯环境/景色的回合可以完全无人。\n"
    "2. 不在【在场 NPC】列表的人物绝不入画；玩家「你」通常不入画（镜头从你的视角出发），"
    "除非本回合画面必须你在场（对峙/同行/被围观），此时也最多「你」+1 名 NPC 共 2 人。\n"
    "3. 描述只写画面能直接看到的：外貌（发色/发长/瞳色/服装）、动作姿势、表情神态、"
    "持有物品、场景可见元素（建筑/家具/天气/光线/时间）。\n"
    "4. 禁抽象情绪（「杀意凛然」改「目光冷厉按刀」）、禁嗅觉/听觉/触觉（「酒香四溢」删）、"
    "禁文学修辞；名字可保留，外貌细节尽量具体。\n"
    "5. 画面捕捉本回合的关键瞬间（交剑/递物/破门/跪拜/远望），不是泛泛的场景摆拍。\n"
    "示例：\n"
    "[img:白芷立于白水酒肆柜台后擦拭酒碗，粗布青裙挽袖，鬓边木簪，柜上灯烛昏黄，门外暮色细雨]\n"
    "[img:铁蛟与病书生对峙于荒庙断墙前，铁蛟玄色劲装持环刀刀尖垂地，病书生灰袍散发"
    "握短匕，身后雷云压城雨幕如注]\n"
    "[img:雨后青石长街无人，两侧灯笼初上，檐水成线滴落，远处山影朦胧]（无人纯景示例）\n"
    "标签只输出 [img:...] 本身，不加说明文字；标签前后不加反引号、代码块或任何包裹符；"
    "标签不影响正文，旁白照常完整写完。\n"
    "=== 图片标签要求结束 ===\n\n"
)

# [场景事件生图] 末尾强制行（注入在系统提示最末，与规则层首尾呼应压服从率）
WORLDSIM_NARRATIVE_IMG_TAIL = (
    "生成img：回复末尾必须按上方「图片标签要求」输出一个 [img:...] 标签（不可省略）。"
)


# [P19] 叙事文风预设（A5 题材语体 + C1 文风层）：独立于规则层的可编辑文风偏好。
# 用户「不用可清空、要用再写」：narrative_system_full() 里非空才注入。
# 与 narrative_system_prompt（规则层：协议/一致性/感官/伤势）解耦——改文风不再碰协议。
DEFAULT_NARRATIVE_STYLE_PROMPT = (
    "按题材选择语体，对话与旁白同语体：\n"
    "- 武侠：白话为主，夹杂武侠术语（内功、轻功、江湖切口），写意克制。\n"
    "- 仙侠：文言味稍重，多用四字雅词与修炼术语，飘渺出尘。\n"
    "- 现代：口语白话，日常用语自然，节奏明快。\n"
    "- 科幻：冷峻精确，术语带科技感，短句利落。\n"
    "- 末日：冷峻压抑，短句，多环境凋敝细节。\n"
    "- 西方奇幻：西式叙事腔，译制文学感。\n"
    "- 自定义题材：以世界基调为准。"
)


# 世界滴答 LLM 默认提示词（P4）。
# 一次调用承担两种模式（据 mode 字段分流），出严格 JSON：
#   mode="key_npc"  : 给出要角 NPC 名单 -> LLM 出每人本回合的决策意图（去哪/做什么/对谁），
#                     引擎层据名称解析回结构变更（name->id + 连通性/alive 校验）。
#   mode="reconcile": 给出势力/经济快照 -> LLM 出调整建议（power/wealth/relations delta），
#                     引擎层应用 + 钳制，防世界状态长期漂移。
# [!] 数值范式：LLM 只出「决策语义」与「调整建议」，引擎算实际变更 + 概率 roll + 钳制；
#     LLM 不直接决定最终数值（如同 P3 战斗：LLM 出 talk_to 语义，引擎算伤害）。
DEFAULT_WORLDSIM_SIM_SYSTEM_PROMPT = (
    "你是 SLG 游戏的世界滴答引擎。我会给你当前世界快照与任务模式，请你输出严格符合 schema 的 JSON。\n\n"
    "只输出 JSON，不要输出任何说明、解释、前后缀或 markdown 代码块标记。\n\n"
    "任务模式 mode 有两种：\n\n"
    "=== mode=\"key_npc\"（要角决策）===\n"
    "我会给你若干「要角 NPC」名单及其当前状态。请你为每个要角决定本回合的离屏行动意图。\n"
    "JSON schema：\n"
    "{\n"
    '  "mode": "key_npc",\n'
    '  "decisions": [\n'
    '    {"name": "NPC名", "action": "move|stay|interact|scheme", '
    '"target_location": "目标地点名(仅 move 填，必须是相邻地点之一)", '
    '"target_npc": "互动对象NPC名(仅 interact 填)", '
    '"narration_hint": "1句该要角本回合在做什么的要点，供传闻/叙事用", '
    '"current_goal": "1句该要角本回合后仍在推进的短期目标（可延续旧目标或据本回合进展更新；空=保持原短期目标不变）", '
    '"new_goal": "仅当该要角的长期目标（【目标】）已达成或已不可能达成时填一句新的长期目标；未达成留空"}\n'
    "  ]\n"
    "}\n\n"
    "=== mode=\"reconcile\"（世界校准）===\n"
    "我会给你各势力的 power/wealth/relations 快照。请你据世界基调与近期事件，给出小幅调整建议，"
    "让世界状态更自洽、防长期漂移。\n"
    "JSON schema：\n"
    "{\n"
    '  "mode": "reconcile",\n'
    '  "faction_adjust": [\n'
    '    {"name": "势力名", "power_delta": -10到10的整数, "wealth_delta": -10到10的整数}\n'
    "  ],\n"
    '  "relation_adjust": [\n'
    '    {"a": "势力A名", "b": "势力B名", "delta": -20到20的整数}\n'
    "  ],\n"
    '  "new_quests": [{"title": "新任务名", "objective": "目标一句话", '
    '"giver": "发布NPC名(必须是快照里的真实NPC名)", "reward": "奖励说明", '
    '"objectives": [{"type": "kill或gather或talk或visit或collect", '
    '"target": "真实NPC名/资源类型/地点名/物品名", "count": 1, "desc": "一句目标描述"}], '
    '"rewards": {"gold": 金币数, "xp": 经验数, "items": ["物品名"]}}],\n'
    '  "world_note": "可选，1句对世界整体走向的备注，供事件日志/叙事参考"\n'
    "}\n\n"
    "要求：\n"
    "1. 所有名称必须使用我给出的真实名称，不要编造。\n"
    "2. 动作要贴合角色性格、目标、势力立场与世界基调；避免角色做出与设定矛盾的行为。\n"
    "2b. current_goal 是该要角本回合后仍要推进的短期目标——多数回合延续旧目标即可，"
    "只在有明确进展/受阻/新情况时更新；new_goal 是长期目标的替换，仅在旧长期目标已达成或"
    "已不可能达成（如目标对象已不存在/目标地点已易主/愿望已实现）时才填，勿频繁更换长期目标。\n"
    "3. delta 是「建议值」，引擎会做概率判定与钳制，你不必精确，给出方向性建议即可。\n"
    "4. 力度适中：不要每回合都给极端 delta 或戏剧性大动作，多数回合应是小幅微调。\n"
    "5. new_quests 可选（长局任务来源）：仅当世界走向明确适合给玩家派生新任务时给"
    "最多 1 条（呼应近期事件/势力变化/玩家进展），平常回合给空数组；giver 与 objectives 的"
    "名称必须真实存在。\n"
    "6. 内容遵循世界观的基调与 NSFW 设定。"
)


# 允许的枚举值（from_dict 白名单校验用，防脏数据）
_ECONOMY_SIM_VALUES = ("off", "light", "medium", "heavy")
_FACTION_WAR_VALUES = ("off", "light", "medium", "heavy")
_COMBAT_SYSTEM_VALUES = ("narrative", "crpg")
_CRPG_GRANULARITY_VALUES = ("light", "medium", "heavy")
_DIFFICULTY_VALUES = ("easy", "normal", "hard")
_SCALE_VALUES = ("small", "medium", "large")


# [P6] 商店备货 LLM 默认提示词。
# 给一家商店（题材类型 + 货币 + 地点 + 商人）出差异化货架 JSON。
# LLM 只产静态种子（物品定义 + 单价 + 库存），交易/补货定价纯 Python（trade_engine）。
DEFAULT_WORLDSIM_SHOP_SYSTEM_PROMPT = (
    "你是 SLG 游戏的商店备货引擎。我会给你一家商店的信息（类型/题材货币/地点/商人）"
    "和全世界的现有物品清单，请你从清单中为这家店挑选合适的商品上架"
    "（不可发明清单外的新物品），输出严格符合 schema 的 JSON。\n\n"
    "只输出 JSON，不要输出任何说明、解释、前后缀或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    "{\n"
    '  "shop_name": "商店显示名（贴合题材，如「百宝阁」「老李丹药铺」）",\n'
    '  "goods": [\n'
    '    {"item_id": "清单里的物品 id（必须原样照抄）", "stock": 1-5的整数, "base_price": 0}\n'
    "  ]\n"
    "}\n\n"
    "要求：\n"
    "1. 只能选清单内的 item_id，原样照抄不可改写；选不到合适的类型时宁缺毋滥，不要编造。\n"
    "1b. 清单里的技能书（描述含「研读可习得」）任何类型的店都可以上架——法器阁/古董铺"
    "卖秘籍、杂货铺卖教程都合情理，品级高的书适合拍卖行。\n"
    "2. 选品要贴合商店类型与题材：兵器铺多选 weapon、丹药铺多选 consumable、杂货铺各类型混合；"
    "[酒楼分家 2026-09-10] consumable 店（酒楼/餐厅/食堂）只选吃喝——清单行带「｜吃喝」标记的"
    "才是本店货（丹药/药酒归丹药铺，酒楼不卖药）；"
    "[!] 货架必须覆盖全部 5 类（weapon/armor/consumable/material/accessory）每类至少 1 件——"
    "专营店本类多、其余类各 1-2 件，不得只上单一类型。"
    "品级分布以 common/uncommon 为主，rare 少量，epic/legendary 极少（1-2 件顶天），mythic 仅在极特殊时 0-1 件。\n"
    "3. 商品数量 8-15 件，要与商店规模相称，避免空架或冗余；同类商品不要重复上架。\n"
    "4. base_price 填 0 表示由系统公式按品级+数值定价（推荐）；也可给整数种子价，但必须落在品级区间内"
    "（common 18-45 / uncommon 32-81 / rare 58-144 / epic 108-243 / legendary 198-445，超出区间会被系统钳回）——"
    "品级即价值锚，epic 材料 51、legendary 芯片 91 这类跨档倒挂是严重违规。\n"
    "5. stock 是当前库存（1-5），不是上限；系统会定期补货。\n"
    "6. 商店名贴合世界观基调与 NSFW 设定。"
)


# [P34f] 交易所股市每日定价 LLM 提示词。每日 1 次（day_count 变化触发），读近期事件/
# 当前各 commodity 价格/经济节奏，输出各 commodity 当日新价。±% 由引擎钳制防崩盘。
DEFAULT_STOCK_MARKET_PROMPT = (
    "你是 SLG 游戏的交易所行情定价引擎。我会给你当前各商品的行情（symbol/名称/当前价/上一日价/基准价）"
    "和近期世界动态（事件/时间天气/经济节奏），请你据世界形势为每件商品定当日新价，"
    "输出严格符合 schema 的 JSON。\n\n"
    "只输出 JSON，不要输出任何说明、解释、前后缀或 markdown 代码块标记。\n\n"
    "JSON schema 与输出示例（假定输入有 IRON/GRAIN/SILK 三个商品）：\n"
    "{\n"
    "  \"mode\": \"stock\",\n"
    "  \"prices\": {\"IRON\": 55, \"GRAIN\": 21, \"SILK\": 120}\n"
    "}\n\n"
    "要求（逐条执行）：\n"
    "1. prices 必须覆盖输入里的全部 symbol，key 原样照抄（大写、一个字母都不能改），不可遗漏、不可新增。\n"
    "2. 定价逻辑要紧扣世界形势，逐商品想一遍因果再给价：战争/围城/军备 -> 军需类（war）涨；"
    "灾荒/歉收/商路断绝 -> 民生类（civil）粮盐涨；太平丰收 -> 回落；奢侈类（lux）随盛世涨、乱世跌。"
    "输入里的【势力局势】块给出明确涨跌方向时，你定的价必须与该方向一致，不许反向。\n"
    "3. 单日波动幅度：常见 ±5%~±20%，重大事件最多 ±30%。以「上一日价」为锚算百分比"
    "（如上日 100、涨 15% 就写 115），不要暴涨暴跌（系统会按 ±% 上限钳制防崩盘，贴着上限给无效）。\n"
    "4. 新价必须是正整数（>=1），不要小数、不要字符串、不要带单位。\n"
    "5. 长期趋势应围绕「基准价」上下波动（基准价是商品的公允价值锚），连续多日单边偏离是错误定价。\n"
    "6. 输出里不要有任何 prices 之外的字段。"
)





# [P34g] 拍卖会拍品 LLM 生成提示词。start_auction 时调 1 次，读题材/玩家等级/拍卖规模，
# 输出拍品骨架（name/type/rarity/level/desc）。level 可超 shop_max_item_level（拍卖会=高 level 物品来源）。
DEFAULT_AUCTION_GENERATE_PROMPT = (
    "你是 SLG 游戏的拍卖会拍品生成引擎。我会给你世界题材、玩家等级和拍品数量，"
    "请生成一场拍卖会的拍品骨架，输出严格符合 schema 的 JSON。\n\n"
    "只输出 JSON，不要输出任何说明、解释、前后缀或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    "{\n"
    "  \"mode\": \"auction\",\n"
    "  \"lots\": [\n"
    "    {\"name\": \"拍品名\", \"type\": \"weapon|armor|consumable|material|accessory\",\n"
    "     \"rarity\": \"common|uncommon|rare|epic|legendary|mythic\",\n"
    "     \"level\": 1-6的整数, \"desc\": \"1句描述\"}\n"
    "  ]\n"
    "}\n\n"
    "输出示例（武侠题材，拍品数 2）：\n"
    "{\"mode\": \"auction\", \"lots\": [{"
    "\"name\": \"断岳重刀\", \"type\": \"weapon\", \"rarity\": \"epic\", \"level\": 4, \"desc\": \"前朝铸剑大师绝笔，刀身有裂纹却锋利如昔\"},"
    " {\"name\": \"雪参王\", \"type\": \"consumable\", \"rarity\": \"rare\", \"level\": 3, \"desc\": \"百年老参，参须如雪，吊命圣药\"}]}\n\n"
    "要求（逐条执行）：\n"
    "1. lots 数量必须正好等于给定拍品数；每件 name 不重复、贴合世界题材（武侠世界不出激光枪/机器人）。\n"
    "2. 品级分布：以 rare/epic 为主体，legendary 1-2 件压轴、mythic 至多 1 件镇场"
    "（拍卖会是玩家获取高价值物品的核心渠道），common/uncommon 少量凑数。\n"
    "3. level 填 1-6 的整数（1=凡品，6=神兵级）；level 与 rarity 要相称"
    "（legendary 配 level 5-6，common 配 level 1），可超过商店等级上限——高 level 拍品起拍价更高更珍贵。\n"
    "4. type 只能从 weapon|armor|consumable|material|accessory 里选"
    "（material=古玩/矿石/原料，consumable=丹药/药剂/食物/卷轴这类可服用或使用即耗之物——铜镜/罗盘等器物类奇物不判 consumable，收藏类归 material、可佩戴归 accessory；accessory=佩饰）。不要发明别的 type。\n"
    "5. desc 只写一句风味描述（来历/传闻/品相），不要写任何数值（价格/属性由系统按 level/rarity 公式定）。\n"
    "6. 输出里不要有任何 lots 之外的字段。"
)






# LLM 自分类合并：summary 整合段落；hybrid 产 [triggers: ...] 明细行（冲突以大 seq 为准，纯追加）。
DEFAULT_NPC_MEMORY_SUMMARY_PROMPT = (
    "你是 SLG 游戏 NPC 的记忆整理助手。我会给你某 NPC 的现有记忆 + 本回合的互动素材，"
    "请整理成一段连贯的记忆（保留仍有效的重要旧记忆，删去过时或被新互动修正的内容，整合新信息并去除冗余）。\n\n"
    "你拿到的素材是玩家的视角，不是该 NPC 的视角——你要自己判断哪些是该 NPC 能感知到的：\n"
    "- 【玩家本回合行动】：玩家本回合的输入（行动意图/原话）。这是该 NPC 能听到的玩家言行。若旁白未逐字还原玩家原话，按此意图理解，不得编造玩家没说过的细节。\n"
    "- 【结算结构化后果】：引擎结算的客观事实（交易完成/获得物品/移动等），双方在场都可见，可直接作为事实。\n"
    "- 【本回合旁白全文】：玩家视角的第二人称叙事，信息最全。但它是全知视角，可能含该 NPC 感知不到的信息——玩家的内心活动、其他在场人物的私下举动、视线外的事件、纯环境氛围。该 NPC 是本回合 talk_to 的对象，旁白中无主语的引号台词大概率是其所说。\n\n"
    "整理规则：\n"
    "1. 以第三人称写，只记录该 NPC 自己能感知、记住的事（玩家的言行、他对玩家的印象、达成的约定、发生的关键事件）。由你从旁白全文中判断哪些是该 NPC 在场时能直接感知/参与的；该 NPC 不在场时发生的事、其他 NPC 的隐私、玩家内心活动一律不记。\n"
    "2. 该 NPC 的台词要区分「陈述事实」与「客套/戏谑/夸张/谎言」——后者不作为事实记忆，但可作为「该 NPC 说话风格/对该玩家的态度」的印象记。\n"
    "3. 分类清晰但不分条目，写成 2-5 句的连贯段落。\n"
    "4. 不要编造互动中没有的信息。\n"
    "5. [!] 输出总长硬上限 2048 字：整理后的记忆全文不得超过 2048 字。接近上限时优先保留仍有效的旧记忆与最新关键事实，删去次要细节与过时内容，宁可精炼不可超限。\n"
    "6. 只输出整理后的记忆段落本身，不要任何前后缀或解释。"
)

DEFAULT_NPC_MEMORY_HYBRID_PROMPT = (
    "你是 SLG 游戏 NPC 的记忆整理助手。我会给你某 NPC 的已有记忆条目（带 seq 序号，序号越大越新）"
    "+ 本回合的互动素材。请为该 NPC 产出新增/更新的记忆条目。\n\n"
    "你拿到的素材是玩家的视角，不是该 NPC 的视角——你要自己判断哪些是该 NPC 能感知到的：\n"
    "- 【玩家本回合行动】：玩家本回合的输入。若旁白未逐字还原玩家原话，按此意图理解，不得编造玩家没说过的细节。\n"
    "- 【结算结构化后果】：引擎结算的客观事实，可直接作为事实。\n"
    "- 【本回合旁白全文】：玩家视角的第二人称叙事，全知视角，可能含该 NPC 感知不到的信息。该 NPC 是本回合 talk_to 的对象，旁白中无主语的引号台词大概率是其所说。\n\n"
    "输出格式（每行一条，严格一致）：\n"
    "[triggers: 词1,词2,词3,词4] 这条记忆的明细内容（一句完整的话）\n\n"
    "要求：\n"
    "1. triggers 恰好 4 个词，分别覆盖「动作/事件、情绪/态度、对象/关系、场景/时间」四个维度，每词 2-4 字。\n"
    "2. 明细只记录该 NPC 自己能感知的事；由你从旁白全文中判断哪些是该 NPC 在场时能直接感知/参与的。该 NPC 不在场时发生的事、其他 NPC 的隐私、玩家内心活动一律不记。不要重复已有事实（看 seq 列表），只产出真正新增或有变化的事实。\n"
    "3. 该 NPC 的台词区分「陈述事实」与「客套/戏谑/夸张/谎言」——后者不作为事实，但可作为说话风格/态度印象。\n"
    "4. 新旧冲突时（如关系从陌生变熟悉），只产出新的那条（引擎以大 seq 为准）。\n"
    "5. [!] 本轮产出的全部条目总长硬上限 2048 字：接近上限时只保留真正新增的关键事实条目，次要细节不立条，宁可精炼不可超限。\n"
    "6. 不要输出任何说明、解释、前后缀，只输出记忆条目行（可空行分隔）。"
)

# [P29] NPC 记忆批量整理提示词（折叠时多 NPC 一次调用）。
# 与单 NPC 即时整理互补：折叠时把待删旧条目（多回合窗口）一次性喂给 LLM，
# 一次出 N 个在场 NPC 的记忆增量（=== NPC名 === 分隔块），拆分后逐 NPC 写入。
# 这是对"即将被折叠删除的旁白"的最后抢救——非 talk_to NPC 也补覆盖面，
# talk_to NPC 用跨回合长上下文重整已有记忆（质量升级非纯冗余）。
DEFAULT_NPC_MEMORY_BATCH_SUMMARY_PROMPT = (
    "你是 SLG 游戏 NPC 的记忆整理助手。我会给你若干在场 NPC 的档案 + 各自现有记忆 + "
    "一批较老的多回合场景记录（玩家视角全知叙事）+ 前情摘要。请为每个 NPC 整理出"
    "一段连贯的记忆，用跨回合脉络重整（合并旧记忆与新见闻，去冗余去过时）。\n\n"
    "你拿到的场景记录是玩家视角的第二人称全知叙事，可能含各 NPC 感知不到的信息"
    "（玩家内心、其他人物私下举动、视线外事件）。每个条目标注了当时 talk_to 的对象"
    "（对话对象），但你要为所有候选 NPC 各出一份记忆——由你判断每个 NPC 在场时能"
    "感知/参与什么，只记这些；不在场时发生的事、其他 NPC 隐私、玩家内心一律不记。\n\n"
    "输出格式（严格一致）：\n"
    "=== NPC名 ===\n"
    "该 NPC 整理后的记忆段落（2-5 句，第三人称，只记该 NPC 自己能感知记住的事；\n"
    "台词区分陈述事实与客套/戏谑/夸张/谎言，后者不作事实但可作风格印象；\n"
    "与现有记忆合并去冗余，保留仍有效的旧记忆，删被新见闻修正的内容；不编造）\n\n"
    "[!] 每个 NPC 的记忆段落硬上限 2048 字：接近上限时优先保留仍有效的旧记忆与最新关键事实，删去次要细节，宁可精炼不可超限。\n"
    "每个候选 NPC 一块，块间空行分隔。只输出我给出的候选 NPC 列表中的名字，"
    "不要输出未在列表中的 NPC。某 NPC 在这批记录里没有可感知的事，块内写「无新增」。"
)

DEFAULT_NPC_MEMORY_BATCH_HYBRID_PROMPT = (
    "你是 SLG 游戏 NPC 的记忆整理助手。我会给你若干在场 NPC 的档案 + 各自已有记忆条目"
    "（带 seq 序号，序号越大越新）+ 一批较老的多回合场景记录（玩家视角全知叙事）+ 前情摘要。"
    "请为每个 NPC 产出新增/更新的记忆条目。\n\n"
    "场景记录是玩家视角全知叙事，可能含各 NPC 感知不到的信息。每个条目标注了当时 talk_to "
    "的对象，但你要为所有候选 NPC 各出记忆条目——由你判断每个 NPC 在场时能感知/参与什么，"
    "只记这些；不在场的事、其他 NPC 隐私、玩家内心一律不记。看各 NPC 已有条目去重，"
    "只产真正新增或有变化的事实；新旧冲突（如关系从陌生变熟悉）只产出新的那条。\n\n"
    "输出格式（严格一致）：\n"
    "=== NPC名 ===\n"
    "[triggers: 词1,词2,词3,词4] 这条记忆的明细内容（一句完整的话）\n"
    "[triggers: 词1,词2,词3,词4] 另一条记忆明细\n\n"
    "triggers 恰好 4 个词，覆盖「动作/事件、情绪/态度、对象/关系、场景/时间」四维度，每词 2-4 字。"
    "每个候选 NPC 一块（块内可多行条目，可空行分隔），块间空行分隔。"
    "[!] 每个 NPC 块的条目总长硬上限 2048 字：接近上限时只保留真正新增的关键事实条目，次要细节不立条，宁可精炼不可超限。"
    "只输出候选 NPC 列表中的名字。某 NPC 无新增则块内写「无新增」。"
    "台词区分陈述事实与客套/戏谑/夸张/谎言，后者不作事实但可作风格印象。"
    "不要输出任何说明或解释。"
)

# [P5b] 内置规模档位（结构化数字，提示词文本由 _scale_hint_text 生成）：
# - id: 唯一标识（与 LLM 提示词拼接、World.scale 字段值一致）
# - label: 下拉显示文本
# - locations/npcs/places: 地点数 / NPC 数 / 每地点场所数（只输入数字，文本固定生成）
BUILTIN_SCALES = [
    # [P12] 基础地点数翻倍（3/5/8 -> 6/10/16，据玩家反馈初始地点太少）；NPC 数不变
    # [P27] 每地点场所数（small=3 / medium=4 / large=5）
    {"id": "small", "label": "小（6地点5NPC3场所/地点）", "locations": 6, "npcs": 5, "places": 3},
    {"id": "medium", "label": "中（10地点10NPC4场所/地点）", "locations": 10, "npcs": 10, "places": 4},
    {"id": "large", "label": "大（16地点15NPC5场所/地点）", "locations": 16, "npcs": 15, "places": 5},
]


def _scale_hint_text(c: dict) -> str:
    """[P5b] 由结构化数字生成给 LLM 的规模提示词文本（固定模板，防手写漂移）。"""
    loc = int(c.get("locations") or 0)
    npc = int(c.get("npcs") or 0)
    plc = int(c.get("places") or 0)
    return f"{loc}地点 {npc}NPC {plc}场所/地点"


def _pick_custom_scales(raw) -> list[dict]:
    """[P5b] 校验 + 清洗用户自定义规模档位列表。

    规则：每项须是 dict 且含 id(str 非空) / label(str 非空) 三字段 + 数字 locations/npcs/places
    （缺数字回退内置 small 档对应值）；id 重复保留先出现的；空 list 返回 []。
    向后兼容：旧预设无此字段 -> from_dict 传入 [] -> 返回 []。
    """
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("id") or "").strip()
        label = str(it.get("label") or "").strip()
        if not sid or not label:
            continue
        if sid in seen:
            continue
        # id 不允许与内置三档冲突（防覆盖默认行为）
        if sid in _SCALE_VALUES:
            continue
        seen.add(sid)
        loc = _si(it, "locations", 6)
        npc = _si(it, "npcs", 5)
        plc = _si(it, "places", 3)
        out.append({"id": sid, "label": label, "locations": max(1, loc),
                    "npcs": max(1, npc), "places": max(1, plc)})
    return out


# [P5c] 5 项属性的内部 key（底层字段 stat_str/dex/int/vit/luk 对应的短 key）。
_STAT_KEYS = ("str", "dex", "int", "vit", "luk")

# [P6] 品级档位内部 key（底层 Item.rarity 取值，白名单与 world.py _ITEM_RARITY_VALUES 一致）。
_RARITY_KEYS = ("common", "uncommon", "rare", "epic", "legendary", "mythic")
# [P6] 商店类型内部 key（Shop.shop_type 取值；Phase B 商店系统用，模板层先落地题材名）。
_SHOP_TYPE_KEYS = ("general", "weapon", "armor", "alchemy", "consumable", "material", "magic", "auction")
# [P6] 物品大类内部 key（底层 Item.type 取值，白名单与 world.py _ITEM_TYPE_VALUES 一致）。
_ITEM_TYPE_KEYS = ("weapon", "armor", "consumable", "material", "key", "accessory")
# [P34d] 物品次级分类内部 key（底层 Item.category 取值，白名单与 world.py _ITEM_CATEGORY_VALUES
# 一致；type 与 category 正交——type 永不变守 §23，category 是背包分桶用的叠加维度）。
_ITEM_CATEGORY_KEYS = ("consume", "skillbook", "weapon", "armor", "accessory",
                       "material", "cultivate", "forge", "craft", "seed", "key",
                       "misc", "pet_food")

# [P6] 题材化字符串默认值（西幻口径；自定义模板缺字段时补全用）。
# 守「底层字段名不变，只改显示中文名」铁律：金币/灵石/信用点都映射到底层 PlayerState.gold。
_DEFAULT_CURRENCY = "金币"
# [P6] 题材化字符串默认值（西幻·中世纪口径；自定义模板缺字段时补全用）。
# [!] 参考知名小说体系而非游戏套路（去「史诗/传说/药水店/魔法店」等 RPG 梗）：
# 西幻参考《冰与火之歌》《魔戒》——品级走锻造造诣（凡铁…传世），商铺走中世纪行会。
_DEFAULT_RARITY_NAMES = {
    "common": "凡铁", "uncommon": "精钢", "rare": "良锻", "epic": "名器", "legendary": "传世", "mythic": "神话",
}
_DEFAULT_SHOP_TYPE_NAMES = {
    "general": "杂货商", "weapon": "铁匠铺", "armor": "护甲匠", "alchemy": "炼金术士",
    "consumable": "草药铺", "material": "材料商", "magic": "古董铺", "auction": "拍卖行",
}
_DEFAULT_ITEM_TYPE_NAMES = {
    "weapon": "武器", "armor": "护甲", "consumable": "药剂",
    "material": "材料", "key": "信物", "accessory": "饰品",
}
# [P34d] 物品次级分类默认名（西幻口径；背包 QTabWidget 分桶 tab 标题用）。
# 与 item_type_display_names 正交：type 是底层大类（永不变），category 是背包分桶维度。
# 11 档对应 world.py _ITEM_CATEGORY_VALUES：装备 3 / 消耗品 + 技能书 / 材料 3（锻造·制造·采集）
# / 培养（鉴定洗练 reagent）/ 任务 / 其他。
_DEFAULT_ITEM_CATEGORY_NAMES = {
    "seed": "种子",
    "weapon": "武器", "armor": "护甲", "accessory": "饰品",
    "consume": "消耗品", "skillbook": "技能书",
    "material": "材料", "forge": "锻造材料", "craft": "制造材料",
    "cultivate": "培养道具", "key": "任务物品", "misc": "其他",
}


def _clean_str_dict(raw, keys) -> dict:
    """[P6] 把 raw(dict) 按 keys 取值清洗成 {key: str}（缺省空串）；raw 非 dict 视为全空。

    [!] 始终返回含全部 keys 的 dict（缺失项为 ""），让调用方的 `all(values)` 校验能正确
    检出缺档并回退默认（`all({})` 是 True 即空真，会误判为「已齐」）。
    """
    raw = raw if isinstance(raw, dict) else {}
    return {k: str(raw.get(k, "") or "") for k in keys}


def _si(d: dict, key: str, default: int) -> int:
    """[P6] 安全读 int 字段（防脏数据/手改 JSON 致崩，守 §11）：缺失/None 用 default，
    非数值字符串也回退 default（合法 0 保留）。"""
    v = d.get(key)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# [P5c/P6] 内置题材模板（6 个题材）。P5c 仅 stat/slot 名；P6 扩展为「统一题材模板」：
# 一个题材承载全部可主题化字符串（属性名/槽名/货币/品级名/商店类型名/物品大类名）。
# 注意：底层字段名（stat_str/.../gold/rarity/type/slot）一律不变，仅改「显示给用户的中文名」，
# 让仙侠/现代/科幻共用同一套引擎（数值/交易/战斗全部题材无关）。
#
# [P6 修订] 所有题材化字符串参考**知名小说体系**而非游戏套路（用户反馈：去 RPG 梗）：
#   - 西幻：参考《冰与火之歌》《魔戒》—— 金币本位、品级走锻造造诣、行会式商铺。
#   - 仙侠：参考《凡人修仙传》《诛仙》—— 灵石本位、法宝品阶、丹药法器阁。
#   - 武侠/古代：参考金庸/古龙 +《琅琊榜》《庆余年》（含历史架空）—— 银两本位、神兵利器。
#   - 现代：参考都市网文 —— 人民币、按工艺/档次分级。
#   - 科幻：参考《三体》《沙丘》《银河帝国》—— 信用点、按技术等级分级。
#   - 末日：参考末日流网文（《末日之终极进化》等）/《地铁》系列 —— 晶核（丧尸/变异兽能量结晶）
#     为硬通货（[!] 弃用游戏梗「瓶盖」）、品级按物品状态。
BUILTIN_ATTRIBUTES_TEMPLATES = [
    {
        "id": "western_fantasy",
        "label": "西幻 · 中世纪（参考冰与火之歌 / 魔戒）",
        "stat_display_names": {"str": "力", "dex": "敏", "int": "智", "vit": "耐", "luk": "运"},
        "slot_display_names": {"head": "头部", "chest": "胸甲", "legs": "护腿", "feet": "靴子",
                                "main_hand": "主手", "off_hand": "副手",
                                "accessory1": "饰品1", "accessory2": "饰品2"},
        "currency_name": "金币",
        "hp_display_name": "生命", "mp_display_name": "法力",
        "rarity_display_names": {"common": "凡铁", "uncommon": "精钢", "rare": "良锻", "epic": "名器", "legendary": "传世", "mythic": "神话"},
        "shop_type_names": {"general": "杂货商", "weapon": "铁匠铺", "armor": "护甲匠", "alchemy": "炼金术士",
                             "consumable": "草药铺", "material": "材料商", "magic": "古董铺", "auction": "拍卖行"},
        "item_type_display_names": {"weapon": "武器", "armor": "护甲", "consumable": "药剂",
                                     "material": "材料", "key": "信物", "accessory": "饰品"},
        "item_category_display_names": {"weapon": "武器", "armor": "护甲", "accessory": "饰品",
                                        "consume": "药剂", "skillbook": "技能书",
                                        "material": "材料", "forge": "锻造材料", "craft": "制造材料",
                                        "cultivate": "鉴定洗练", "key": "信物", "misc": "杂物", "seed": "种子",
                                        "pet_food": "宠物食品"},
    },
    {
        "id": "xianxia",
        "label": "仙侠修真（参考凡人修仙传 / 诛仙）",
        "stat_display_names": {"str": "蛮力", "dex": "身法", "int": "灵根", "vit": "体魄", "luk": "机缘"},
        "slot_display_names": {"head": "发冠", "chest": "法袍", "legs": "下装", "feet": "踏云履",
                                "main_hand": "本命法宝", "off_hand": "副器",
                                "accessory1": "玉佩", "accessory2": "储物戒"},
        "currency_name": "灵石",
        "hp_display_name": "气血", "mp_display_name": "灵力",
        "rarity_display_names": {"common": "凡器", "uncommon": "灵器", "rare": "法宝", "epic": "仙器", "legendary": "神器", "mythic": "圣器"},
        "shop_type_names": {"general": "百宝阁", "weapon": "法器阁", "armor": "防具坊", "alchemy": "丹药铺",
                             "consumable": "灵药铺", "material": "灵材铺", "magic": "符箓铺", "auction": "珍宝阁"},
        # [2026-08-23 真人测试] accessory 显示名原为「法宝」，与主手槽「本命法宝」、
        # rare 品级「法宝」三重撞名——玩家见「法宝」以为装本命法宝槽，实际却装玉佩/
        # 储物戒槽。改「灵饰」对应 accessory1/2（玉佩/储物戒）槽位语义，消除歧义。
        "item_type_display_names": {"weapon": "法器", "armor": "防具", "consumable": "丹药",
                                     "material": "灵材", "key": "信物", "accessory": "灵饰"},
        "item_category_display_names": {"weapon": "法器", "armor": "防具", "accessory": "灵饰",
                                        "consume": "丹药", "skillbook": "功法玉简",
                                        "material": "灵材", "forge": "炼器材料", "craft": "炼丹材料",
                                        "cultivate": "鉴宝洗练", "key": "信物", "misc": "杂物", "seed": "灵种",
                                        "pet_food": "灵饲"},
    },
    {
        "id": "wuxia",
        "label": "武侠 / 古代（参考金庸古龙 / 琅琊榜，含历史架空）",
        "stat_display_names": {"str": "臂力", "dex": "轻功", "int": "内功", "vit": "根骨", "luk": "气运"},
        "slot_display_names": {"head": "头巾", "chest": "劲装", "legs": "下裳", "feet": "快靴",
                                "main_hand": "兵刃", "off_hand": "暗器",
                                "accessory1": "佩玉", "accessory2": "护腕"},
        "currency_name": "银两",
        "hp_display_name": "气血", "mp_display_name": "内力",
        "rarity_display_names": {"common": "凡兵", "uncommon": "利器", "rare": "宝刃", "epic": "名剑", "legendary": "神兵", "mythic": "绝世"},
        "shop_type_names": {"general": "杂货铺", "weapon": "铁匠铺", "armor": "绸缎庄", "alchemy": "药铺",
                             "consumable": "酒楼", "material": "山货行", "magic": "当铺", "auction": "英雄会黑市"},
        "item_type_display_names": {"weapon": "兵刃", "armor": "护具", "consumable": "丹药",
                                     "material": "药材", "key": "信物", "accessory": "饰物"},
        "item_category_display_names": {"weapon": "兵刃", "armor": "护具", "accessory": "饰物",
                                        "consume": "丹药", "skillbook": "武功秘籍",
                                        "material": "药材", "forge": "锻造材料", "craft": "制药材料",
                                        "cultivate": "鉴宝洗练", "key": "信物", "misc": "杂物", "seed": "药种",
                                        "pet_food": "兽粮"},
    },
    {
        "id": "modern",
        "label": "现代都市（参考都市网文）",
        "stat_display_names": {"str": "体能", "dex": "反应", "int": "智力", "vit": "体魄", "luk": "运气"},
        "slot_display_names": {"head": "帽子", "chest": "上衣", "legs": "裤子", "feet": "鞋子",
                                "main_hand": "主武器", "off_hand": "副武器",
                                "accessory1": "饰品1", "accessory2": "饰品2"},
        "currency_name": "元",
        "hp_display_name": "生命", "mp_display_name": "精力",
        "rarity_display_names": {"common": "普通", "uncommon": "优质", "rare": "精品", "epic": "高级", "legendary": "顶级", "mythic": "传奇"},
        "shop_type_names": {"general": "便利店", "weapon": "军品店", "armor": "战术装备店", "alchemy": "药房",
                             "consumable": "餐厅", "material": "五金店", "magic": "电子市场", "auction": "地下黑市"},
        "item_type_display_names": {"weapon": "武器", "armor": "护具", "consumable": "药品",
                                     "material": "材料", "key": "证物", "accessory": "饰品"},
        "item_category_display_names": {"weapon": "武器", "armor": "护具", "accessory": "饰品",
                                        "consume": "药品", "skillbook": "教程手册",
                                        "material": "材料", "forge": "加工材料", "craft": "制造材料",
                                        "cultivate": "鉴定洗练", "key": "证物", "misc": "杂物", "seed": "种子",
                                        "pet_food": "宠物粮"},
    },
    {
        "id": "scifi",
        "label": "科幻未来（参考三体 / 沙丘 / 银河帝国）",
        "stat_display_names": {"str": "力量", "dex": "敏捷", "int": "神经", "vit": "耐力", "luk": "运气"},
        "slot_display_names": {"head": "头盔", "chest": "外骨骼", "legs": "腿甲", "feet": "动力靴",
                                "main_hand": "主武器", "off_hand": "副武器",
                                "accessory1": "植入体1", "accessory2": "植入体2"},
        "currency_name": "信用点",
        "hp_display_name": "生命", "mp_display_name": "能量",
        "rarity_display_names": {"common": "民用", "uncommon": "工业", "rare": "军用", "epic": "实验型", "legendary": "未知科技", "mythic": "神级"},
        "shop_type_names": {"general": "补给站", "weapon": "军械站", "armor": "护甲站", "alchemy": "药剂站",
                             "consumable": "餐厅", "material": "材料站", "magic": "义体诊所", "auction": "星际拍卖网"},
        "item_type_display_names": {"weapon": "武器", "armor": "护甲", "consumable": "药剂",
                                     "material": "材料", "key": "密钥", "accessory": "植入体"},
        "item_category_display_names": {"weapon": "武器", "armor": "护甲", "accessory": "植入体",
                                        "consume": "药剂", "skillbook": "数据芯片",
                                        "material": "材料", "forge": "锻造材料", "craft": "合成材料",
                                        "cultivate": "解析洗练", "key": "密钥", "misc": "杂物", "seed": "种苗",
                                        "pet_food": "合成饲料"},
    },
    {
        "id": "apocalypse",
        "label": "末日废土（参考末日流网文 / 地铁系列）",
        "stat_display_names": {"str": "体能", "dex": "敏捷", "int": "智力", "vit": "体魄", "luk": "运气"},
        "slot_display_names": {"head": "头具", "chest": "护甲", "legs": "腿甲", "feet": "靴子",
                                "main_hand": "主武器", "off_hand": "副武器",
                                "accessory1": "饰品1", "accessory2": "饰品2"},
        "currency_name": "晶核",
        "hp_display_name": "生命", "mp_display_name": "体力",
        "rarity_display_names": {"common": "报废", "uncommon": "破损", "rare": "可用", "epic": "精修", "legendary": "珍品", "mythic": "传说"},
        "shop_type_names": {"general": "交易站", "weapon": "军火商", "armor": "护具站", "alchemy": "诊所",
                             "consumable": "食堂", "material": "拾荒站", "magic": "避难所", "auction": "废土黑市"},
        "item_type_display_names": {"weapon": "武器", "armor": "护甲", "consumable": "补给品",
                                     "material": "材料", "key": "信物", "accessory": "饰品"},
        "item_category_display_names": {"weapon": "武器", "armor": "护甲", "accessory": "饰品",
                                        "consume": "补给品", "skillbook": "生存手册",
                                        "material": "材料", "forge": "改造材料", "craft": "合成材料",
                                        "cultivate": "鉴定洗练", "key": "信物", "misc": "杂物", "seed": "种子",
                                        "pet_food": "罐头饲料"},
    },
]

# [P5c] 内置模板 id 集合（校验用）。
_BUILTIN_TEMPLATE_IDS = {t["id"] for t in BUILTIN_ATTRIBUTES_TEMPLATES}

# [P5c] 默认模板 id（缺省回退用，与现状一致即西幻 CRPG）。
DEFAULT_ATTRIBUTE_TEMPLATE_ID = "western_fantasy"

def _sf(d: dict, key: str, default: float) -> float:
    """[!] 安全读 float（守 §11，同 _si）：缺失/None/脏类型回默认，合法 0.0 保留
    （temperature=0.0 是合法值——用户要确定性输出，`or default` 会吞成默认温度）。"""
    v = d.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default



def _normalize_template(t: dict) -> dict:
    """[P6] 把任意模板 dict（内置或自定义）归一为字段齐全的模板。

    保证返回的 dict 含全部 6 组题材化字段：stat_display_names(5)/slot_display_names(8)/
    currency_name/rarity_display_names(5)/shop_type_names(8)/item_type_display_names(6)。
    缺失或部分缺失的字段用西幻默认补全（守向后兼容：P5c 旧自定义模板只有 stat/slot）。
    传入非 dict 返回西幻完整模板。
    """
    from src.models.world import EQUIPMENT_SLOTS
    if not isinstance(t, dict):
        t = {}
    sdn = _clean_str_dict(t.get("stat_display_names"), _STAT_KEYS)
    if not all(sdn.values()):
        sdn = {k: v for k, v in zip(_STAT_KEYS, ("力", "敏", "智", "耐", "运"))}
    ldn = _clean_str_dict(t.get("slot_display_names"), EQUIPMENT_SLOTS)
    if not all(ldn.values()):
        ldn = {"head": "头部", "chest": "胸甲", "legs": "护腿", "feet": "靴子",
               "main_hand": "主手", "off_hand": "副手", "accessory1": "饰品1", "accessory2": "饰品2"}
    rdn = _clean_str_dict(t.get("rarity_display_names"), _RARITY_KEYS)
    if not all(rdn.values()):
        rdn = dict(_DEFAULT_RARITY_NAMES)
    stn = _clean_str_dict(t.get("shop_type_names"), _SHOP_TYPE_KEYS)
    if not all(stn.values()):
        stn = dict(_DEFAULT_SHOP_TYPE_NAMES)
    itn = _clean_str_dict(t.get("item_type_display_names"), _ITEM_TYPE_KEYS)
    if not all(itn.values()):
        itn = dict(_DEFAULT_ITEM_TYPE_NAMES)
    # [P34d] 物品次级分类名（背包分桶 tab 标题用）；缺档回退西幻默认。
    icn = _clean_str_dict(t.get("item_category_display_names"), _ITEM_CATEGORY_KEYS)
    if not all(icn.values()):
        icn = dict(_DEFAULT_ITEM_CATEGORY_NAMES)
    return {
        "id": str(t.get("id") or "").strip(),
        "label": str(t.get("label") or "").strip(),
        "stat_display_names": sdn,
        "slot_display_names": ldn,
        "currency_name": str(t.get("currency_name") or "").strip() or _DEFAULT_CURRENCY,
        "hp_display_name": str(t.get("hp_display_name") or "").strip() or "生命",
        "mp_display_name": str(t.get("mp_display_name") or "").strip() or "法力",
        "rarity_display_names": rdn,
        "shop_type_names": stn,
        "item_type_display_names": itn,
        "item_category_display_names": icn,
    }


def _pick_attribute_templates(raw) -> list[dict]:
    """[P5c/P6] 校验 + 清洗用户自定义题材模板列表。

    规则：每项须是 dict 且含 id(str 非空) / label(str 非空) /
          stat_display_names(dict 5 项) / slot_display_names(dict 8 槽) 字段；
    缺字段或类型不对的项丢弃；id 重复或与内置冲突的丢弃；空 list 直接返回。
    [P6] 通过校验的项再用 _normalize_template 补全 currency/rarity/shop_type/item_type
    （P5c 旧自定义模板只有 stat/slot，新字段补西幻默认，向后兼容）。
    向后兼容：旧预设无此字段 -> from_dict 传入 [] -> 返回 []（仅用内置 6 模板）。
    """
    if not isinstance(raw, list):
        return []
    from src.models.world import EQUIPMENT_SLOTS
    seen: set[str] = set()
    out: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        tid = str(it.get("id") or "").strip()
        label = str(it.get("label") or "").strip()
        if not tid or not label:
            continue
        if tid in seen or tid in _BUILTIN_TEMPLATE_IDS:
            continue
        # 校验 stat_display_names
        sdn = it.get("stat_display_names")
        if not isinstance(sdn, dict):
            continue
        sdn_clean = {k: str(sdn.get(k, "") or "") for k in _STAT_KEYS}
        if not all(sdn_clean.values()):
            continue
        # 校验 slot_display_names（8 槽）
        ldn = it.get("slot_display_names")
        if not isinstance(ldn, dict):
            continue
        ldn_clean = {k: str(ldn.get(k, "") or "") for k in EQUIPMENT_SLOTS}
        if not all(ldn_clean.values()):
            continue
        seen.add(tid)
        # [P6] 补全新字段（货币/品级/商店类型/物品大类/物品分类），缺失走默认
        out.append(_normalize_template({
            "id": tid, "label": label,
            "stat_display_names": sdn_clean,
            "slot_display_names": ldn_clean,
            "currency_name": it.get("currency_name"),
            "rarity_display_names": it.get("rarity_display_names"),
            "shop_type_names": it.get("shop_type_names"),
            "item_type_display_names": it.get("item_type_display_names"),
            "item_category_display_names": it.get("item_category_display_names"),
        }))
    return out


# [数据量铁律] 默认题材标签单点定义：dataclass 默认值与 from_dict 回退共用，
# 防两处清单漂移（曾出现 from_dict 还是旧 8 个、dataclass 已扩到 16 个的不一致）。
# [!] 只收录有对应内置属性模板（BUILTIN_ATTRIBUTES_TEMPLATES）的标签：题材化行为全按
# attribute_template_id 查表，无对应模板的标签会被 _resolve_template_id 静默回退
# western_fantasy，等于触发不了题材逻辑。故删去赛博朋克/蒸汽朋克/克苏鲁/都市异能/
# 星际战争/废土/三国/和风/黑暗奇幻/种田日常。
# [题材对齐 2026-10-01 用户拍板] 标签与内置六题材 **1:1**（以代码可触发的模板为主）：
# 旧表「中世纪/奇幻」两标签挤西幻一套且 modern 无标签（现代世界观缺信号）——
# 改为六标签六 id 单一来源 GENRE_TAG_TEMPLATE_MAP，提示词据此硬指定 template_id
# （world_sim_service._genre_tag_directive）。老存档/自定义标签不受影响（不在映射内不指引）。
GENRE_TAG_TEMPLATE_MAP: list[tuple[str, str]] = [
    ("仙侠修真", "xianxia"),
    ("武侠古代", "wuxia"),
    ("西幻中世纪", "western_fantasy"),
    ("现代都市", "modern"),
    ("科幻未来", "scifi"),
    ("末日废土", "apocalypse"),
]
_DEFAULT_GENRE_TAGS = [tag for tag, _tid in GENRE_TAG_TEMPLATE_MAP]

# [P7d3] 题材化境界名（据 level 分段，GenreText.level_name 用）
# 让升级有题材沉浸感：修仙练气->筑基->金丹、现代新手->专家->大师、末日幸存者->传奇
# [数据量铁律] 每题材 7-9 档。
# [均摊口径 2026-09-05 用户指示] 旧表把境界压进前 40 级（仙侠 10 级即元婴、40-100 全程
# 渡劫），前期升境太快后期 60 级不换称呼。现改为渐进均摊：档宽随进度从 5-7 级渐增到
# 15-18 级、总和收在满级 100（守护测试钳档宽 5-20），同档数题材共用同一阈值梯子。
# 仙侠新梯：练气 1-5 / 筑基 6-12 / 金丹 13-21 / 元婴 22-32 / 化神 33-45 / 炼虚 46-59 /
# 合体 60-74 / 大乘 75-89 / 渡劫 90-100（10 级=筑基，元婴回到中段位次）。
_GENRE_LEVEL_NAMES = {
    "xianxia": [(5, "练气"), (12, "筑基"), (21, "金丹"), (32, "元婴"), (45, "化神"),
                (59, "炼虚"), (74, "合体"), (89, "大乘"), (100, "渡劫")],
    "wuxia": [(7, "初学"), (18, "入门"), (32, "小成"), (48, "大成"), (65, "登峰"),
              (82, "绝顶"), (100, "宗师")],
    "modern": [(6, "新手"), (15, "熟练"), (27, "专家"), (41, "资深"), (56, "精英"),
               (72, "权威"), (88, "泰斗"), (100, "大师")],
    "scifi": [(7, "见习"), (18, "正式"), (32, "高级"), (48, "资深"), (65, "主管"),
              (82, "精英"), (100, "首席")],
    "apocalypse": [(7, "幸存者"), (18, "拾荒者"), (32, "猎手"), (48, "清扫者"),
                   (65, "辐射行者"), (82, "守望者"), (100, "传奇")],
    "western_fantasy": [(7, "学徒"), (18, "冒险者"), (32, "老手"), (48, "勇士"),
                        (65, "游侠"), (82, "屠龙者"), (100, "传奇英雄")],
}


class GenreText:
    """[P6] 题材化字符串只读 bundle。

    一次读 world.config_overlay，暴露题材适配访问器（货币/品级/属性/槽/商店类型/物品大类名）。
    底层字段名（gold/rarity/type/slot/stat_*）一律不变，本类只给「显示给用户的中文名」，
    让仙侠/现代/科幻共用同一套引擎。无 LLM/Qt/DB 依赖（worker 线程安全）。
    老 P5c 世界 overlay 缺 P6 新字段时经 _normalize_template 补西幻默认。
    """

    # 内置题材的显示名字段组（纯装饰，build 时从模板拷贝落 overlay，无逐世界定制入口）。
    # [UI 禁英文 2026-10-01] hp/mp_display_name：气血/内力等题材显示名（UI 全中文契约）。
    _DISPLAY_GROUPS = ("stat_display_names", "slot_display_names", "currency_name",
                       "rarity_display_names", "shop_type_names",
                       "item_type_display_names", "item_category_display_names",
                       "hp_display_name", "mp_display_name")

    def __init__(self, overlay=None):
        raw = overlay if isinstance(overlay, dict) else {}
        n = _normalize_template(raw)
        # [P7d3] 单独存题材 id（_normalize_template 不保留此字段，level_name 据它查境界名）
        self._tid = str(raw.get("attribute_template_id", "western_fantasy") or "western_fantasy")
        # [2026-08-23 真人测试] 内置题材的显示名以「当前模板」为准覆盖 overlay 落盘副本：
        # overlay 在 build 时拷贝模板存盘，老世界会把旧显示名（如仙侠 accessory「法宝」）
        # 固化下来，模板改名后老世界不跟随。显示名纯装饰、内置题材无逐世界定制，覆盖安全。
        # [!] 仅当 overlay 显式带内置 id 才覆盖（build 落盘必带 id）；无 id 的自定义/残缺
        # overlay 保留其落盘显示名（_normalize_template 口径），不误伤自定义题材。
        raw_tid = str(raw.get("attribute_template_id", "") or "")
        if raw_tid in _BUILTIN_TEMPLATE_IDS:
            tmpl = next((t for t in BUILTIN_ATTRIBUTES_TEMPLATES if t["id"] == raw_tid), None)
            if tmpl is not None:
                for g in self._DISPLAY_GROUPS:
                    if g in tmpl:
                        n[g] = dict(tmpl[g]) if isinstance(tmpl[g], dict) else tmpl[g]
        self._n = n

    @property
    def currency(self) -> str:
        return self._n["currency_name"]

    @property
    def hp(self) -> str:
        """[UI 禁英文] 生命值题材显示名（武侠=气血/科幻=生命），替代裸 HP。"""
        return self._n.get("hp_display_name", "") or "生命"

    @property
    def mp(self) -> str:
        """[UI 禁英文] 法力值题材显示名（武侠=内力/仙侠=灵力），替代裸 MP。"""
        return self._n.get("mp_display_name", "") or "法力"

    def rarity(self, level: str) -> str:
        return self._n["rarity_display_names"].get(level, "") or level

    def stat(self, key: str) -> str:
        return self._n["stat_display_names"].get(key, "") or key

    def slot(self, slot_id: str) -> str:
        return self._n["slot_display_names"].get(slot_id, "") or slot_id

    def shop_type(self, shop_id: str) -> str:
        return self._n["shop_type_names"].get(shop_id, "") or shop_id

    def item_type(self, t: str) -> str:
        return self._n["item_type_display_names"].get(t, "") or t

    def item_category(self, c: str) -> str:
        # [P34d] 物品次级分类题材化名（背包 QTabWidget 分桶 tab 标题用）。
        return self._n["item_category_display_names"].get(c, "") or c

    def rarity_names(self) -> dict:
        return dict(self._n["rarity_display_names"])

    def level_name(self, level: int) -> str:
        """[P7d3] 题材化境界名（据 level 分段，如修仙 1-5 练气 / 6-12 筑基 / 13-21 金丹；
        [均摊口径 2026-09-05] 档宽随进度渐增、末档收 100，见 _GENRE_LEVEL_NAMES 注释）。"""
        try:
            lv = int(level)
        except (TypeError, ValueError):
            lv = 1
        tiers = _GENRE_LEVEL_NAMES.get(self._tid, _GENRE_LEVEL_NAMES["western_fantasy"])
        for threshold, name in tiers:
            if lv <= threshold:
                return name
        return tiers[-1][1] if tiers else f"Lv{lv}"


# [提示词版本门] 引擎提示词默认值版本号：**改任何 DEFAULT_*_PROMPT 后必须 +1**。
# from_dict 发现存的 rev 低于当前值时，把 7 个提示词字段视为过期旧默认值丢弃（回退现行默认），
# 防止 data/world_sim_preset.json 里设置对话框某次整体保存写入的历史默认提示词永久遮蔽
# 代码新默认（症状：改了 DEFAULT 提示词但 API 日志里仍是旧文本/schema 对不上）。
# 用户在当前 rev 上手动定制的提示词保留；rev 提升 = 明确丢弃旧定制（开发版本口径）。
# rev 1 = 无此字段的历史文件；rev 2 = P17 提示词-数据池联动（quests schema/规模行/备货只选现有物品等）。
DEFAULT_NPC_LIFE_PLAN_PROMPT = (
    "你是游戏世界的 NPC 行为规划器。为给出的 NPC 们规划今日行动，让世界像活人社会一样自然运转。\n\n"
    "每个 NPC 的 action 只能从下面 6 个动作里选一个（动作名必须逐字照抄，小写英文，不要自造）：\n"
    "- gather（采集）：去野外采药/挖矿/拾取自然资源；\n"
    "- craft（合成）：用手头材料制作物品（可附可选字段 target=想造的物品名，从其随身材料与在售商品出发取名）；\n"
    "- stock_shop（上架补货）：把自家产出摆上自家商店货架（[游商 2026-09-08] 仅旧档"
    "真商人有店；新世界商店由游商自营，无人上架）；\n"
    "- buy_materials（批发买料）：钱包宽裕时购入原料扩大生产（[游商] 旧档商人扩产用；\n"
    "  非商人合成者仅缺料时进场）；\n"
    "- hunt（外出狩猎）：武者打猎谋生，有真实风险——可能负伤回城、重伤遗失物品、甚至阵亡；\n"
    "- rest（休整）：养伤/歇业/静修（伤病者、老人、遭变故者选这个）。\n\n"
    "选动作的思路（逐人想一遍再定）：[游商 2026-09-08] 旧档商人的常态是 gather/craft/stock_shop"
    " 循环，钱包鼓了升级为 buy_materials 扩产；新世界商店由游商自营，手艺人只 craft 不上架；"
    "武者/猎户可 hunt（穷则搏命，富则安稳）；"
    "伤病/年迈/心绪不宁者 rest。同一人连日重复同一动作是允许的（营生稳定），"
    "但遭遇变故（受伤/破产/亲友变故，见其档案与近期遭遇）时应明显改变。\n\n"
    "thought 与 goal 两个字段会展示给玩家「察言观色」并指导旁白言行，必须像活人在说话：\n"
    "- thought：此人此刻惦记什么（内心戏，贴合其处境/性格/近期遭遇，<=50 字，口语化，不要出现动作英文名）；\n"
    "- goal：今天想达成什么（与 action 呼应但更具体，<=50 字，口语化，不要出现动作英文名）。\n"
    "字面示例（照抄这种感觉）：{\"name\":\"徐娘子\",\"action\":\"stock_shop\","
    "\"thought\":\"雨后药材价该起来了，趁早把新晒的止血草摆出去。\","
    "\"goal\":\"今日卖出三成存货，顺便打听渡口的动静。\"}\n\n"
    "只输出纯 JSON，不要任何说明文字：\n"
    "{\"plans\": [{\"name\": \"NPC名（照抄输入名单）\", \"action\": \"上面6个动作之一\", "
    "\"target\": \"仅 craft 时可给的物品名（其他动作省略此字段）\", "
    "\"thought\": \"此刻念头<=50字\", \"goal\": \"今日目标<=50字\"}]}"
)




DEFAULT_STORY_ARC_PROMPT = (
    "你是这个世界的剧情导演。你的职责：从给定的人物、势力矛盾与近期大事里，编排 1-2 条「正在这个世界发生」的剧情线——有策划者、有参与者、有阶段推进的故事骨架。\n\n"
    "编排原则（逐条遵守）：\n"
    "1. 人名铁律：mastermind 与 participants 只能照抄【候选人物】名单里的名字，一个都不能编、不能改字。名单外的人名会导致整条线被引擎丢弃。\n"
    "2. 有据取材：premise 必须引用【势力关系】【近期大事】里给定的真实矛盾（负关系、仇怨、大事余波），不得凭空制造世界不存在的冲突。\n"
    "3. 同聚落：策划者（mastermind）与至少 1 名参与者必须位于同一地点（候选名单每人都标了所在地点）。\n"
    "4. 不越权：剧情线只推动事态，绝不安排任何人死亡、任何据点易主（标题与描述禁止出现「陨落」「据点失守」「灭门」「夺城」字样的结果）。\n"
    "5. conflict 段必须有真实敌意背书（势力双边关系为负，或名单内人物有明显仇怨）；关系正常者之间的摩擦写 scheme。\n"
    "6. [S01 2026-09-30] 不要输出 kind 字段——阶段类型由引擎按本批【原型指派】的序列自动生成，你输出的 kind 会被忽略（旧版要求输出 kind 已废止）。\n"
    "7. 每条线 2-5 个阶段；每段 days 为必填的 1-5 整数，全段总天数控制在 5-15 天；阶段之间必须有递进（起->承->转->收），末段是能收束结局的高潮。\n"
    "8. [S01 2026-09-30] 阶段内容须贴合【世界时间】给定的历法日期/时令/昼夜相位（春耕秋收、冬雪夏汛、夜行昼市等），不得出现与当前季节相悖的场景；days 的推进与本批有效期相称。\n"
    "9. hook（可选，整条线至多 1-2 段给）：给玩家留的介入点。type 只能取 kill/talk/visit/collect/deliver_items/dungeon_room_resolved/dungeon_cleared；target 必须照抄名单内 NPC 名 / 真实地点名 / 真实物品名（material）/【可介入秘境】里的秘境名，编造的目标会被引擎丢弃（丢 hook 不丢线）。deliver_items=玩家收集指定材料后亲手交付给托付人；dungeon_room_resolved=玩家深入指定秘境探明三处房间；dungeon_cleared=玩家攻克指定秘境首领。hook.stance 可写 support（帮策划者，默认）或 oppose（帮对立方）；oppose 只用于 faction_rivalry/mediation/trade_war 三种可站边原型，其他原型会按 support 处理。\n"
    "10. 已有剧情线标题（见【已有剧情线】）不得重复、不得近似。\n"
    "11. 标题 <=12 字、premise <=80 字、阶段 name <=8 字、阶段 desc <=60 字，全部用世界观内中文口吻（贴合题材，不出现现代引擎词汇）。\n"
    "12. 阶段 condition 可省略；若要让既有参与者未来死亡时本段提前受挫，只能写 {\"type\":\"npc_dead\",\"target\":\"名单内参与者名\"}。这不是让玩家杀人的指令，也不得把策划者死亡写成可继续的普通阶段。\n\n"
    "只输出纯 JSON（不要任何说明文字、不要代码块标记）：\n"
    '{"mode":"story_arc","arcs":[{"title":"标题","premise":"一句话前提与矛盾来源",'
    '"mastermind":"真实NPC名","participants":["真实NPC名","真实NPC名"],'
    '"faction":"势力名或空字符串",'
    '"stages":[{"name":"阶段名","desc":"这个阶段世界在发生什么","days":2,'
    '"hook":{"type":"visit","target":"真实地点名","count":1,"stance":"support"},'
    '"condition":{"type":"npc_dead","target":"名单内参与者名"}}]}]}' + "\n\n"
    "（hook 字段是可选的：不想要介入点的阶段直接省略该字段；days 是必填字段，不要省略。）"
)


DEFAULT_STORY_ARC_REPLAN_PROMPT = (
    "你是世界剧情导演，只能为一条正在发生的剧情线续写尚未开始的阶段。"
    "已发生的阶段、人物生死、任务贡献和世界数值都是事实，不得改写。\n\n"
    "规则：\n"
    "1. 只返回用户消息列出的阶段 id，每个 id 恰好一次；不得增加、删除、交换阶段。"
    "阶段 kind、days、截止时间由引擎保持不变，你只写 name、desc 与可选 hook/condition。\n"
    "2. name/desc 写世界中将发生的可能行动，不能把尚未结算的交易、死亡、获得或胜利写成既成事实。"
    "若策划者或参与者已死亡，不得安排死者出场、发言或赴约；可以写在世者如何处理其留下的事。\n"
    "3. hook 若提供，type 只能是 kill/talk/visit/collect/deliver_items/"
    "dungeon_room_resolved/dungeon_cleared，target 只能照抄给定名单。"
    "stance 只能为 support（帮策划者）或 oppose（帮对立方）；oppose 仅适用于已标注可站边的原型。"
    "没有可行介入点可写 hook:null。\n"
    "4. condition 若提供，只能是 {\"type\":\"npc_dead\",\"target\":\"在世参与者名\"}，"
    "表示该人未来若死亡，此段提前受挫；不要要求玩家杀人。没有条件写 condition:null。\n"
    "5. 只写纯 JSON，不输出解释、代码块或数值效果。字面结构："
    '{"mode":"story_arc_replan","arc_id":"照抄ID","revision":0,'
    '"stages":[{"id":"照抄阶段ID","name":"新阶段名","desc":"新阶段内容",'
    '"hook":null,"condition":null}]}。'
)


PROMPT_DEFAULTS_REV = 57  # rev 57: [回滚 2026-10-02 用户指示] 规则D 恢复 rev55 原文（A 转化闸真机复测 OOC：打变异犬冒出人形掠夺者——怪物池按 danger 抽不认「犬」语义，整套 A 回滚见 read.md）; rev 56: [A 2026-10-02 用户拍板·战斗veto转化闸] settle 规则D 攻击一律判combat并填目标名（可行性交引擎裁定；原口径让LLM对旁白虚构目标自行resolved=false，真机案开枪被LLM拒绝、引擎转化闸收不到combat意图）; rev 55: [D01] 房间具名移动/楼梯/逐房撤离协议与位置真相; rev 54: [S03 完整版] 新增阶段续写提示词，只许改未开始阶段且按真实结算/人物生死续写；rev 53: [S04 2026-09-30 R2] 剧情线 hook 类型扩三件（deliver_items/dungeon_room_resolved/dungeon_cleared）+ target 须抄 user 消息注入的【可介入秘境】名单（sae._sanitize_hook 白名单与校验同步：秘境只认 open、交付同 collect 限 material）；rev 52: [S01 2026-09-30 R1] 剧情规划读到真实时间--arc user 消息注入【世界时间】块（历法日期/相位/天气/回合 + 本批有效期，interval 经参数传入）+ DEFAULT_STORY_ARC_PROMPT 对齐 P46.1 现状（schema 示例去 kind、规则6 改「勿输出 kind 由引擎按原型生成」、days 必填声明、新规则8 时令贴合）+ settle 首行残留「并生成下一批互动选项」清理（rev 44 移除选项后的漏网句）；rev 51: [酒楼分家 2026-09-10 用户指示] consumable 店只卖吃喝--商店备货规则2加酒楼选品约束（清单吃喝打标+丹药归药铺）+ heal_full 并入 heal_pct（gen schema heal_amount->heal_pct/范围表/两处效果说明收口）；rev 50: [用户指示 2026-09-10] 只有夜晚算打烊--settle 规则17 夜晚/黎明->夜晚（清晨/白昼/黄昏均营业）；rev 49: [游商 2026-09-08 用户指示] 商人 NPC 摘除--gen NPC 提示词禁经商 role（规则6）+ 数量指引去商人角色；npc_life_plan stock_shop/buy_materials 动作注旧档商人限定；rev 48: [用户指示 2026-09-07] 伤情动态化--narrate 规则14 改为【伤情】块唯一依据（无块不写伤；最近经历/前情摘要/结算要点旧伤一律不延续，根治伤情回声闭环，真机「等你站起来」OOC）+ 引擎侧 HP<五成才注入【伤情】块、【玩家状态】删 HP 与「重伤」标签；rev 47: [2026-09-06 晚] settle 加 gift 意图（schema 枚举+gift_item 字段+速查表 G 条+例6）；rev 46: [用户指示 2026-09-06] 提示词面向弱模型详尽化--settle 加「意图判定速查表」（判定顺序 A-G + 5 条字面示例：去某处必填 move_to、货架实物绝不判 stock_buy、搜刮无获得）+ narrate 规则11 命令行给逐字照抄范例（大写 [NPC]/长箭头→/分号；禁小写与变体）；同批：heal_full/heal_mp 恢复全对齐百分比（amount=百分数 1-100，范围表同步）、gen NPC inventory 初始携带封顶 epic（清单带[禁随身]标注）。rev 45: [用户指示 2026-09-06] NPC 活在世界--npc_life_plan schema note 升级为 thought（此刻念头）+ goal（今日目标）两字段（随日计划批产出零新增调用，落 NPC 持久字段每日刷新；上限 50 字）；rev 44: [用户指示 2026-09-05] 去掉每回合生成行动选项（避免干扰）--settle schema 删 next_options 字段 + 规则5/6 置空占位 + 规则7/18 去选项提及；上下文删【交互方式/选项数量/允许自由输入】块；rev 43: [定版裁剪 2026-09-05 用户指示] 节日系统整体移除--settle 规则17 删节日集市句 + 股市提示词删节日措辞（节日天商品波动）；rev 42: [修 2026-09-05] 结算真相批次--settle 规则3 禁 effects/hint 编造获得 + 规则10 采集示例收窄（搜刮不再是 gather 触发词）+ 新规则21 战利品/搜刮真相（战斗胜利已自动掉落入包、搜刮遗骸判 interact/custom 不判 gather、不写获得）；叙事新规则19 结算真相硬约束（获得只能来自结算要点、行动受阻一律零获得）；rev 41: [2026-08-31] 商店备货提示词规则2加「货架覆盖全部 5 类每类至少 1 件」硬约束 + 规则3 件数 6-12 -> 8-15（商人不分类，货架全品类覆盖，主营类型仅数量重心）；rev 40: [用户定稿 2026-08-28] 地点内场所自由到达——key_npc 提示词场所移动措辞去相邻约束（service 侧 move 分支连通校验已同步移除）；rev 39: [用户定稿 B方案 2026-08-28] settle schema 加 stock_buy/stock_sell 意图枚举与 stock_qty 字段 + 规则19 股市自由文本路由 + 规则20 委托交付语境路由；rev 38: [2026-08-28] 叙事加规则18位置真相硬约束（30回合真机：受阻回合旁白自由发挥跨区旅行/不在场NPC到场——交易闸管住了但移动没管）+ 规则17补氛围路人禁实质交互；rev 37: [用户指示 2026-08-28 方案A] 物品生成提示词 schema 加 category/reagent_kind 字段 + 规则8 功能性物品生成指引（cultivate x3/种子 x2/宠物食品 x1，名字 desc 贴题材；引擎收编校验钉契约字段）；rev 36: [修 2026-08-28] 商店备货提示词规则4加 base_price 品级区间指引（防跨档倒挂种子价，配合引擎侧 _clamp_item_stats 护栏双闸）；rev 35: [用户指示 2026-08-28] NPC 记忆四套提示词加输出总长硬上限 2048 字约束（场景上下文记忆召回不再 [:200] 硬截断，长度控制移交提示词侧）；rev 34: [修 2026-08-28] 虚构摊主交易批次--叙事加规则17交易真相硬约束（无结算要点不得写成交/虚构摊贩）+ settle 加规则18（意图/选项禁无货架者卖货推销，指向时降级 talk/interact）+ 上下文【今日节日】行写明节庆商品只在商店货架（配合【交易】块）；rev 33: [修 2026-08-27] settle/narrative/sim + expand/gen 提示词剥离开发注记（[P27 场所移动]/[已废弃·P42b]/[NPC 调度] 等方括号注解不再暴露给 LLM）；rev 32: [修 2026-08-25] settle 规则16 交易意图加硬约束——trade 必须填 trade_mode+trade_item，不得只说「先达成交易窗口」等含糊表述（配合 _resolve_trade 的 mode 白名单硬闸，杜绝「旁白演了交易、引擎没做交易」的 no-op）；rev 31: [用户指示 2026-08-25] 物品属性 LLM 全权产出——gen schema items 加 attack/defense/heal_amount/stat_bonus/level/consume_effect 数值字段 + 范围表自填指引（引擎仅对 0 兜底）；rev 30: gen 规则 9e 补 combat_role 必填声明（漏标按 none 平民，[P44] 关键词兜底路移除）；rev 29: settle 移除 memory_worthy 字段（[P42b 用户指示] 记忆整理时机纯 interval 确定）；rev 28: narrative 规则12 近期世界动态改名坊间热议+编年史可被在场 NPC 提及（P39d+e）；rev 27: settle 加夜间作息规则（P39c 商店打烊敲门价）；rev 26: npc_life_plan 加 buy_materials 动作 + target 字段（P37 经济闭环）；rev 25: 新增 npc_life_plan_prompt（P36a NPC 生活批量日计划）；rev 24: gen schema 顶层加 world_name + factions relations + npc combat_role + settle 模糊移动方向（验收收尾）；rev 23: 同 24 批段；rev 22: key_npc 决策加 current_goal/new_goal；rev 21: auction_generate_prompt（P34g）；rev 20: stock_market_prompt（P34f）；rev 19: venues/settlement_size（P34e）；rev 18: 技能 inflicts（Q1）；rev 17: 同伴插话署名（P32）；rev 16: NPC 记忆批量（P29）；rev 15: trade 意图（P28）；rev 14: places（P27）；rev 13: 秘境深入（P25a）；rev 12: 记忆视角护栏；rev 11: memory_worthy；rev 10: 感官放宽
_PROMPT_FIELD_KEYS = (
    "settle_system_prompt", "narrative_system_prompt",
    "sim_system_prompt", "shops_system_prompt", "stock_market_prompt",
    "auction_generate_prompt", "npc_life_plan_prompt",
    "npc_memory_summary_prompt", "npc_memory_hybrid_prompt",
    "npc_memory_batch_summary_prompt", "npc_memory_batch_hybrid_prompt",
    "story_arc_prompt",
)


@dataclass
class WorldSimPreset:
    """世界模拟全局配置（单例）。"""
    id: str = "world_sim_preset"
    # [提示词版本门] 见模块级 PROMPT_DEFAULTS_REV 注释；to_dict 写当前值供 from_dict 比对
    prompt_defaults_rev: int = PROMPT_DEFAULTS_REV
    # ---- LLM 角色 ----
    calculator_api_id: str = ""          # 结算 LLM（贵/强，JSON），v1 用于世界骨架生成
    narrative_api_id: str = ""           # 叙事 LLM（便宜/快），v1 存字段 Phase 2 用
    settle_system_prompt: str = DEFAULT_WORLDSIM_SETTLE_SYSTEM_PROMPT       # P2 场景结算意图 JSON
    narrative_system_prompt: str = DEFAULT_WORLDSIM_NARRATIVE_SYSTEM_PROMPT
    # [P19] 文风层（独立于规则层）：非空注入叙事 system 尾部【文风偏好】段；清空=不注入。
    # 默认 = A5 题材语体锚（各题材对话/旁白语体），用户可在设置里清空或自写。
    narrative_style_prompt: str = DEFAULT_NARRATIVE_STYLE_PROMPT
    calculator_temperature: float = 0.4
    calculator_max_tokens: int = 10000
    calculator_top_p: float = 0.9
    narrative_temperature: float = 0.8
    narrative_max_tokens: int = 10000
    narrative_top_p: float = 0.95
    # ---- 模拟旋钮（v1 存字段，Phase 3/4 接逻辑）----
    sim_enabled: bool = True
    economy_sim: str = "medium"
    faction_war: str = "medium"
    offscreen_npc_tick: bool = True
    reconcile_interval: int = 10         # 每 N 回合世界校准（0=关）
    sim_budget_per_tick: int = 8         # 每滴答 LLM 调用预算上限
    combat_system: str = "crpg"          # narrative | crpg
    crpg_granularity: str = "medium"     # light | medium | heavy
    difficulty: str = "normal"           # easy | normal | hard
    # ---- P4 世界滴答细分旋钮 ----
    economy_volatility: str = "medium"   # 经济波动幅度（白名单 _ECONOMY_SIM_VALUES）
    faction_war_lethality: str = "medium"  # 势力战致命度，控杂兵死亡率（要角不死）（白名单 _FACTION_WAR_VALUES）
    event_log_max: int = 200             # world.event_log 最大条数（超出截尾）
    max_events_per_tick: int = 3         # 单 tick 入库事件上限
    key_npc_decision_enabled: bool = True  # 要角 NPC 走 LLM 决策开关（关则全走规则）
    key_npc_budget: int = 3              # 每 tick 最多几个要角走 LLM（受 sim_budget_per_tick 总预算约束）
    # ---- P4 滴答 LLM（要角决策 + 世界校准）----
    sim_api_id: str = ""                 # 滴答 LLM（默认回退 calculator_api_id）
    sim_temperature: float = 0.3
    sim_max_tokens: int = 10000
    sim_system_prompt: str = DEFAULT_WORLDSIM_SIM_SYSTEM_PROMPT
    # ---- 生图勾选清单 ----
    image_enabled: bool = True
    image_world_banner: bool = True
    image_location_bg: bool = True
    image_npc_avatar: bool = True
    image_legendary_item: bool = True
    image_normal_item: bool = False
    image_event: bool = False
    image_style_prefix: str = ""         # 画风统一前缀（追加到生图 positive 前）
    image_skip_allowed: bool = True      # 创建时允许跳过生图
    # ---- 场景事件生图（场景交互循环内，关键叙事回合自动出图，P2）----
    # [!] 与 image_* (世界生成) 字段独立：场景生图由叙事 LLM 旁白里的 [img:...] 标签驱动，
    # 受 freq/max 限频；世界生图由 generate_world_images 在世界创建时批量出。两者互不干扰。
    # 0/False = 关闭/不限（受 LLM 主动控制）。
    narrative_image_event: bool = False       # 总开关
    narrative_image_freq: int = 3            # 每 N 回合允许一次（0=仅 LLM 主动控制）
    narrative_image_max_per_session: int = 0 # 单场景累计上限（0=不限）
    # ---- 世界生成默认 ----
    default_world_scale: str = "medium"  # small | medium | large（内置三档之一）
    # [P5b] 用户自定义规模档位（每项 dict: id/label/hint）。生成世界时下拉选项 =
    # 内置三档 + 用户自定义档。LLM 提示词按 ID 注入 hint，自定义档的 hint 字段就是
    # 用户给 LLM 的语义提示（如"微型沙盒：2地点3NPC"）。
    custom_world_scales: list[dict] = field(default_factory=list)
    # [P5c] 用户自定义属性/装备模板（每项 dict: id/label/stat_display_names/slot_display_names）。
    # 内置 6 模板（西幻/仙侠/武侠/现代/科幻/末日）走 BUILTIN_ATTRIBUTES_TEMPLATES 常量；
    # 自定义模板合并到列表尾。世界生成时 LLM 据 genre_tags 选 template_id，
    # UI 显示走 _per_world 读 stat_display_names/slot_display_names 题材化字段名。
    attribute_templates: list[dict] = field(default_factory=list)
    # [P5c] 玩家立绘生成开关（世界生成时是否调 ComfyUI 出玩家立绘）。
    # 默认 False 保持兼容（不影响现有生成流程）。
    image_player_avatar: bool = False
    # [2026-08-21] 怪物图预生成开关（世界生成时为题材怪物池 + 拓展新增物种批量出图，
    # 战斗 UI 只展示缓存不现场渲染）。默认 True（用户指示怪物要生图）。
    image_monster: bool = True
    # [住宅视觉] 建筑/家具图标预生成开关（building_/furniture_{题材}_{kind}.png，
    # HomeDialog 卡片图标；无图回退彩色徽章）。默认 True。
    image_home: bool = True
    # [2026-09-25 用户指示] 人物/怪物/宠物图的「纯色背景 -> 代码抠透明」：
    # 生图侧给这几类图追加纯色背景 tag（剥掉 LLM 自带的背景 tag 再统一追加）+ negative
    # 压制复杂背景，出图后由 image_cutout 把背景 flood fill 成透明 PNG（战斗立绘/卡片
    # 头像叠在场景图上更好看）。默认 True。失败静默保留原图，不影响出图主链路。
    # [!] 只作用于 tag 模式的人像/怪物/宠物图；场景背景/横幅/物品与各类图标不受影响。
    # [P47-C2 敌情预告 2026-09-25] 战斗中播报敌方主力本回合的锁定目标与威胁档
    # （Into the Breach 式信息先行：每回合从「掷骰看结果」变「看着明牌做选择」）。
    # 关闭则战斗 UI 不播报敌情（引擎规划幂等且零副作用，仅省日志）。
    combat_intents_enabled: bool = True
    image_cutout_enabled: bool = True
    image_cutout_bg: str = "white"       # 背景色 tag（白名单 CUTOUT_BG_COLORS，非法回退 white）
    # ---- P6 商店与经济系统旋钮 ----
    shops_enabled: bool = True                 # 总开关：关则世界无商店（商人不摆货）
    shops_restock_interval: int = 5            # 确定性补货间隔（tick）：每 N 回合货架向 max 靠拢
    # [删死旋钮 2026-09-10] 原 shops_price_volatility 全仓无任何消费方（UI 文案还写成
    # 「每次补货时补进多少货」，与字段名「价格浮动」自相矛盾）——旋钮改了没反应，违反
    # 「不做无功能展示」。真正生效的是 shops_price_drift（价格漂移）。
    shops_price_drift: str = "medium"          # [价格漂移] 货架价格随 tick 浮动幅度（off/light/medium/heavy）
    shops_llm_restock_enabled: bool = True
    # ---- [P14] 交易体验 + 经济节奏 ----
    trade_reply_enabled: bool = True             # 关店后店主对本次交易的 LLM 回应
    economy_pace: str = "standard"               # 经济节奏（relaxed/standard/hard/hardcore）     # LLM 重生成开关：开则过期的店走 LLM 全量换货，关则只确定性补货
    shops_wallclock_restock_minutes: int = 10  # 墙钟定时器：每 N 分钟扫描过期商店重生成（0=关）
    shops_system_prompt: str = DEFAULT_WORLDSIM_SHOP_SYSTEM_PROMPT  # 商店备货 LLM 提示词
    # [P34f] 交易所股市每日定价 LLM 提示词（tick_stock_market 每日 1 次调用）
    stock_market_prompt: str = DEFAULT_STOCK_MARKET_PROMPT
    # [P34g] 拍卖会拍品生成 LLM 提示词（start_auction 时调 1 次）
    auction_generate_prompt: str = DEFAULT_AUCTION_GENERATE_PROMPT
    # ---- P6c NPC 个人记忆旋钮 ----
    npc_memory_enabled: bool = True            # 总开关
    npc_memory_mode: str = "summary"           # summary | embedding_hybrid（白名单）
    npc_memory_interval: int = 5               # 每 N 次交互整理一次（取窗 = interval）
    npc_memory_top_k: int = 5                  # 召回注入上限（emb top-K 或最近 N）
    npc_memory_api_id: str = ""                # 记忆整理 LLM API（空=回退 calculator_api_id -> 首个 enabled）
    npc_memory_summary_prompt: str = DEFAULT_NPC_MEMORY_SUMMARY_PROMPT
    npc_memory_hybrid_prompt: str = DEFAULT_NPC_MEMORY_HYBRID_PROMPT
    npc_memory_batch_summary_prompt: str = DEFAULT_NPC_MEMORY_BATCH_SUMMARY_PROMPT  # [P29] 折叠时批量整理
    npc_memory_batch_hybrid_prompt: str = DEFAULT_NPC_MEMORY_BATCH_HYBRID_PROMPT    # [P29] 折叠时批量整理
    # ---- [P7g] 采集系统旋钮 ----
    gathering_enabled: bool = True              # 总开关：关则地点无资源点（不挂载/不结算）
    gathering_base_rate: float = 0.6            # 采集基础成功率（success_chance 的 base 参数）
    resource_cooldown_ticks: int = 3            # 资源点采完后冷却多少 tick 再生（0=不冷却）
    resource_richness_decay: int = 15           # 每次采集丰度递减量（丰度到 0 枯竭）
    # ---- [P7f] 地图拓展旋钮 ----
    map_expansion_enabled: bool = True          # 总开关：地图边界迷雾块可点击拓展（无限增殖沙盒）
    map_expansion_batch_size: int = 2           # 每次拓展生成几个新地点（1-5）
    map_expansion_item_count: int = 2           # [P11c] 每个新地点伴随生成几件新物品（0=关，1-10）
    map_expansion_recipe_count: int = 2         # [数据量] 每次拓展生成新配方目标数（0=关，0-10）
    map_expansion_dungeon_chance: float = 0.25  # [数据量] 新野外地点挂秘境入口概率（0-1，0=永不）
    map_expansion_monster_count: int = 1        # [数据量] 每次拓展新增怪物物种数（0=关，0-5）
    # ---- [P12] 资源/配方/怪物/技能旋钮 ----
    gen_recipe_count: int = 8                   # LLM 生成配方目标数量（钳 3-100；兜底配方也补到此数）
    item_gen_count: int = 12                    # [拆分生成 2026-08-25] 世界生成物品目标数量（钳 4-200）
    talent_pool_size: int = 10                  # 天赋池生成数量（钳 6-100）
    skill_pool_size: int = 10                    # [P13] 技能池生成数量（钳 6-100；每池技能配一本书）
    monster_pool_size: int = 8                  # [数据量] 世界生成怪物池目标数量（钳 6-100）
    wilderness_monsters_enabled: bool = True    # 野外地点怪物遭遇总开关
    wilderness_monster_chance: float = 0.25     # 野外每回合遇怪基础概率（+危险度加成，钳 0-1）
    elite_monster_chance: float = 0.12          # 精英怪概率（高级特殊怪：+2 级 + 掉落升档）
    npc_autonomous_enabled: bool = True         # NPC 离屏自主生活（打怪/采集/采购更新背包；升级走打猎结算 P45）
    # ---- [P34e] 探查/地图拓展概率（拓展新地点时 roll，SeededRng 确定性独立 salt）----
    expand_new_city_chance: float = 0.15        # 拓展新地点 roll 命中->强制变城市型聚落（含交易所/锻造屋/药店）
    expand_new_npc_chance: float = 0.25         # 拓展新地点 roll 命中->生成 NPC 挂此地点（题材兜底池纯引擎）
    expand_new_npc_count: int = 1               # 拓展命中时生成几个 NPC（0-10，配合 chance 门控）
    # [要角补档 2026-09-12] 拓展新 NPC 有多大概率是剧情要角（走 LLM 离屏决策 + 永不受势力战
    # 伤亡 + 死后不补员）。默认 0.25；0=拓展 NPC 全是非要角（旧口径）。
    expand_new_npc_key_chance: float = 0.25
    expand_new_faction_chance: float = 0.10     # 拓展新地点 roll 命中->生成 1 势力挂此地点
    expand_new_quest_chance: float = 0.20       # [P] 拓展新地点 roll 命中->生成 1 支线任务（探访新地点）
    # ---- [P34f] 交易所股市旋钮 ----
    stock_market_enabled: bool = True           # 股市总开关（city 交易所 venue 入口门控 + tick 定价）
    stock_price_drift_pct: float = 0.30         # 单日行情价格漂移幅度上限（±%，钳 0-1，防崩盘）
    # ---- [P34g] 拍卖会旋钮 ----
    auction_enabled: bool = True                # 拍卖会总开关（tick 周期生成 + city 入口门控）
    auction_interval_days: int = 5              # 拍卖会生成间隔（天，每 N 天一场新拍卖会）
    auction_duration_days: int = 3              # 拍卖会持续天数（start_day + N = end_day）
    auction_lot_count: int = 5                  # 每场拍卖会拍品数量
    # ---- [P7h] 回合制战斗旋钮 ----
    combat_player_controlled: bool = True       # 玩家可操作回合制战斗（false=兼容老一击制自动结算）
    combat_max_rounds: int = 30                 # 单场战斗回合上限（防死循环，超限脱战）
    combat_enemy_delay_ms: int = 600            # [2026-08-21 用户指示] 敌方行动前停顿（毫秒，交互节奏；0=立即结算）
    # ---- [P7i] 任务系统旋钮 ----
    quest_system_enabled: bool = True           # 总开关：结构化任务（接取/追踪/领奖）
    # ---- [P15a] 场景日志滚动总结 ----
    scene_summary_threshold: int = 20           # 场景日志超过此条数触发滚动总结（0=关）
    scene_summary_max_chars: int = 400          # 前情摘要字数上限（0=不限；注入总结提示词作硬约束）
    # ---- [P7k8] 永久死亡（roguelike 硬核模式）----
    permadeath_enabled: bool = False            # 玩家 HP 归 0 战斗失败后删除整个世界（回首页）
    # ---- [P7k1] 合成/炼制 ----
    crafting_enabled: bool = True               # 总开关：世界生成合成配方 + 合成台可用
    # ---- [P36a] NPC 生活模拟 ----
    npc_autonomy_enabled: bool = True        # 总开关：tick npc_life 子阶段 + NPC 自主行为
    npc_permadeath: bool = False             # [用户指示 2026-08-21] 开=所有 NPC 死亡不重生（含杂兵/商人，商店随之永久停摆）；关=现状（[P45 v3 2026-09-12] 要角/杂兵统一 8-14 tick 重生；主线发布人不死）
    npc_backfill_enabled: bool = True        # [P46 用户指示 2026-08-23] 开=npc_permadeath 下死者到岗期补员（商人接店铺/平民随机补位；敌对/要角不补）
    social_enabled: bool = True              # [P38a] NPC 社交演化（同地 NPC 结交/深交/结仇/和解）
    commissions_enabled: bool = True         # [P39b] 委托订单板（NPC 收购单：玩家生产交付赚钱）
    # [D7 2026-09-05] 据点每日随机遭遇敌袭概率（0-1；0=关）。
    domain_siege_chance: float = 0.08
    chronicle_enabled: bool = True           # [P39d+e] 剧情导演层 + 世界编年史
    errands_enabled: bool = True             # [P42c] 日常跑腿任务（城市传话/捎物小差事）
    npc_life_per_day: int = 6               # 每日活跃 NPC 数上限（轮换 roster）
    npc_life_llm_interval: int = 3          # 每 N 天一次 LLM 批量日计划（0=关，纯规则 AI）
    npc_life_plan_prompt: str = ""          # 空串回退 DEFAULT_NPC_LIFE_PLAN_PROMPT
    # [NPC 初始等级 2026-09-12 用户指示] 新建世界时要角（boss/hostile/friendly）的**初始**等级：
    # 1..npc_max_level 之间随机（默认 1-30），不再看所在地 danger。世界成长天花板是 100 级，
    # 但开局就 90+ 级让玩家的成长与追赶都失去意义（可玩性）；之后靠狩猎成长（P45 2026-09-12：打过怪的才涨经验，被动升级已删）
    # 缓慢升到 100。
    npc_max_level: int = 30
    # ---- [P35] 种植系统 ----
    farm_enabled: bool = True                   # 总开关：种子入世 + tick 推进 + 灵田可用
    # [饱食度 2026-09-06 用户指示] 总开关（默认关=保持现状）：开启后主城/据点铺食堂场所、
    # 题材食物入世（商店可进货）、玩家与 NPC 每日饱食度衰减；玩家低于饥饿线（30）全属性
    # x0.5，NPC 饿了自己去食堂堂食/吃背包食物。
    hunger_enabled: bool = False
    # [自然恢复 2026-09-06 用户指示] 每日自然恢复：跨日时玩家与存活 NPC 各回
    # hp_max 的 daily_hp_regen_pct%（默认 30；0=关；钳上限）。纯日结零 LLM，
    # 不乘饥饿系数（世界规则的「静养自愈」，与当日状态惩罚无关）。
    daily_hp_regen_pct: int = 30
    # [季节玩法化 2026-09-06 用户指示] 季节驱动经济/采集/遇怪/敌袭（默认开；纯推导零 LLM）：
    # 冬粮价上浮/资源恢复减半/野兽与饥匪更活跃，夏物产丰茂等——系数表见 calendar_engine._SEASON_EFFECTS。
    season_effects_enabled: bool = True
    # [物价联动 2026-09-10 用户指示] 本地势力财富档位影响货架价（默认开；纯引擎零 LLM）：
    # 势力穷 -> 买贵卖压、富 -> 买贱卖抬（tre.wealth_price_factors 七档）；build 写入
    # overlay "wealth_price"，缺键=关护老世界。任务/委托/势力战结算喂 wealth。
    faction_price_enabled: bool = True
    # [事件任务 2026-09-06 用户指示] major 世界事件自动派生后续任务（复国讨伐/货品托付/
    # 据点重建起步；每 7 天最多 1 条、在途 <=2；纯引擎零 LLM，文案题材模板）。
    event_quests_enabled: bool = True
    # ---- [剧情线 P46 2026-09-12 用户指示] 前瞻导演层（世界自演剧情线）----
    story_arcs_enabled: bool = True      # 总开关：规划（LLM/模板）+ 每日推进
    arc_interval_days: int = 5           # 规划节拍（天；每 N 天一次批量规划）
    arc_max_active: int = 4              # 常态并行 active 线上限（满编本轮不出线）
    arc_max_new: int = 2                 # 每轮规划至多采纳几条
    story_arc_prompt: str = ""           # 空串回退 DEFAULT_STORY_ARC_PROMPT
    # [旁听对话 2026-09-06 用户指示] 旁听升级：命中时生成两个 NPC 的完整对话（4-6 轮，
    # 1 次 LLM 约 1k token）存 NPC 档案（主页面只留一行轻提示）；失败回退旁白两句模式。
    overheard_dialog_enabled: bool = True
    farm_wither_days: int = 3                   # 连续多少天不浇水（且非雨天）作物枯萎（0=不枯萎）
    # ---- [P10] 同场景 NPC 主动行为（打招呼/送礼/氛围行动）----
    npc_reactions_enabled: bool = True          # 总开关：关则同场景 NPC 只被动存在，不主动反应
    npc_gift_chance: float = 0.25               # 高交情 NPC 谈话后送礼概率（0-1）
    # ---- [P10] 好友 + 私聊 ----
    friend_affinity_threshold: int = 60         # 交情达此值可添加好友（0-100）
    friend_chat_enabled: bool = True            # 好友私聊总开关
    # [A3 2026-08-28] 好友主动私聊：交情>=70 的好友低概率主动发私聊（LLM 生成落私聊记录，
    # 玩家可回）；与好友被动「托人带话」模板事件并存。
    friend_chat_proactive_enabled: bool = True
    # ---- [P10b] 同行（邀请 NPC 跟随移动，交情达门槛；点对话退队）----
    companions_enabled: bool = True             # 总开关：关则不可邀请同行
    companion_affinity_threshold: int = 50      # 邀请同行所需交情（0-100，低于好友门槛，高于助战）
    companion_max_followers: int = 2            # 最多同时几名 NPC 同行（0-3）
    # ---- [P10] 战斗同伴助战 + LLM 遭遇判定 ----
    combat_allies_enabled: bool = True          # 同场景高交情 NPC 可在玩家战斗时助战
    combat_max_allies: int = 2                  # 单场战斗最多几名同伴助战（0-4）
    combat_encounter_judge_llm: bool = True     # 遭遇规模（敌人数量）+ 助战人选由 LLM 判定（失败回退确定性）
    combat_max_enemies: int = 3                 # 单场战斗敌人上限（含主目标，1-5）
    # ---- [P25d] 世界 Boss 旋钮 ----
    world_boss_enabled: bool = True             # 总开关：tick crisis/major 事件高危险地点可生成世界 Boss
    world_boss_min_danger: int = 7              # 触发生成的地点最低危险度（1-10；越高越易出 Boss）
    world_boss_window_days: int = 10            # Boss 盘踞窗口天数（到期未击败则离去留传闻，3-30）
    # ---- [P26a] 结义/婚恋旋钮 ----
    relations_enabled: bool = True              # 总开关：关系阶段机（结义/恋人/配偶）+ 联手伤害
    sworn_damage_bonus: float = 1.10            # 结义联手伤害倍率（玩家+结义同伴同场各乘此值；1.0=无加成）
    # ---- [P27] 二层地图旋钮 ----
    places_enabled: bool = True                 # 总开关：地点内场所（关闭退化单层地图兼容）
    places_per_location: int = 4                # 每地点场所数（世界生成 + 拓展用；规模档覆盖）
    # ---- [P33] 隐藏/被动检定开关（进入秘境陷阱/宝藏房 + 地点到达时 roll 被动察觉）----
    hidden_check_enabled: bool = True
    # ---- [P34a] 器物体系旋钮（物品品阶档 + 商店等级上限，门控高 level 物品来源闭环）----
    # item_max_level: 物品 level 上限（生成处钳制；0=不启用 level 体系退化凡品世界）。
    # [数值对齐 2026-09-06] 默认 6 = 品阶档（白1绿2蓝3紫4橙5红6，与 _EQUIP_RARITY_LEVELS 同梯）——
    #   数值只随 rarity 走，level 高于 6 无强度支撑（旧默认 10 让 7-10 档沦为纯溢价档）。
    # shop_max_item_level: 商店备货 level 上限——高 level 物品绝不出现在任何商店，唯一来源 =
    #   掉落/锻造/拍卖会（用户定稿经济闭环）。3 = 商店最多卖 Lv3 物品，Lv4+ 只能肝/造/拍。
    item_max_level: int = 6
    shop_max_item_level: int = 3
    # ---- [P34c] 住所建设旋钮（住宅内建筑带等级，门控锻造/炼制/洗练高 level 物品）----
    # buildings_enabled: 总开关（关闭则无建筑系统，配方无建筑门控退化老口径）。
    # buildings_max_level: 建筑等级上限（建造/升级钳制；高 level 配方需高 level 建筑）。
    buildings_enabled: bool = True
    buildings_max_level: int = 5
    # ---- [P32] 同伴插话旋钮（引擎算"何时说"，LLM 写"说什么"）----
    companion_interject_enabled: bool = True       # 总开关：同伴在场景中偶尔插话（须同伴在场）
    companion_interject_interval: int = 5          # 同伴插话最小间隔（tick；防连续刷屏）
    # ---- [④ 2026-08-30] 共享世界知识分层（市井传闻）旋钮 ----
    rumor_enabled: bool = True                     # 总开关：major/crisis 事件口述化成传闻 + 按 NPC 过滤注入
    # ---- [⑦ 2026-08-30] 旁听 NPC-NPC 对话旋钮 ----
    overheard_enabled: bool = True                 # 总开关：场景中偶尔旁听到两名 NPC 闲聊
    overheard_chance: float = 0.20                 # 每回合旁听触发概率（0-1）
    overheard_cooldown_tick: int = 6               # 旁听最小间隔（tick；防连续刷屏）
    # ---- [⑥ 2026-08-30] NPC 人格演化旋钮 ----
    personality_evolution_enabled: bool = True     # 总开关：引擎判转折点 + LLM 批量写性格偏移
    personality_drift_interval: int = 12           # 批量性格偏移 LLM 的最小间隔（tick）+ 每 NPC 冷却
    default_genre_tags: list[str] = field(
        default_factory=lambda: list(_DEFAULT_GENRE_TAGS)
    )
    # [2026-09-13 用户指示] 新建世界聚落数量：0=自动（LLM 按世界观分配）；>0=注入提示词
    # 固定聚落数、其余为野外（野外保底 1 块——刷怪/采集阵地；引擎一致性收口另有硬兜底）。
    gen_settlement_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt_defaults_rev": self.prompt_defaults_rev,
            "calculator_api_id": self.calculator_api_id,
            "narrative_api_id": self.narrative_api_id,
            "settle_system_prompt": self.settle_system_prompt,
            "narrative_system_prompt": self.narrative_system_prompt,
            # [P19] 文风层：显式空串 = 用户清空禁用，to_dict 原样写空串（勿用 or 兜底）
            "narrative_style_prompt": self.narrative_style_prompt,
            "calculator_temperature": self.calculator_temperature,
            "calculator_max_tokens": self.calculator_max_tokens,
            "calculator_top_p": self.calculator_top_p,
            "narrative_temperature": self.narrative_temperature,
            "narrative_max_tokens": self.narrative_max_tokens,
            "narrative_top_p": self.narrative_top_p,
            "sim_enabled": self.sim_enabled,
            "economy_sim": self.economy_sim,
            "faction_war": self.faction_war,
            "offscreen_npc_tick": self.offscreen_npc_tick,
            "reconcile_interval": self.reconcile_interval,
            "sim_budget_per_tick": self.sim_budget_per_tick,
            "combat_system": self.combat_system,
            "crpg_granularity": self.crpg_granularity,
            "difficulty": self.difficulty,
            "economy_volatility": self.economy_volatility,
            "faction_war_lethality": self.faction_war_lethality,
            "event_log_max": self.event_log_max,
            "max_events_per_tick": self.max_events_per_tick,
            "key_npc_decision_enabled": self.key_npc_decision_enabled,
            "key_npc_budget": self.key_npc_budget,
            "sim_api_id": self.sim_api_id,
            "sim_temperature": self.sim_temperature,
            "sim_max_tokens": self.sim_max_tokens,
            "sim_system_prompt": self.sim_system_prompt,
            "image_enabled": self.image_enabled,
            "image_world_banner": self.image_world_banner,
            "image_location_bg": self.image_location_bg,
            "image_npc_avatar": self.image_npc_avatar,
            "image_legendary_item": self.image_legendary_item,
            "image_normal_item": self.image_normal_item,
            "image_event": self.image_event,
            "image_style_prefix": self.image_style_prefix,
            "image_skip_allowed": self.image_skip_allowed,
            "narrative_image_event": self.narrative_image_event,
            "narrative_image_freq": self.narrative_image_freq,
            "narrative_image_max_per_session": self.narrative_image_max_per_session,
            "default_world_scale": self.default_world_scale,
            "custom_world_scales": [dict(x) for x in (self.custom_world_scales or [])],
            "attribute_templates": [dict(x) for x in (self.attribute_templates or [])],
            "image_player_avatar": bool(self.image_player_avatar),
            "image_monster": bool(self.image_monster),
            "image_home": bool(self.image_home),
            "combat_intents_enabled": bool(self.combat_intents_enabled),
            "image_cutout_enabled": bool(self.image_cutout_enabled),
            "image_cutout_bg": self.image_cutout_bg,
            "default_genre_tags": list(self.default_genre_tags),
            "gen_settlement_count": max(0, int(self.gen_settlement_count or 0)),
            "shops_enabled": bool(self.shops_enabled),
            "shops_restock_interval": max(1, int(self.shops_restock_interval)),
            "shops_price_drift": self.shops_price_drift,
            "shops_llm_restock_enabled": bool(self.shops_llm_restock_enabled),
            "trade_reply_enabled": bool(self.trade_reply_enabled),
            "economy_pace": self.economy_pace if self.economy_pace in ("relaxed", "standard", "hard", "hardcore") else "standard",
            "shops_wallclock_restock_minutes": max(0, int(self.shops_wallclock_restock_minutes)),
            "shops_system_prompt": self.shops_system_prompt,
            # [P34f] 股市 prompt + 2 旋钮
            "stock_market_prompt": self.stock_market_prompt,
            "stock_market_enabled": bool(self.stock_market_enabled),
            "stock_price_drift_pct": max(0.0, min(1.0, float(self.stock_price_drift_pct))),
            # [P34g] 拍卖会
            "auction_generate_prompt": self.auction_generate_prompt,
            "auction_enabled": bool(self.auction_enabled),
            "auction_interval_days": max(1, int(self.auction_interval_days)),
            "auction_duration_days": max(1, int(self.auction_duration_days)),
            "auction_lot_count": max(1, int(self.auction_lot_count)),
            "npc_memory_enabled": bool(self.npc_memory_enabled),
            "npc_memory_mode": self.npc_memory_mode,
            "npc_memory_interval": max(1, int(self.npc_memory_interval)),
            "npc_memory_top_k": max(1, int(self.npc_memory_top_k)),
            "npc_memory_api_id": self.npc_memory_api_id,
            "npc_memory_summary_prompt": self.npc_memory_summary_prompt,
            "npc_memory_hybrid_prompt": self.npc_memory_hybrid_prompt,
            "npc_memory_batch_summary_prompt": self.npc_memory_batch_summary_prompt,
            "npc_memory_batch_hybrid_prompt": self.npc_memory_batch_hybrid_prompt,
            "gathering_enabled": bool(self.gathering_enabled),
            "gathering_base_rate": float(self.gathering_base_rate),
            "resource_cooldown_ticks": max(0, int(self.resource_cooldown_ticks)),
            "resource_richness_decay": max(1, int(self.resource_richness_decay)),
            "map_expansion_enabled": bool(self.map_expansion_enabled),
            "map_expansion_batch_size": max(1, min(5, int(self.map_expansion_batch_size))),
            "map_expansion_item_count": max(0, min(10, int(self.map_expansion_item_count))),
            "map_expansion_recipe_count": max(0, min(10, int(self.map_expansion_recipe_count))),
            "map_expansion_dungeon_chance": max(0.0, min(1.0, float(self.map_expansion_dungeon_chance))),
            "map_expansion_monster_count": max(0, min(5, int(self.map_expansion_monster_count))),
            "gen_recipe_count": max(3, min(100, int(self.gen_recipe_count))),
            "item_gen_count": max(4, min(200, int(self.item_gen_count))),
            "talent_pool_size": max(6, min(100, int(self.talent_pool_size))),
            "skill_pool_size": max(6, min(100, int(self.skill_pool_size))),
            "monster_pool_size": max(6, min(100, int(self.monster_pool_size))),
            "wilderness_monsters_enabled": bool(self.wilderness_monsters_enabled),
            "wilderness_monster_chance": max(0.0, min(1.0, float(self.wilderness_monster_chance))),
            "elite_monster_chance": max(0.0, min(1.0, float(self.elite_monster_chance))),
            "npc_autonomous_enabled": bool(self.npc_autonomous_enabled),
            # [P34e] 探查/地图拓展三概率旋钮（钳 0-1）
            "expand_new_city_chance": max(0.0, min(1.0, float(self.expand_new_city_chance))),
            "expand_new_npc_chance": max(0.0, min(1.0, float(self.expand_new_npc_chance))),
            "expand_new_npc_count": max(0, min(10, int(self.expand_new_npc_count))),
            "expand_new_npc_key_chance": max(0.0, min(1.0,
                                                      float(self.expand_new_npc_key_chance))),
            "expand_new_faction_chance": max(0.0, min(1.0, float(self.expand_new_faction_chance))),
            "expand_new_quest_chance": max(0.0, min(1.0, float(self.expand_new_quest_chance))),
            "combat_player_controlled": bool(self.combat_player_controlled),
            "combat_max_rounds": max(5, min(200, int(self.combat_max_rounds))),
            "combat_enemy_delay_ms": max(0, min(5000, int(self.combat_enemy_delay_ms))),
            "quest_system_enabled": bool(self.quest_system_enabled),
            "scene_summary_threshold": max(0, min(200, int(self.scene_summary_threshold))),
            "scene_summary_max_chars": max(0, min(2000, int(self.scene_summary_max_chars))),
            "permadeath_enabled": bool(self.permadeath_enabled),
            "crafting_enabled": bool(self.crafting_enabled),
            "farm_enabled": bool(self.farm_enabled),
            "hunger_enabled": bool(self.hunger_enabled),
            "daily_hp_regen_pct": max(0, min(100, int(self.daily_hp_regen_pct))),
            "season_effects_enabled": bool(self.season_effects_enabled),
            "faction_price_enabled": bool(self.faction_price_enabled),
            "event_quests_enabled": bool(self.event_quests_enabled),
            "story_arcs_enabled": bool(self.story_arcs_enabled),
            "arc_interval_days": max(1, min(30, int(self.arc_interval_days))),
            "arc_max_active": max(0, min(12, int(self.arc_max_active))),
            "arc_max_new": max(1, min(4, int(self.arc_max_new))),
            "story_arc_prompt": str(self.story_arc_prompt or ""),
            "overheard_dialog_enabled": bool(self.overheard_dialog_enabled),
            "farm_wither_days": max(0, min(10, int(self.farm_wither_days))),
            "npc_autonomy_enabled": bool(self.npc_autonomy_enabled),
            "npc_permadeath": bool(self.npc_permadeath),
            "npc_backfill_enabled": bool(self.npc_backfill_enabled),
            "social_enabled": bool(self.social_enabled),
            "commissions_enabled": bool(self.commissions_enabled),
            "domain_siege_chance": max(0.0, min(1.0, float(self.domain_siege_chance))),
            "chronicle_enabled": bool(self.chronicle_enabled),
            "errands_enabled": bool(self.errands_enabled),
            "npc_life_per_day": max(1, min(30, int(self.npc_life_per_day))),
            "npc_life_llm_interval": max(0, min(30, int(self.npc_life_llm_interval))),
            "npc_life_plan_prompt": str(self.npc_life_plan_prompt or ""),
            "npc_max_level": max(1, min(100, int(self.npc_max_level))),
            "npc_reactions_enabled": bool(self.npc_reactions_enabled),
            "npc_gift_chance": max(0.0, min(1.0, float(self.npc_gift_chance))),
            "friend_affinity_threshold": max(0, min(100, int(self.friend_affinity_threshold))),
            "friend_chat_enabled": bool(self.friend_chat_enabled),
            "friend_chat_proactive_enabled": bool(self.friend_chat_proactive_enabled),
            "companions_enabled": bool(self.companions_enabled),
            "companion_affinity_threshold": max(0, min(100, int(self.companion_affinity_threshold))),
            "companion_max_followers": max(0, min(3, int(self.companion_max_followers))),
            "combat_allies_enabled": bool(self.combat_allies_enabled),
            "combat_max_allies": max(0, min(4, int(self.combat_max_allies))),
            "combat_encounter_judge_llm": bool(self.combat_encounter_judge_llm),
            "combat_max_enemies": max(1, min(5, int(self.combat_max_enemies))),
            "world_boss_enabled": bool(self.world_boss_enabled),
            "world_boss_min_danger": max(1, min(10, int(self.world_boss_min_danger))),
            "world_boss_window_days": max(3, min(30, int(self.world_boss_window_days))),
            "places_enabled": bool(self.places_enabled),
            "places_per_location": max(0, min(10, int(self.places_per_location))),
            "hidden_check_enabled": bool(self.hidden_check_enabled),
            "item_max_level": max(0, int(self.item_max_level)),
            "shop_max_item_level": max(0, int(self.shop_max_item_level)),
            "buildings_enabled": bool(self.buildings_enabled),
            "buildings_max_level": max(0, int(self.buildings_max_level)),
            "companion_interject_enabled": bool(self.companion_interject_enabled),
            "companion_interject_interval": max(1, min(30, int(self.companion_interject_interval))),
            "rumor_enabled": bool(self.rumor_enabled),
            "overheard_enabled": bool(self.overheard_enabled),
            "overheard_chance": max(0.0, min(1.0, float(self.overheard_chance))),
            "overheard_cooldown_tick": max(1, min(30, int(self.overheard_cooldown_tick))),
            "personality_evolution_enabled": bool(self.personality_evolution_enabled),
            "personality_drift_interval": max(1, min(60, int(self.personality_drift_interval))),
        }

    def narrative_system_full(self) -> str:
        """叙事 system 完整内容 = 规则层 + 文风层（[P19]）。

        规则层（协议/一致性/感官/伤势，narrative_system_prompt）固定在前；
        文风层（narrative_style_prompt）非空时以【文风偏好】段追加——清空即不注入，
        用户改文风不再触碰协议规则。
        """
        base = self.narrative_system_prompt or DEFAULT_WORLDSIM_NARRATIVE_SYSTEM_PROMPT
        if self.narrative_style_prompt and self.narrative_style_prompt.strip():
            return base + "\n\n【文风偏好】\n" + self.narrative_style_prompt.strip()
        return base

    @classmethod
    def from_dict(cls, d: dict) -> "WorldSimPreset":
        if not d:
            return cls()

        # [提示词版本门] 旧 rev 存的提示词 = 过期旧默认值，丢弃让下方 or DEFAULT 回退现行默认。
        try:
            stored_rev = int(d.get("prompt_defaults_rev") or 1)
        except (TypeError, ValueError):
            stored_rev = 1
        if stored_rev < PROMPT_DEFAULTS_REV:
            d = {k: v for k, v in d.items() if k not in _PROMPT_FIELD_KEYS}

        def _pick(key: str, default: str, allowed: tuple) -> str:
            v = d.get(key, default) or default
            return v if v in allowed else default

        return cls(
            id=d.get("id", "world_sim_preset"),
            calculator_api_id=d.get("calculator_api_id", "") or "",
            narrative_api_id=d.get("narrative_api_id", "") or "",
            settle_system_prompt=d.get("settle_system_prompt", "") or DEFAULT_WORLDSIM_SETTLE_SYSTEM_PROMPT,
            narrative_system_prompt=d.get("narrative_system_prompt", "") or DEFAULT_WORLDSIM_NARRATIVE_SYSTEM_PROMPT,
            # [P19] 缺键（老预设）回退默认文风；显式空串保留空串（用户清空=禁用，勿 or 兜底）
            narrative_style_prompt=d.get("narrative_style_prompt", DEFAULT_NARRATIVE_STYLE_PROMPT),
            calculator_temperature=_sf(d, "calculator_temperature", 0.4),
            calculator_max_tokens=max(10000, _si(d, "calculator_max_tokens", 10000)),
            calculator_top_p=_sf(d, "calculator_top_p", 0.9),
            narrative_temperature=_sf(d, "narrative_temperature", 0.8),
            narrative_max_tokens=max(10000, _si(d, "narrative_max_tokens", 10000)),
            narrative_top_p=_sf(d, "narrative_top_p", 0.95),
            sim_enabled=bool(d.get("sim_enabled", True)),
            economy_sim=_pick("economy_sim", "medium", _ECONOMY_SIM_VALUES),
            faction_war=_pick("faction_war", "medium", _FACTION_WAR_VALUES),
            offscreen_npc_tick=bool(d.get("offscreen_npc_tick", True)),
            reconcile_interval=_si(d, "reconcile_interval", 10),
            sim_budget_per_tick=_si(d, "sim_budget_per_tick", 8),
            combat_system=_pick("combat_system", "crpg", _COMBAT_SYSTEM_VALUES),
            crpg_granularity=_pick("crpg_granularity", "medium", _CRPG_GRANULARITY_VALUES),
            difficulty=_pick("difficulty", "normal", _DIFFICULTY_VALUES),
            economy_volatility=_pick("economy_volatility", "medium", _ECONOMY_SIM_VALUES),
            faction_war_lethality=_pick("faction_war_lethality", "medium", _FACTION_WAR_VALUES),
            event_log_max=max(10, min(2000, _si(d, "event_log_max", 200))),
            max_events_per_tick=max(0, min(20, _si(d, "max_events_per_tick", 3))),
            key_npc_decision_enabled=bool(d.get("key_npc_decision_enabled", True)),
            key_npc_budget=max(0, min(20, _si(d, "key_npc_budget", 3))),
            sim_api_id=d.get("sim_api_id", "") or "",
            sim_temperature=_sf(d, "sim_temperature", 0.3),
            sim_max_tokens=max(10000, _si(d, "sim_max_tokens", 10000)),
            sim_system_prompt=d.get("sim_system_prompt", "") or DEFAULT_WORLDSIM_SIM_SYSTEM_PROMPT,
            image_enabled=bool(d.get("image_enabled", True)),
            image_world_banner=bool(d.get("image_world_banner", True)),
            image_location_bg=bool(d.get("image_location_bg", True)),
            image_npc_avatar=bool(d.get("image_npc_avatar", True)),
            image_legendary_item=bool(d.get("image_legendary_item", True)),
            image_normal_item=bool(d.get("image_normal_item", False)),
            image_event=bool(d.get("image_event", False)),
            image_style_prefix=d.get("image_style_prefix", "") or "",
            image_skip_allowed=bool(d.get("image_skip_allowed", True)),
            narrative_image_event=bool(d.get("narrative_image_event", False)),
            # [!] 场景生图频次钳制 [0, 100]：0=不限，正整数=每 N 回合一次。
            narrative_image_freq=max(0, min(100, _si(d, "narrative_image_freq", 3))),
            # 单场景累计上限钳制 [0, 999]：0=不限，正整数=上限。
            narrative_image_max_per_session=max(0, min(999, _si(d, "narrative_image_max_per_session", 0))),
            default_world_scale=_pick("default_world_scale", "medium", _SCALE_VALUES),
            # [P5b] 自定义规模档位：list[dict]，每项需带 id/label/hint，过滤掉字段不全的项。
            # 旧预设没此字段 -> 回退 []（仅显示内置三档，向后兼容）。
            custom_world_scales=_pick_custom_scales(d.get("custom_world_scales") or []),
            # [P5c] 自定义属性/装备模板：list[dict]，校验 id/label/stat_display_names/slot_display_names 完整性。
            attribute_templates=_pick_attribute_templates(d.get("attribute_templates") or []),
            image_player_avatar=bool(d.get("image_player_avatar", False)),
            image_monster=bool(d.get("image_monster", True)),
            image_home=bool(d.get("image_home", True)),
            combat_intents_enabled=bool(d.get("combat_intents_enabled", True)),
            image_cutout_enabled=bool(d.get("image_cutout_enabled", True)),
            image_cutout_bg=_pick("image_cutout_bg", "white", CUTOUT_BG_COLORS),
            default_genre_tags=list(d.get("default_genre_tags") or list(_DEFAULT_GENRE_TAGS)),
            gen_settlement_count=max(0, _si(d, "gen_settlement_count", 0)),
            shops_enabled=bool(d.get("shops_enabled", True)),
            shops_restock_interval=max(1, _si(d, "shops_restock_interval", 5)),
            shops_price_drift=_pick("shops_price_drift", "medium", _ECONOMY_SIM_VALUES),
            shops_llm_restock_enabled=bool(d.get("shops_llm_restock_enabled", True)),
            trade_reply_enabled=bool(d.get("trade_reply_enabled", True)),
            economy_pace=d.get("economy_pace", "standard") if d.get("economy_pace") in ("relaxed", "standard", "hard", "hardcore") else "standard",
            shops_wallclock_restock_minutes=max(0, _si(d, "shops_wallclock_restock_minutes", 10)),
            shops_system_prompt=d.get("shops_system_prompt", "") or DEFAULT_WORLDSIM_SHOP_SYSTEM_PROMPT,
            # [P34f] 股市 prompt + 2 旋钮
            stock_market_prompt=d.get("stock_market_prompt", "") or DEFAULT_STOCK_MARKET_PROMPT,
            stock_market_enabled=bool(d.get("stock_market_enabled", True)),
            stock_price_drift_pct=max(0.0, min(1.0, _sf(d, "stock_price_drift_pct", 0.30))),
            # [P34g] 拍卖会
            auction_generate_prompt=d.get("auction_generate_prompt", "") or DEFAULT_AUCTION_GENERATE_PROMPT,
            auction_enabled=bool(d.get("auction_enabled", True)),
            auction_interval_days=max(1, _si(d, "auction_interval_days", 5)),
            auction_duration_days=max(1, _si(d, "auction_duration_days", 3)),
            auction_lot_count=max(1, _si(d, "auction_lot_count", 5)),
            npc_memory_enabled=bool(d.get("npc_memory_enabled", True)),
            npc_memory_mode=_pick("npc_memory_mode", "summary", ("summary", "embedding_hybrid")),
            npc_memory_interval=max(1, _si(d, "npc_memory_interval", 5)),
            npc_memory_top_k=max(1, _si(d, "npc_memory_top_k", 5)),
            npc_memory_api_id=d.get("npc_memory_api_id", "") or "",
            npc_memory_summary_prompt=d.get("npc_memory_summary_prompt", "") or DEFAULT_NPC_MEMORY_SUMMARY_PROMPT,
            npc_memory_hybrid_prompt=d.get("npc_memory_hybrid_prompt", "") or DEFAULT_NPC_MEMORY_HYBRID_PROMPT,
            npc_memory_batch_summary_prompt=d.get("npc_memory_batch_summary_prompt", "") or DEFAULT_NPC_MEMORY_BATCH_SUMMARY_PROMPT,
            npc_memory_batch_hybrid_prompt=d.get("npc_memory_batch_hybrid_prompt", "") or DEFAULT_NPC_MEMORY_BATCH_HYBRID_PROMPT,
            gathering_enabled=bool(d.get("gathering_enabled", True)),
            gathering_base_rate=max(0.05, min(0.95, float(d.get("gathering_base_rate", 0.6) or 0.6))),
            resource_cooldown_ticks=max(0, _si(d, "resource_cooldown_ticks", 3)),
            resource_richness_decay=max(1, _si(d, "resource_richness_decay", 15)),
            map_expansion_enabled=bool(d.get("map_expansion_enabled", True)),
            map_expansion_batch_size=max(1, min(5, _si(d, "map_expansion_batch_size", 2))),
            map_expansion_item_count=max(0, min(10, _si(d, "map_expansion_item_count", 2))),
            map_expansion_recipe_count=max(0, min(10, _si(d, "map_expansion_recipe_count", 2))),
            map_expansion_dungeon_chance=max(0.0, min(1.0, _sf(d, "map_expansion_dungeon_chance", 0.25))),
            map_expansion_monster_count=max(0, min(5, _si(d, "map_expansion_monster_count", 1))),
            gen_recipe_count=max(3, min(100, _si(d, "gen_recipe_count", 8))),
            item_gen_count=max(4, min(200, _si(d, "item_gen_count", 12))),
            talent_pool_size=max(6, min(100, _si(d, "talent_pool_size", 10))),
            skill_pool_size=max(6, min(100, _si(d, "skill_pool_size", 10))),
            monster_pool_size=max(6, min(100, _si(d, "monster_pool_size", 8))),
            wilderness_monsters_enabled=bool(d.get("wilderness_monsters_enabled", True)),
            wilderness_monster_chance=max(0.0, min(1.0, _sf(d, "wilderness_monster_chance", 0.25))),
            elite_monster_chance=max(0.0, min(1.0, _sf(d, "elite_monster_chance", 0.12))),
            npc_autonomous_enabled=bool(d.get("npc_autonomous_enabled", True)),
            # [P34e] 探查/地图拓展三概率旋钮（钳 0-1）
            expand_new_city_chance=max(0.0, min(1.0, _sf(d, "expand_new_city_chance", 0.15))),
            expand_new_npc_chance=max(0.0, min(1.0, _sf(d, "expand_new_npc_chance", 0.25))),
            expand_new_npc_count=max(0, min(10, _si(d, "expand_new_npc_count", 1))),
            expand_new_npc_key_chance=max(0.0, min(1.0,
                                                   _sf(d, "expand_new_npc_key_chance", 0.25))),
            expand_new_faction_chance=max(0.0, min(1.0, _sf(d, "expand_new_faction_chance", 0.10))),
            expand_new_quest_chance=max(0.0, min(1.0, _sf(d, "expand_new_quest_chance", 0.20))),
            combat_player_controlled=bool(d.get("combat_player_controlled", True)),
            combat_max_rounds=max(5, min(200, _si(d, "combat_max_rounds", 30))),
            combat_enemy_delay_ms=max(0, min(5000, _si(d, "combat_enemy_delay_ms", 600))),
            quest_system_enabled=bool(d.get("quest_system_enabled", True)),
            scene_summary_threshold=max(0, min(200, _si(d, "scene_summary_threshold", 20))),
            scene_summary_max_chars=max(0, min(2000, _si(d, "scene_summary_max_chars", 400))),
            permadeath_enabled=bool(d.get("permadeath_enabled", False)),
            crafting_enabled=bool(d.get("crafting_enabled", True)),
            farm_enabled=bool(d.get("farm_enabled", True)),
            hunger_enabled=bool(d.get("hunger_enabled", False)),
            daily_hp_regen_pct=max(0, min(100, int(d.get("daily_hp_regen_pct", 30) or 0))),
            season_effects_enabled=bool(d.get("season_effects_enabled", True)),
            faction_price_enabled=bool(d.get("faction_price_enabled", True)),
            event_quests_enabled=bool(d.get("event_quests_enabled", True)),
            story_arcs_enabled=bool(d.get("story_arcs_enabled", True)),
            arc_interval_days=max(1, min(30, _si(d, "arc_interval_days", 5))),
            arc_max_active=max(0, min(12, _si(d, "arc_max_active", 4))),
            arc_max_new=max(1, min(4, _si(d, "arc_max_new", 2))),
            story_arc_prompt=d.get("story_arc_prompt", "") or "",
            overheard_dialog_enabled=bool(d.get("overheard_dialog_enabled", True)),
            farm_wither_days=max(0, min(10, _si(d, "farm_wither_days", 3))),
            npc_autonomy_enabled=bool(d.get("npc_autonomy_enabled", True)),
            npc_permadeath=bool(d.get("npc_permadeath", False)),
            npc_backfill_enabled=bool(d.get("npc_backfill_enabled", True)),
            social_enabled=bool(d.get("social_enabled", True)),
            commissions_enabled=bool(d.get("commissions_enabled", True)),
            domain_siege_chance=max(0.0, min(1.0, _sf(d, "domain_siege_chance", 0.08))),
            chronicle_enabled=bool(d.get("chronicle_enabled", True)),
            errands_enabled=bool(d.get("errands_enabled", True)),
            npc_life_per_day=max(1, min(30, _si(d, "npc_life_per_day", 6))),
            npc_life_llm_interval=max(0, min(30, _si(d, "npc_life_llm_interval", 3))),
            npc_life_plan_prompt=d.get("npc_life_plan_prompt", "") or DEFAULT_NPC_LIFE_PLAN_PROMPT,
            npc_max_level=max(1, min(100, _si(d, "npc_max_level", 30))),
            npc_reactions_enabled=bool(d.get("npc_reactions_enabled", True)),
            npc_gift_chance=max(0.0, min(1.0, float(d.get("npc_gift_chance", 0.25) or 0.25))),
            friend_affinity_threshold=max(0, min(100, _si(d, "friend_affinity_threshold", 60))),
            friend_chat_enabled=bool(d.get("friend_chat_enabled", True)),
            friend_chat_proactive_enabled=bool(d.get("friend_chat_proactive_enabled", True)),
            companions_enabled=bool(d.get("companions_enabled", True)),
            companion_affinity_threshold=max(0, min(100, _si(d, "companion_affinity_threshold", 50))),
            companion_max_followers=max(0, min(3, _si(d, "companion_max_followers", 2))),
            combat_allies_enabled=bool(d.get("combat_allies_enabled", True)),
            combat_max_allies=max(0, min(4, _si(d, "combat_max_allies", 2))),
            combat_encounter_judge_llm=bool(d.get("combat_encounter_judge_llm", True)),
            combat_max_enemies=max(1, min(5, _si(d, "combat_max_enemies", 3))),
            world_boss_enabled=bool(d.get("world_boss_enabled", True)),
            world_boss_min_danger=max(1, min(10, _si(d, "world_boss_min_danger", 7))),
            world_boss_window_days=max(3, min(30, _si(d, "world_boss_window_days", 10))),
            places_enabled=bool(d.get("places_enabled", True)),
            places_per_location=max(0, min(10, _si(d, "places_per_location", 4))),
            hidden_check_enabled=bool(d.get("hidden_check_enabled", True)),
            item_max_level=max(0, _si(d, "item_max_level", 6)),
            shop_max_item_level=max(0, _si(d, "shop_max_item_level", 3)),
            buildings_enabled=bool(d.get("buildings_enabled", True)),
            buildings_max_level=max(0, _si(d, "buildings_max_level", 5)),
            companion_interject_enabled=bool(d.get("companion_interject_enabled", True)),
            companion_interject_interval=max(1, min(30, _si(d, "companion_interject_interval", 5))),
            rumor_enabled=bool(d.get("rumor_enabled", True)),
            overheard_enabled=bool(d.get("overheard_enabled", True)),
            overheard_chance=max(0.0, min(1.0, _sf(d, "overheard_chance", 0.20))),
            overheard_cooldown_tick=max(1, min(30, _si(d, "overheard_cooldown_tick", 6))),
            personality_evolution_enabled=bool(d.get("personality_evolution_enabled", True)),
            personality_drift_interval=max(1, min(60, _si(d, "personality_drift_interval", 12))),
        )


def default_world_sim_preset() -> WorldSimPreset:
    return WorldSimPreset()
