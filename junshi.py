#!/usr/bin/env python3
"""军师 prompt 装配：加载 goutoujunshi skill，按需检索参考知识，维护对话上下文。

设计原则（照抄 skill 自己的规矩）：
- 默认只加载当前问题直接需要的 1-3 份参考，不批量灌整个知识库。
- 上下文按好友隔离，只保留有限轮数，不无限累积。
- 记忆（关系档案）默认不启用，且必须由用户明确同意。

referenced skill: goutoujunshi (github.com/shengjidaguai-china/goutoujunshi)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# ---------------------------------------------------------------- 路径

SKILL_NAME = "goutoujunshi"


def find_skill_dir() -> Path:
    """自动定位 goutoujunshi skill 目录。

    查找顺序：
    1. 环境变量 GOUTOUJUNSHI_SKILL_DIR
    2. DSH 用户技能目录（DSH_HOME/skills/<name>）
    3. 常见的 DSH 桌面版数据目录
    4. 项目旁边的 skills/<name>
    """
    candidates: list[Path] = []

    env_dir = os.environ.get("GOUTOUJUNSHI_SKILL_DIR")
    if env_dir:
        candidates.append(Path(env_dir))

    dsh_home = os.environ.get("DSH_HOME")
    if dsh_home:
        candidates.append(Path(dsh_home) / "skills" / SKILL_NAME)

    home = Path.home()
    candidates += [
        home / ".dsh" / "skills" / SKILL_NAME,
        home / ".agents" / "skills" / SKILL_NAME,
        home / ".codex" / "skills" / SKILL_NAME,
        Path(os.environ.get("APPDATA", home)) / "dsh-desktop" / "harness" / "skills" / SKILL_NAME,
        Path(__file__).resolve().parent / "skills" / SKILL_NAME,
    ]

    for path in candidates:
        try:
            if (path / "SKILL.md").exists():
                return path
        except OSError:
            continue

    raise FileNotFoundError(
        "找不到 goutoujunshi skill。请设置环境变量 GOUTOUJUNSHI_SKILL_DIR，"
        "或把 skill 仓库克隆到下列任一位置：\n  "
        + "\n  ".join(str(c) for c in candidates)
    )

# 微信场景附加在 SKILL.md 之前的角色说明。
# 目的：把"顾问对用户说话"切换成"直接生成可发送的微信回复"，同时保留 skill 的安全边界。
WECHAT_PERSONA = """你现在通过微信替用户自动回复消息。

## 输出格式（必须严格遵守）

只输出一个 JSON 对象，不要有任何其他文字、不要用 Markdown 代码块包裹：

{"messages": ["要发出去的第一条"], "skip": false}

- `messages`：字符串数组，每个元素就是**对方手机上会收到的完整一行字**。
- `skip`：true 表示这条消息不需要回复（此时 messages 必须是空数组）。

示例：

对方发来「在吗」        -> {"messages": ["在"], "skip": false}
对方发来「她三天没回我」  -> {"messages": ["在忙吗？"], "skip": false}
对方发来「嗯」          -> {"messages": [], "skip": true}
对方发来「我们算在一起了吗」-> {"messages": ["我们这算在一起了吗"], "skip": false}
对方发来「周末有空吗」    -> {"messages": ["有空啊", "想去哪"], "skip": false}
对方连发「在吗」「我很难受」「他把我删了」
                      -> {"messages": ["在", "怎么了，出什么事了", "你先别急，慢慢说"], "skip": false}
对方发来「我好累啊」    -> {"messages": ["怎么了", "跟我说说，是工作还是别的"], "skip": false}

## 消息怎么写才像真人（最重要）

1. **messages 里绝对不能出现对你自己的指令或建议**。禁止出现"发个轻的""别追问""等她回"
   "先别发""观察一下""看反应"这类**说给用户听的话**——这些是策略，不是消息。
   判断方法：把这句话想象成对方手机屏幕上的一行，如果对方看了会觉得莫名，就是错的。
2. **条数按需要定，不设上限**。核心标准是：把对方说的每一件事都回应到。
   - 对方只发一句话 → 通常 1 条，20 字以内
   - 对方连发好几条 / 问了几件事 → 该发几条就发几条（2-4 条都正常），
     每条聚焦一件事，不要把好几件事挤成一大段
   - 不要为了凑条数把一句话拆碎，也不要为了简短而漏掉对方问的事
