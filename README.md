# im-mcp (本地绿泡泡 & DuangDuang AI 检索 MCP Server)

基于 **Model Context Protocol (MCP)** 规范构建的本地 IM（绿泡泡 & DuangDuang）聊天记录与通讯录检索服务。
极简、只读、完全离线本地运行，让 Claude Desktop、Cursor、Cline、Pi 等 AI Agent 能够安全、高速地检索与总结你的工作沟通、群聊记录和知识库。

> 💡 **复制即用提示词（直接发给你的 AI Agent / 客户端）**：
> 
> ```text
> 请阅读并遵循开源项目 https://github.com/stkoecn/im-mcp 中的 README.md 与 skills/im-mcp/SKILL.md，帮我配置并启用本机的绿泡泡与DuangDuang MCP 服务。
> ```

> 注：原底层参考项目以 Git Submodule 形式统一归档在 `reference/` 目录下（`reference/wechat-local-viewer` 与 `reference/dingwave-V3`）。

---

## ✨ 核心特性

- ⚡ **超低延迟**：直接基于本地解密 SQLite 数据库，毫秒级响应。
- 🔒 **纯本地与只读**：所有数据均在本地，绝不上云；以只读方式运行，不破坏原始数据。
- 📦 **按需安装（Optional Dependencies）**：一个仓库，两套独立入口；支持只装绿泡泡、只装DuangDuang或全量安装。
- 💬 **丰富格式解析**：
  - **绿泡泡**：支持普通文本、引用消息、富文本卡片、小程序、传输文件（PDF/Excel/Word等）。
  - **DuangDuang**：支持多表分片聚合（`tbmsg_000`~`tbmsg_127`）、联系人实名映射、Markdown卡片、协同日程通知、文件附件提取。

---

## 🔍 支持的客户端与系统版本 (Compatibility)

| 平台 | 支持版本范围 | 实测验证版本 | 架构与平台 | 解密 / 同步原理 |
|---|---|---|---|---|
| **绿泡泡 (WeChat)** | **绿泡泡 4.x** 全系列<br>*(兼容 3.9.x)* | **`4.1.15.13`** (Windows 最新版)<br>**`4.1.35.15`** (macOS ARM64 最新版)<br>**`4.1.15` (270115)** (macOS x86_64 实测验证)<br>`4.1.11.x` 等 | Windows x64<br>macOS (Apple Silicon & Intel) | **Windows**: 运行时扫描 `Weixin.exe` Config.Cipher 动态提取 Salt 密钥；<br>**macOS**: 原生 `CommonCrypto` (CCCrypt) 零额外依赖解密，支持 passphrase PBKDF2 派生；离线 HMAC 校验并解密 WCDB SQLite |
| **DuangDuang (DingTalk)** | **DuangDuang 7.x** 全系列<br>*(涵盖 V2 与 V3)* | **`7.6.0.x`** (最新版)<br>`7.5.x` / `7.0.x` | Windows x64 / macOS | 纯离线读取用户目录 `user_config`，通过 PBKDF2-HMAC-SHA1 + MD5 推导 AES-128 密钥并合并 WAL 事务 |

---

## 🧰 提供的 MCP Tools

### 1. 绿泡泡 (`wechat-mcp`)

共 16 个工具，全部只读。

> 本 fork 相对上游新增 / 修复的完整清单见 [CHANGELOG.md](./CHANGELOG.md)。

**消息检索**

| 工具名称 | 说明 | 适用场景 |
|---|---|---|
| `search_wechat_messages` | 全文检索聊天消息（支持限定人/群、`offset` 分页） | “找一下群里谁发过关于‘某某需求’的讨论” |
| `get_session_history` | 获取指定会话的最近对话（时间正序，可限定天数） | “总结一下这个项目群最近 50 条消息都在聊什么” |
| `get_messages_by_date` | 按指定日期拉取全量或限定会话记录（支持 `offset` 分页） | “帮我汇总 2026-10-16 那天所有沟通做日报” |
| `get_messages_by_range` | 按自定义时间范围（支持跨天与任意时段、`offset` 分页）拉取记录 | “拉一下上周三到周五这个群聊了什么” |

