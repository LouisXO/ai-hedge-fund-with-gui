# 应急手册(RUNBOOK)

适用于 Alpaca 模拟盘上的三本书(长线、内部人、SPY 核心仓)和它们的定时任务。每种情况都按同一顺序写:怎么发现 → 先看什么 → 执行哪条命令 → 什么时候停手。
写于 2026-09-29(S48 包 F)。命令都在 `~/hedge-fund` 下运行,先设 `PY=~/.hedgefund-venv/bin/python`。本文件在公开仓库里,**不写任何真实账户(moomoo)的数字**。

## 总原则

1. **先停发单,再查原因。** 停发单只要改一行(见下),它对 16:10 任务立即生效。
2. **账本对不上不准用交易去"修"**(AGENT.md 铁律 7)。对账不符的股票会被冻结(买卖都不发),先解释,再改账。
3. **错过一天比做错一天便宜。** 内部人书有次日重试;长线书的名单一天不会大变。拿不准就跳过当晚,不手工补单。
4. 下单只在美东 16:05–09:25 之间(= 太平洋 13:05–次日 06:25)。开盘是 06:30 PT;挂着的 DAY 单在这之前撤掉就不会成交。
5. Alpaca 网页 https://app.alpaca.markets(选模拟盘账户;手机浏览器也能打开)可以直接撤单和平仓,不依赖这台电脑。**电脑或代码出问题时,网页是后备手段。**

### 停止发单(所有情况的第一步)

```bash
sed -i '' 's/^AGENT_EXEC=.*/AGENT_EXEC=off/' ~/.hedge-fund/.env && grep '^AGENT_EXEC' ~/.hedge-fund/.env    # 期望输出 AGENT_EXEC=off
```

- 之后 16:10 任务只做试运行(日志里是 `[DRY RUN]`)。13:25 任务本来就不下单。
- 恢复:把 `off` 改回 `on`。**只在对账为 ok 以后恢复**(见第 2 节的检查脚本)。
- 注意:手工运行 `-m agent.execute --submit` 是否受这个开关约束,以包 A 合并后的代码为准(S46 审计时手工运行会绕过它)。开关关着的时候不要手工带 `--submit`。

### 去哪里看

