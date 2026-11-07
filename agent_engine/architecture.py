"""架构图的唯一数据源。

**以后改架构图，只改这个文件。** 页面 /architecture 从 /architecture/data 读取，
测试会校验节点、连线、流程引用是否一致，以及代码路径是否真实存在。

加一个模块/节点：
1. 在 NODES 里加一条（id 唯一，layer 用 LAYERS 里的 id）
2. 需要连线就加进 EDGES
3. 如果它属于某个功能，补到 FEATURES[].nodes；属于主流程就补到 FLOW
4. 跑 `python manage.py test chat.tests.ArchitectureDataTest`

字段约定：
- files：仓库内相对路径，测试会检查存在；外部服务留空
- symbols：关键函数/类名，方便从图跳回代码（不写行号，行号会漂）
- inputs/outputs/notes：给详情面板看的短句
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/ibhYin/Agent_ybh"

LAYERS = [
    {"id": "ui", "title": "界面层", "hint": "浏览器 · 原生 JS"},
    {"id": "web", "title": "Django 层", "hint": "页面 + JSON / SSE 接口"},
    {"id": "core", "title": "agent_engine", "hint": "不依赖 Django 的核心"},
    {"id": "ext", "title": "外部能力", "hint": "LLM / MCP / 配置文件"},
    {"id": "sec", "title": "数据与安全", "hint": "横切关注点"},
]


def _node(node_id, layer, title, summary, role, files=(), symbols=(), inputs=(), outputs=(), notes=""):
    return {
        "id": node_id,
        "layer": layer,
        "title": title,
        "summary": summary,
        "role": role,
        "files": list(files),
        "symbols": list(symbols),
        "inputs": list(inputs),
        "outputs": list(outputs),
        "notes": notes,
    }


NODES = [
    # ---------------- 界面层 ----------------
    _node("chat-ui", "ui", "聊天工作台", "中栏对话 + 输入区",
          "会话切换、发送消息、消费 SSE 事件，把文本写进气泡；工具细节交给右栏。",
          files=["chat/templates/chat/index.html", "chat/static/chat/js/chat.js"],
          symbols=["chat.init", "chat.openSession", "chat.handleEvent"],
          inputs=["GET /chat/history", "POST /chat/stream (SSE)"],
          outputs=["用户消息", "当前会话状态"]),
    _node("sessions-ui", "ui", "会话侧栏", "列表 / 新建 / 重命名 / 删除",
          "维护当前会话 id，点击会话触发历史回放。",
          files=["chat/static/chat/js/sessions.js"],
          symbols=["sessions.init", "sessions.activate", "sessions.newSession"],
          inputs=["GET /chat/sessions", "localStorage"],
          outputs=["activeSessionId"]),
    _node("trace-ui", "ui", "执行过程面板", "右栏时间线",
          "把 tool_start / tool_result 事件渲成时间线，显示参数、返回、耗时与 LOCAL/MCP。",
          files=["chat/static/chat/js/trace.js"],
          symbols=["trace.startTurn", "trace.addTool", "trace.finishTool", "trace.renderHistory"],
          inputs=["thought", "tool_start", "tool_result", "done 事件"],
          outputs=["本轮工具数 / MCP 数 / 耗时 / 思考块"]),
    _node("settings-ui", "ui", "设置抽屉", "LLM / 外观 / 工具",
          "保存 LLM 配置、切换主题、查看工具 schema；MCP 配置的完整管理在独立页。",
          files=["chat/static/chat/js/settings.js"],
          symbols=["settings.open", "settings.saveConfig", "settings.loadTools", "applyTheme"],
          inputs=["GET /chat/config", "GET /chat/tools?with_mcp=1"],
          outputs=["LLM 配置", "主题选择", "工具清单"]),
    _node("mcp-page", "ui", "MCP 管理页", "服务卡片 / 状态 / 启停",
          "展示连接状态、工具清单与耗时；支持增删改、启停、内置服务一键添加。",
          files=["chat/templates/chat/mcp.html", "chat/static/chat/js/mcp.js"],
          symbols=["mcpPage.init", "renderServers", "checkAll"],
          inputs=["GET /chat/mcp", "GET /chat/mcp/status", "GET /chat/mcp/builtin"],
          outputs=["服务配置变更", "启停状态"]),

    _node("theme", "ui", "主题系统", "四套配色 · CSS 变量",
          "浅色 / 暖色 / 深色 / 午夜蓝；由 html[data-theme] 切换，选择存 localStorage。",
          files=["chat/static/chat/css/app.css", "chat/templates/chat/_theme_init.html"],
          symbols=["data-theme", "--bg", "--accent", "ui_theme"],
          inputs=["设置 → 外观"],
          outputs=["三张页面共用的一套主题令牌"],
          notes="在 <head> 同步读取 localStorage，避免刷新时闪一下默认主题。"),

    # ---------------- Django 层 ----------------
    _node("api-chat", "web", "聊天流接口", "POST /chat/stream",
          "读历史、追加用户消息、调用 run_agent，把事件转成 SSE；流出时建/关 MCPHub。",
          files=["chat/views.py"],
          symbols=["chat_stream", "event_stream"],
          inputs=["历史消息", "LLM 配置", "用户文本"],
          outputs=["SSE 事件流", "user / assistant / tool 消息落库"]),
    _node("api-sessions", "web", "会话接口", "列表 / 新建 / 重命名 / 删除",
          "维护 ChatSession；第一条用户消息自动成为标题。",
          files=["chat/views.py"],
          symbols=["sessions", "session_create", "session_rename", "session_delete"],
          inputs=["/chat/sessions*"],
          outputs=["会话列表与元信息"]),
    _node("api-config", "web", "配置与工具接口", "配置 / 工具 schema",
          "数据库优先、.env 兜底；GET 永不返回 key 明文；/chat/tools 可带 MCP。",
          files=["chat/views.py"],
          symbols=["current_config", "config_save", "tools"],
          inputs=["/chat/config", "/chat/config-save", "/chat/tools"],
          outputs=["生效配置", "工具 schema"]),
    _node("api-mcp", "web", "MCP 管理接口", "CRUD / 状态 / 启停 / 内置",
          "组合 mcp.json 与数据库配置，提供状态探测与按名测试。",
          files=["chat/views.py"],
          symbols=["mcp_list", "mcp_save", "mcp_toggle", "mcp_status", "mcp_builtin", "mcp_test_stored"],
          inputs=["/chat/mcp/*"],
          outputs=["服务配置", "连接状态", "工具清单"]),
    _node("models", "web", "数据模型", "会话 / 消息 / 配置",
          "ChatSession 存会话元信息，ChatMessage 存 OpenAI 格式消息，其余存配置。",
          files=["chat/models.py"],
          symbols=["ChatSession", "ChatMessage", "LLMConfig", "MCPServerConfig"],
          inputs=["HTTP 请求 / 流式事件"],
          outputs=["持久化对象"]),
    _node("db", "sec", "SQLite 数据库", "db.sqlite3（本地，不入库）",
          "会话、消息与配置的落库位置；测试与本地开发共用同一个模型。",
          files=[],
          symbols=["sqlite3"],
          notes="db.sqlite3 被 .gitignore 排除，克隆后需 migrate 生成。"),

    # ---------------- agent_engine ----------------
    _node("agent-loop", "core", "Agentic Loop（ReAct）", "run_agent 同步生成器",
          "调 LLM → 显式思考 → 判断是否请求工具 → 执行 → 回填 role=tool → 再调，最多 10 轮。",
          files=["agent_engine/agent.py"],
          symbols=["run_agent", "_run_mcp_tool", "_extract_thought", "SYSTEM_PROMPT"],
          inputs=["消息历史", "工具 schema", "LLM 配置"],
          outputs=["thought / text_delta / tool_start / tool_result / done 事件"],
          notes="ReAct 的 Thought/Action/Observation：思考优先读 reasoning_content，"
                "没有则解析 Thought: 前缀；同步生成器是核心约束。"),
    _node("tools-registry", "core", "工具注册表", "@tool + schema 自动生成",
          "一份注册表同时服务 LLM、页面工具列表与对外 MCP 服务。",
          files=["agent_engine/tools.py"],
          symbols=["TOOL_REGISTRY", "build_tools_schema", "execute_tool_result"],
          inputs=["@tool 装饰的 Python 函数"],
          outputs=["OpenAI tools schema", "(文本, 是否失败)"],
          notes="新增工具不需要改服务端；schema 从函数签名生成。"),
    _node("mcp-hub", "core", "MCPHub", "配置合并 + 工具路由",
          "合并 mcp.json 与数据库（同名以数据库为准）；按 服务名__工具名 路由到对应连接。",
          files=["agent_engine/mcp_config.py"],
          symbols=["MCPHub", "load_specs", "merge_specs", "MCPClient"],
          inputs=["mcp.json", "MCPServerConfig"],
          outputs=["合并后的远程工具 schema", "调用路由"],
          notes="单个服务连不上只记录，不影响本地工具与其他服务。"),
    _node("mcp-client", "core", "MCP 客户端", "同步桥接 + 常驻连接",
          "后台线程跑一个长任务，把异步 MCP SDK 包成同步 call()；stdio/SSE 都支持。",
          files=["agent_engine/mcp_client.py"],
          symbols=["MCPClient", "MCPServerSpec", "mcp_tool_name"],
          inputs=["MCPServerSpec", "工具名与参数"],
          outputs=["(文本, 是否失败)"],
          notes="连接/请求/关闭必须在同一个任务内，否则 anyio cancel scope 报错。"),
    _node("mcp-server", "core", "MCP 服务端", "stdio / SSE 对外提供工具",
          "把同一份注册表暴露成 MCP 服务；本地 fs/sqlite 服务复用它。",
          files=["agent_engine/mcp_server.py"],
          symbols=["build_server", "run_cli", "build_asgi_app"],
          inputs=["TOOL_REGISTRY 或自定义注册表"],
          outputs=["MCP tools/list / tools/call"],
          notes="stdio 模式 stdout 只能是 JSON-RPC，日志全部走 stderr。"),
    _node("config", "core", "配置读取", "数据库优先，.env 兜底",
          "合并页面保存的配置与 .env；补全 base_url 时不会把版本段补坏。",
          files=["agent_engine/config.py"],
          symbols=["get_config", "normalize_base_url"],
          inputs=["LLMConfig", ".env"],
          outputs=["base_url / api_key / model"]),
    _node("skills", "core", "Agent Skill 库", "按需加载的操作手册",
          "只把 name+description 注入 system prompt，正文由模型调 load_skill 读取——渐进式披露。",
          files=["agent_engine/skills.py", "skills/"],
          symbols=["SkillLibrary", "load_skill_tool", "prompt_block"],
          inputs=["skills/*/SKILL.md"],
          outputs=["system prompt 清单", "load_skill 全文"],
          notes="与 MCP 的分工：MCP 给能力，Skill 给方法；Skill 纯文档，不执行代码。"),
    _node("local-servers", "core", "本地 MCP 服务", "文件系统 / SQLite 只读",
          "两个独立模块，各自 stdio 启动；沙箱与只读 authorizer 是重点。",
          files=["agent_engine/mcp_servers/fs.py", "agent_engine/mcp_servers/sqlite.py",
                 "agent_engine/mcp_servers/__init__.py"],
          symbols=["list_files", "read_text", "search_text", "list_tables", "query", "_connect"],
          inputs=["MCP_FS_ROOT / MCP_SQLITE_DB"],
          outputs=["受限目录读写 / 只读 SQL 查询"]),

    # ---------------- 外部能力 ----------------
    _node("llm", "ext", "LLM 服务", "OpenAI 兼容协议",
          "只负责生成文本与 tool_calls；任意兼容服务都可替换。",
          files=[],
          symbols=["chat.completions"],
          notes="模型必须支持 function calling，否则工具调用不会发生。"),
    _node("external-mcp", "ext", "外部 MCP 服务", "任意第三方 stdio / SSE",
          "用户配置的能力，工具描述与返回都按不可信数据对待。",
          files=[],
          notes="外部服务可以执行任意本地命令，当前默认信任配置者。"),
    _node("a2a-server", "ext", "A2A 服务端", "Agent2Agent 对外协作",
          "把整个 agent（而不只是工具）暴露给别的 agent：Task 有生命周期、可多轮、可取消。",
          files=["chat/a2a_views.py", "agent_engine/a2a_server.py", "agent_engine/a2a_protocol.py"],
          symbols=["agent_card", "a2a_endpoint", "TaskRunner", "TaskStore", "build_agent_card"],
          inputs=["POST /a2a（JSON-RPC）", "GET /agent-card.json"],
          outputs=["Task 状态 / artifacts / SSE 流"],
          notes="与 MCP 的分工：MCP 给工具，A2A 给 agent。contextId 复用 ChatSession。"),
    _node("a2a-clients", "ext", "A2A 客户端", "其他 agent",
          "发现 AgentCard 后按 Task 语义协作，可多轮追问、可取消。",
          files=[],
          notes="客户端可以是另一个智能体，也可以是人写的脚本。"),
    _node("mcp-clients", "ext", "外部 MCP 客户端", "Claude Desktop / Cursor",
          "把本项目当 MCP 服务端拉起，调用 calculate / 文件工具 / 本地服务。",
          files=[],
          notes="stdio 模式下由客户端负责拉起子进程。"),
    _node("mcp-json", "ext", "mcp.json", "仓库外的本地配置文件",
          "与 Claude Desktop 格式兼容；和数据库配置合并，同名以数据库为准。",
          files=["mcp.json.example"],
          symbols=["mcpServers"],
          notes="真实的 mcp.json 被 gitignore，仓库只留 example。"),

    # ---------------- 数据与安全 ----------------
    _node("workspace", "sec", "workspace 沙箱", "文件工具的唯一读写区",
          "所有文件工具路径都相对 workspace/ 解析，越界直接拒绝。",
          files=["agent_engine/tools.py"],
          symbols=["WORKSPACE", "_resolve_safe_path"],
          notes="MCP 的本地文件服务用同一套边界。"),
    _node("crypto", "sec", "API Key 加密", "Fernet + SECRET_KEY 派生",
          "key 存库前加密，前端不持有、不回显、请求不传输明文。",
          files=["chat/crypto.py"],
          symbols=["encrypt_secret", "decrypt_secret"],
          notes="SECRET_KEY 是仓库里的开发默认值；派生密钥因此可被公开复算。"),
    _node("prompt-guard", "sec", "注入防护", "system prompt 约束",
          "明确消息历史与工具结果是数据不是指令，防止诱导模型泄露配置。",
          files=["agent_engine/agent.py"],
          symbols=["SYSTEM_PROMPT"]),
    _node("sqlite-ro", "sec", "SQLite 只读", "mode=ro + authorizer",
          "物理只读连接之上再挂 authorizer：写、ATTACH、危险 PRAGMA 全部拒绝。",
          files=["agent_engine/mcp_servers/sqlite.py"],
          symbols=["_connect", "authorizer", "_ALLOWED_PRAGMAS"],
          notes="两层限制是刻意的：只靠其中一层都可能被绕过。"),
]

# (from, to, 标签, kind)；kind=primary 实线，dashed 虚线（安全/回推）
_EDGE_SPECS = [
    ("chat-ui", "api-chat", "POST /chat/stream", "primary"),
    ("chat-ui", "api-config", "配置 / 工具", "primary"),
    ("sessions-ui", "api-sessions", "会话 CRUD", "primary"),
    ("settings-ui", "api-config", "保存配置", "primary"),
    ("mcp-page", "api-mcp", "管理接口", "primary"),
    ("settings-ui", "theme", "切换主题", "primary"),
    ("theme", "chat-ui", "配色令牌", "dashed"),
    ("theme", "mcp-page", "配色令牌", "dashed"),

    ("api-chat", "models", "消息落库", "primary"),
    ("api-sessions", "models", "会话元信息", "primary"),
    ("api-config", "models", "LLM 配置", "primary"),
    ("api-mcp", "models", "服务配置", "primary"),
    ("api-chat", "agent-loop", "run_agent", "primary"),
    ("api-config", "tools-registry", "本地 schema", "primary"),
    ("api-mcp", "mcp-client", "探活 / 测试", "primary"),

    ("agent-loop", "tools-registry", "执行本地工具", "primary"),
    ("skills", "tools-registry", "注册 load_skill", "primary"),
    ("agent-loop", "skills", "注入技能清单", "primary"),
    ("agent-loop", "mcp-hub", "合并 MCP 工具", "primary"),
    ("agent-loop", "config", "读 LLM 配置", "primary"),
    ("mcp-hub", "mcp-client", "按名字路由", "primary"),
    ("mcp-hub", "mcp-json", "读文件配置", "primary"),
    ("mcp-server", "tools-registry", "复用同一注册表", "primary"),

    ("agent-loop", "llm", "chat.completions", "primary"),
    ("mcp-client", "local-servers", "stdio", "primary"),
    ("mcp-client", "external-mcp", "stdio / SSE", "primary"),
    ("mcp-server", "mcp-clients", "stdio / SSE", "primary"),
    ("a2a-server", "agent-loop", "包住整个循环", "primary"),
    ("a2a-server", "models", "contextId = 会话", "primary"),
    ("a2a-clients", "a2a-server", "AgentCard → Task", "primary"),

    ("models", "db", "SQLite", "primary"),
    ("api-chat", "chat-ui", "SSE 文本", "dashed"),
    ("api-chat", "trace-ui", "SSE 事件", "dashed"),
    ("local-servers", "workspace", "沙箱约束", "dashed"),
    ("tools-registry", "workspace", "沙箱约束", "dashed"),
    ("crypto", "models", "加密 key", "dashed"),
    ("prompt-guard", "agent-loop", "注入防护", "dashed"),
    ("sqlite-ro", "local-servers", "只读约束", "dashed"),
]

EDGES = [
    {"from": f, "to": t, "label": label, "kind": kind}
    for f, t, label, kind in _EDGE_SPECS
]


FEATURES = [
    {
        "id": "sessions",
        "title": "会话管理",
        "summary": "多会话持久化，左栏可切换、重命名、删除。",
        "items": [
            {"text": "新建 / 切换 / 删除会话，消息按 session 隔离", "nodes": ["sessions-ui", "api-sessions", "models"]},
            {"text": "第一条用户消息自动生成标题", "nodes": ["api-sessions", "models"]},
            {"text": "空会话也能出现在列表，刷新不丢", "nodes": ["sessions-ui", "api-sessions"]},
        ],
    },
    {
        "id": "chat",
        "title": "对话与流式",
        "summary": "一条消息经 SSE 逐字返回，刷新后靠历史接口回放。",
        "items": [
            {"text": "text_delta 打字机效果，支持停止生成", "nodes": ["chat-ui", "api-chat", "agent-loop"]},
            {"text": "历史回放：用户 / 助手文本按角色重建", "nodes": ["chat-ui", "api-chat", "models"]},
            {"text": "轻量 Markdown（加粗与代码）", "nodes": ["chat-ui"]},
        ],
    },
    {
        "id": "tools",
        "title": "工具与执行过程",
        "summary": "工具定义只写一次，三种消费者共用。",
        "items": [
            {"text": "@tool 注册 + schema 从签名自动生成", "nodes": ["tools-registry", "agent-loop"]},
            {"text": "右栏时间线：思考、参数、返回、LOCAL/MCP、耗时", "nodes": ["trace-ui", "agent-loop"]},
            {"text": "新增工具无需改服务端与前端", "nodes": ["tools-registry", "mcp-server"]},
        ],
    },
    {
        "id": "mcp",
        "title": "MCP 双向能力",
        "summary": "既当客户端调外部工具，也当服务端对外提供工具。",
        "items": [
            {"text": "客户端：stdio / SSE，常驻连接与同步桥接", "nodes": ["mcp-client", "mcp-hub", "agent-loop"]},
            {"text": "服务端：同一注册表暴露为 stdio / SSE", "nodes": ["mcp-server", "tools-registry", "mcp-clients"]},
            {"text": "管理页：状态探测、启停、内置服务一键添加", "nodes": ["mcp-page", "api-mcp", "local-servers"]},
        ],
    },
    {
        "id": "skills",
        "title": "技能（Skill）",
        "summary": "把「怎么做」沉淀成文档，按需加载而不是常驻上下文。",
        "items": [
            {"text": "渐进式披露：清单进 prompt，正文调 load_skill 才读", "nodes": ["skills", "agent-loop"]},
            {"text": "load_skill 是普通工具，复用 tool call 循环与前端展示", "nodes": ["skills", "tools-registry"]},
            {"text": "示例：sqlite-query / filesystem / answer-style", "nodes": ["skills"]},
        ],
    },
    {
        "id": "a2a",
        "title": "A2A 对外协作",
        "summary": "把 agent 本身当作服务，而不只是提供工具。",
        "items": [
            {"text": "AgentCard 描述能力，供外部发现", "nodes": ["a2a-server", "agent-loop"]},
            {"text": "message/send 与 message/stream（SSE）", "nodes": ["a2a-server", "a2a-clients"]},
            {"text": "Task 状态机：working / completed / failed / canceled", "nodes": ["a2a-server"]},
            {"text": "contextId 复用 ChatSession，页面上能接着聊", "nodes": ["a2a-server", "models"]},
        ],
    },
    {
        "id": "config",
        "title": "配置与密钥",
        "summary": "数据库优先、.env 兜底；key 加密存储。",
        "items": [
            {"text": "页面保存 LLM 配置，立即生效", "nodes": ["settings-ui", "api-config", "config"]},
            {"text": "API Key 加密落库，前端不回显", "nodes": ["crypto", "models", "api-config"]},
            {"text": "MCP 配置双源合并：mcp.json + 数据库", "nodes": ["mcp-json", "mcp-hub", "api-mcp"]},
        ],
    },
    {
        "id": "theme",
        "title": "外观与主题",
        "summary": "四套配色，三张页面共用同一份令牌。",
        "items": [
            {"text": "设置 → 外观切换，立即生效并记住选择", "nodes": ["settings-ui", "theme"]},
            {"text": "浅色 / 暖色 / 深色 / 午夜蓝", "nodes": ["theme"]},
            {"text": "颜色全部走 CSS 变量，加主题只需复制一组", "nodes": ["theme", "chat-ui"]},
        ],
    },
    {
        "id": "security",
        "title": "安全边界",
        "summary": "能力越强，越要写明边界在哪里。",
        "items": [
            {"text": "文件工具限制在 workspace/，越界拒绝", "nodes": ["workspace", "tools-registry", "local-servers"]},
            {"text": "SQLite 只读：mode=ro + authorizer", "nodes": ["sqlite-ro", "local-servers"]},
            {"text": "提示词注入防护与密钥加密", "nodes": ["prompt-guard", "crypto"]},
            {"text": "已知缺口：MCP 服务无认证、外部工具默认信任", "nodes": ["mcp-server", "external-mcp"]},
        ],
    },
]


FLOW = [
    {
        "title": "用户输入",
        "detail": "输入框按 Enter；前端带 session_id 发起 POST /chat/stream。",
        "nodes": ["chat-ui", "api-chat"],
    },
    {
        "title": "载入上下文",
        "detail": "按 session 读完整历史，追加本次用户消息，并用 DB 配置覆盖 .env。",
        "nodes": ["api-chat", "models", "config"],
    },
    {
        "title": "进入循环",
        "detail": "run_agent 注入 system prompt 与工具 schema，调用 LLM 流式接口。",
        "nodes": ["agent-loop", "llm", "config"],
    },
    {
        "title": "思考与决定",
        "detail": "模型先思考（reasoning_content 或 Thought: 前缀），再决定调用哪个工具；"
                  "思考作为 thought 事件进右栏。",
        "nodes": ["agent-loop", "tools-registry", "mcp-hub"],
    },
    {
        "title": "执行工具",
        "detail": "本地工具直接调用；MCP 工具经常驻连接执行，单个服务失败转成错误文本。",
        "nodes": ["agent-loop", "mcp-client", "local-servers", "external-mcp"],
    },
    {
        "title": "结果回填",
        "detail": "工具结果作为 role=tool 消息回填历史，模型继续生成，直到不再请求工具。",
        "nodes": ["agent-loop", "mcp-client", "tools-registry"],
    },
    {
        "title": "SSE 推送",
        "detail": "text_delta / tool_start / tool_result / done 逐条推送；中栏打字，右栏时间线同步。",
        "nodes": ["api-chat", "chat-ui", "trace-ui"],
    },
    {
        "title": "落库与回放",
        "detail": "每条完成的 message 与 done 文本落库；刷新后历史与工具过程可重建。",
        "nodes": ["api-chat", "models", "db"],
    },
]


MCP_PATHS = [
    {
        "id": "client",
        "title": "作为客户端：调用外部工具",
        "caption": "agent 侧的 MCP 调用链；连接常驻，按工具名前缀路由。",
        "steps": [
            {"node": "agent-loop", "label": "决定调用工具"},
            {"node": "mcp-hub", "label": "按 服务名__工具名 路由"},
            {"node": "mcp-client", "label": "后台线程 + 常驻连接"},
        ],
        "branches": [
            {"node": "local-servers", "label": "本地 stdio：文件系统 / SQLite"},
            {"node": "external-mcp", "label": "外部 stdio / SSE"},
        ],
    },
    {
        "id": "server",
        "title": "作为服务端：对外提供工具",
        "caption": "同一份工具注册表，被 Claude Desktop / Cursor 等拉起。",
        "steps": [
            {"node": "mcp-clients", "label": "MCP 客户端"},
            {"node": "mcp-server", "label": "stdio / SSE 服务端"},
            {"node": "tools-registry", "label": "同一份工具注册表"},
        ],
        "branches": [
            {"node": "local-servers", "label": "本地服务也复用它"},
            {"node": "mcp-json", "label": "客户端配置示例"},
        ],
    },
]

SHARED_NOTE = "工具定义只在 tools.py 存在一处：喂 LLM、对外暴露、右栏展示与本地服务共用。"


def as_dict() -> dict:
    return {
        "repo_url": REPO_URL,
        "source": "agent_engine/architecture.py",
        "layers": LAYERS,
        "nodes": NODES,
        "edges": EDGES,
        "features": FEATURES,
        "flow": FLOW,
        "mcp_paths": MCP_PATHS,
        "shared_note": SHARED_NOTE,
    }


def validate() -> list[str]:
    """返回问题列表；测试断言为空。改图后跑测试即可发现断链与不存在的文件。"""
    problems: list[str] = []
    layer_ids = {layer["id"] for layer in LAYERS}
    node_ids = [node["id"] for node in NODES]

    seen = set()
    for node_id in node_ids:
        if node_id in seen:
            problems.append(f"节点 id 重复：{node_id}")
        seen.add(node_id)

    nodes = {node["id"]: node for node in NODES}
    for node in NODES:
        if node["layer"] not in layer_ids:
            problems.append(f"{node['id']} 的 layer 未定义：{node['layer']}")
        for file in node["files"]:
            if not (PROJECT_ROOT / file).exists():
                problems.append(f"{node['id']} 引用的文件不存在：{file}")

    for edge in EDGES:
        for key in ("from", "to"):
            if edge[key] not in nodes:
                problems.append(f"连线引用了不存在的节点：{edge[key]}")

    for feature in FEATURES:
        for item in feature["items"]:
            for node_id in item["nodes"]:
                if node_id not in nodes:
                    problems.append(f"功能 {feature['id']} 引用了不存在的节点：{node_id}")

    for index, step in enumerate(FLOW, 1):
        for node_id in step["nodes"]:
            if node_id not in nodes:
                problems.append(f"流程第 {index} 步引用了不存在的节点：{node_id}")

    for path in MCP_PATHS:
        for step in path["steps"]:
            if step["node"] not in nodes:
                problems.append(f"MCP 路径 {path['id']} 引用了不存在的节点：{step['node']}")
        for branch in path["branches"]:
            if branch["node"] not in nodes:
                problems.append(f"MCP 路径 {path['id']} 的分支引用了不存在的节点：{branch['node']}")

    return problems