**会话与通讯录**

| 工具名称 | 说明 | 适用场景 |
|---|---|---|
| `list_wechat_sessions` | 查询群聊或联系人列表（获取唯一 ID） | “帮我看看关于‘量化’有哪些群” |
| `search_contacts` | 按昵称 / 备注 / 微信号搜索联系人 | “查一下张工的微信号和备注名” |
| `list_chatroom_members` | 列出指定群聊的成员名单（自动关联昵称与微信号） | “这个群一共多少人，都有谁” |

**文件 / 图片 / 媒体 / 收藏 / 表情**

| 工具名称 | 说明 | 适用场景 |
|---|---|---|
| `get_message_image` | 按消息 ID 定位并按需解密聊天图片（支持本地物理路径或多模态 Base64） | “把刚才查到的那张聊天图片解密提取出来看下” |
| `get_user_avatars` | 批量取用户 / 群 / 公众号头像的本地图片路径（供面板等宿主直接读盘渲染，不返回 base64） | “把群里发言者的头像都给我” |
| `search_shared_files` | 查找聊天中发送/接收的文件文档 | “找一下上周有人发在群里的那个理财报告 Excel” |
| `search_media_files` | 查找聊天中的图片 / 文件 / 视频及其磁盘路径 | “把群里发过的截图路径给我” |
| `list_favorites` | 查询绿泡泡收藏条目（标题 / 描述 / 来源） | “我收藏过哪些资料” |
| `list_emoticons` | 列出表情包与表情（含官方文字说明） | “那个‘偷听’表情在哪个包里” |

**分析与同步**

| 工具名称 | 说明 | 适用场景 |
|---|---|---|
| `get_chat_activity_stats` | 聊天活跃度统计（消息总量、按小时/按天分布、发言最多的人） | “这个群什么时候最活跃” |
| `get_persona_style` | 分析某位联系人的沟通风格（画像 + 高频用语 + 典型语句） | “模仿一下这个人的说话风格” |
| `get_sync_status` | 查看本地索引的新鲜度与规模，判断是否需要重新同步 | “数据是不是最新的” |
| `sync_wechat_database` | 触发增量提取与 ETL 同步 | “刚聊了几句，帮我更新一下数据库后再查” |

### 2. DuangDuang (`dingtalk-mcp`)

| 工具名称 | 说明 | 适用场景 |
|---|---|---|
| `search_dingtalk_messages` | 全文检索DuangDuang消息（支持按群/私聊过滤） | “搜一下DuangDuang里成鹏发过的报表统计结果” |
| `list_dingtalk_conversations` | 列出DuangDuang最近会话（自动解析群名与实名） | “看看DuangDuang里最近活跃的项目组群” |
| `get_dingtalk_messages` | 获取指定DuangDuang会话的历史消息上下文 | “拉取‘定制报表项目组’最近的历史消息” |
| `list_dingtalk_contacts` | 查询团队通讯录与联系人（手机号、邮箱、真名） | “查一下DuangDuang里某个同事的联系方式” |
| `sync_dingtalk_data` | 自动拷贝DuangDuang数据库并执行 WAL 解密同步 | “重新同步最新的DuangDuang聊天记录” |

---

## 📦 安装与运行方式

本项目已完整遵循 PEP 517 / 621 标准打包，原生支持 **`uv` / `uvx`** 以及 `pip`，免手动配置虚拟环境与路径。

### 1. 推荐：通过 `uv tool install` 全局隔离安装（最推荐，一次安装全局可用）

使用 `uv tool` 会为工具创建独立受控的持久化运行环境，并将 CLI 注册到系统全局路径，**之后无需重复拉取，启动极快**：

