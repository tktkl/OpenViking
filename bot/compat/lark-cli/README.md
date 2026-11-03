# 官方 Lark CLI 与 Python 关单兼容说明

服务台关单现已改为随 VikingBot 安装的 Python 模块，旧 `--headers` CLI 补丁及 Go 构建脚本已移除。保留官方 CLI，使用原有登录态和配置即可：

```json
{"cli": {"executable": "lark-cli", "profile": "support"}}
```

更新 VikingBot、确认运行账号及 CLI profile、重启 Gateway。原来指向补丁二进制的 `cli.executable` 改为官方 CLI 路径；无需额外二进制、脚本路径或 SQLite 迁移。完整权限与配置见 [服务台部署说明](../../docs/zh/extensions/feishu-helpdesk-followup.md)。

## 授权归属与两阶段调用

欢迎、提醒、关闭说明、IM 查询和 OAuth 续期继续由官方 CLI 显式以用户身份执行。Bot 自动使用当前 Python 解释器启动 `python -m vikingbot.extensions.feishu_helpdesk.close_helper`，通过 stdin 传递固定关单参数和服务台授权，stdout 只输出版本为 `1` 的 JSON 行协议。

子进程先用 CLI 核对应用、profile、当前用户和工作区，执行目标关单命令的 `--dry-run` 策略检查，再发只读用户请求，由 CLI 完成必要续期。随后获取相同目录和命名的 CLI 刷新锁，读取并解密最新凭据，核对身份、权限和有效期，返回 `ready`。Bot 再校验最新业务状态，持久化待确认操作后 `commit`，条件变化则 `abort`。持锁期间不调用 CLI，不用 refresh token 自行续期，不改写或导出凭据。

一次提交最多发送一次固定的服务台 PUT，使用用户 Authorization 和服务台专用请求头，只允许与渠道品牌一致的官方域名且不跟随重定向。提交后结果不确定时保留本地记录并查询远端，不自动重放旧请求。CLI 的 `ok` 信封与原生 API 的 `code` 返回分别判断，确认远端关闭后才发送说明。

## 支持的凭据布局

参考官方 `larksuite/cli` 提交 `4e2cbea94e33913378ce515f0fccfb9b1f2f2bb9`（v1.0.66）的 AES-256-GCM 存储及 POSIX 刷新锁契约。

| 平台 | 读取位置 | 主密钥 |
| --- | --- | --- |
| Linux | `$LARKSUITE_CLI_DATA_DIR/lark-cli`；未配置时 `~/.local/share/lark-cli` | 已有 `master.key` |
| macOS | `~/Library/Application Support/lark-cli` | 优先已有 `master.key.file`；否则只读系统 Keychain 的 `lark-cli / master.key` |

profile 元数据及锁目录沿用 `$LARKSUITE_CLI_CONFIG_DIR` 或 `~/.lark-cli`，根据 CLI 返回的当前工作区选择相应子目录。仅读取已确认的应用和用户，不扫描其他授权。容器中需同时保留配置目录、加密凭据及既有主密钥，运行账号和目录环境须与 CLI 登录时一致。macOS Keychain 无法访问时暂缓关单，不自动创建主密钥或执行凭据降级。

初版仅支持本地 profile 登录。环境 Token、环境应用凭据、自定义 provider、未知格式或错误身份明确拒绝关单，并输出固定脱敏原因；人工消息、欢迎、提醒和后台回执对账仍正常运行。access token 仅在子进程内存中使用，不出现在 argv、协议输出或日志中；refresh token 不用于调用，也不另存。

CLI 升级不按版本号一律阻断。必要字段、加密布局或刷新锁契约变化时，先停止关单并适配 `cli_credentials.py`，无需对每个 CLI 版本重新打源码补丁。自动测试使用模拟凭据和 API，不代表真实服务台验收；上线前仍需验证测试服务台的用户权限和实际关单行为。
