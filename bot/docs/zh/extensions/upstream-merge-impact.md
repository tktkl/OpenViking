# bot-main 本地扩展与上游合并影响

本文用于更新官方 OpenViking 代码前评估本地改动，并在合并后核对功能是否保留。覆盖 `bot-main` 已提交、已暂存、未暂存及未跟踪的 Bot 文件。功能细节分别见 [飞书服务台](feishu-helpdesk.md) 和 [后台任务](background-tasks.md)。

当前实现采用“扩展目录承载业务逻辑，核心提供少量接入点”。可以继续合并上游，但仍需维护核心接入点；Git 自动合并成功不能证明行为兼容。本文的冲突等级是维护优先级，不是对未来提交的冲突预测。

## 核对基线与范围

最后核对日期：**2026-10-30**。

| 项目 | 核对时的值 |
| --- | --- |
| 本地分支 | `bot-main` |
| 官方远端 | `upstream`：`https://github.com/volcengine/OpenViking.git` |
| Fork 远端 | `origin`：`https://github.com/tktkl/OpenViking.git` |
| 本地上游引用 | `upstream/main`：`05c516dc6` |
| 本地 HEAD | `544e6fffb`，已合并上述上游提交 |
| 额外功能提交 | `7f020f97e` 服务台渠道与 CLI 保活；`d7bd92a56` 可信服务台上下文 |

此次只核对了本地上游引用，没有拉取远端新提交。后续评估必须先取得新的上游基线，不能将上述提交号当作官方最新版本。

核对时，源码相对该上游基线有 **17 个文件变化：修改 7 个已有文件，新增 10 个扩展文件**；另有 7 个测试文件、2 份功能文档变化。此数量不包含本文，后续新增文件后应重新统计。

服务台和可信来源的主体已提交；后台任务、通用运行时接缝及部分服务台修复仍有未提交改动。核对时，下列文件尚未被 Git 跟踪，普通 `git diff` 不会显示其内容：

- `bot/vikingbot/extensions/background_tasks/segments.py`
- `bot/tests/test_background_task_segments.py`

以上提交状态是此次快照，不作为后续判断依据。每次更新前重新检查暂存区、工作区和未跟踪文件，并保存需要保留的改动。

## 七个核心接入点

以下路径均相对仓库根目录；链接指向实际文件。上游触及这些方法、调用点或消息契约时，即使没有文本冲突，也需要人工核对。

| 核心文件 | 本地改动及必须保留的行为 | 上游更新后的检查重点 | 关注程度 |
| --- | --- | --- | --- |
| [agent/loop.py](../../../vikingbot/agent/loop.py) | 创建并启动/关闭运行时扩展；主队列执行内部通知；续办开始前调用 `before_process`；普通消息和 steer 更新来源上下文 | `AgentLoop` 生命周期、关闭顺序、消息调度、steer、内部事件与用户轮次隔离；避免扩展关闭晚于沙箱释放 | 高 |
| [agent/context.py](../../../vikingbot/agent/context.py) | `set_message_source()` 将已确认服务台的渠道 metadata 转换为独立 system 来源区块；新消息更新或移除旧来源 | `build_messages()` 参数与调用链、系统消息顺序、上下文压缩；正文不能伪造服务台来源，普通 IM 不继承旧来源 | 中高 |
| [channels/manager.py](../../../vikingbot/channels/manager.py) | 配置启用时选择服务台子类；发送后调用 `on_delivery`，仅渠道明确返回 `True` 才确认成功 | 渠道创建流程、发送返回值、异常路径；回执异常不能停止公共发送器，普通消息没有回执也能正常发送 | 中高 |
| [channels/feishu.py](../../../vikingbot/channels/feishu.py) | 将原有 IM 事件注册提取为 `_event_handler_builder()`，允许子类追加工单事件后统一 `build()` | 飞书 SDK 事件构建、长连接启动和回调注册；保留原生 IM 行为，避免复制启动流程或重复注册事件 | 中 |
| [config/schema.py](../../../vikingbot/config/schema.py) | 增加默认关闭的 `helpdesk` 配置与 `runtime_extensions` 配置；服务台专属字段由扩展模块定义 | 配置解析、默认值、类型导入及已有配置兼容；未启用时不启动扩展任务 | 中低 |
| [bus/events.py](../../../vikingbot/bus/events.py) | 入站增加 `internal_handler/internal/before_process`，出站增加 `on_delivery`；均有兼容默认值 | 消息构造、复制、序列化与投递方式；这些回调只在进程内使用，不能从用户正文或渠道 metadata 重建 | 中低 |
| [bus/queue.py](../../../vikingbot/bus/queue.py) | `count_pending_inbound_session()` 排除扩展内部事件 | 待处理用户消息计数、中断逻辑、队列实现变化；内部通知不能消耗用户中断计数 | 低 |