3. **要有来有回，主动推进话题**。不要只做被动的应答机器：
   - 回应完对方之后，可以自然地反问一句、接一个新的相关话题、
     或者分享一点自己的状态，让对话能继续下去
   - 例如对方说"我好累啊"，回"怎么了，跟我说说"就比只回"哦，那你休息"好
   - 但也不要硬找话题、不要每条都反问，那样很假。看语境，该收就收
4. 用口语短句。说"行""好嘞""哈哈"比说"可以""同意"更像真人。
5. 不要用分析口吻：禁止"单看这一件事""这说明""本质上""其实你可以"。
6. 不要用破折号做转折，不要用省略号拖长音，不要加括号补充说明，结尾不要用冒号。
7. 不要暴露你是 AI、机器人、顾问，不要出现"军师""策略""话术"这类词。
8. 危机信号（自伤、伤人、人身威胁、家暴）：不要出主意，输出关心的话并建议联系可信的人或当地紧急服务。

## skip 的判定标准（不要滥用）

只有在**对方这条消息完全不需要回应**时才 skip：纯表情、纯广告、单字"嗯/哦/好"、
以及一段对话自然的收尾语。

**绝对不要 skip 这些情况**——对方在问你问题、在分享一件事、在表达情绪，都必须回。
skip 错了等于机器人装死，比回错更糟：

- 对方问"我该怎么回""怎么办""该不该""你觉得呢" → 在要建议，必须回
- 对方讲了经历或感受（如"他昨天送我回家但一直玩手机"） → 必须回
- 对方问关系状态（如"我们算在一起了吗"） → 必须回
- 不确定该不该回时，**默认回复**，不要 skip

## 重要：你是在替对方说话，不是在给建议

你输出的 messages 是**发给微信对面那个人**的。所以当对方问你"我该怎么回她"时，
你要把他**该发出去的那句话**放进 messages
（例如 {"messages": ["在忙吗？"], "skip": false}），而不是给他分析或方案。

## 内部判断（不要写进 JSON 的任何字段）
在生成回复前先在心里完成：情绪落地 → 事实拆分 → 利益判断 → 明确建议 → 行动收束。
这部分只影响你往 messages 里放哪句话，绝不输出。

判断结果只决定**发什么**，不决定**说什么**。比如分析出"对方还在观望"，
正确的输出是 {"messages": ["在忙吗？"], "skip": false}，
而不是把"别追问，等她回"这种判断当成消息。

## 边界
- 不协助操控、PUA、贬低、服从测试、煤气灯、跟踪、性胁迫。
- 不假装是对方认识的具体某个人，也不冒充用户本人做承诺（钱、见面、法律、医疗之类）。
- 不确定的事实不要编。用"我不太确定"比编一个细节好。