```bash
# 全局安装 CLI 工具链 (wechat-init, dingtalk-init, wechat-mcp, dingtalk-mcp, im-clean)
uv tool install "git+https://github.com/stkoecn/im-mcp.git" --with "im-mcp[all]"
# （国内网络用户亦可使用 Gitee 源：uv tool install "git+https://gitee.com/ws4m/im-mcp.git" --with "im-mcp[all]"）

# 后续如需更新到最新版本，仅需执行：
uv tool upgrade im-mcp
```

### 2. 备选：通过 `uvx` 即席运行（临时/测试体验）

`uvx` 适合一次性临时体验或不想常驻安装的场景（每次可能存在版本检查开销；国内环境可替换为 `git+https://gitee.com/ws4m/im-mcp.git[all]`）：

```bash
# 直接免安装即席运行初始化
uvx --from "git+https://github.com/stkoecn/im-mcp.git[all]" dingtalk-init
uvx --from "git+https://github.com/stkoecn/im-mcp.git[all]" wechat-init
```

### 3. 传统 `pip` 安装（支持按需依赖）

```bash
# 两个平台都需要（全量）
pip install "git+https://github.com/stkoecn/im-mcp.git#egg=im-mcp[all]"

# 只需要绿泡泡
pip install "git+https://github.com/stkoecn/im-mcp.git#egg=im-mcp[wechat]"

# 只需要DuangDuang
pip install "git+https://github.com/stkoecn/im-mcp.git#egg=im-mcp[dingtalk]"
```

---

## 🤖 智能体配套技能文档 (Agent Skills)

本项目在 [`skills/im-mcp/SKILL.md`](skills/im-mcp/SKILL.md) 中完整定义了面向各平台 AI Agent（Pi、Claude Desktop/Code、Cursor、Cline 等）的完整自动化运维规范（涵盖**依赖运行策略、初始化前置检查、MCP 工具链调用 SOP、异常处置禁令及附录参考目录**）。

### 安装方式：直接复制到当前 Agent 技能目录

- **通用智能体环境 (Pi / Codex / Pebrel)**：
  ```bash
  mkdir -p ~/.agents/skills/im-mcp && cp skills/im-mcp/SKILL.md ~/.agents/skills/im-mcp/
  ```
- **Claude Code / Desktop**：
  ```bash
  mkdir -p ~/.claude/skills/im-mcp && cp skills/im-mcp/SKILL.md ~/.claude/skills/im-mcp/
  ```
- **当前项目工作区本地生效**：
  ```bash
  mkdir -p .agents/skills/im-mcp && cp skills/im-mcp/SKILL.md .agents/skills/im-mcp/
  ```

---

## 🛠️ 开箱即用：一键初始化 CLI (`wechat-init` & `dingtalk-init`)

安装本包后，系统会自动注册两个专用的命令行初始化向导。在日常使用 AI 客户端连接前，只需在控制台**运行一次**即可完成密钥提取与数据库初始化：

### 1. 绿泡泡一键初始化 (`wechat-init`)
> **注意**：
> - **Windows**：由于需要读取绿泡泡客户端内存（安全校验与运行时 Cipher 扫描），请确保**以管理员身份打开终端**，并且**Windows 绿泡泡已登录**。
> - **macOS**：支持自动检测数据目录 `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/db_storage`；微信 4.1+ 可通过 `--passphrase <64位hex>` 传入或由 `wechat-passphrase.json` 持久化，采用系统原生 `CommonCrypto` 完成 256,000 轮 PBKDF2 派生与全库零依赖极速解密。

```bash
# Windows: 一键提取密钥、解密为 SQLite 并自动构建 FTS5 全文索引（默认保存至 ~/.wechat_mcp/）
wechat-init

# macOS (微信 4.1+ 配合 passphrase):
wechat-init --passphrase <64位十六进制passphrase>

# 高级选项（跨平台通用）
wechat-init --output ~/.wechat_mcp/decrypted   # 指定解密数据库保存路径（默认 ~/.wechat_mcp/decrypted）
wechat-init --only-extract                     # 仅提取密钥保存至 all_keys.json，不立即解密
wechat-init --etl-only                         # 仅执行数据清洗与重构 FTS5 索引（无需重复解密）
```