## 扩展目录与能力归属

| 新增模块 | 职责 | 与上游的依赖 |
| --- | --- | --- |
| [extensions/runtime.py](../../../vikingbot/extensions/runtime.py) | 可信配置指定 `module:factory`，提供 `create_extension(context, options)`、`start()`、`close()` 接口 | 主循环生命周期、工具注册、消息总线、会话与沙箱管理接口 |
| [extensions/feishu_helpdesk/](../../../vikingbot/extensions/feishu_helpdesk/) | 工单事件订阅、Bot 入群、CLI 授权检查与保活、工单/会话绑定、有限重试；继承原生飞书消息处理和发送 | `FeishuChannel` 的事件注册、启动/停止与消息处理方法，以及飞书 SDK 和 lark-cli 契约 |
| [extensions/background_tasks/](../../../vikingbot/extensions/background_tasks/) | 注册 `background_task`；固定 task/run 监测、绑定原会话、完整结果分页读取、编号分段、回执游标、故障隔离、重启恢复、补充暂存与续办 | `RuntimeContext`、`ToolContext`、原 session 的沙箱文件/执行 API、会话保存、内部队列和发送回执 |
| [extensions/__init__.py](../../../vikingbot/extensions/__init__.py) | 扩展包入口 | Python 包布局和打包规则 |

新增目录通常比修改现有核心流程更少产生直接文本冲突，但仍依赖上游接口。若上游新增同名扩展目录、原生后台任务或服务台能力，应先比较职责和迁移方式，避免并行启动两套任务。

后台扩展可在后续打包为独立 Python 包，通过 factory 指向安装位置；迁出业务代码后，七个核心接入点仍需保留或迁移到上游等价接口，不能仅删除它们。

## 需要保持的行为边界

- **取消**：没有新增飞书即时取消入口。此次核对时，`AgentLoop.interrupt()` 函数本体与上游一致；`loop.py` 和 `queue.py` 仍有内部事件隔离改动。自然语言取消需等 Bot 处理消息，明确 task/run 后再通过现有工具停止，不能宣称收到消息就立即取消。
- **默认关闭**：`helpdesk.enabled=false`、`runtime_extensions=[]` 时不启动这些扩展。普通单聊、群聊、白名单、命令授权和话题行为继续沿用原生链路。
- **来源可信度**：服务台身份由渠道确认的绑定生成，用户正文不能设置；模型侧的业务环境规则由 engineering skill 判断。上游重构上下文时不能把这一来源区块降为普通用户文本，也不能让旧工单来源残留到其他消息。
- **结果回传**：Codex 最终答复由扩展交给 Bot 渠道发送，普通完成不增加一次模型改写；只有暂存补充需要新的编排轮次。发送游标仅在成功回执持久化后推进。
- **执行范围**：Bot 的 `exec` 工具实现、MCP 连接实现和沙箱权限逻辑没有因此修改。Codex CLI 的 full access、任务进程管理、认证包装器及共享 skills 在独立的 `agent-skills/wangoon` 仓库维护，不计入此分支的源码差异；升级时仍需核对 runner 协议与双端部署。
- **认证职责**：业务取证凭据由 `wangoon-auth-mcp` 提供；服务台固定渠道代码的事件订阅及邀请/保活，沿用服务台配置与 lark-cli 授权，详见服务台文档。两条链路不能混为同一份认证缓存。
- **持久状态**：扩展 journal、结果分段和 runner 状态使用工作区 `tmp/wangoon/`。部署更新需保留仍需恢复的任务状态；代码合并不应顺带清空该目录或更换其权限、挂载位置。

## 下一次更新的评估步骤

以下命令在仓库根目录执行。先保存当前已提交以外的改动，包括需要保留的未跟踪文件；不要直接在有重叠改动的工作区执行合并。只保存代码和必要文档，不将凭据、任务日志或工作区 tmp 加入提交。

