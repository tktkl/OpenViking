# VikingBot 会话并发与飞书服务台使用手册

本手册独立维护，适用于 `codex/vikingbot-helpdesk-followup` 分支中的会话并发、服务台主动跟进、Web Messenger 工单回复、普通 IM 升级和 Python 独立关单功能，便于后续合并上游时保留。部署代码应包含 Python 独立关单、发送恢复、升级和关单兼容修复。普通 PyPI 发行包不保证包含本分支功能。

已有部署的升级步骤是：更新 VikingBot 代码、保留官方 Lark CLI 登录态和 auth-mcp 的 `feishu_helpdesk` Web 会话、合并下面的配置，然后重启 Gateway。不要把 `reply.mode` 设为 `feishu_web` 后只保留 CLI 登录；Web 会话 Cookie 和 CLI 授权是两套独立认证。SQLite 结构只新增可选字段，无须删库迁移。

## 1. 部署前准备

准备 Linux 或 macOS 运行环境，推荐 Python 3.12。沿用已能正常运行的 OpenViking 模型、存储和沙箱配置；服务台判断复用现有模型，无须另建模型服务。

还需要以下信息及授权：

| 项目 | 用途与要求 |
| --- | --- |
| 飞书企业自建应用的 App ID、App Secret | 与现有 VikingBot 飞书渠道及 CLI profile 使用同一个应用，启用机器人能力 |
| 服务台 ID、服务台 Token | 在服务台管理后台“设置中心 → API 凭证”获取；用于服务台读取、订阅及关单请求头 |
| CLI 当前用户 | 有目标工单权限的客服、管理员或所有者；能够访问关联群，并具有所需邀请权限 |
| 应用权限及发布 | 开通服务台读取/订阅权限 `helpdesk:all:readonly`，保留原有机器人消息与成员查询权限；完成应用发布和所需租户审批 |
| 用户 OAuth 授权 | 关单需要 `helpdesk:all`；CLI 读取、邀请 Bot 及续期还需要下文列出的权限 |
| auth-mcp Web 会话 | `reply.mode="feishu_web"` 需要 `wangoon-auth-mcp` 的 `feishu_helpdesk` prod profile 为 ready，并提供完整原始 Cookie；Cookie 不写日志、不落盘 |
| 无头浏览器与依赖 | 自动解析 `oc_* → feed_id` 需要 Chromium/Playwright 可执行文件和 `websocket-client`；也可用 `reply.feed_ids` 手工固定映射 |
| 升级群 Bot 成员关系 | `escalation.sender="bot"` 时当前应用 Bot 必须已在每个升级群中；发送走官方 IM API，不使用 Web Cookie |

开放平台事件接收方式沿用长连接，保留标准 IM 事件 `im.message.receive_v1`。扩展在启动时调用服务台订阅接口，复用同一条长连接接收工单创建、更新及 `helpdesk.ticket_message.created_v1`。无须新建 Webhook 或 cron。

服务台 Token 和 CLI 用户授权都需要配置。Token 不能替代用户的工单访问权限。相同应用只运行一个接收事件的 Gateway，避免与另一个 Gateway 或调试进程重复消费。

## 2. 安装包含功能的代码

在独立部署目录操作。将仓库地址替换为包含该分支及所需提交的实际地址，目录可按运行账号的权限调整：

```bash
git clone --branch codex/vikingbot-helpdesk-followup <包含该分支的仓库地址> /srv/vikingbot-helpdesk
cd /srv/vikingbot-helpdesk
git log -1 --oneline
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[bot]"
python -m vikingbot.extensions.feishu_helpdesk.close_helper --version
```

最后一条命令只检查随包安装的 Python 关单模块，不读取用户凭据。预期返回：

```json
{"protocol_version": 1, "implementation": "python"}
```

未安装官方 CLI 时，可按官方方式安装：

```bash
npx @larksuite/cli@latest install
```

后续升级已有部署时，在该部署 checkout 中更新代码并重新执行安装命令。不要在保留其他未提交工作的 `bot-main` 工作区切分支或覆盖文件。

## 3. 配置官方 CLI 的客服身份

**以下命令必须在 Gateway 的运行账号下执行，并使用相同的 HOME、CLI 目录及工作区环境。** 交互终端登录成功，不代表 systemd、容器或其他系统账号能读取同一份登录态。