| 看什么 | 位置 |
|---|---|
| 下单和收盘后任务的日志 | `~/optradar/out/agent/execute.log`(16:10 和 13:25 都写这里) |
| 当晚计划和发出的订单 | `~/optradar/out/agent/exec_<日期>.json`(`orders`、`skipped_reason`、`reconcile`、`bars_coverage_pct`);13:25 的是 `sync_<日期>.json` |
| 系统健康 | 私有站首页顶部(https://optradar.tail5b470b.ts.net,Tailscale 内),数据在 `~/optradar/out/health.json`;出现新的红色项时 Mac 通知"系统健康:有故障" |
| 当日复盘 | 私有站 `agent/review_<日期>.html`(13:25 任务生成) |
| 早报和采集日志 | `~/optradar/out/cron.log`、`~/optradar/out/agent/collect.log` |
| launchd 上次退出码 | `launchctl list \| grep com.louis`(第二列是上次退出码,`-` 表示还没跑过) |
| 券商侧的真相 | Alpaca 网页的 Orders 和 Positions 页 |
| 公开站推送被中止 | Mac 通知"公开站推送已中止";日志里的 `publish_guard:` 行(见第 10 节) |

---

## 1. 下单任务失败(16:10 PT)

**怎么发现**:健康页"任务"一节变红,或 Mac 通知;`execute.log` 当晚那段末尾出现 `execute failed`;当天没有 `exec_<日期>.json`。

**先看**

```bash
tail -80 ~/optradar/out/agent/execute.log
ls -l ~/optradar/out/agent/exec_$(date +%F).json
launchctl list | grep com.louis.agent.execute
```

- 如果有一行 `[SUBMITTED] ... NOTHING PLANNED: <原因>` 再跟着 `execute failed`:这是保护开关主动不下单(退出码 3),不是崩溃。原因是 `bar date != last session`、`market hours ...` 或日线覆盖不足时,按第 4 节处理。
- 如果是 Python 报错(Traceback):看报错发生在发单之前还是之后。发单之后才报错时,一部分订单可能已经到了券商:打开 Alpaca 网页 Orders 页核对。
- 电脑在 16:10 睡着:launchd 在醒来后补跑(关机时不补跑);补跑落在交易时段时保护开关会拒绝下单(见上)。

**命令**

```bash
cd ~/hedge-fund
PYTHONPATH=. $PY -W ignore -m agent.execute              # 试运行:只打印计划,不发单,也不写订单
zsh agent/bin/execute.sh                                  # 修好后重跑整个任务(仍受 AGENT_EXEC 约束)
```

重跑是安全的:订单号 `client_order_id` 按 书|日期|代码|方向 生成,已经发出或已在账本里的会被跳过(日志 `skipped N already-submitted ids`)。

**什么时候停**:同一晚最多重跑一次;第二次还失败,或者报错在发单路径里(`BrokerError`、账本写入失败),就停发单、跳过当晚,第二天白天再查。06:25 PT 以后不再尝试。

## 2. 对账不一致

**怎么发现**:`execute.log` 里 `reconcile [...]` 不是 `ok`(例如 `XYZ: lots 10 vs alpaca 1`、`alpaca position with no lot (manual?)`);`exec_`/`sync_<日期>.json` 的 `reconcile` 列表非空;这些股票当晚不会有任何订单。

**先看**:不一致是哪一类。

| 现象 | 常见原因 | 处理 |
|---|---|---|
| 券商持仓是账本的整数分之一或整数倍 | 合股 / 拆股 | 核对公司公告;改账本要单独开一个 session 记录原因(包 A 只报警,不自动改账) |
| 券商有、账本没有 | 在网页上手工交易;或订单发出但账本没记上(网络中断) | 在 Orders 页找这笔订单的 `client_order_id`;是系统的单,等包 A 的"认领孤儿订单"或手工补记 |
| 账本有、券商没有 | 卖单成交后还没同步;或在网页上手工平仓 | 先重新同步一次(下面的命令),还不一致就查 Orders 页 |
| 数量差一点 | 部分成交 | 等当天收盘后的同步;DAY 单收盘后自动失效 |

**命令(只读)**

```bash
cd ~/hedge-fund
PYTHONPATH=. $PY -W ignore - <<'PY'
from agent import ledger
from agent.broker.alpaca import from_env
from agent.execute import open_lots, reconcile
con = ledger.connect(read_only=True)
blocked, msgs = reconcile(open_lots(con), from_env().positions())
print("对账 ok" if not msgs else "\n".join(msgs))
PY
PYTHONPATH=. $PY -W ignore -m agent.execute --sync-only  # 重新同步成交(和 13:25 做的一样;不下单)
```

**什么时候停**:不要为了让数字对上去下单或平仓。冻结的股票可以一直冻结,直到原因写清楚、账本改好、上面的检查打印 `对账 ok`。

## 3. 券商故障(Alpaca 接口不可用)

**怎么发现**:健康页"Alpaca 模拟盘"变红;`execute.log` 里有 HTTP 5xx、连接超时或 `BrokerError`;Alpaca 状态页 https://status.alpaca.markets 有事故。

**先看**:故障发生在发单之前、之中还是之后。打开 Alpaca 网页的 Orders 页,看当晚的订单到了多少(订单号以书名开头)。

**命令**:接口恢复后,在 06:25 PT 之前重跑 `zsh agent/bin/execute.sh`;已经到达的订单会被跳过,只补发没到的。接着 `-m agent.execute --sync-only` 同步一次。

**什么时候停**:06:25 PT 之前接口还没恢复就跳过当晚。开盘时如果网页能用、而你不想让已挂的单成交,在网页 Orders 页撤单。网页也不可用时什么都不做:这是模拟盘,记录下来即可。

## 4. 数据异常日

**怎么发现**:`execute.log` 里 `NOTHING PLANNED: bar date != last session` 或日线覆盖不足;健康页"数据"一节有过期项;`agent.drift` 的偏离检查变红;名单突然大换(长线书一天要卖出很多只)。

**先看**:`exec_<日期>.json` 的 `skipped_reason`、`bars_coverage_pct`、`bars_missing_for_held`、`bars_update_failed`;`collect.log` 里当天的数据任务有没有报错。

**命令**

```bash
cd ~/hedge-fund
PYTHONPATH=. $PY -W ignore -m agent.execute               # 试运行,看计划是否合理(卖出数量、买入名单)
PYTHONPATH=. $PY -W ignore -m agent.audit                 # 七层数据审计
```

如果订单已经发出、事后才发现数据有问题:06:30 PT 开盘之前撤掉挂单(第 5 节的撤单命令或网页)。

**什么时候停**:数据不对就跳过当晚,不在同一晚用修过的数据手工补单。保护开关拒绝下单的日子不需要任何操作。

## 5. 立即全部平仓 / 撤掉全部挂单

**顺序**:先停发单(见上),再撤挂单,最后平仓。不先停发单,16:10 任务会把空出来的槽位重新买上。

**撤挂单**

```bash
cd ~/hedge-fund
PYTHONPATH=. $PY -W ignore -m agent.execute --cancel-open   # 包 A 新增:默认只打印要撤的单;真正撤单的参数以合并后的 --help 为准
```

网页后备:Alpaca 网页 Orders 页的全部撤销;或用接口(密钥只从文件读进变量,不出现在命令历史里):

```bash
K=$(grep '^ALPACA_KEY_ID=' ~/.hedge-fund/.env | cut -d= -f2-); S=$(grep '^ALPACA_SECRET=' ~/.hedge-fund/.env | cut -d= -f2-)
curl -s -X DELETE -H "APCA-API-KEY-ID: $K" -H "APCA-API-SECRET-KEY: $S" https://paper-api.alpaca.markets/v2/orders                        # 撤销全部挂单
curl -s -X DELETE -H "APCA-API-KEY-ID: $K" -H "APCA-API-SECRET-KEY: $S" "https://paper-api.alpaca.markets/v2/positions?cancel_orders=true"  # 撤单并平掉全部持仓
```

(两个接口来自 Alpaca 公开文档,本系统还没有实际演练过;第一次用之前先在模拟盘演练一次并在文末记录。)

**平仓**:网页 Positions 页的全部平仓,或上面第二条命令。交易时段内是市价成交;收盘后提交的平仓单会排到下一个开盘。

**之后**:账本里的批次仍是未平仓状态,对账会把全部股票冻结,这是预期的。把平仓原因和时间写进 `docs/AGENT_PLAN.md` §9,再单独开一个 session 把批次按券商的成交记为平仓。账本改好之前不要恢复 `AGENT_EXEC=on`。

## 6. 电脑丢失或被盗

按这个顺序,都可以在另一台设备上完成:

1. **Alpaca**:网页里重新生成模拟盘密钥(旧密钥立即失效)。需要的话先撤单、平仓(第 5 节,网页操作)。
2. **Alpha Vantage**:在其网站重新申请密钥。`SEC_USER_AGENT` 不是密钥,不用换。
3. **GitHub**:撤销这台电脑的 SSH 密钥和 `gh` 令牌(Settings → SSH keys / Applications)。
4. **Tailscale**:在管理后台删除节点 `optradar`。私有站在 tailnet 里,节点删除后别人无法从网络访问;磁盘上的文件靠第 5 步。
5. **Apple**:"查找"里锁定或抹掉这台 Mac。iCloud 里的备份不受影响。
6. **moomoo**:改登录密码(OpenD 在这台电脑上保存过登录状态);**Claude**:在账户里登出所有会话。
7. 在新电脑上按第 8 节重建,数据按第 7 节恢复。

## 7. 从 iCloud 备份恢复

备份在 `~/Library/Mobile Documents/com~apple~CloudDocs/OptRadarBackup/`:

- `daily/<日期>/`(13:25 任务末尾,留 14 份):`optradar.db.zst`(模拟盘账本和其他 optradar 表)、`archive/estimates/retail/auctions/filings.db.zst`、`state/`(小状态文件)、`out_agent.tgz`(`out/agent` 的 exec/review/watch 文件)。
- `weekly/<日期>/`(周日 03:00,留 2 份):`panel/options/news/short/intraday.db.zst`。
- `last_backup.json`:最近一次的清单,每个文件的状态、表数和行数。自 S48 起,行数是把写好的 `.zst` 解压到临时目录后读出来的。
- 密钥不在备份里(按设计),要重新申请。

在任务间隙做(避开 06:00、08:41、13:15、13:25、16:10 PT 前后),或者先停掉相关任务。`zstd -d` 在目标文件已存在时会拒绝,所以先把旧文件挪开。**旧库旁边的 `.db.wal` 必须一起挪开**:需要恢复时旧库往往是崩溃后留下的,带着 WAL;只挪 `.db`、把备份解压到原位置后,DuckDB 打开时会把旧 WAL 重放进恢复出的库,不报错,账本里混进旧文件的行。下面的 `park` 把 `.db` 和 `.db.wal` 一起挪走,原位置还剩任何一个就停下,不解压:

```bash
B="$HOME/Library/Mobile Documents/com~apple~CloudDocs/OptRadarBackup"
ls "$B/daily" "$B/weekly"; $PY -m json.tool "$B/last_backup.json" | head -40
D="$B/daily/<日期>"; W="$B/weekly/<日期>"          # 选定要恢复的日期
OLD=~/restore_old_$(date +%Y%m%d%H%M); mkdir -p "$OLD"
park() { for x in "$1" "$1.wal"; do [ -e "$x" ] && mv "$x" "$OLD"/; done; if [ -e "$1" ] || [ -e "$1.wal" ]; then echo "停下:$1 或 $1.wal 仍在原位,不解压"; return 1; fi; }
park ~/optradar/optradar.db && zstd -d "$D/optradar.db.zst" -o ~/optradar/optradar.db
for f in archive estimates retail auctions filings; do
  park ~/.hedge-fund/agent/$f.db && zstd -d "$D/$f.db.zst" -o ~/.hedge-fund/agent/$f.db
done
cp "$D"/state/* ~/.hedge-fund/agent/
tar -xzf "$D/out_agent.tgz" -C ~/optradar/out
for f in panel options news short intraday; do                  # 只有要恢复大库时
  park ~/.hedge-fund/agent/$f.db && zstd -d "$W/$f.db.zst" -o ~/.hedge-fund/agent/$f.db
done
ls ~/optradar/*.wal ~/.hedge-fund/agent/*.wal 2>/dev/null         # 第一次打开之前应当没有输出
```

备份里的 `.zst` 本身不带 WAL(备份时已把 WAL 并进副本),解压出来的就是完整的库。

**恢复后检查**

```bash
$PY -c "import duckdb,sys; c=duckdb.connect(sys.argv[1],read_only=True); print(c.execute(\"SELECT count(*) FROM information_schema.tables\").fetchone())" ~/optradar/optradar.db   # 表数对照 last_backup.json
cd ~/hedge-fund && PYTHONPATH=. $PY -W ignore -m agent.health --no-llm-probe --no-notify
```

然后跑第 2 节的对账检查。账本停在备份那一刻(某天 13:25 任务末尾):备份之后 16:10 发出的订单不在 `agent_orders` 里。对照更新一天的 `out_agent.tgz` 里的 `exec_<日期>.json` 和 Alpaca 网页的订单记录补记,**不用交易去补**。补记完成、对账 ok 之前保持 `AGENT_EXEC=off`。

## 8. 换机重建清单

代码路径在脚本和 plist 里写死为 `/Users/louis/...`,新机器用同一个用户名最省事。

1. **Homebrew 工具**:`brew install zstd terminal-notifier gh python@3.13 python@3.10`。
2. **代码**:`git clone` 两个仓库到 `~/hedge-fund`(分支 `v2-rebuild`)和 `~/optradar`(分支 `main`);`gh auth login`(公开站推送要用)。
3. **虚拟环境**
   ```bash
   python3.13 -m venv ~/.hedgefund-venv && ~/.hedgefund-venv/bin/pip install -r ~/hedge-fund/requirements.txt
   python3.10 -m venv ~/.moomoo/venv && ~/.moomoo/venv/bin/pip install -r ~/optradar/requirements.txt
   ```
4. **密钥文件(只写名字,值重新申请)**,两个都 `chmod 600`:
   - `~/.hedge-fund/.env`:`SEC_USER_AGENT`、`ALPACA_KEY_ID`、`ALPACA_SECRET`(模拟盘,`PK` 开头)、`AGENT_EXEC`(先写 `off`)。可选:`TIINGO_TOKEN`、`EODHD_TOKEN`(只有价格探测脚本用)。
   - `~/optradar/.env`:`ALPHAVANTAGE_KEY`。
   - 可选:`~/.hedge-fund/publish_guard_terms.txt`,公开站推送检查的私有词表(账户号等,一行一个)。
5. **数据**:按第 7 节从 iCloud 恢复。没有备份时,大库要用各加载器重建,需要数天。
6. **moomoo OpenD**:安装到 `/Applications/moomoo_OpenD.app`,图形界面登录,确认端口 11111 可连。只读,永远不解锁交易。
7. **Tailscale**:安装并登录,节点名 `optradar`;私有站由 `com.louis.optradar.private`(127.0.0.1:8787)提供,再用 Tailscale Serve 转成 https(具体 serve 命令本仓库没有记录,重建时核对 Tailscale 文档后补在这里)。
8. **Claude CLI**:安装到 `~/.claude/local/claude`,终端运行 `claude` 后 `/login`;然后做第 9 节的加固。
9. **launchd 任务**:`zsh ~/hedge-fund/agent/bin/install_launchd.sh` 列出每个任务的状态和安装命令(脚本只打印,不执行);逐行复制运行。`com.louis.agent.chain` 保持停用。
10. **电源**:`zsh ~/optradar/bin/install_power.sh`(输入一次密码:写 `/etc/sudoers.d/pmset`、装电源策略任务;仓库里的 execute 和 optradar 两个 plist 已经带 caffeinate)。
11. **系统权限**:第一次通知时允许 terminal-notifier;iCloud Drive 打开(备份目的地)。
12. **验证**
    ```bash
    cd ~/hedge-fund && PYTHONPATH=. $PY -m pytest -q agent hedge_fund -p no:cacheprovider
    ./agent/tests/selftest_agent.sh
    PYTHONPATH=. $PY -W ignore -m agent.execute                # 试运行
    PYTHONPATH=. $PY -W ignore -m agent.health --no-notify
    ```
    都通过、对账 ok 之后,才把 `AGENT_EXEC` 改成 `on`。

## 9. 无人值守 `claude -p` 的加固(用户手工做)

早报(`~/optradar/bin/run_and_notify.sh` 第 57–58 行)和周报(`~/optradar/bin/run_weekly.sh` 第 14 行)的 `claude -p` 只用了 `--allowedTools`。它只是免确认名单,不会移除其他工具;用户级设置里放行的命令在这两个任务里同样有效。输入里有外部文本(数据源报错、周报里的日志行和国会交易数据),而这台电脑上有券商密钥。本包不改这两个脚本,由用户修改:

1. 早报第 57–58 行改成:
   ```bash
   "$CLAUDE" -p "$(cat bin/ai_prompt.txt)" \
     --tools "Read,PushNotification" --allowedTools "Read,PushNotification" \
     --strict-mcp-config --mcp-config '{"mcpServers":{}}' --setting-sources "" >> "$LOG" 2>&1
   ```
2. 周报第 14 行的调用改成:
   ```bash
   "$CLAUDE" -p "$PROMPT" --model opus --tools "Read" --allowedTools "Read" \
     --strict-mcp-config --mcp-config '{"mcpServers":{}}' --setting-sources "" > "$W/${TODAY}.md" 2>> "$LOG"
   ```
   这组参数和 `integrations/claude_code_llm.py` 的 `SEALED_FLAGS` 同源,已经在本机的 CLI 版本上使用。
3. 在 `~/.claude/settings.json` 的 allow 名单里删掉 `Bash(python3 -c *)`,在 `~/.claude/settings.local.json` 里删掉 `Bash(python3 -)`。审计和代理都不改 `~/.claude`,这一步只能用户本人做。
4. 改完手工验证一次推送还能发出:
   ```bash
   cd ~/optradar && ~/.claude/local/claude -p "用 PushNotification 发一条通知,内容是 hardening test,然后只回复 OK" \
     --tools "Read,PushNotification" --allowedTools "Read,PushNotification" \
     --strict-mcp-config --mcp-config '{"mcpServers":{}}' --setting-sources ""
   ```
   手机或 Mac 收到通知即可。收不到时,去掉 `--setting-sources ""`(保留 `--tools` 和两个 MCP 参数)再试,并在这里记下结论。
5. 上实盘之前这一条必须完成:实盘密钥不放进 `~/.hedge-fund/.env`(见 `docs/LIVE_MIGRATION.md`)。

## 10. 公开站推送被关键词检查中止

**怎么发现**:Mac 通知"公开站推送已中止";`execute.log`(13:25 任务)或 `cron.log`(08:41 任务)里有 `publish_guard: 发现 N 处疑似真实账户信息`,下面逐行列出 `文件:行号 [标记] 片段`。

**原因**:`site/publish.sh` 在提交 `site/public` 之前扫描暂存的页面,在 `git push` 之前扫描待推送范围(`site/publish_guard.py --range`):每个待推送提交各自的新增行和文件名(命中显示为 `<提交号>:<文件>:<行号>`)、整个范围的净差异(覆盖合并提交里的冲突解决)、以及提交说明。推送的是整个 `v2-rebuild` 分支,所以别的 session 提交的文档也在扫描范围里。

**处理**

- 命中在 `site/public`:生成器把不该公开的内容写进了页面。页面已取消暂存、没有提交;修生成器,重跑 `PUBLISH=1 zsh site/publish.sh`。
- 命中在别的提交(还没推送):**补一个删除提交没有用**。`git push` 会把范围内的全部提交都推上去,泄露的那个提交进入公开历史,凭提交号就能读到;检查也会因为那个提交继续拦截。必须改写本地未推送的历史,让泄露提交不再出现在 `v2-rebuild` 上:
  1. 先把命中的内容抄到私有仓库(`optradar/docs/PRIVATE.md` 等)。
  2. 改写。最简单的办法是把所有未推送的提交压成一个:
     ```bash
     cd ~/hedge-fund && git log --oneline origin/v2-rebuild..HEAD   # 看清要改写哪些提交
     # 不要先 git fetch:publish.sh 刚把本地分支变基到 origin/v2-rebuild 上;远端若又前进,reset 后的提交会撤销远端的新改动
     git reset --soft origin/v2-rebuild          # 未推送的改动全部回到暂存区,工作区文件不变
     # 编辑命中的文件,删掉那几行(或 git rm 整个文件),然后
     git add -A && git commit -m "<重新写的说明>"
     ```
     想保留每个提交各自的说明时,由用户本人对未推送的提交做交互式改写(`git rebase -i origin/v2-rebuild`,把泄露的提交标成 `edit` 修掉)。提交说明本身命中时也用这两种办法之一改写。
  3. 重跑检查,确认返回 0 再推:`PYTHONPATH=. $PY site/publish_guard.py --range origin/v2-rebuild..HEAD`,然后 `PUBLISH=1 zsh site/publish.sh`。
  4. 只有这样,泄露提交才不会进入公开历史。改写前不要在任何地方 `git push` 这个分支。
- 命中的内容已经推送过(出现在 `git log origin/v2-rebuild` 里):删除提交同样不能撤回,它已经公开,fork 和缓存里可能都有。按泄露处理:评估泄露了什么,需要时向 GitHub 申请清除;是否强推改写公开历史由用户决定。
- 确认是误报:手工运行 `PUBLISH=1 PUBLISH_GUARD=off zsh site/publish.sh`(定时任务永远不设这个变量),并把误报的写法记在这里,以后调整 `publish_guard.py` 的标记。
- 私有词表(可选):`~/.hedge-fund/publish_guard_terms.txt`,一行一个字面量(账户号、精确金额等),`#` 开头是注释,少于 4 个字符的忽略。命中时不回显词表内容。

## 11. 仍然存在的风险(不在本手册的处理范围)

- 生产任务直接运行研究工作区 `~/hedge-fund` 的代码,任务脚本里没有测试关卡。审计建议:生产用单独的检出(git worktree 固定在一个 tag 上,`pytest` 通过后才移动 tag),同时改各 plist 的 `WorkingDirectory`、`PYTHONPATH` 和三个脚本里的 `cd` 路径。需要用户决定。
- 整套任务在一台会合盖、会带出门的笔记本上。上实盘前把下单链路迁到一直插电的机器(见 `docs/LIVE_MIGRATION.md`)。

## 演练记录

| 日期 | 情况 | 做了什么 | 结果 |
|---|---|---|---|
| (待做) | 第 5 节撤单 + 平仓,在模拟盘上演练一次 | | |
| (待做) | 第 7 节恢复到临时目录,核对表数和行数 | | |
