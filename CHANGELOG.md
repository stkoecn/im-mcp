# Changelog

本文件逐条记录本仓库相对上游 `stkoecn/im-mcp` 初始版本（v0.0.1）的全部改动，
便于上游维护者对照 review（对应 PR #10）。所有改动均已实测复现后再修改。

## 图片解密修复（`wechat_mcp/images.py`）

- **XOR 尾段密钥推导错误（致命）**：原实现按单个文件取 `tail_xor[-1] ^ 0xD9` 反推密钥，
  仅在 padding 恰好为整块时成立，其余情况解出的 JPEG 尾部不是合法 `FF D9`。实测同一
  目录下全部 `_t.dat` 缩略图投票一致，密钥是**全局单字节**（与具体文件无关，可由同目录
  缩略图自动推导），新增 `_detect_xor_key()`：扫同目录 `*_t.dat`（上限 48 个），用 AES 解
  `data[15:31]` 验 `FF D8 FF`，对 `data[-1] ^ 0xD9` 投票取众数并缓存；推导失败回退原逻辑。
- **原图为 `wxgf` 时无可渲染输出**：`<md5>.dat` 存的是私有 HEVC 容器（magic `wxgf`），
  宿主与通用图片库均无法渲染；可渲染的 JPEG 缩略图在 `<md5>_t.dat`。原实现优先选非 `_t`
  文件，因此永远返回 `wxgf`。新增 `_find_thumbnail_sibling()`（剥 `_t_W` / `_t` / `_h`
  后缀后定位 `<base>_t.dat`），命中 `wxgf` 时回退；`fmt` 判定同时补上
  `gif` / `webp` / `wxgf`。

## 新增 MCP 工具（绿泡泡侧 6 → 15 个，全部只读）

| 工具 | 说明 |
|---|---|
| `search_contacts` | 按昵称 / 备注 / 微信号搜索联系人 |
| `list_chatroom_members` | 群成员名单（自动关联昵称与微信号，支持 keyword 过滤） |
| `get_messages_by_range` | 任意时间范围取消息（支持跨天与任意时段，带 `offset`） |
| `get_chat_activity_stats` | 活跃度统计（消息总量、按小时 / 按天分布、发言最多的人） |
| `search_media_files` | 图片 / 文件 / 视频索引及磁盘路径（可按类型、月份筛选） |
| `list_favorites` | 收藏条目（标题 / 描述 / 来源） |
| `list_emoticons` | 表情包与表情（含官方文字说明） |
| `get_persona_style` | 沟通风格蒸馏（画像 + 高频用语 + 典型语句） |
| `get_sync_status` | 索引新鲜度与规模（判断是否需要重新同步） |

## 新增参数 / 行为

- `search_wechat_messages`、`get_messages_by_date`、`get_messages_by_range` 新增可选
  `offset` 分页参数（默认 0，向后兼容；此前单日 / 区间查询无法翻页）。
- 全部工具的 `limit` 统一钳制到合法区间（新增 `_clamp()`）。SQLite 中 `LIMIT -1`
  等价于「无限制」，原实现下 `limit` 传 0 或负数会让单次调用返回全表；绿泡泡与
  钉钉两侧策略一致。
- 搜索类工具 `query` 为空时返回明确的错误项，而不是静默返回空结果。
- `wechat_mcp.tools.key_tool` 的 `extract` 子命令新增 `--decrypted-dir`
  （代码本就读取该属性，此前只能经 `wechat-init` 间接传入）。

## 数据完整性 / 正确性修复

- **消息库漏索引**：除 `message_*.db` 外还有 `biz_message_0.db`，原 glob 只匹配
  `message_*.db`，整个库被跳过（本机恢复 5,191 条消息）。
- **FTS 索引膨胀**：ETL 每次重复写入造成 `messages_fts_docsize` 5 倍膨胀
  （1,505,094 → 256,040），改为按版本号只 rebuild 一次，并在 ETL 收尾重建。