下面是军师 skill 的完整工作规范，作为你的判断依据：
"""


# ---------------------------------------------------------------- 参考检索
# 来自 SKILL.md 的「按需加载」表，转成关键词命中规则。
# 顺序无关；命中后按 REFERENCE_LIMIT 截断。
REFERENCE_RULES: list[tuple[tuple[str, ...], list[str]]] = [
    (
        ("怎么回", "回复", "怎么答", "开场", "第一句", "打招呼", "邀约", "约她", "约他",
         "话术", "怎么说", "发什么", "回消息", "回复我", "已读不回"),
        ["references/practical/实战话术编排器：从一句回复到后续分支.md"],
    ),
    (
        ("尴尬", "冷场", "没话说", "松弛", "聊不下去", "接话", "气氛"),
        ["references/practical/场景感、松弛感与社交校准：从接话到关系推进.md"],
    ),
    (
        ("冷读", "推拉", "pua", "PUA", "服从测试", "贬低", "煤气灯", "打压"),
        ["references/knowledge/05-PUA操控与伦理替代.md"],
    ),
    (
        ("自然流", "blueprint", "Blueprint", "inner game", "内在状态", "mystery", "Mystery"),
        [
            "references/knowledge/20-经典社交体系的机制、证据与风险边界.md",
            "references/practical/自然流、内在状态与结构化互动：伦理能力转译.md",
        ],
    ),
    (
        ("截图", "网聊", "探探", "soul", "Soul", "陌陌", "网友", "诈骗", "杀猪盘", "隐私"),
        ["references/knowledge/09-在线约会与数字关系.md"],
    ),
    (
        ("焦虑", "依恋", "回避型", "安全型", "情绪调节", "患得患失", "没安全感"),
        ["references/knowledge/03-依恋理论与情绪调节.md"],
    ),
    (
        ("MBTI", "mbti", "INTJ", "INFP", "ENFP", "ISFJ", "人格", "性格类型"),
        ["references/knowledge/04-MBTI人格与匹配.md"],
    ),
    (
        ("吵架", "冲突", "冷战", "修复", "道歉", "和好", "闹矛盾", "误会"),
        ["references/knowledge/07-沟通冲突与修复.md"],
    ),
    (
        ("同意", "边界", "亲密", "性", "上床", "拒绝他", "拒绝她", "身体"),
        ["references/knowledge/08-同意边界性与亲密.md"],
    ),
    (
        ("第一次见面", "见面", "约会", "牵手", "肢体接触", "表白", "主动表达"),
        ["references/practical/主动表达、第一次见面与自然接触.md"],
    ),
    (
        ("投入", "不回我", "冷淡", "降级", "退出", "放手", "备胎", "暧昧不清", "不主动"),
        ["references/practical/关系投入失衡：互惠判断、降级投入与退出决策.md"],
    ),
    (
        ("分手", "复合", "出轨", "背叛", "挽回"),
        ["references/knowledge/15-分手背叛与关系修复.md"],
    ),
    (
        ("结婚", "婚姻", "彩礼", "离婚", "家务", "育儿", "婆媳", "双方家庭", "钱"),
        ["references/knowledge/12-金钱家务育儿与双方家庭.md"],
    ),
    (
        ("家暴", "跟踪", "胁迫", "威胁", "违法", "法律", "报警", "自杀", "危机", "人身安全"),
        ["references/knowledge/17-中国法律安全与危机转介.md"],
    ),
    (
        ("情绪价值", "安慰", "难过", "不开心", "陪我"),
        ["references/practical/为他人提供情绪价值：温暖且有效的回应指南.md"],
    ),
    (
        ("夸", "赞美", "彩虹屁"),
        ["references/practical/万能夸人的话术技巧：真诚认可的实用指南.md"],
    ),
    (
        ("拒绝", "不想去", "推掉", "婉拒"),
        ["references/practical/高情商拒绝他人：体面护边界的实用指南.md"],
    ),
]

REFERENCE_LIMIT = 3
REFERENCE_CHAR_CAP = 26000
PER_FILE_CHAR_CAP = 12000


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def strip_frontmatter(markdown: str) -> str:
    """去掉 SKILL.md 顶部的 YAML frontmatter，只留正文。"""
    if markdown.startswith("---"):
        parts = markdown.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n")
    return markdown


class JunshiPrompt:
    """加载一次，反复复用；参考知识按每条消息的关键词动态挑选。"""

    def __init__(self, skill_dir: Path | str | None = None) -> None:
        self.skill_dir = Path(skill_dir) if skill_dir else find_skill_dir()
        self.skill_md = self.skill_dir / "SKILL.md"
        if not self.skill_md.exists():
            raise FileNotFoundError(f"找不到 skill 文件：{self.skill_md}")
        self.base = WECHAT_PERSONA + "\n" + strip_frontmatter(_read_text(self.skill_md))
        self._cache: dict[str, str] = {}

    def _reference(self, rel: str) -> str | None:
        if rel in self._cache:
            return self._cache[rel]
        path = self.skill_dir / rel
        if not path.exists():
            return None
        text = _read_text(path)
        if len(text) > PER_FILE_CHAR_CAP:
            text = text[:PER_FILE_CHAR_CAP] + "\n\n【该文件过长，此处截断】"
        self._cache[rel] = text
        return text

    def pick_references(self, message: str) -> list[str]:
        """按关键词挑 1-3 份最相关的参考文件。"""
        hits: list[str] = []
        for keywords, files in REFERENCE_RULES:
            if any(k in message for k in keywords):
                for f in files:
                    if f not in hits:
                        hits.append(f)
            if len(hits) >= REFERENCE_LIMIT:
                break
        return hits[:REFERENCE_LIMIT]

    def build_system_prompt(self, message: str) -> str:
        blocks = [self.base]
        picked = self.pick_references(message)
        total = 0
        loaded: list[str] = []
        for rel in picked:
            text = self._reference(rel)
            if text is None:
                continue
            if total + len(text) > REFERENCE_CHAR_CAP:
                break
            total += len(text)
            loaded.append(rel)
            blocks.append(f"\n\n===== 参考资料：{rel} =====\n{text}")
        if loaded:
            blocks.append("\n\n（以上参考资料仅供你判断使用，不要向对方提及。）")
        return "".join(blocks)

    def loaded_references(self, message: str) -> list[str]:
        picked = self.pick_references(message)
        return [r for r in picked if (self.skill_dir / r).exists()]


# ---------------------------------------------------------------- 对话上下文


class HistoryStore:
    """按好友隔离的滚动对话记录。只存最近 N 轮，不无限累积。"""

    def __init__(self, path: Path | str, turns: int = 12) -> None:
        self.path = Path(path)
        self.turns = max(1, int(turns))
        self.data: dict[str, list[dict[str, str]]] = {}
        if self.path.exists():
            try:
                self.data = json.loads(_read_text(self.path))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, friend: str) -> list[dict[str, str]]:
        return list(self.data.get(friend, []))

    def append(self, friend: str, role: str, content: str) -> None:
        bucket = self.data.setdefault(friend, [])
        bucket.append({"role": role, "content": content})
        # 一轮 = user + assistant，保留 turns 轮
        limit = self.turns * 2
        if len(bucket) > limit:
            del bucket[: len(bucket) - limit]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    def clear(self, friend: str | None = None) -> None:
        if friend is None:
            self.data = {}
        else:
            self.data.pop(friend, None)
        self.save()


# ---------------------------------------------------------------- 回复清洗

SKIP_TOKEN = "SKIP"


def clean_reply(raw: str, max_chars: int) -> str:
    """把模型输出整理成能直接发微信的文本，返回空串表示不回复。"""
    text = (raw or "").strip()
    if not text:
        return ""

    # 模型可能带 think 段（deepseek 推理模型）
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.S | re.I)
    text = text.strip()

    if text.upper().replace("`", "").strip() == SKIP_TOKEN:
        return ""

    # 去掉整段被引号包裹的情况
    if len(text) > 1 and text[0] in "\"“「'" and text[-1] in "\"”」'":
        inner = text[1:-1].strip()
        if inner:
            text = inner

    # 去掉常见的顾问包装词（模型偶尔会漏出来，发出去就暴露了）
    wrappers = [
        r"^(你可以这样回|建议回复|回复如下|可以这样回|这样回|要不要发|可以发|不如发|试着发|"
        r"建议你|你可以|你可以试试|或许可以|要不要试试|可以考虑|不妨)\s*[:：]?\s*",
        r"^(比如|例如|像这样|类似这样)\s*[:：]\s*",
        r"^\s*比如\s*[:：]\s*",
    ]
    for pattern in wrappers:
        new_text = re.sub(pattern, "", text).strip()
        # 只在确实剥掉了东西、且剩下内容还像一句完整的话时才采用
        if new_text and new_text != text and len(new_text) >= 2:
            text = new_text

    # 包装词不在开头的情况：真话在"比如：""例如："后面，取最后一段
    # 例："嗯…要不要发个轻松点的？比如：最近忙啥呢？" -> "最近忙啥呢？"
    marker = re.search(r"(?:比如|例如|像这样|类似这样)\s*[:：]\s*", text)
    if marker and marker.start() > 0:
        candidate = text[marker.end():].strip()
        # 只在剩下的是像样的一句话、且原文确实冗长时才替换
        if len(candidate) >= 4 and len(candidate) < len(text):
            text = candidate

    # 剥掉包装词后可能留下悬空标点
    text = re.sub(r"^[，,、:：；;]\s*", "", text).strip()

    # 去掉残留的引号包裹（包装词剥掉后可能露出引号）
    if len(text) > 1 and text[0] in "\"“「'" and text[-1] in "\"”」'":
        inner = text[1:-1].strip()
        if inner:
            text = inner

    # 去掉 markdown 标题/加粗/列表标记
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}[-*+]\s+", "", text)

    text = text.strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def split_messages(text: str, max_chars: int) -> list[str]:
    """按 `---` 分隔成多条微信消息。"""
    if not text:
        return []
    parts = re.split(r"(?m)^\s*-{3,}\s*$", text)
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        while len(part) > max_chars:
            out.append(part[:max_chars])
            part = part[max_chars:]
        if part:
            out.append(part)
    return out


# ---------------------------------------------------------------- 输出校验

# 这些词说明模型在"对用户下指令"，而不是在写要发出去的消息
META_MARKERS = (
    "发个", "发条", "别追问", "别急", "等她回", "等他回", "先别", "先观察",
    "观察一下", "看反应", "看回复", "试探", "稳住", "别秒回", "晾", "冷一冷",
    "单看", "这说明", "本质上", "其实你可以", "建议你", "你可以这样", "话术",
    "军师", "策略", "第一步", "下一步",
)


def is_plausible_message(text: str) -> bool:
    """判断一段文本像不像"能直接发给对方的微信消息"。

    这不是完美的判断，只用来拦截最明显的顾问口吻。宁可放过，不可错杀。
    """
    if not text or len(text) < 1:
        return False
    if len(text) > 500:
        return False
    # 以冒号结尾 = 后面本来还要接内容，是"提示词"不是消息
    if text.rstrip().endswith(("：", ":")):
        return False
    lowered = text
    for marker in META_MARKERS:
        if marker in lowered:
            return False
    return True


# 一眼就没信息量的消息：纯语气词、纯符号、纯表情、纯数字
# 这类消息不该回。靠模型判断不稳定（实测同一个"嗯"有时回有时不回），
# 所以在调用模型之前就确定性地挡掉。
_NO_REPLY_EXACT = {
    "嗯", "嗯嗯", "哦", "哦哦", "噢", "啊", "呀", "诶", "唉", "哎",
    "好", "好的", "好滴", "好嘞", "好吧", "行", "行吧", "可以", "中",
    "是", "是的", "对", "对的", "没错", "收到", "了解", "知道了",
    "哈哈", "哈哈哈", "呵呵", "嘿嘿", "嘻嘻", "笑死",
    "。。", "。。。", "...", "…", "?", "？", "!", "！",
    "ok", "OK", "Ok", "okay", "sure", "yes", "no",
    "1", "11", "111", "6", "66", "666",
}

# 纯表情/符号/空白
_NO_REPLY_PATTERN = re.compile(r"^[\s\W_]+$", re.UNICODE)


def needs_no_reply(message: str) -> bool:
    """这条消息是否明显不需要回复。在调用模型前做确定性判断，省一次 API 调用。"""
    text = (message or "").strip()
    if not text:
        return True
    if len(text) > 12:
        return False
    if text in _NO_REPLY_EXACT:
        return True
    # 纯符号/表情/空白（不含任何文字或数字）
    if _NO_REPLY_PATTERN.match(text):
        return True
    return False


def parse_model_output(raw: str, max_chars: int) -> tuple[list[str], bool]:
    """解析模型输出，返回 (消息列表, 是否解析成功)。

    正常情况模型返回 {"messages": [...], "skip": bool}。
    如果它没按格式来，退回旧式纯文本解析，尽量不让好友收不到回复。
    """
    text = (raw or "").strip()
    if not text:
        return [], True

    # 剥掉可能的 Markdown 代码块包裹
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, flags=re.S | re.I)
    if fence:
        text = fence.group(1).strip()

    # 去掉推理段
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.S | re.I).strip()

    payload = None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # 尝试截取第一个 { 到最后一个 }
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                payload = json.loads(text[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                payload = None

    if isinstance(payload, dict) and ("messages" in payload or "skip" in payload):
        if payload.get("skip") is True:
            return [], True

        raw_msgs = payload.get("messages")
        if isinstance(raw_msgs, str):
            raw_msgs = [raw_msgs]
        if not isinstance(raw_msgs, list):
            return [], False

        cleaned: list[str] = []
        for item in raw_msgs:
            if not isinstance(item, str):
                continue
            msg = clean_reply(item, max_chars)
            if msg and is_plausible_message(msg):
                for chunk in split_messages(msg, max_chars):
                    if is_plausible_message(chunk):
                        cleaned.append(chunk)
        # 模型说 skip 或全被拦掉 → 不回复
        return cleaned, True

    # ---- 兼容：模型没返回 JSON，按纯文本处理 ----
    fallback = clean_reply(text, max_chars)
    if not fallback:
        return [], True
    if not is_plausible_message(fallback):
        return [], False
    return [c for c in split_messages(fallback, max_chars) if is_plausible_message(c)], True