### 2. DuangDuang一键初始化 (`dingtalk-init`)
> DuangDuang采用纯 Python 本地解密算法（支持 V2/V3 及 WAL 事务合并），自动查找当前登录的 UID，普通用户终端即可执行：

```bash
# 一键同步并解密当前登录DuangDuang的数据库
dingtalk-init

# 高级选项（可选）
dingtalk-init --output-dir ~/.dingtalk_mcp/decrypted  # 指定输出路径
```

---

## 🧹 一键清理工具 (`im-clean`)

如果在测试或使用后需要清理导出的明文 SQLite 数据库、密钥文件及 FTS 中间缓存，可以使用 `im-clean` 一键彻底清理：

```bash
# 1. 模拟预览（只查看待清理文件与磁盘占用，不真正删除）
im-clean --dry-run

# 2. 交互式安全清理（支持二次确认）
im-clean

# 3. 按需清理指定平台
im-clean --scope wechat      # 仅清理绿泡泡导出的数据与密钥
im-clean --scope dingtalk    # 仅清理DuangDuang导出的数据与缓存

# 4. 静默全量删除（适合自动化脚本）
im-clean -y
```

---

## 🚀 客户端接入配置 (Claude Desktop / Cursor / Pi)

### 方案 A：使用 `uvx` 即席运行（零环境污染，免手动建 venv，最推荐）

客户端通过 `uvx` 动态拉取执行，并在 `env` 中显式固化全局数据目录，杜绝工作目录变动影响：

```json
{
  "mcpServers": {
    "wechat": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/stkoecn/im-mcp.git[all]",
        "wechat-mcp"
      ],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      },
      "description": "绿泡泡聊天记录与群聊检索 MCP"
    },
    "dingtalk": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/stkoecn/im-mcp.git[all]",
        "dingtalk-mcp"
      ],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      },
      "description": "DuangDuang聊天记录、群聊与通讯录检索 MCP"
    }
  }
}
```

### 方案 B：全局 `uv tool` 运行（一次安装，秒级冷启动）

若当前环境未受沙箱拦截，且已执行 `uv tool install "git+https://github.com/stkoecn/im-mcp.git" --with "im-mcp[all]"`，直接使用系统 PATH 中的命令：

> 💡 **PATH 说明**：若安装后 uv 提示 `warning: ... is not on your PATH`，可运行 `uv tool update-shell`（或重启终端生效）。在严格安全限制无法修改 PATH 的环境下，亦可直接使用绝对路径配置（例如 Windows 下填写 `"command": "C:\\Users\\<用户名>\\.local\\bin\\wechat-mcp.exe"`）。

```json
{
  "mcpServers": {
    "wechat": {
      "command": "wechat-mcp",
      "description": "绿泡泡聊天记录与群聊检索 MCP"
    },
    "dingtalk": {
      "command": "dingtalk-mcp",
      "description": "DuangDuang聊天记录、群聊与通讯录检索 MCP"
    }
  }
}
```

### 方案 C：本地源码 / 指定虚拟环境运行

> **注意**：如果是在特定的 Python 虚拟环境中，可将 `"command"` 设为对应环境的 Python 路径，`args` 设为 `["-m", "wechat_mcp.server"]`。本地源码调试时可追加 `"env": { "PYTHONPATH": "<源码绝对路径>/src" }`。

---

## 🗑️ 彻底卸载与依赖清理指南 (Complete Uninstall & Teardown)

如果您不再需要使用 `im-mcp`，或者希望在公共机器上彻底清理所有导出的聊天数据、依赖包及客户端配置，请按照以下标准顺序进行完整清理：

### 第一步：退出 AI 客户端或结束常驻 MCP 服务进程 (Stop Running Services)

