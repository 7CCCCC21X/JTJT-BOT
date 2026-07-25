# JTJT-BOT — 链上地址/代币监控 Telegram 机器人

监控指定钱包地址的交易(转入/转出、原生币 + 代币),或监控某个代币合约的**所有转账**,有新动作时实时推送到 Telegram。

- 数据源:Etherscan V2 多链 API —— **一个免费 key** 同时支持 Ethereum / BNB Chain / Base / Arbitrum / Polygon
- Etherscan 免费套餐不覆盖的链(如 Base)会**自动降级到 Blockscout** 免费 API,无需额外配置
- 默认每 30 秒轮询一次
- 支持多个 chat 各自维护自己的监控列表
- 专为 Railway 部署设计(Dockerfile + railway.toml,无需开放端口的 worker 进程)

## 使用方式

**最简单的用法:直接把 `0x...` 地址发给机器人**,它会先自动识别地址类型——在每条链上检查是普通地址 (EOA)、合约还是代币合约(显示代币符号)——然后弹出卡片让你选择:地址监控 / 代币监控 / 查看近10条交易。选"代币监控"时会自动预选检测到该代币的链。添加监控时链可以**多选**(点击切换 ✅,选完点「完成」),再填备注或跳过,一路点按钮即可完成。

支持**批量添加**:一行一个地址,地址后面直接跟备注,例如:

```
0xEe7b429ea01f76102f053213463d4e95d5d24ae8 部署者
0x50614CC8e44F7814549c223aA31db9296e58057c 金库
```

菜单里的「监控地址 / 监控代币 / 查近10条交易」按钮和不带参数的 `/add`、`/addtoken`、`/recent` 命令,都会发出一条「回复这条消息…」的提示,直接回复地址即可,链和其余选项全部用卡片按钮选择。

也可以用 `/menu` 打开按钮菜单(添加监控、查看列表、运行状态、测试推送都有按钮),或使用命令:

| 命令 | 说明 |
|---|---|
| `/menu` | 打开按钮菜单 |
| `/add <地址> [链] [备注]` | 监控地址的转入/转出(原生币 + 代币转账) |
| `/addtoken <合约> [链] [备注]` | 监控代币合约的**所有**转账(如新部署的 ERC-20/BEP-20) |
| `/label <地址> [链] <备注>` | 修改已监控地址的备注(不带链则更新所有链上该地址的备注) |
| `/remove <地址> [链]` | 取消监控(不带链则移除该地址在所有链上的监控) |
| `/recent <地址> [链]` | 查看地址近10条交易(原生币 + 代币,按时间倒序) |
| `/list` | 查看当前监控列表 |
| `/status` | 运行状态(运行时长、轮询次数、推送数、错误数、监控数) |
| `/test` | 发送一条示例推送,并检测 ETH/BSC/Base 三条链的 API 连通性 |
| `/chains` | 支持的链:`eth` `bsc` `base` `arb` `polygon` |
| `/cancel` | 取消进行中的卡片添加流程 |
| `/id` | 显示当前 chat id(配置白名单用) |

示例:

```
/add 0xEe7b429ea01f76102f053213463d4e95d5d24ae8 bsc 部署者钱包
/addtoken 0x53f39e5C53EE40bbc3Da97C3B47BD2968d110a8D eth ALIGN
```

添加时以当前链上最新一笔为基准,只推送**之后的新交易**,不会把历史记录刷屏。

## 准备工作

1. **创建 Telegram Bot**:在 Telegram 找 [@BotFather](https://t.me/BotFather),发送 `/newbot`,得到 `TELEGRAM_BOT_TOKEN`
2. **申请 Etherscan API Key**:注册 [etherscan.io](https://etherscan.io/myapikey) 创建免费 key(V2 API 一个 key 通用所有链,包括 BscScan 的数据)

## 部署到 Railway

1. 把本仓库推到你的 GitHub(已完成的话跳过)
2. 打开 [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** → 选择本仓库
3. Railway 会自动识别 Dockerfile 构建
4. 在 **Variables** 页添加环境变量:
   - `TELEGRAM_BOT_TOKEN` = BotFather 给的 token
   - `ETHERSCAN_API_KEY` = Etherscan 的 key
   - 可选:`POLL_INTERVAL`(秒,默认 30)、`ALLOWED_CHAT_IDS`(逗号分隔的白名单)
5. 部署完成后,给机器人发 `/start` 即可使用

> 机器人用 Telegram 长轮询,不需要域名和端口;如果 Railway 提示生成域名,忽略即可。

### (可选)持久化监控列表

监控列表默认存在容器磁盘的 `data/watches.json`,重新部署会丢失。要持久化:

1. Railway 服务页 → 右键 → **Attach Volume**,挂载路径填 `/data`
2. 添加环境变量 `DATA_DIR=/data`

## 本地运行

```bash
pip install -r requirements.txt
cp .env.example .env   # 填好 token 和 key
export $(grep -v '^#' .env | xargs)
python bot.py
```

## 环境变量一览

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | BotFather 的 token |
| `ETHERSCAN_API_KEY` | ✅ | — | Etherscan V2 key(多链通用) |
| `POLL_INTERVAL` | | `30` | 轮询间隔(秒) |
| `ALLOWED_CHAT_IDS` | | 空(不限制) | 允许使用的 chat id,逗号分隔 |
| `DATA_DIR` | | `data` | 监控列表存储目录 |
| `MAX_ALERTS_PER_POLL` | | `8` | 单次轮询每个监控最多推送几条 |