先查看已有 profile：

```bash
lark-cli profile list
```

已有与渠道相同应用的 profile 可以复用。下面统一以 `support` 为例；还没有时运行：

```bash
lark-cli config init --name support
```

在向导中选择“手动输入应用凭证”，填写 VikingBot 渠道已有的 App ID、App Secret 和对应品牌。不要为服务台另外创建一个应用。

检查 profile，并以有工单权限的客服用户完成授权：

```bash
lark-cli --profile support config show
lark-cli --profile support auth login \
  --scope "offline_access helpdesk:all im:message.send_as_user im:message im:chat.members:write_only im:message:readonly im:message.group_msg:get_as_user"
lark-cli --profile support auth status --json
lark-cli --profile support api GET /open-apis/authen/v1/user_info --as user --format json
```

核对应用、品牌、当前用户及授权权限。`auth status` 检查本地状态，最后一条只读请求验证实际用户授权，并由 CLI 在需要时续期。新增权限时，先开通应用权限并发布，再重新进行用户授权。

工单群的欢迎、提醒和关闭说明在 `reply.mode="feishu_web"` 下使用 auth-mcp 的 Web 会话身份；普通 IM 升级群默认使用应用 Bot 官方 IM API；工单消息读取、回执对账、Bot 入群、授权检查和续期仍由官方 CLI 以当前用户执行。Python 子进程读取该用户的现有加密凭据，只负责固定的服务台关单请求。凭据只在子进程内存使用，不导出 Token、不另存凭据、不自行使用 refresh token 续期。

初版关单仅支持本地 profile 登录。环境 Token、环境应用凭据、自定义 provider 或未知凭据格式会使关单暂缓。macOS 后台进程还必须能读取原有文件主密钥或访问对应 Keychain；建议使用实际服务运行方式完成测试。

## 4. 合并到现有 `ov.conf`

下面是需要合并的配置片段，**不能用它整份替换原有配置**。在已有飞书渠道中增加 `helpdesk`，保留原有模型、存储、沙箱、其他渠道、白名单和命令权限。配置路径是 OpenViking 的 `bot.channels[].helpdesk`，不使用 Hermes 的 `platforms.feishu.helpdesk`。

```json
{
  "bot": {
    "agents": {
      "max_concurrent_sessions": 4
    },
    "channels": [
      {
        "type": "feishu",
        "enabled": true,
        "app_id": "cli_your_app",
        "app_secret": "<应用 App Secret>",
        "domain": "https://open.feishu.cn",
        "bot_name": "<机器人显示名>",
        "allow_from": [],
        "thread_require_mention": true,
        "helpdesk": {
          "enabled": true,
          "helpdesk_id": "<服务台 ID>",
          "helpdesk_token": "<服务台 Token>",
          "followup_interval_minutes": 5,
          "messages": {
            "prefix": "【AI分身自动回复】",
            "welcome": "您好，请描述遇到的问题，并补充相关截图或其他信息。如已经提供，可忽略此消息。",
            "second_reminder_suffix": "如仍未收到{audience}，{minutes}分钟后将自动关闭本工单。",
            "escalation_reminder_suffix": "如仍未收到{audience}，{minutes}分钟后将升级处理。",
            "closed_resolved": "问题已解决，工单已关闭。",
            "closed_timeout": "因催办后仍未收到业务补充或确认，工单已关闭。"
          },
          "reply": {
            "mode": "feishu_web",
            "auth_mcp_server": "wangoon-auth-mcp",
            "provider": "feishu_helpdesk",
            "environment": "prod",
            "profile": "default",
            "actor_open_id": "ou_actor_open_id",
            "gateway_url": "https://internal-api-lark-api.feishu.cn/im/gateway/",
            "web_url": "https://tenant.feishu.cn/next/messenger/",
            "feed_ids": {},
            "auto_resolve_feed_id": true,
            "browser_executable": "",
            "feed_resolution_timeout_seconds": 60,
            "feed_cache_path": "",
            "cookie_cache_seconds": 60
          },
          "escalation": {
            "enabled": true,
            "interval_minutes": 45,
            "sender": "bot",
            "groups": {
              "TMS": "oc_tms_escalation_group",
              "SCM": "oc_business_escalation_group",
              "SRM": "oc_business_escalation_group",
              "QMS": "oc_business_escalation_group",
              "PDM": "oc_business_escalation_group",
              "PLM": "oc_business_escalation_group"
            }
          },
          "cli": {
            "executable": "lark-cli",
            "profile": "support",
            "timeout_seconds": 30,
            "auth_check_interval_seconds": 60,
            "keepalive_interval_seconds": 1800
          }
        }
      }
    ]
  }
}
```