先确认当前状态及差异范围：

```bash
git status --short
git log --oneline upstream/main..HEAD
git diff --stat upstream/main
git diff upstream/main -- bot/vikingbot
git ls-files --others --exclude-standard -- bot
```

`git diff upstream/main` 包含已提交、已暂存和未暂存的受跟踪文件差异，仍不包含未跟踪文件。上述上游引用在 fetch 前可能过期。

保存改动后取得新的官方引用；fetch 不合并代码。用共同祖先分别查看本地和上游的变化，避免将新增上游差异误判成本地补丁：

```bash
git fetch upstream
merge_impact_base=$(git merge-base HEAD upstream/main)
git log --oneline HEAD..upstream/main
git diff --name-status "$merge_impact_base" HEAD -- bot
git diff --name-status "$merge_impact_base" upstream/main -- bot
git diff "$merge_impact_base" upstream/main -- bot/vikingbot/agent bot/vikingbot/bus bot/vikingbot/channels bot/vikingbot/config bot/vikingbot/sandbox bot/vikingbot/session
```

根据七个核心接入点和扩展依赖核对上游变化，再按仓库的合并流程处理。处理冲突时保留上游的新行为，并重新接入扩展；不要整文件选择 ours/theirs。若上游已有等价接口，核对语义后优先迁移到该接口。

合并后复查：配置默认关闭时的行为、启用服务台后的原生 IM 行为、后台结果直接回传与回执、来源可信度、任务身份和重启恢复。新增接口能自动合并但语义变化时，同样需要调整代码和测试。

## 回归检查与验证记录

下列命令使用已安装 Bot 测试依赖的 Python，在仓库根目录运行；测试使用本地模拟，不访问生产服务、不发送真实飞书消息，也不运行付费 Codex 任务：

```bash
PYTHONPATH=bot:. PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -c bot/pytest.ini \
  bot/tests/test_background_tasks.py \
  bot/tests/test_background_task_segments.py \
  bot/tests/test_channel_delivery_metadata.py \
  bot/tests/test_agent_loop_outcome.py \
  bot/tests/test_exec_tool.py \
  bot/tests/test_message_source_context.py \
  bot/tests/test_feishu_helpdesk.py \
  bot/tests/test_feishu_helpdesk_api.py \
  bot/tests/test_feishu_helpdesk_cli.py -q --tb=short
git diff --check
```

若本次还升级了 `agent-skills/wangoon/codex`，在该仓库按其文档运行模拟 runner 测试，并确认 Bot 扩展要求的协议 v2、固定 run 校验和完整结果分页仍兼容。Bot 代码测试通过不代表实际 MCP、共享 skills、CLI 和远端沙箱部署已验证。

重点验证以下可观察行为，避免仅检查文件是否存在：

1. 服务台关闭或初始化失败时，普通 IM 仍可使用；启用后同群工单事件不重复邀请，关单不恢复旧绑定，授权与重试仍有限。
2. 可信来源只出现在已确认工单消息；伪造正文无效，后续普通消息和 steer 正确更新或移除来源。
3. start/resume 后监测实际 task/run，旧 watch 不能停止新 run；仅查询登记不代表业务完成，失败不自动重跑写任务。
4. 结果完整分页、校验后按顺序分段发送；成功回执才推进，失败或重启只重试未确认段，全部确认后才派发补充续办。
5. 登记、补充、ack、回执或结果文件写入失败时不制造成功状态；单 watch 故障不停止其他任务，修复后可显式 retry。
6. 内部通知不调用模型、不 steer 用户轮次、不消耗用户中断计数；内部续办不扩展上游取消接口。
7. 慢查询不阻塞其他 watch；扩展关闭先取消并等待监测任务，再释放 journal 锁和沙箱。

**2026-10-30 验证快照**：以上 Bot 相关回归为 **306 passed**，有 8 条既有弃用警告；`interrupt()` AST 与上述上游基线一致。本次没有真实 MCP、飞书渠道或部署验收。此记录只证明当时的工作区状态，不能代替下一次合并后的测试。

## 每次更新后维护本文

记录新的核对日期、上游基线和本地提交，重新检查未提交/未跟踪文件；若核心接入点增减或迁移到上游接口，同步更新清单。补充实际执行的测试、结果与未验证部署项。提交状态变化后更新快照描述，避免下一次把历史未提交文件误认为仍未保存。