> 💡 **核心提示**：AI 客户端（如 WorkBuddy、Claude Desktop、Pi、Cursor 等）在调用过工具后，通常会保持 `wechat-mcp` 与 `dingtalk-mcp` 服务进程常驻。这些进程打开的 SQLite 句柄与 `-shm` / `-wal` 锁会导致底层数据文件无法直接被删除。

- **推荐做法**：直接**完全退出当前 AI 客户端**应用；
- **或通过终端结束服务进程**（Windows PowerShell）：
  ```powershell
  Get-Process | Where-Object { $_.ProcessName -match "wechat-mcp|dingtalk-mcp" } | Stop-Process -Force
  ```
- *注：内置清理工具 `im-clean` 也已提供进程占用自检与 `--force` 强制结束机制。*

### 第二步：清理导出的本地解密数据库与明文缓存 (Data Cleanup)

运行内置的 `im-clean` 命令，或直接手动删除对应的全局数据目录：

```bash
# 方式 A：使用内置命令清理（自带进程占用检测，推荐）
im-clean -y

# 若遇到残留进程句柄拒绝删除，可追加 --force 强制结束进程并强力清除
im-clean -y --force

# 方式 B：手动删除用户家目录下的数据文件夹
# Windows (PowerShell):
Remove-Item -Recurse -Force ~/.wechat_mcp, ~/.dingtalk_mcp
# Linux / macOS:
rm -rf ~/.wechat_mcp ~/.dingtalk_mcp
```

### 第三步：卸载 CLI 工具与 Python 依赖环境 (Environment Teardown)

根据最初采用的安装方式，执行对应的卸载命令：

1. **若使用了 `uv tool install`（全局持久化安装）**：
   ```bash
   uv tool uninstall im-mcp
   ```
2. **若使用了 `uvx` 即席运行（临时缓存）**：
   ```bash
   # 清理 uv 缓存中的归档与虚拟环境缓存（释放磁盘空间）
   uv cache clean
   ```
3. **若使用了全局/环境 `pip install`**：
   ```bash
   pip uninstall im-mcp
   ```

### 第四步：清理各客户端的 MCP 配置文件与 Skill (Client Config Removal)

> ⚠️ **注意**：由于各 AI 客户端（WorkBuddy、Pi、Claude Desktop、Cursor、Cline、Cherry Studio 等）采用本地配置文件管理 MCP，程序无法跨权限修改其他软件的私有配置，**这一步需用户根据自己的客户端环境手动操作**（常见客户端配置路径与清理方法详见 [`skills/im-mcp/SKILL.md`](skills/im-mcp/SKILL.md) 附录参考目录）。

---

## 📂 项目结构

```text
├── src/
│   ├── wechat_mcp/       # 绿泡泡 MCP 核心模块
│   │   ├── config.py
│   │   ├── parser.py
│   │   ├── database.py
│   │   ├── server.py     # 绿泡泡 MCP 服务 (wechat-mcp)
│   │   └── tools/        # 绿泡泡一键提取与解密 CLI (wechat-init)
│   │       └── key_tool.py
│   └── dingtalk_mcp/     # DuangDuang MCP 核心模块
│       ├── decrypt.py    # V2/V3 AES-ECB + WAL 解密
│       ├── db.py         # 128分表聚合查询与联系人实名映射
│       ├── server.py     # DuangDuang MCP 服务 (dingtalk-mcp)
│       └── tools/        # DuangDuang一键同步 CLI (dingtalk-init)
├── pyproject.toml        # 标准打包与 optional-dependencies 定义
├── README.md             # 架构与接入文档
└── skills/               # 配套 Agent Skill 指南
    └── im-mcp/
        └── SKILL.md      # 智能体自动化初始化与清理技能规范
```

---

## 🔗 友情链接 (Friendly Links)

- [LINUX DO](https://linux.do/) - 真诚、友善、团结、专业，共建自由开放的技术社区。