国际版 Lark 的 `domain` 使用 `https://open.larksuite.com`，CLI profile 品牌也必须一致。服务进程找不到 CLI 时，可将 `executable` 改为官方 CLI 可执行文件的绝对路径。完整 JSON 示例见 [feishu-helpdesk-config.example.json](feishu-helpdesk-config.example.json)。

| 配置 | 默认值 | 使用说明 |
| --- | --- | --- |
| `bot.agents.max_concurrent_sessions` | `1` | 严格正整数；示例 `4` 允许不同会话并发，`1` 保持全局顺序处理 |
| `helpdesk.enabled` | `false` | 显式设为 `true`，同时提供 ID 和 Token |
| `helpdesk.followup_interval_minutes` | `5` | 严格正整数，单位分钟；首次催办与上次消息之间的间隔 |
| `helpdesk.escalation.enabled` | `true` | 开关升级；关闭时保留无升级路由的兼容行为 |
| `helpdesk.escalation.interval_minutes` | `45` | 升级后重复提醒间隔，单位分钟 |
| `helpdesk.escalation.sender` | `bot` | 升级群发送身份；`bot` 走官方 IM API，`lark_cli` 走 CLI 用户身份，均不使用 Web Messenger |
| `helpdesk.escalation.groups` | 空 | 系统名到升级群 `oc_*` 的映射；配置后按工单最初选择的系统路由 |
| `helpdesk.messages` | 示例中的文案 | 可全部省略；欢迎、提醒及关闭说明统一带 `prefix` 和换行 |
| `helpdesk.reply.mode` | `lark_cli` | 服务台工单群推荐 `feishu_web`；该模式使用 auth-mcp Cookie 和 Web Messenger，不回退到 CLI 发送 |
| `helpdesk.reply.auto_resolve_feed_id` | `false` | 开启后缓存未命中时启动无头 Chromium 解析 `oc_* → feed_id`；已有 `feed_ids` 时优先使用映射 |
| `helpdesk.reply.cookie_cache_seconds` | `60` | auth-mcp Cookie 的内存缓存秒数，不落盘 |
| `helpdesk.cli.executable` | `lark-cli` | 使用官方 CLI；Python 关单模块随 VikingBot 自动启动 |
| `helpdesk.cli.profile` | 空，使用默认 profile | 建议明确指定 `support`，避免切换默认 profile 后身份变化 |
| `helpdesk.cli.timeout_seconds` | `30` | 单次操作超时，包含锁等待、脚本、授权准备、最终检查和请求；不是业务静默间隔 |
| `helpdesk.cli.auth_check_interval_seconds` | `60` | 用户授权状态检查间隔，单位秒 |
| `helpdesk.cli.keepalive_interval_seconds` | `1800` | 通过 CLI 进行用户授权保活的间隔，单位秒 |

配置修改后重启生效。分钟数不能写成 `0`、小数、布尔值或字符串。连续跟进次数固定为两次，不另设次数配置。

欢迎正文固定为示例中的一句话，不包含时间、不调用模型，统一使用“您好”。首次提醒在配置升级路由时追加 `escalation_reminder_suffix`，无升级路由时首次提醒追加旧版兼容字段 `second_reminder_suffix`；两者都必须且只能包含一次裸 `{minutes}`，可选使用 `{audience}`。等待业务补充或确认时，`audience` 为“业务方的补充或确认”；等待处理时，为“产品/研发/测试的处理进展或结论”。跟进正文仍由模型结合对话生成，首句问候统一为“您好”。

