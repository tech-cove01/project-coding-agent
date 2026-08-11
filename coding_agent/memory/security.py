"""记忆写入安全扫描（Memory Write Sanitizer）。

记忆文件会被注入 system prompt（长期记忆注入），本质上是一个"持久化的
prompt 注入入口"。模型可能在一次对话中被诱导写出恶意记忆，而这些记忆会
被后续所有会话重复加载——跨会话污染的攻击面远大于单次注入。

本模块在记忆被写入磁盘之前执行扫描，从源头拦截四类风险：

1. **Prompt 注入**：内容试图改写/忽略系统指令、扮演系统、诱导 agent
   执行危险动作。
2. **角色 / 权限劫持**：内容冒充系统或超级用户，要求输出密钥、泄露环境
   变量、绕过权限。
3. **敏感凭证泄露**：内容包含真实的 AccessKey / 私钥 / token 等凭据。
4. **隐藏文本攻击**：零宽字符、控制字符、HTML 注释等不可见指令，用于
   在 human 不可见的情况下操纵模型。

设计上保持零依赖（纯标准库 + 正则），扫描是确定性的、可审计的，并且
结果通过 ``MemoryScanResult`` 结构化返回，便于上层决定"拦截"或"告警"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 扫描结果
# ---------------------------------------------------------------------------


@dataclass
class MemoryScanIssue:
    """一条扫描发现的风险。"""

    severity: str  # "block"（必须拦截）或 "warn"（仅告警）
    category: str  # 风险类别：injection / hijack / secret / hidden_text
    message: str
    snippet: str = ""  # 触发风险的内容片段（截断后）

    def to_text(self) -> str:
        snippet = f"\n      片段: {self.snippet}" if self.snippet else ""
        return f"[{self.severity}] {self.category}: {self.message}{snippet}"


@dataclass
class MemoryScanResult:
    """记忆内容扫描结果。"""

    issues: list[MemoryScanIssue] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """是否存在必须拦截的严重问题。"""
        return any(i.severity == "block" for i in self.issues)

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)

    def render(self, max_issues: int = 5) -> str:
        """渲染为人类可读的拦截报告（写入工具的错误返回）。"""
        if not self.issues:
            return ""
        head = "⚠ 记忆内容安全检查未通过："
        lines = [head]
        for issue in self.issues[:max_issues]:
            lines.append("  " + issue.to_text())
        if len(self.issues) > max_issues:
            lines.append(f"  … 另有 {len(self.issues) - max_issues} 项风险")
        lines.append(
            "记忆会长期注入后续会话，请勿写入可疑内容。可拆掉被拦截的片段后重试。"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 风险模式（按类别分组）
# ---------------------------------------------------------------------------

# 1) Prompt 注入：试图改写系统指令 / 忽略规则 / 诱导执行
_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 中文：忽略/遗忘/跳过 系统指令/规则/设定
    (re.compile(r"(忽略|无视|忘记|遗忘|跳过)\s*(上面|以上|之前|先前)?\s*(的|所有|全部|一切)?\s*(系统)?\s*(指令|规则|设定|提示词|system\s*prompt)", re.IGNORECASE), "试图忽略/覆盖系统指令"),
    (re.compile(r"重写\s*(你|系统)?\s*的?\s*(系统提示词|system\s*prompt|角色设定|人格)", re.IGNORECASE), "试图重写系统提示词"),
    (re.compile(r"(从现在开始|接下来|之后).{0,15}(当作|扮演|自认为|假装你是).{0,15}(admin|root|超级用户|最高权限|系统)", re.IGNORECASE), "试图切换为高权限角色"),
    # 英文：disregard/ignore/override + 指令/规则/提示词
    (re.compile(r"(ignore|disregard|override|forget)\s+(all\s+|any\s+)?(the\s+)?(previous|above|prior|system|prior\s+system|earlier)?\s*(instructions|instruction|rules?|guidelines?|prompts?|directions?)", re.IGNORECASE), "试图忽略/覆盖系统指令"),
    (re.compile(r"(do\s+not|never|stop\s+following|don't\s+follow|do\s+not\s+follow)\s+(the\s+)?(system|above|prior|previous)", re.IGNORECASE), "试图违背系统指令"),
    # 角色替换：you are now ... / from now on you are ... + 敏感身份
    (re.compile(r"(you\s+are\s+now|from\s+now\s+on\s+(you\s+are|you're|act\s+as)|act\s+as\s+an?\s+(unrestricted|unfiltered))\b", re.IGNORECASE), "试图替换角色设定"),
    # 关闭安全限制
    (re.compile(r"remove\s+(your\s+)?(safety\s+)?(guardrails?|filters?|restrictions?|limitations?)", re.IGNORECASE), "试图关闭安全限制"),
    # 伪造 system 消息标记
    (re.compile(r"\[(system|SYS)\s*\]", re.IGNORECASE), "伪造 system 消息标记"),
    (re.compile(r"<\|?sys(tem)?\|?>\s*:?\s*", re.IGNORECASE), "伪造系统消息标签"),
    (re.compile(r"(now|please|make\s+sure|from\s+now)\s+(disregard|override|ignore)", re.IGNORECASE), "试图覆盖指令"),
]

# 2) 角色 / 权限劫持
_HIJACK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(输出|打印|告诉我|给我|透露|泄露)\s*(你的|我的|所有)?\s*(密钥|密码|token|access\s*key|secret|api\s*key)", re.IGNORECASE), "诱导输出密钥/凭证"),
    (re.compile(r"(reveal|show|print|give|send|leak)\s*(me\s+)?(your|my|the)\s+(api\s*key|password|secret|token|credentials|\.env)", re.IGNORECASE), "诱导泄露凭证"),
    (re.compile(r"读取并输出\s*(环境变量|\.env|配置文件|credentials)", re.IGNORECASE), "诱导泄露环境配置"),
    (re.compile(r"(绕过|关闭|禁用|解除)\s*(权限|安全|沙箱|验证|审查|限制)", re.IGNORECASE), "诱导绕过安全机制"),
    (re.compile(r"(bypass|disable|turn\s+off|remove)\s+(the\s+)?(security|permission|sandbox|review|restrictions?)", re.IGNORECASE), "诱导绕过安全机制"),
    (re.compile(r"(你是|你现在的身份是|假装你是|act\s+as)\s*(admin|root|superuser|super\s*user|hacker|最高权限|超级用户)", re.IGNORECASE), "冒充高权限/恶意角色"),
    (re.compile(r"exfiltrate|泄漏\s*(数据|信息)|发送\s*到\s*(外部|远程|http)|send\s+(data|files?)\s+to\s+an?\s+external", re.IGNORECASE), "诱导数据外泄"),
    (re.compile(r"把.{0,20}(敏感|机密|密钥|token).{0,20}(写到|保存到|提交到)", re.IGNORECASE), "诱导敏感信息落库"),
]

# 3) 敏感凭证：真实的密钥 / token 形态
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"AKID[A-Za-z0-9]{13,}", re.IGNORECASE), "疑似阿里云 AccessKey ID"),
    (re.compile(r"\bLTAI[A-Za-z0-9]{12,}\b", re.IGNORECASE), "疑似阿里云 AccessKey"),
    (re.compile(r"\b(sk|sk-)[A-Za-z0-9]{20,}\b", re.IGNORECASE), "疑似 Secret Key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36,}\b", re.IGNORECASE), "疑似 GitHub Token"),
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b", re.IGNORECASE), "疑似 Google API Key"),
    (re.compile(r"-----BEGIN\s+(RSA|EC|OPENSSH|PGP)\s+PRIVATE\s+KEY-----", re.IGNORECASE), "私钥块"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{20,}\b", re.IGNORECASE), "疑似 Bearer Token"),
    (re.compile(r"\b(access_key|secret_key|api_key|app_secret)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}", re.IGNORECASE), "显式密钥赋值"),
]

# 4) 隐藏文本攻击：零宽字符 / 控制字符 / 不可见指令
_HIDDEN_TEXT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 零宽字符（ZWSP / ZWNJ / ZWJ / LRM / RLM / BOM）
    (re.compile(r"[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad]"), "含零宽/软连字符（隐藏文本）"),
    # 常见 Unicode 控制字符
    (re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f]"), "含控制字符"),
    # 隐藏的 HTML 注释（可能夹带指令）
    (re.compile(r"<!--(?!\s*(name|description|type)\s*-->)", re.IGNORECASE), "含 HTML 注释（可能隐藏指令）"),
    # 反向/双向文本控制
    (re.compile(r"[\u202a-\u202e\u2066-\u2069]"), "含双向文本控制符（视觉欺骗）"),
]


# ---------------------------------------------------------------------------
# 扫描器
# ---------------------------------------------------------------------------


class MemoryWriteSanitizer:
    """对即将写入记忆目录的内容进行确定性安全检查。

    用法：拿到要写入记忆文件的内容（含 frontmatter）后调用 ``scan``，
    若 ``result.blocked`` 为真则应拒绝写入。
    """

    def __init__(
        self,
        extra_patterns: dict[str, list[tuple[str, str]]] | None = None,
    ) -> None:
        self._groups: dict[str, list[tuple[re.Pattern[str], str]]] = {
            "injection": list(_INJECTION_PATTERNS),
            "hijack": list(_HIJACK_PATTERNS),
            "secret": list(_SECRET_PATTERNS),
            "hidden_text": list(_HIDDEN_TEXT_PATTERNS),
        }
        if extra_patterns:
            for group, patterns in extra_patterns.items():
                target = self._groups.setdefault(group, [])
                for regex_str, reason in patterns:
                    target.append((re.compile(regex_str, re.IGNORECASE), reason))

    def scan(self, content: str) -> MemoryScanResult:
        """扫描记忆内容，返回所有风险。"""
        result = MemoryScanResult()
        if not content:
            return result
        # hidden_text 默认是"警告"（可能为合法中文引号等），其余类别默认"拦截"
        for group, patterns in self._groups.items():
            severity = "warn" if group == "hidden_text" else "block"
            for pattern, reason in patterns:
                m = pattern.search(content)
                if m:
                    result.issues.append(
                        MemoryScanIssue(
                            severity=severity,
                            category=group,
                            message=reason,
                            snippet=_snippet(content, m.start(), m.end()),
                        )
                    )
        return result


def _snippet(content: str, start: int, end: int, width: int = 40) -> str:
    """截取风险片段附近的上下文，方便定位。"""
    s = max(0, start - 10)
    e = min(len(content), end + 10)
    return content[s:e].replace("\n", "\\n").strip()
