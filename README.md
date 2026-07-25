# JTJT-BOT — 链上地址/代币监控 Telegram 机器人

监控指定钱包地址的交易(转入/转出、原生币 + 代币),或监控某个代币合约的**所有转账**,有新动作时实时推送到 Telegram。

- 数据源:Etherscan V2 多链 API —— **一个免费 key** 同时支持 Ethereum / BNB Chain / Base / Arbitrum / Polygon
- 默认每 30 秒轮询一次
- 支持多个 chat 各自维护自己的监控列表
- 专为 Railway 部署设计(Dockerfile + railway.toml,无需开放端口的 worker 进程)

## 机器人命令

| 命令 | 说明 |
|---|---|
| `/add <地址> [链] [备注]` | 监控地址的转入/转出(原生币 + 代币转账) |
| `/addtoken <合约> [链] [备注]` | 监控代币合约的**所有**转账(如新部署的 ERC-20/BEP-20) |
| `/label <地址> [链] <备注>` | 修改已监控地址的备注(不带链则更新所有链上该地址的备注) |
| `/remove <地址> [链]` | 取消监控(不带链则移除该地址在所有链上的监控) |
| `/list` | 查看当前监控列表 |
| `/chains` | 支持的链:`eth` `bsc` `base` `arb` `polygon` |
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