启用升级后，首次提醒再等待一个跟进间隔仍无解决，就向对应升级群发送包含客户名、工单号、系统和原工单群可点击链接的通知，并附带模型根据当前对话生成的简短“问题概要”。同一轮 45 分钟重复提醒复用摘要，业务产生新消息后才重新生成；摘要失败不阻塞升级。之后每 45 分钟重复，直到工单解决或关闭。升级群默认以应用 Bot 身份通过官方 IM API 发送。原工单群继续同步和判断，但不再发送提醒。升级路由启用时，首次提醒后不会因业务静默自动超时关单。

示例 `allow_from: []` 代表不限制用户；已有白名单时保留原值。`thread_require_mention: true` 沿用普通 Agent 回答需要 `@Bot` 的规则，服务台人工活动监测不要求 `@Bot`。

## 5. 启动与运行检查

默认配置文件为 `~/.openviking/ov.conf`；也可通过 `OPENVIKING_CONFIG_FILE` 或显式参数指定：

```bash
vikingbot gateway --config /path/to/ov.conf
```

已使用 OpenViking 一体模式的部署，继续使用：

```bash
openviking-server --config /path/to/ov.conf --with-bot
```

选择现有部署方式之一，同一应用只启动一个 Gateway。进程管理器需使用安装依赖的 Python 环境，并能找到官方 CLI；重启后查看标准输出或现有服务日志。

可用以下日志定位初始化情况：

```text
Agent loop started (max concurrent sessions: 4)
Feishu Helpdesk ready
Feishu Helpdesk CLI: authorized user keepalive active; credentials managed by lark-cli
```

`ready` 表示初始化成功，完整关单能力仍需测试工单验证。若出现 `Feishu Helpdesk unavailable; native IM remains active`，普通 IM 可以继续使用，但服务台扩展没有初始化成功；修复配置、权限或网络后重启。

## 6. 工单发现和自动跟进规则

服务台会主动查询。实时事件负责及时接收变化，启动后每 10 秒增量发现负责补漏；新工单及状态、群关联发生变化的工单优先定向同步，失败按 10、20、40、60 秒上限退避重试。存量工单每 60 秒最多轮转补扫 10 张；本地期限调度最多每 10 秒校准一次自然时间。这里的发现与补扫周期和 5 分钟业务间隔相互独立。

首次启用时持久化接管起点，只接管此后新建且未关闭的工单。首次启用前的历史工单不会批量发送欢迎；已有数据库的部署会继续原有接管记录，并补发现停机期间的新工单。

工单必须具有可信群关联才能发送消息。尚无群 ID 时保持“待绑定”，等待工单事件或消息详情补齐，通常需工单进入有群会话的阶段；不会按群名猜发送目标。重启先核对状态并补齐消息，再允许到期动作执行。

### 默认时间线

每个新工单只成功欢迎一次，实际消息为：

```text
【服务台自动消息】
您好，请描述遇到的问题，并补充相关截图或报错信息。
```

没有人工沟通时，从欢迎成功发送时间开始计时；有人工沟通后，从最后一条人工消息的产生时间计算。本轮一直静默且提醒均成功时：

| 时间 | 动作 |
| --- | --- |
| 静默满 5 分钟 | 第一次提醒 |
| 第一次成功发送后再静默 5 分钟 | 仍未解决时升级到配置的系统群 |
| 升级通知成功后每 45 分钟 | 升级群重复提醒，直到解决或关闭 |

通常对应第 3、6、9 分钟升级。排队、网络及状态核对可能使实际执行稍晚；发送失败不增加次数，也不推进下一期限。停机恢复后不会一次补发多轮提醒，下一次等待从本次成功时间重新计算。

### 三种场景

| 当前对话情况 | 自动跟进 | 首次成功催办后（已配置升级） |
| --- | --- | --- |
| 业务已提问题，等待处理方响应、调查或解决 | 提醒产品／研发／测试响应和推进 | 再静默一个间隔后升级，原工单群停止提醒 |
| 等待业务描述问题或补充信息 | 请业务描述问题、补充信息 | 再静默一个间隔后升级，原工单群停止提醒 |
| 处理方已给结果，等待业务验证或确认 | 请业务验证或确认结果 | 再静默一个间隔后升级，原工单群停止提醒 |

首次提醒会明确告知“N 分钟后将升级处理”。业务确认，或模型有充分对话依据判断已解决，会立即关单，不必等待催办或升级。模型失败、来源未核实、仅附件无法判断或信息不足时暂缓关单并继续升级节奏。