- **中文检索召回**：FTS5 `unicode61` 分词器对中文召回极低（实测约 3.8%），改为
  `LIKE` 子串匹配（FTS 表仍维护，留作备用），并转义 `% _ \` 通配符。
- **引用消息解析**：被引用内容本身是 appmsg 时递归解析 `<title>`/`<des>`，并剥离
  `wxid_` / `gh_` / `@openim` 前缀；自闭合标签 `<tag />` 会吞掉后续同名真标签，
  改为扫描前先剔除。
- **解密主链路补 WAL 处理**：原先只解密 `.db`、完全忽略 `-wal`，若 WAL 真压着未
  checkpoint 的数据会丢消息。新增带三重保护的合并：页数守卫（WAL 记录的库页数小于
  主库文件页数 ⇒ 已 checkpoint 的过期残留，跳过）+ 只应用最后一个提交帧及之前的帧
  + 合并后 `quick_check` 校验失败即回退为仅解密数据库本体。
- **DuangDuang 解密**：输出目录残留的 `dingtalk.db-wal` / `-shm` 会被 SQLite 叠加到
  新解密库上（实测读成 `database disk image is malformed`），解密前清理；
  `DINGTALK_DATA_DIR` 带尾部分隔符时版本被误判为 V2 而用错密钥。
- **DuangDuang 检索**：分表检索改为扫描全部分表后全局按时间排序再截断（原先按分表
  顺序边扫边停，结果被低编号分表主导）；`LIKE` 补 `ESCAPE`。
- **多账号**：`db_storage` 探测不再依赖 glob 顺序（实测会固定选中较旧账号），改取
  最近写入者；`wechat-init` 与 MCP 侧使用同一策略。
- **密钥工具**：`icacls` 失败时如实上报（原先不看返回码，失败也报「已限制」，等于
  谎报密钥文件已收紧）；`load_passphrase()` 对非法内容不再让 `bytes.fromhex` 抛异常
  中断整个 extract。
- **派生表结构**：`media_files` 主键改为 `id` + `UNIQUE(md5, rel_path)`，同一张图在
  多个会话下的路径不再互相覆盖；派生表带版本号，结构升级时整体重建。
- **其他健壮性**：`limit` / `offset` 边界、时间戳溢出兜底、缺表时 `init_schema()`
  自愈、旧库无 `Msg_` 表时跳过、`Name2Id` 读取失败不再写入不稳定会话键、会话昵称
  预载消除 N+1 查询、`sync_data` 返回真实统计（原先统计缺失导致显示为 0）。
- **依赖锁定**：`uv.lock` 与 `pyproject.toml` 版本对齐（原先 `uv lock --check`
  直接报错：lockfile needs to be updated）。

## macOS 微信 4.1+ 密钥捕获（lldb）稳定性修复

以下两个缺陷均会导致**用户微信进程被冻结或强杀**，已在真机（macOS 12.6 x86_64，
微信 4.1.15）复现并修复：

- **断点命中后进程冻结**：原实现把 `memory read` + `process continue` 写成 lldb
  断点命令块。lldb 默认 `stop-command-source-on-error=true`，而微信在独立工作线程
  调用 `CCKeyDerivationPBKDF`，`$rsi` 指向栈上临时缓冲区，命中瞬间 `memory read`
  可能失败；一旦失败，lldb 会中止命令块剩余命令，导致末尾的 `process continue`
  永不执行，微信被永久冻结在断点上（实测进程状态 `SX` 且 CPU 时间零增长）。
  改为 **Python 断点回调 + `try/finally` 无条件 `proc.Continue()`**，并叠加 12 秒
  冻结看门狗（回调失效时自动 detach 解冻）。
  附注：`settings set interpreter.stop-command-source-on-error false` 对断点命令块
  无效；在命令块内用 `-s python` 也无法注册回调，必须用独立 `.py` 文件 + `-F`。
- **收尾阶段强杀微信（关键）**：原 `_cleanup_lldb_orphans_macos()` 会执行
  `pkill -TERM -f "LLDB.framework.*debugserver"`。真机隔离实验证明：**向 debugserver
  发送任何信号（SIGTERM 或 SIGKILL）都会使 macOS 调试子系统连带把 tracee（微信）
  SIGKILL 强杀**（现象：`runningboardd` 记录 `termination reported by launchd (2, 9, 9)`，
  且因 SIGKILL 不可捕获而**不产生崩溃报告**）。原注释只警告了 `-9`，实际 `-TERM`
  同样致命。
  现改为：**完全不触碰 debugserver**，仅在 tracee 已不存在时才向孤立 lldb 前端
  发送 SIGTERM，且永不使用 SIGKILL；tracee 存活时直接跳过全部清理。
  另新增 `_wait_lldb_exit()`（确认 detach 完成后再清理）与
  `_resume_tracee_if_stopped()`（清理前恢复暂停态，避免停在 `int3` 上触发
  `EXC_BREAKPOINT/SIGTRAP`）。
- **稳定性结论**：同一账号在同一机器上多次重新登录，捕获到的 passphrase 恒定不变
  （实测 4 次完全一致，均为同一 64 位十六进制值）。因此一次捕获通过 HMAC 自证即可
  确认成功，**无需反复让用户退出/登录微信**（反复登录存在账号风控风险）。

## 文档

- `README.md` 与 `skills/im-mcp/SKILL.md` 补齐全部工具、参数与 CLI 说明，修正
  DuangDuang 侧工具名与参数名。
- `skills/im-mcp/SKILL.md` 新增 **「智能体绝对禁令：严禁手动清理 lldb / debugserver」**
  一节，以真机实验表格形式列明哪些信号会连带杀死微信、哪些操作安全，并规定：
  - 禁止在 `wechat-init` 运行期间或刚结束后自行 `pkill` / `kill -9` 调试器进程；
  - 必须等 `wechat-init` 主进程自行退出后再做任何判断；
  - 残留 lldb 孤儿进程默认保持不动（惰性无害）；确需清理只能 `pkill -x lldb`；
  - 微信“意外消失”时用 `log show ... | grep "termination reported"` 判断是否为
    SIGKILL，切勿把“无崩溃报告”误判为“未崩溃”；
  - **禁止反复要求用户退出/登录微信来“验证稳定性”**（一次 HMAC 自证通过即足够，
    反复登录有账号风控风险）。