已解决关单使用 `solved=2`。旧版无升级路由的兼容模式才会在 `awaiting_details` / `awaiting_confirmation` 首次提醒后再等待一个间隔，以 `solved=1` 超时关单；`awaiting_handler` 不超时关单。关闭工单不会关闭聊天页面，远端确认关闭后只尝试发送一次关闭说明。

### 人工消息、并发和旧判断作废

任何话题的新人工消息到达接收层，就清零本轮次数并作废旧期限。文本、图片和文件均计入人工活动；CLI 当前用户手动发言也计入。自动欢迎、提醒、关闭说明及系统消息不重置计时。

普通回答与服务台任务共用 `max_concurrent_sessions`。不同完整 SessionKey 可以并发，同一会话顺序执行；配置 `4` 不代表同时进行四次 CLI 发送，CLI 操作仍共用锁。提高并发时需留意模型服务的容量。

排队、模型返回、发送及关单前都会核对轮次。关单前重读工单和最新消息，同群其他话题仍有活动、排队或最终保存任务时推迟关闭。服务台 API 不支持原子条件更新，最后读取和远端关单请求之间仍存在网络时间窗口。

正常关闭后再次提问应产生新工单，新 ID 独立接管并欢迎。若外部系统重新打开同一 ID，兼容逻辑保留欢迎记录、清零本轮提醒，等待关闭之后的新人工沟通，不拿旧的解决结论立即关单。

## 7. 测试服务台验收

首次启用、切换运行账号或升级 CLI 后，先在测试服务台验收。关单会真实修改工单状态。

1. 启动后创建新工单并建立群关联，确认欢迎只发一次且发送者为 `actor_open_id` 对应的 Web 会话用户；首次解析应写入 feed 缓存，后续不再启动新浏览器。
2. 不描述问题并持续静默，核对第 5／10 分钟的首次提醒和升级；首次提醒应写明 5 分钟后升级。
3. 确认 TMS 和其他系统分别进入配置映射的升级群，发送者为应用 Bot；首次通知后每 45 分钟重复，原工单群不再提醒。
4. 在升级后发送人工消息，确认原工单群仍恢复判断但不恢复提醒；可信“已解决”沟通应立即关单，只发送一次关闭说明。
5. 在一个会话中等待较慢回答，同时向另一会话提问，确认第二个会话能处理；同会话仍保持顺序。
6. 停机期间新建工单后重启，确认补接管；已欢迎工单重启后不重复欢迎，也不补发多轮过期提醒或升级通知。

本地模拟测试覆盖了调度、状态机及凭据协议，但不能替代真实服务台、用户权限和 macOS Keychain 的验收。

## 8. 持久化、升级与回退

服务台状态保存在：

```text
{storage.workspace}/bot/helpdesk/<SHA256(app_id:helpdesk_id)>/state.sqlite3
```

未配置 `storage.workspace` 时，默认工作区为 `~/.openviking/data`。SQLite 保存首次启用时间、工单与消息、欢迎记录、轮次、期限、发送回执及关单待确认操作，不保存 OAuth 或服务台 Token。单实例文件锁防止同一状态目录被重复使用。

升级前先停服务，再备份配置及整个服务台状态目录，包括 SQLite WAL 等文件。不要只复制运行中的 `state.sqlite3`，也不要删除数据库排障或让多个实例同时写同一个目录。改变存储目录会丢失原目录中的接管、欢迎和回执恢复信息。

CLI 也必须保留以下目录和解密材料：

| 内容 | 位置 |
| --- | --- |
| profile、工作区配置及刷新锁 | `$LARKSUITE_CLI_CONFIG_DIR`，未设置时为 `~/.lark-cli`；包含实际使用的工作区子目录 |
| Linux 加密凭据及主密钥 | `$LARKSUITE_CLI_DATA_DIR/lark-cli`，未设置时为 `~/.local/share/lark-cli`，包括既有 `master.key` |
| macOS 加密凭据 | `~/Library/Application Support/lark-cli`，优先使用既有 `master.key.file`，否则只读访问 Keychain 的 `lark-cli / master.key` |

容器应持久化 Bot 存储、CLI 配置及完整加密凭据目录，并保持运行账号、目录环境和既有解密材料一致；只挂载 `config.json` 不够。macOS Keychain 访问失败时不会自动创建主密钥或降级存储方式。

CLI 升级通常不需要修改源码补丁；必要字段、加密布局或刷新锁约定变化仍可能需要更新 Python 凭据适配模块。格式不兼容时暂缓关单，人工消息、欢迎、提醒和回执对账继续运行。

将 `max_concurrent_sessions` 改为 `1` 并重启可恢复全局顺序处理。若要关闭服务台自动化，将 `helpdesk.enabled` 改为 `false` 并重启，保留数据库以便后续恢复；原生飞书 IM 沿用原配置。

## 9. 常见问题

| 现象或日志 | 检查与处理 |
| --- | --- |
| `Feishu Helpdesk unavailable; native IM remains active` | 核对应用与服务台 ID、Token、读取/订阅权限、长连接及网络；修复后重启 |
| 启动正常，但历史工单没有欢迎 | 首次启用前的工单不接管；使用启用后新建的测试工单，检查是否仍待群绑定 |
| CLI 登录过，但服务进程提示 `no usable user login` | 核对服务系统账号、HOME、profile、工作区及持久化目录；在相同环境下登录 |
| `App ID does not match this bot; user operations paused` | 让 CLI profile 使用与渠道相同的应用，再以正确客服用户授权 |
| `CLI user has not authorized helpdesk:all` 或明确权限拒绝 | 开通并发布应用权限，再重新 OAuth 授权；核对用户实际有目标工单权限 |
| `compatible local CLI credentials` 相关不兼容错误 | 使用本地 profile 登录；核对是否用了环境凭据、自定义 provider 或 CLI 存储格式已变化 |
| `CLI Keychain master key is unavailable` | 检查服务账号及 Keychain 访问；不要新建密钥替换原有密钥 |
| 发送或入群出现 `232011`／`232017` | 检查 CLI 用户是否在关联群、是否有相应邀请权限 |
| `Helpdesk synchronization failed` | 检查服务台读取权限、Token、网络与限流；未补齐消息时不会依据不完整历史自动关单 |
| `Helpdesk receipt reconciliation failed` | 检查当前用户的群消息读取权限及网络；人工 IM 交付继续运行 |
| 首次催办后没有关闭 | 当前配置启用升级，本就不会超时关单；检查是否已按 45 分钟节奏升级，以及是否已有可信“已解决”判断 |
| 升级群没有收到通知 | 检查系统识别、`escalation.groups`、Bot 是否在升级群，以及 outbox 的 `transport=feishu_bot` 和发送日志 |
| 工单群提示 `feed id` 或 Web 登录失败 | 检查 auth-mcp `feishu_helpdesk` 会话是否 ready、Chromium 是否可执行、`web_url`/`feed_ids` 是否正确 |
| `Helpdesk automation paused` | 查看暂停原因；发送回执过期或跨轮不确定时暂停该工单自动化，独立后台对账继续运行 |
| 远端关单可能成功，但没有关闭说明 | 提交后结果不确定时保留 `close_pending` 查询远端，确认归属本次操作后补发；不要手动重放旧请求或清库 |

发送回执不确定时，同轮只在有效幂等窗口内使用原 UUID 重试；超过 3500 秒或旧轮仍不确定时暂停该工单自动化。后台会按发送记录查询群历史，确认后恢复，旧轮回执不计入新轮次数。若长期无法确认，需要人工核对远端消息及本地记录。

关单提交后超时、响应丢失或不可解析时，不会自动重放旧 PUT。修复权限或重新授权后允许后续重新评估，但保留已有不确定操作用于对账；不要以删除状态库的方式解除保护。

## 10. 相关资料

- [服务台跟进技术说明](feishu-helpdesk-followup.md)：事件、扫描、状态机及回执细节。
- [服务台渠道接入](feishu-helpdesk.md)：应用与原生 IM 的接入说明。
- [会话并发](../concepts/07-session-concurrency.md)：调度、会话控制和并发配置。
- [官方 CLI 与 Python 关单兼容说明](../../../compat/lark-cli/README.md)：支持的凭据布局与维护边界。
- [上游合并说明](helpdesk-followup-merge.md)：合并接入点和回归记录。
- [VikingBot 中文说明](../../../README_CN.md)：基础运行、模型与沙箱配置。
