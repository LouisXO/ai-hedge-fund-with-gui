# ERRORS.md — 报错 → 原因 → 修法

按报错原文搜(Ctrl-F)。每条写在哪、原因、修法、预防;"已记录"指向已有的规则或说明。路径默认相对 `~/hedge-fund`,跑法见 AGENT.md §2。
条目来自 2026-09 到 10 月的会话记录,逐条对照过代码和提交。"已修"后面的括号是 commit。

## 目录

- DuckDB 与数据库(9 条)
- Shell、zsh 与 git(11 条)
- Alpaca 模拟盘下单与对账(7 条)
- 行情与数据源:Alpaca、AV、yfinance、EDGAR、XBRL(12 条)
- 定时任务、健康检查与通知(6 条)
- 密封 LLM(claude -p)(4 条)
- 云端 routine(3 条)
- Claude Code 工具环境(8 条)
- 都不管用时
- 未收录

## DuckDB 与数据库

### `_duckdb.IOException: IO Error: Could not set lock on file ".../panel.db": Conflicting lock is held in .../Python (PID ...)`
- **在哪**: panel.db、optradar.db、options.db、short.db、intraday.db。共用者:launchd 任务(06:00、08:41、13:25、16:10、19:15 PT)、后台加载器(Form 4、期权、空头、盘中 K 线等)、临时 session。任务日志里是 `panel: panel.db is locked by another process; waiting 10 s (… of 300 s so far)` → `agent: panel refresh failed, scoring on the existing panel`。
- **原因**: DuckDB 一个文件只允许一个写进程;写的时候别的进程连 `read_only=True` 也打不开。见过的触发:读写并行启动;等日志行而写进程还没退出;Form 4 抓取在 2–4 小时下载期间一直持有写连接;PanelStore 打开时不重试,重叠的任务整步失败(9/21、9/28 早报影子打分)。
- **修法**: 已修:`PanelStore.connect_with_retry` 每 10 秒重试、最多 5 分钟(07335ce);`ledger.connect` 试 6 次、间隔 10 秒,只重试含 lock 的 IOException;抓取器下载时不开库、最后再写(9ee7834、7dfadc1、73bd08c),有测试断言下载期间没有 panel.db 连接;drift 读完立即关账本(394688a)。临时工作等写进程退出再读:`while pgrep -f "<加载模块>" >/dev/null; do sleep 15; done; <读>`。长时间只读分析:read_only 打开,`shutil.copy2` 复制 .db 和 .wal,关闭,在副本上做。补跑排在正在跑的任务之后。
- **预防**: 同时只有一个写进程。等 `pgrep`,不等日志文本。网络 I/O 和长计算期间不持有连接。写之前 `ps` 看 postclose/execute 在不在跑(postclose 带盘中回填时约 2 小时),避开 06:00、08:41、13:25、16:10、19:15 PT。用副本前确认它比最近一次账本变动新。
- **已记录**: AGENT.md §2 "DuckDB 锁"、§7 多窗口;`agent/ledger.py` 模块 docstring 和 `connect()`;`hedge_fund/features/panel.py` `LOCK_WAIT_S` 注释。

### `_duckdb.BinderException: Binder Error: Referenced column "day" not found in FROM clause! Candidate bindings: "as_of"`
- **在哪**: 临时只读查询 panel.db(bars、index_daily、insider_tx、fundamentals_pit、underlying_daily)和账本表(agent_book_nav、agent_signals、agent_picks)。同类:`agent.books.data.fundamentals()` 上 `KeyError: 'filing_date'`;猜路径时 `Cannot open database "/Users/louis/optradar/db/optradar.db" in read-only mode: database does not exist`、`ugrep: warning: agent/books/long_live.py: No such file or directory`。
- **原因**: 列名和路径凭记忆猜。真实的:`bars.trade_date`(不是 date);index_daily 按 symbol 不按 ticker;`insider_tx.owner_name`;`fundamentals_pit.filed`(没有 filing_date 和 form 列);agent_signals 只有 (as_of, ticker, signal_name, signal_version, value, components, abstained, validated, run_id),没有 rank 和 composite,排名在 `agent_picks(signal_name, rank, run_id)`。optradar.db 在 optradar 仓库根目录;execute.py 里是 `from agent.books import live as long_live`,文件是 `agent/books/live.py`;计划在 `docs/AGENT_PLAN.md`。
- **修法**: 先 `DESCRIBE <表>`(或 information_schema.columns、`print(df.columns)`),报错里的 Candidate bindings 也列了真名。库用 `/Users/louis/optradar/optradar.db`,最好经 `from agent import ledger; ledger.connect(read_only=True)`(带锁重试)。模块别名顺着 import 行找,或先 `grep -rn 'def targets' agent/`。
- **预防**: 本 session 没打印过结构的表,同一脚本里先打印列再查。库路径从 `ledger.OPTRADAR_DB` / PanelStore 取,不凭记忆。

### 同一回测在不同数据快照上结果不同 / 用旧副本算出假报警
- **在哪**: panel.db 上的回测;在 DB 副本上做的分析(S54 实现)。
- **原因**: 全量刷新改写历史输入,名单的微小差异沿路径放大:同一代码、同一窗口的长线回测 alpha2 在不同快照上是 +8.54% / +7.31% / +8.10% / +7.61%。S54 长线报警第一次计算就"触发"(−7.60%,阈值 −7.29%),因为副本早于 10/20 17:20 记入的 $323.66 分红;在当前账本上是 −7.02%,不触发。
- **修法**: 每次全量刷新前保留 panel.db 副本(`weekly_fundamentals.sh` 已在刷新前做快照)。评估时在当天数据上重算同一窗口,报告修订噪声,不和旧数字比。报警在当前账本上重算;drift 把每次读数和输入追加到 `out/agent/long_alarm_log.jsonl`,任何一次都能单独复现。
- **预防**: 用快照前确认它比最近一次账本变动新。
- **已记录**: `docs/AGENT_PLAN.md` §9 "基数漂移诊断(2026-10-14)"、S54 实施(10/21 排查更正)。

### `_duckdb.ParserException: Parser Error: syntax error at or near "rows"`
- **在哪**: `agent/sources/sec_form4.py`、`agent/sources/alpaca_options.py` 和临时查询(`SELECT count(*) rows, …`)。"days"、"last" 同样报错;CREATE TABLE 里列名 `right` 被拒。
- **原因**: 裸别名和列名是 DuckDB 关键字:rows、days、last、right。first、rank、date 也有风险。
- **修法**: 一律写 AS 加非保留名:`count(*) AS n_rows, count(DISTINCT ticker) AS n_tickers, min(filing_date) AS first_filing, max(filing_date) AS last_filing`。`right` 改名 `cp`,options.db 删掉重建。
- **预防**: 别名一律 AS + `n_` 或描述性名字,列名不用 SQL 关键字。

### `_duckdb.ConstraintException: Constraint Error: NOT NULL constraint failed: insider_tx.trans_date`
- **在哪**: 加载器 sec_form4、sec_daily_form4、sec_xbrl;S47 修复分支合并后的测试夹具。也见过 insider_tx.ticker、xbrl_facts.period_start、listing_status.ipo_date。夹具的同类错误:`Binder Error: There are no UNIQUE/PRIMARY KEY constraints that refer to this table, specify ON CONFLICT columns manually`(`agent/tests/test_s53_fpi.py`)。
- **原因**: 主键列隐含 NOT NULL。部分 Form 4 行没有 trans_date 或 ticker;XBRL 资产负债表事实是时点值,没有起始日;另一个分支把 ipo_date 加进 listing_status 主键,旧夹具失效。ON CONFLICT 那条:测试手写 CREATE TABLE,漏了生产 upsert 依赖的 PRIMARY KEY。
- **修法**: sec_form4(季度批量)插入前丢掉没有 filing_date/trans_date 的行(sec_form4.py L131-132)。sec_xbrl 存 `period_start = it.get("start") or it["end"]`、`is_instant = "start" not in it`。**sec_daily_form4 只跳过没有 ticker 的申报,不过滤 trans_date**:解析不了的 transactionDate 变成 NaT,写入时仍会这样报错。夹具用模块自己的 DDL 常量建表(`fpi_data.ISSUER_DDL`)。合并工作树分支后更新夹具,在集成工作树跑全套测试;加载器续跑时跳过已完成的季度。
- **预防**: 每个加载器在 INSERT 前处理主键字段的空值。测试夹具复用生产 DDL 常量。合并并行分支后跑全套测试。

### `_duckdb.ConversionException: Conversion Error: Could not convert string '' to BOOL when casting from source column gate_passed`
- **在哪**: `agent/ledger.py` `_insert`(对注册的 DataFrame 做 `INSERT OR REPLACE INTO {table} SELECT * FROM _in`),任何 agent_* 表;`agent/sources/daily_archive.py` 借券存档在 60b5ff2 之前是同样写法。
- **原因**: 从注册的 DataFrame `SELECT *` 按位置对列,而用 dict 建的 DataFrame 列序是字典顺序,不是表结构顺序。表加列后按位置插入也会坏。
- **修法**: 已修。ledger._insert 自 S4–S5(97953d1)起按名字对齐:`cols=[r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]`,缺的列补 None,再 `con.register("_in", df[cols])`。借券存档改成在 INSERT 里写列名(60b5ff2):`INSERT OR REPLACE INTO borrow ({cols}) SELECT {cols} FROM _b`(daily_archive.py L183)。改表结构用 `ALTER TABLE ADD COLUMN`(旧行为 NULL),再按列名插入。
- **预防**: 新代码不按位置插 DataFrame:按 DESCRIBE 的列序重排,或在 INSERT 里写列名。
- **已记录**: `docs/AGENT_PLAN.md` §9 S4–S6(按列名而非字典顺序插入);`agent/ledger.py` `_insert` docstring。

### `TypeError: Cannot compare Timestamp with datetime.date. Use ts == pd.Timestamp(date) or ts.date() == date instead.`
- **在哪**: `agent/auction_basis.py` `next_session()`、`agent/s26_option_gate.py` `exits()` 的 merge、`agent.market_breadth.compute`、DuckDB 结果的临时 pandas 分析。同类:`ValueError: You are trying to merge on object and datetime64[us] columns for key 't0'`、`TypeError: Invalid comparison between dtype=datetime64[us] and date`、`KeyError: datetime.date(2026, 10, 2)`;`pandas.errors.MergeError: incompatible merge keys [0] dtype('int32') and dtype('int64'), must be the same type`(`agent/books/fundamentals_fpi.py` 按 cik 的 merge_asof);`ValueError: time data "2026-10-14" doesn't match format "%Y-%m-%dT%H:%M:%S"`(`agent/sources/ads_ratio.py` 的 fetched_at)。
- **原因**: DuckDB 的 DATE 列经 `.df()`/`fetchdf()` 变成 datetime64,`fetchall()` 标量和 Python 代码用 datetime.date,pandas 不让两者比较、合并、查找。DuckDB 的 INT 列回来是 int32,另一边的 cik 是 int64。fetched_at 有两个写入路径,混着纯日期和带时间的 ISO 字符串,pandas 按第一个元素推断格式。
- **修法**: 在边界统一:`df[col] = pd.to_datetime(df[col])`,和 `pd.Timestamp(day)` 比(索引查找也一样,如 `last_ts - pd.Timedelta(days=30)`);或两边都 `.dt.date`;join 在 SQL 里 CAST 成 DATE。merge 前 `.astype({'cik': 'int64'})`。混合格式用 `pd.to_datetime(df['fetched_at'], format='ISO8601')`。
- **预防**: 每个模块只用一种日期类型,在 DuckDB→pandas 边界转换。DuckDB `.df()` 之后把 join 键转 int64。多个路径写入的时间列一律 `format='ISO8601'`。

### JSON 输出里出现 NaN(不合法 JSON),未打分字段本应是 null
- **在哪**: pandas `df.where(pd.notna(df), None).to_dict('records')` 之后 `json.dump`。
- **原因**: 在 float 列上 `where(..., None)` 会把 None 又转回 NaN,json.dump 写出 NaN 字面量。
- **修法**: 先转 object:`head = df.head(15).astype(object); head.where(pd.notna(head), None)`。加测试断言未打分字段是 None。
- **预防**: 输出 JSON 前先 `astype(object)` 再 `where(..., None)`;测试里用 `json.dump(..., allow_nan=False)`。

### 从 iCloud 备份恢复后,库里悄悄多了旧的、崩溃文件里的行
- **在哪**: `docs/RUNBOOK.md` §7 恢复(`zstd -d` 解 `*.db.zst`)。
- **原因**: 恢复出的 .db 旁边留着旧的 .db.wal,DuckDB 第一次打开时重放它。`zstd -d` 也不肯覆盖已存在的目标文件。
- **修法**: 已修(12e6791、a3b6142):`park()` 把 .db 和 .db.wal 一起移走,任一还在就停;备份把 WAL 并进副本,并对 .zst 做恢复测试。
- **预防**: 第一次打开前,`find ~/optradar ~/.hedge-fund/agent -maxdepth 1 -name '*.wal'` 必须无输出。**不要信 RUNBOOK §7 最后一行 `ls ~/optradar/*.wal ~/.hedge-fund/agent/*.wal 2>/dev/null`**:zsh 下没有 WAL 时打印 no matches found;只有一个目录有 WAL 时整条 ls 中止,不列出它(见 zsh `no matches found` 条)。
- **已记录**: `docs/RUNBOOK.md` §7 从 iCloud 备份恢复(其 WAL 检查行有 zsh glob 问题)。

## Shell、zsh 与 git

### pytest 输出 `1 failed, 355 passed`,`&&` 后面的提交照样执行
- **在哪**: 以 `| tail` 或 `| grep` 结尾的命令链:pytest、`site/publish_guard.py`、git commit/push;后台 python 任务(sec_form4 load 等)显示 Traceback 却报 `[exited with code 0]`。
- **原因**: 管道的退出码是最后一个命令(tail)的,`&&` 和后台任务的退出码都看不到 pytest、guard 或 Python 的失败。9/29 因此两次带着失败的测试提交、一次在 publish_guard 报警后推送。相反的假报警:命令以 `[ $r -ne 0 ] && grep …` 结尾时,测试通过了整个调用也报 Exit code 1。
- **修法**: 先存退出码:`cmd > log 2>&1; r=$?; tail log; [ $r -eq 0 ] && git …`。结尾用 `if [ $r -ne 0 ]; then …; fi`,通过时不会退出 1。或 `set -o pipefail`(zsh 看 `$pipestatus`)。信后台任务退出码前先 grep 输出里的 Traceback/Error。报警的提交在推送前用 `git reset --soft origin/v2-rebuild` 合并。
- **预防**: 门禁命令(pytest、publish_guard)不接 tail 再 `&&`。命令以管道结尾时,不把 `[exited with code 0]` 当成功。
- **已记录**: AGENT.md §1 提交规则("先看退出码再提交和推送")。

### `(eval):1: no matches found: --include=*.py`
- **在哪**: Bash 工具(本机是 zsh):`grep -rn … --include=*.py`;匹配不到文件的 ls/grep glob(如 `agent/s45*`、`~/.hedge-fund/agent/*.log`)。
- **原因**: zsh 的 NOMATCH:没匹配到的未加引号 glob 直接报错,整条命令行中止(bash 会原样传下去);`2>/dev/null` 挡不住。`--include=*.py` 被当成相对 cwd 的 glob 展开。后面串的命令不跑,输出悄悄缺失。
- **修法**: 给模式加引号:`--include='*.py'`。可选的文件 glob 加引号、用 `(N)` 限定符或 `setopt nullglob`,或改用 find;`git grep` 也行。有风险的 glob 单独一条命令,读其余输出前先看有没有 `(eval):1: no matches found`。
- **预防**: 交给程序的 glob 参数(grep --include/--exclude、find -name)和可能匹配不到的 glob 一律单引号。

### `(eval):24: command not found: timeout` / `usage: pgrep [-Lfilnoqvx] [-d delim] …`
- **在哪**: macOS 上的 Bash:`timeout 900 python -m agent.execute`、`PYTHONPATH=. timeout 600 python …`、`pgrep -fc run_backtest`。
- **原因**: macOS 是 BSD 工具链,没有 GNU timeout 也没有 gtimeout,命令根本没跑(退出码 127),接了 tail 还显示 exit=0。BSD pgrep 没有 -c。
- **修法**: 去掉 timeout:用 Bash 工具的 timeout 参数、run_in_background 加 TaskStop、curl 的 `--max-time`,或脚本内看门狗 `threading.Timer(45, lambda: (print('TIMEOUT'), os._exit(3))).start()`。计数用 `pgrep -f x | wc -l` 或 `ps aux | grep -c "[x]"`。sed 用 BSD 写法 `sed -i ''`。
- **预防**: 本机按 BSD 工具假设:没有 timeout、没有 pgrep -c、没有 GNU `sed -i`。

### `error: cannot pull with rebase: You have unstaged changes. error: Please commit or stash them.`
- **在哪**: `site/publish.sh`(每日推送前 pull)和 agent 的任何 commit + push。
- **原因**: 起初 publish.sh 不 pull 直接推,分支一动就推送失败;加了普通 `pull --rebase` 后,其他任务留下的已跟踪文件改动(如 `site-data/events.db`)让它拒绝执行。
- **修法**: `git pull -q --rebase --autostash origin v2-rebuild && git push -q origin v2-rebuild`。publish.sh 已改(ef2618a、f4d6753)。
- **预防**: 本仓库 pull 一律 `--rebase --autostash`。
- **已记录**: `site/publish.sh` 第 41 行(pull --rebase --autostash → publish_guard --range → push)。

### 提交带了 `Co-Authored-By` 尾行 / `fatal: unable to auto-detect email address (got '<user>@<host>.(none)')`
- **在哪**: 两个仓库的 git commit;新机器上 `~/hedge-fund`、`~/optradar` 的 merge/commit。
- **原因**: harness 默认署名要求加 Co-Authored-By,和用户规则冲突。新机器上没配 git 身份;第一次从 `git log` 猜的身份是错的,用户改成 LouisXO。
- **修法**: 作者用 AGENT.md §1 写明的 LouisXO 和邮箱,不加 Co-Authored-By,保留 `Claude-Session:` 尾行。新机器在每个仓库设 user.name/user.email,未推送的提交 `git commit --amend --no-edit --reset-author`。两个仓库的本地配置现在都有身份。
- **预防**: AGENT.md 里的用户规则优先于 harness 默认署名。身份用文档里的,或者问;不从历史推断。
- **已记录**: AGENT.md §1 提交规则;`optradar/AGENT.md` §1 铁律 4。

### `(eval):1: command not found: sec_xbrl`(用 heredoc 编辑文档之后)
- **在哪**: 用未加引号分隔符的 `python3 - <<EOF`(为了让 $W 展开)编辑含 markdown 代码片段的 `docs/AGENT_PLAN.md`。同时报 `weekly_fundamentals.sh`、`health.py` command not found。
- **原因**: 未加引号的 heredoc 对反引号做命令替换。shell 执行了 Python 字符串里每个反引号片段,其中 `touch ~/.hedge-fund/agent/refresh_fundamentals.ok` 真的跑了,创建了只该由用户创建的周更刷新开关;文档那一行的代码片段全丢。轻一点的同类:`\$` 转义带进单引号 awk 程序,报 `awk: syntax error`。
- **修法**: 当时删掉误建的开关文件,用 `<<'PYEOF'` 重写受损行(3ff44b1)。**该开关在 S47 切换时(2026-09-29 00:18 PT)被有意重新创建,是 `agent/bin/weekly_fundamentals.sh` 检查的现行许可,不要删现在这个。**
- **预防**: heredoc 分隔符一律加引号(`<<'EOF'`)。shell 值经 argv(`python - "$P" <<'EOF'`,里面读 `sys.argv[1]`)或环境变量传入,不插值。heredoc 编辑后出现 command not found,按"可能执行了代码"处理。

### `/Users/louis/.hedge-fund/.env:1: command not found: <email>`
- **在哪**: 调 Alpaca API 前 `set -a && . ~/.hedge-fund/.env && set +a`。
- **原因**: SEC_USER_AGENT 的值是名字加邮箱,中间有未加引号的空格。source 时第二个词被当命令执行,变量也被截断。
- **修法**: 只 source 需要的键:`set -a && . <(grep -E '^ALPACA' ~/.hedge-fund/.env) && set +a`;或在 Python 里解析 `dict(l.strip().split('=',1) for l in open(p) if '=' in l and not l.startswith('#'))`。User agent 用 `grep -E '^SEC_USER_AGENT=' ~/.hedge-fund/.env | cut -d= -f2-`(execute.sh、late_insider.sh 取 AGENT_EXEC 也是 grep|cut,不 source 整个文件)。
- **预防**: 不 source 整个 .env,不打印其中的值。

### `(eval):1: ======= not found` / `(eval):1: condition expected: <`
- **在哪**: Bash 工具(zsh):`echo ======== INSIDER` 当分隔线;循环里 `[ "$a" \< "$b" ] || continue`。
- **原因**: zsh 里以 `=` 开头的词做 =command 展开,`=======` 被当命令查找,报错并中止整行(提示里的 = 比输入少一个)。zsh 的 `[`/test 内建不支持 bash 接受的 `\<` 字符串比较。
- **修法**: 分隔线加引号(`echo '====='`)或用 `----`、`####`。字符串比较用 `[[ $a < $b ]]`,或在 Python 里比。
- **预防**: zsh 里不写以 = 开头的未加引号词;字符串排序用 `[[ ]]`。

### `sort: Illegal byte sequence`
- **在哪**: 对 `~/optradar/out/agent/execute.log`、`collect.log` 做 grep/awk/sort(中文和二进制字节混杂)。同类:对含中文的 JSONL 用 `grep -o` 加否定字符类的有界重复(`[^"]\{0,60\}…`),报 `ugrep: error: error at position 63 ... exceeds complexity limits`(本机 grep 是 ugrep)。
- **原因**: UTF-8 locale 下 BSD sort/grep 遇到非法字节序列就停。否定字符类的有界重复挨着多字节中文,展开后超出 ugrep 的复杂度上限。
- **修法**: 加 LC_ALL=C:`LC_ALL=C grep -a ... | LC_ALL=C sort | uniq -c`。JSONL 改用 python(json + re)或 duckdb 取字段。
- **预防**: 这些日志一律 `LC_ALL=C` 加 `grep -a`;含中文的 JSONL 不用 grep 的 {m,n} 区间。

### `ModuleNotFoundError: No module named 'pandas'`
- **在哪**: 用 Homebrew 的 `python3`(3.14)跑 `python3 - <<EOF` 临时分析。同类:报告脚本(`agent/s26_option_gate.py`、`agent/s27_exec_cost.py`)里 `DataFrame.to_markdown()` 报 `ImportError: Missing optional dependency 'tabulate'.`。
- **原因**: 系统 python3 没有项目依赖,hedge-fund 的依赖在 `~/.hedgefund-venv`。tabulate 没装在 hedge-fund venv 里,也不在 `requirements.txt`。
- **修法**: 用 `/Users/louis/.hedgefund-venv/bin/python`,在 `~/hedge-fund` 下加 `PYTHONPATH=.`。to_markdown 换 `to_string()`(`sed -i '' 's/dec.to_markdown()/dec.to_string()/'`)或手写 markdown 表。
- **预防**: 凡是 import pandas 或 duckdb 的,不用裸 python3。本仓库不调 to_markdown,除非有意把 tabulate 加进依赖。

### `(eval):1: no such file or directory: /Users/louis/.hedgefund-venv/bin/python`
- **在哪**: macOS 账户从 `<old-user>` 改名为 louis 后的新机器;同时 `/Users/louis/.local/bin/poetry` 不存在。
- **原因**: venv 符号链接目标、venv 里的 shebang、pipx shim 仍指向 `/Users/<old-user>/…`。
- **修法**: 按 RUNBOOK §8 第 3 步重建(S35 起没有 pyproject.toml,poetry 那套不再适用):`rm ~/.hedgefund-venv`(目前是指向 `~/Library/Caches/pypoetry/virtualenvs` 的符号链接),`python3.13 -m venv ~/.hedgefund-venv && ~/.hedgefund-venv/bin/pip install -r ~/hedge-fund/requirements.txt`;`rm -rf ~/.moomoo/venv && python3.10 -m venv ~/.moomoo/venv && ~/.moomoo/venv/bin/pip install -r ~/optradar/requirements.txt`。pipx 用 `pipx reinstall-all`。
- **预防**: 新机器用同一个用户名(脚本和 plist 写死 `/Users/louis`)。venv 重建,不复制。
- **已记录**: `docs/RUNBOOK.md` §8 换机重建清单。

## Alpaca 模拟盘下单与对账

### OPG 单 `expired`、`filled_qty 0` / `40310000 "opg orders must be submitted after 7:00pm and before 9:28am"`
- **在哪**: `agent/execute.py` 向 Alpaca 模拟盘发内部人书和长线书的开盘竞价单(POST /v2/orders,time_in_force=opg)。
- **原因**: 模拟盘不能可靠模拟开盘竞价:OPG 单只有一次机会,之后过期,部分成交被取消。9/24 市价开盘单 5/5 过期;9/23 限价开盘单 7/9 过期,虽然开盘价在限价内;Alpaca 显示 04:00 ET 提交、09:31 ET 过期;同期 DAY 单 30/30 成交。40310000:Alpaca 只在 19:00–09:28 ET 接受 OPG 单,第一次实发在 16:5x ET。
- **修法**: `agent.execute` 默认发 DAY 单(5c902a0,S36)。只有 `AGENT_TIF=opg` 且 `opg_window()` 判定在 19:00–09:28 ET 时才发 OPG(execute.py L905),那时是市价开盘单;窗口外退回 DAY。任务已移到 16:10 PT = 19:10 ET(0eed28d)。代码不检查是不是实盘账户,模拟盘上不设 AGENT_TIF。评估按真实开盘竞价重记成交(`agent/auction_basis.py`)。
- **预防**: 模拟盘只用 DAY,不要因为"更像回测"改回 OPG。手动带 `AGENT_TIF=opg` 跑之前看纽约时间。执行偏差按竞价价衡量,不按模拟器成交。
- **已记录**: `docs/AGENT_PLAN.md` §9 S36、S20;AGENT.md §3 表下说明(40310000)、§4;`agent/execute.py` L198-209 注释。

### `reconcile [...]` 不 ok:`XYZ: lots 10 vs alpaca 1` / `alpaca position with no lot (manual?)`
- **在哪**: `agent/execute.py` `plan_book`,execute.log。
- **原因**: 旧代码只有买入循环检查冻结集合,卖出循环仍按账本数量发卖单,拆股或手动平仓后可能开出空头。
- **修法**: 已修(5a1b1c5;拆股提示 884e62b):`plan_book` 有 `frozen` 参数,对账不符的票两边都不发单。整数倍不符报为 `possible_splits`,不自动修。
- **预防**: 不用交易让账本对上,先解释不符的原因。
- **已记录**: `docs/RUNBOOK.md` §2 对账不一致;AGENT.md §0 铁律 7;`docs/AGENT_PLAN.md` §9 S45 结果 + S46。

### `AGENT_EXEC=off` 时手动 `python -m agent.execute --submit` 仍会发模拟盘单
- **在哪**: `agent/execute.py` 对比 `agent/bin/execute.sh`、`agent/bin/late_insider.sh`。
- **原因**: AGENT_EXEC 只由 shell 包装脚本读(execute.sh L15-23、late_insider.sh L17-20,另有 health.py);execute.py 只看 `--submit`,`agent/broker/alpaca.py` 也不查。
- **修法**: 代码里没有闸门。规则:开关关着时,不手动传 `--submit`。
- **预防**: 考虑在 execute.py 内强制检查 AGENT_EXEC。在那之前 RUNBOOK 应明说 `--submit` 绕过开关;AGENT.md §0 铁律 1 对开关的说法偏强。
- **已记录**: `docs/RUNBOOK.md` 总原则"停止发单"第三条(未解决);AGENT.md §0 铁律 1(说法偏强)。

### `[SUBMITTED] bar … (last session …, NOTHING PLANNED: market hours: orders are only planned between 16:05 and 09:25 ET)`,退出码 3
- **在哪**: Mac 睡眠后 launchd 补跑的 agent.execute;execute.log 随后是 `execute FAILED (exit 3)`。变体:`bar date != last session`。
- **原因**: 16:10 时 Mac 在睡眠。launchd 在下次唤醒时补跑错过的任务(关机后不补),可能在 08:30 PT = 11:30 ET,正在盘中。
- **修法**: 这是 S46 保护(5a1b1c5)在起作用:只在美东 16:05–09:25 之间提交,且日线日期必须等于上一个交易日(双向检查)。不用处理。
- **预防**: grep execute.log 时不区分大小写找 `execute failed`(包装脚本现在写 `execute FAILED (exit N)`;RUNBOOK §1 还引用旧的小写文本)。
- **已记录**: `docs/RUNBOOK.md` §1 先看;AGENT.md §4 下单保护(S46)。

### DAY 市价买单按卖一成交,远高于开盘竞价
- **在哪**: 模拟盘 DAY 市价单(内部人书入场)。2026-09-28 INBX 买在 105.33,开盘竞价 98.69,高于当天所有成交。
- **原因**: 冷门股开盘后几分钟的卖一是做市商的占位报价,模拟盘按这个报价记成交。
- **修法**: 所有入场改 DAY 限价单,前收盘 × 1.03(705bd90,S36c);高开超过 3% 的次日重试一次(S36b)。卖出仍是市价单,改不改在 §10 待定。
- **预防**: 入场不用市价单。
- **已记录**: AGENT.md §4 内部人行(不用市价单);`docs/AGENT_PLAN.md` §9 S36c。

### `BrokerError: GET /account -> no answer: URLError: <urlopen error [Errno 8] nodename nor servname provided, or not known>`
- **在哪**: `agent/broker/alpaca.py` `PaperBroker._req`;`agent/execute.py` 提交循环和 `sync_fills`。Mac 离线时现在报这一行。
- **原因**: 旧代码 `_req` 只把 HTTPError 转成 BrokerError,URLError、TimeoutError 逃出循环的 `except BrokerError`;agent_orders 在整个循环之后才写一次,第 k 单超时会让第 1..k-1 单不进账本(没有 lot、没记现金),重跑又当重复跳过,留下孤儿持仓,次晚同一槽位再买一次。另:部分成交时把 Alpaca 累计的 filled_qty 当增量,每次同步都重复记现金。
- **修法**: 已修(S48 A 包:30fb411、884e62b,合并 3fc7ad9;1bb1569)。URLError、超时、连接重置、非 JSON 回复都变成 `BrokerError(status=None)`。每单 POST 后立即写入。异常后按 client_order_id 查询:查到算已接受,404 算 not_sent。启动时认领带 `book|date|ticker|side` client_order_id 的孤儿订单。被拒的 POST 记为 `rejected`、alpaca_id 为空,不算已提交,重跑会重发。同晚重跑日志里的 `skipped N already-submitted ids` 是正常的;已在券商的卖单现在计入买单资金(c18a0fc)。成交按增量记:agent_orders 的 applied_qty/applied_notional,agent_lots 的 sold_qty/sold_notional。
- **预防**: 每个券商调用包住所有网络错误;订单逐单记账,用确定性的 client_order_id。每晚最多重跑一次,06:25 PT 后停。
- **已记录**: `docs/AGENT_PLAN.md` §9 S48(A 下单和账本);`docs/RUNBOOK.md` §1、§3。

### 账本现金和券商现金差 $122.20(模拟盘)
- **在哪**: `agent/execute.py` 现金记账对比 Alpaca 模拟盘账户 activities。
- **原因**: 账本现金只随成交变动。实际上模拟盘收 REG/CAT 费用(9/29 起)、付现金分红(10/14 起),虽然 Alpaca 文档说模拟盘两者都不模拟。
- **修法**: `agent/cash_events.py` 把每条 DIV/FEE activity 按 activity id 只记一次,记进对应的书(d6a1e08)。健康检查"券商分红和费用入账"。
- **预防**: 券商文档说不模拟的,也照样对照账户 activity 流对账。
- **已记录**: `docs/AGENT_PLAN.md` §9 "长线分红记成 $0 和券商现金差 $122.20"、S48 结果最后一条。

## 行情与数据源

### `'reason': 'could not list: efts search; HTTP 500 (https://efts.sec.gov/LATEST/search-index)'` → `form4 realtime refresh FAILED (exit 1)`
- **在哪**: `agent/sources/sec_daily_form4.py` `Http.get` / `index_form4`(16:10 execute.sh 里的 `--realtime`;06:00 每日任务)。06:00 的形式:`could not list: daily index 2026-09-30; HTTP 403`。长回填还见过 HTTP 503、`URLError Errno 60` 超时、`Errno 65 No route to host`。
- **原因**: EDGAR EFTS 分页里偶发单页 500,抓取器遇到第一个就整次失败、不重试,9/29、9/30 内部人书因此没有新买入。当天每日索引发布前 SEC 回 403 而不是 404(SEC 限流也是 403),代码只把 404 当"还没发布"。
- **修法**: 已修(99a296e):`Http.get` 对 5xx 或超时重试两次(3 秒、10 秒后)再算失败。当天或之后日期的索引 403/404 算"还没发布";过去日期的 403 仍算限流,不快速重试。每次运行记进 fetch_runs(ok/partial/failed)。仍失败时几分钟后手动重跑,当时返回 ok。
- **预防**: EDGAR 5xx 和超时可重试;当天索引的 403/404 是"还没发布"。health 读 fetch_runs,抓取失败不会被当成"没有申报"。
- **已记录**: `docs/AGENT_PLAN.md` §10 "9/29 晚的状态";`agent/sources/sec_daily_form4.py` Http docstring。

### `news fetch failed: RuntimeError: {'Information': 'Thank you for using Alpha Vantage! This is a premium endpoint. ...'}`
- **在哪**: `agent/sources/av_news.py`(13:15 任务 `com.louis.agent.news`,日志 `~/optradar/out/agent/collect.log`)和 AV MCP 的 NEWS_SENTIMENT。health 显示 `13:15 新闻情绪: 上次退出码 1`。
- **原因**: 对这个免费 key,NEWS_SENTIMENT 现在算付费端点,每次都失败。
- **修法**: 还没修(截至 2026-10-26 仍失败)。任务每个工作日非致命地失败:在 fetch_runs 记一次失败,退出 1。临时要新闻用 Alpaca 新闻(`agent.sources.alpaca_news`)或 Yahoo。
- **预防**: 不调 AV NEWS_SENTIMENT。考虑停掉 13:15 任务或换数据源。

### SEC companyfacts 缺数据:2026 年 4 月中以后没有 ifrs-full 事实
- **在哪**: `agent/sources/sec_xbrl.py`(companyfacts API)。
- **原因**: SEC 那边的缺口:4 月中以后申报的 ifrs-full 事实没有(TSM 等只有股数);V 的 2026-07-29 10-Q 在 submissions 里有、companyfacts 里没有;多类股发行人(BULL、TBLA)按类别的股数事实被丢掉。
- **修法**: 无。IFRS 数据要解析每份申报里的 XBRL instance。审计把 V 当已知缺口。
- **已记录**: `docs/AGENT_PLAN.md` §9 S53a 补充 3;§10 等待中(V 没有恢复打分)、"S53 / S53a 之后"。

### `[291/291] rows 0 symbols with data 0 failed 291 ... 'aborted': '3 batches in a row without an answer'`
- **在哪**: Alpaca 免费层历史数据(data.alpaca.markets bars/auctions,SIP):`agent/sources/alpaca_bars.py` 和新调用者(如 fpi_data 的 bars 加载器)。底层是 HTTP 403。
- **原因**: 免费层 SIP 延迟 15 分钟,END 落在最近 15 分钟内的查询一律被拒。新加载器用今天日期或 now 当 END。
- **修法**: END 用 `alpaca_bars._end_now()`(UTC now 减 16 分钟);收盘后它仍能取到当天完整日线。重跑得到 288/291,缺的 3 个是优先股代码。
- **预防**: END 不用 `date.today()` 或 now。日线用 `backfill.download_end`(美东 16:15 前截到昨天,也避开 15 分钟的 403);只有确实要当天数据的盘中或分钟线调用才用 `_end_now()`(见"盘中刷新把当天的半日线存成了日线"条)。
- **已记录**: `agent/sources/alpaca_bars.py` `_end_now` 注释;`docs/DATA_SOURCES.md`(只写了"15 分钟前")。

### `BRK-A failed: HTTP Error 400: Bad Request`
- **在哪**: `agent/sources/alpaca_bars.py` `fetch_batch`;`agent/sources/alpaca_intraday.py`(收盘后存档);`agent.market_breadth` 的股数/cik 连接。
- **原因**: Alpaca 的类别股用点(BRK.A),上市名单和多数日线用横线(BRK-A);面板里 BRK-A 和 BRK.B 并存,申报用 BRK.B。100 个代码的批次里有一个横线类别股,整批返回 400;非 200 回复返回部分结果,不报错也不记日志,约 300 个代码一直没有日线。
- **修法**: 已修(4dfd1ec;盘中 99a296e,带 `test_intraday_spelling.py`):请求时 `-` 换 `.`,返回时换回。失败批次重试并对半拆分,找出坏代码并报告。券商写法只映射到池内代码,yfinance 保留 BRK.B/BF.B(0d0db23)。market_breadth 两种写法都映射、去重后再连股数和 cik。类别股不交易(S47 补充 10)。
- **预防**: 每个数据源边界都规范类别股代码(Alpaca 用点,名单用横线),用 BRK-A/BRK.B 测试。非 200 回复必须记日志或抛错。
- **已记录**: `docs/AGENT_PLAN.md` §9 S45 结果 + S46(数据错误)、S47 第 4 项。

### `RuntimeError: {}`(`agent/sources/av_listing.py` fetch)
- **在哪**: Alpha Vantage 免费层:urllib 调 LISTING_STATUS;AV MCP 的 TIME_SERIES_DAILY outputsize=full、HISTORICAL_OPTIONS;同一轮里多个 AV MCP 调用(GLOBAL_QUOTE + EARNINGS_CALENDAR + GOLD_SILVER_SPOT)。同类:`{"error":{"type":"rate_limit","message":"… The outputsize=full parameter value is a premium feature …"}}`、`… Please consider spreading out your free API requests more sparingly (1 request per second)…`。
- **原因**: 限流时免费层回 HTTP 200 加空的 `{}`。付费参数和端点的错误也标成 rate_limit。免费 key 每天 25 次、每秒 1 次,一条消息里发多个工具调用只有一个有回复。
- **修法**: fetch() 最多重试 4 次、每次等 20 秒,直到正文以 `symbol,` 开头(之后取到 14,455 个在市、9,513 个退市)。用 outputsize=compact,在 `av_quota.json` 计数。AV 工具一轮只调一个;报价可退回 yfinance(`~/.hedgefund-venv`,`yf.Ticker(t).history`)。
- **预防**: `{}` 或 premium 当软失败,重试或跳过,不当数据解析。每天 25 次要做预算。不在一条消息里批量调 AV MCP。

### `av_estimates_log status = "error: {'Information': 'We have detected your API key as <KEY> and our stand…"`
- **在哪**: `agent/sources/av_estimates.py` 往 estimates.db 写错误。另有两处同样的写法仍未清洗(待修)。
- **原因**: AV 的每日配额消息开头附近复述 API key,加载器原样存了 `str(exc)`。
- **修法**: av_estimates 已改(c69c775):存 `msg = str(exc).replace(api_key(), "***")[:80]`,遇到 rate limit、premium、25 requests 停掉整批;已存的错误行已删。未做:另两处在打印或存储前做同样的替换。
- **预防**: 供应商错误文本进 DB 或日志之前先去掉密钥。
- **已记录**: `agent/sources/av_estimates.py` L108 注释。

### `Error: result (307,505 characters) exceeds maximum allowed tokens. Output has been saved to …`
- **在哪**: 云端 routine 里的 AV MCP EARNINGS_CALENDAR(horizon=3month、return_full_data=true 或不给 symbol);本地 AV MCP LISTING_STATUS。
- **原因**: 全市场的返回远超工具结果上限。
- **修法**: 让 harness 存成文件,再用 jq 或 python 过滤(或按 offset/limit 读)。只要几个代码时传 symbol=。routine 提示词写的是 `EARNINGS_CALENDAR(horizon=3month、return_full_data=true,结果文件用 jq 过滤)`。本地 market_breadth 把日历缓存在 `~/.hedge-fund/agent/earnings_calendar.csv`。
- **预防**: 大的 AV 返回当文件处理再过滤,不内联。
- **已记录**: 云端 routine 提示词;`agent/market_breadth.py` 模块 docstring(日历缓存)。

### 盘中刷新把当天的半日线存成了日线
- **在哪**: `agent/daily.py` 08:41 刷新、`agent/backfill.py`、`agent/sources/alpaca_bars.py` missing/backfill。
- **原因**: 下载 END 没跳过正在进行的交易日:9/22 有 119 个名字存了半日线,当天打分用了它;9/29 `agent.sources.alpaca_bars missing` 把 NRG 未完成的日线写进生产 panel.db。回填还用 PT 日期,21:00 PT 后手动重跑会漏掉刚收盘的交易日。
- **修法**: 美东 16:15 前下载只到昨天,打分用今天之前的最后一根(c8bbc19);因子 ETF(3d8b060)和指数(aa89d3a)同样处理;截止用纽约日期和时钟(bd767f1,`download_end`)。**未修**:`alpaca_bars missing/backfill` 的 END 仍是 now−16 分钟(`_end_now`),改它要用户批准(§10)。NRG 那根由 13:25 postclose 同步覆盖,覆盖前没人读过。
- **预防**: 盘中不跑 `alpaca_bars missing`。新加载器用 `backfill.download_end`。清理生产数据交给用户。
- **已记录**: `docs/AGENT_PLAN.md` §9 S20、"长线报警排查后续" ③、§10 等待中;`agent/backfill.py` L119 注释。

### `OperationalError('unable to open database file')` / `$ALTR: possibly delisted; no timezone found` / `curl: (77) error adding trust anchors …`
- **在哪**: launchd 下的 yfinance 增量日线/指数刷新(agent.daily 08:41、optradar cron)。随后是 `agent: compute failed: 'trade_date'`;也见过 `Cookie/crumb fetch failed (DNSError)`。
- **原因**: launchd 下 yfinance 的 sqlite 时区缓存和 curl 不稳定,Mac 刚唤醒时 DNS 也会失败。Yahoo 删掉退市代码的历史(2016 年非标普覆盖只有 44%),很多代码失败;空的指数表再触发 `KeyError('trade_date')`。
- **修法**: 面板刷新是尽力而为:打印 `agent: panel refresh failed, scoring on the existing panel`,用现有数据打分(Alpaca 收盘后更新已经有最后一根)。`backfill_index` 跳过空表(fa9e683)。2016 年起以 Alpaca 日线为主(覆盖 77%),yfinance 只补 2015。
- **预防**: 一个供应商的刷新不能拖垮早报;launchd 任务里关键的东西不依赖 yfinance;对幸存者偏差敏感的工作用 Alpaca 日线。
- **已记录**: `agent/daily.py` L178 / main() 注释;`agent/health.py` L89(日志模式)。

### `index <代码>: update failed, not written: the response leaves out N stored day(s) (...)`
- **在哪**: `agent.backfill index|factors`(basis_guard),在 13:25、16:10、08:41 任务里。同类:`factor ETF <代码>: update failed, not written`、`factor ETF update partly failed`。
- **原因**: Yahoo 每次分配都重新缩放整条 Adj Close。如果返回同时漏了已存的日子,写进去会在新旧基数的接缝处造出假收益。
- **修法**: 出现一次就等下次运行。同样的日子连续两次被漏,只删这些已存行(避开 13:25/16:10),再跑 `python -m agent.backfill index`(或 factors)。
- **预防**: 只出现一次时不动手,不绕过 basis_guard 强写。
- **已记录**: `docs/RUNBOOK.md` §4 "指数 / 因子 ETF 没写入(basis_guard,2026-10-21 起)"。

### Alpaca `adj_close = 0`;分红变成 `basis_break $0`;除息日前出现假下跌
- **在哪**: `agent/sources/alpaca_bars.py` 加载器和 `alpaca_bars.update`(7 天重取)、`agent/dividends.py` `factor_events`、`agent/auction_basis.py`、`agent/drift.py`。
- **原因**: 供应商数据缺陷:审计发现 710 行 adj_close = 0。每晚只重取最近 7 天,相邻行可能带不同的调整基数;factor_events 要求除息日两边来自同一次抓取,行一出窗口这一步就变成 $0(BZ 10/01 是 $68.34,10/05 起 $0);整表每晚重算,改写了历史。另:auction_basis 的分红步骤出错时,所有历史 div_cum 写成 0;drift 在缺 equity_auction_tr 时悄悄退回只算价格的净值。
- **修法**: 已修。adj_close <= 0 置 NULL(`alpaca_bars.py:203`);有原始日线没有调整日线的代码直接失败,不把原始价当调整价存。相邻行检查调整因子连续性,断了整只重取(S47 第 5 项 / 补充 9,12f4b85)。分红改从 Alpaca corporate actions 取,只追加缓存在 agent_corp_actions(d6a1e08)。分红步骤失败时退回最近一次存的 agent_dividends 并带时间,不可用就留 NULL,报警、evaluate、health 标出该日并不计入,最新一天缺失算坏(aa89d3a ②);分红失败不再停掉净值(9ebf6db)。
- **预防**: 未知不是零。
- **已记录**: `docs/AGENT_PLAN.md` §9 S28、"长线分红记成 $0 和券商现金差 $122.20"、S47 第 5 项和补充 9、"长线报警排查后续" ②;§10 10/21。

## 定时任务、健康检查与通知

### `publish_guard: 发现 1 处疑似真实账户信息,中止推送: <commit>:docs/notes/<TICKER>.md:<行> [持股数量] ...`
- **在哪**: `site/publish.sh` + `site/publish_guard.py --range origin/v2-rebuild..HEAD`,git push 之前;Mac 通知"公开站推送已中止"。
- **原因**: publish.sh 推整个 v2-rebuild 分支,包括其他 session 的文档提交;guard 逐个扫描待推提交自己的 patch。持股数量规则对中文"持有"后接数字和"股"的写法不论主语都报,包括公开基金的 13F 持仓(英文分支要求主语是 I/we/user/owner)。之后再改写或删除的提交,仍会把被标记的那个提交带进公开历史。
- **修法**: 确属私密的,先复制到 optradar 私有文档。改写措辞(用"持仓""持股")。用 `git reset --soft origin/v2-rebuild`(之前不要 fetch)改写未推送的历史,合成一个提交。重跑 `publish_guard.py --range origin/v2-rebuild..HEAD` 直到返回 0,再 `PUBLISH=1 zsh site/publish.sh`。
- **预防**: 公开笔记里不用"持有 + 数字 + 股"的写法,写第三方也不用。只合并未推送的历史。
- **已记录**: `docs/RUNBOOK.md` §10 公开站推送被关键词检查中止;AGENT.md §1。

### 手动重跑成功后 health 仍报 `上次退出码 N`
- **在哪**: `launchctl list | grep com.louis` 的退出码;`agent/health.py` 任务检查(约 L630-643)。
- **原因**: launchd 保留上次退出码直到下一次定时运行。health.py 只对 `com.louis.optradar.weekly` 在当周六 `out/weekly/<date>.html` 存在时清掉(4075f64),其他任务一直报到下次定时运行退出 0。
- **修法**: 周报:重跑到报告生成,警告自动清除。其他任务:在该任务的日志段确认手动重跑成功,然后到下次定时运行前忽略这条警告。
- **预防**: 确认过的手动重跑之后,残留的 `上次退出码 N` 不当新故障,看任务最新的日志段。
- **已记录**: `agent/health.py` L638 注释(只管周报);`docs/RUNBOOK.md` 去哪里看(launchctl list 第二列)。

### launchd 任务的 Mac 通知不出现(终端里正常),退回不可点击的 osascript
- **在哪**: optradar `bin/run_and_notify.sh`(08:41 早报)和 hedge-fund `agent.watch` 里的 terminal-notifier 调用。
- **原因**: launchd 的 PATH 没有 `/opt/homebrew/bin`,`command -v terminal-notifier` 失败。另:cron.log 里"推送没发出去:终端现在有人在用…"一类的行是预期行为,终端 session 活跃时手机推送有意跳过。
- **修法**: 写死绝对路径 `TN=/opt/homebrew/bin/terminal-notifier`(optradar 68429b2,hedge-fund 723bedb)。
- **预防**: launchd 脚本里所有 Homebrew 程序都用绝对路径。
- **已记录**: optradar `bin/run_and_notify.sh` 第 73 行注释;`optradar/AGENT.md` §1 铁律 3。

### `[warn] 长线名单来源: 2026-10-16 composite_long ... 是事后修复(repair-2026-10-19)`
- **在哪**: `agent/daily.py` `long_book_picks`;`agent/shadow_v2.py`;agent_picks 里的长线名单(早报 vs 16:10 execute)。
- **原因**: 10/16 的 16:10 任务在更新日线之前死在 `broker.account()`(Mac 离线)。早报补记缺失日的名单时不检查日线覆盖(下单任务有自己的 98% 检查),也没有检查谁写的名单:10/19 早报只用 502 个有日线的名字(平时约 5,300)补了 10/16 的名单,前 30 名全是大盘股,和记录的 10/16 前 30 只重合 3 个。
- **修法**: `thin_bar_day`:有日线的名字少于前 5 个交易日中位数的 90% 时不记名单(a1fe323)。`agent/health.py` `check_list_writers` 标出最近 10 个交易日里 run_id 不是 `exec` 的名单(repair- 显示为 warn)。修复时先把名单备份到 manual_backups/,再用带撤销文件的幂等脚本重写,run_id 标 `repair-2026-10-19`。
- **预防**: 任何一次 16:10 没跑成,先查缺口日 agent_picks 的 run_id,再信 drift 或归因重放。已知局限:部分批次失败(约少 6%)抓不到;早报要不要也用 98% 覆盖检查在 §10 待定。
- **已记录**: `docs/AGENT_PLAN.md` §9 "10/16 名单污染的修复和名单保护";`agent/health.py` `check_list_writers` docstring。

### 任务某步失败,launchd 和健康页却显示 OK
- **在哪**: `agent/bin/execute.sh`、`postclose.sh`、`weekly_fundamentals.sh`;`agent/health.py`;`agent/sources/sec_xbrl.py` load(周日 `com.louis.agent.fundamentals`)。
- **原因**: 每步以 `|| echo "execute failed"` 结尾,退出码永远是 0;health 只看 `=== execute` 开始标记。周更脚本调了不存在的 `--force`,首次运行(2026-09-27 03:00)失败、用旧事实重建,健康页仍显示 ok;`load()` 跳过 xbrl_load_log 里已有的所有 CIK;新鲜度检查看的是 max(filed)。反方向的误报:软失败正则 `\b[Ff]ailed\b` 匹配到打印出来的统计字典里值为 0 的 `'failed'` 键,报 `[warn] ... 非关键步骤失败:{... 'failed': 0, ...}`。
- **修法**: 已修。影响下单的步骤写 FAILED,剩余步骤跑完后让任务失败;名单刷新不再 `|| true`(7d41524)。health 读最近一次运行里的 FAILED、failed、Traceback,并标出迟到的启动(cdc0564)。新增 `sec_xbrl load --max-age-days N`,失败时非零退出;health 看 fetched_at 最旧的十分位,不看最大值;运行由 `~/.hedge-fund/agent/refresh_fundamentals.ok` 把关,每次全量刷新前做经过恢复测试的 panel 快照(S54)。`ZERO_FAIL_RE = r"\bfailed['\"]?:?\s*0\b|\b0 failed\b"`,带 `'failed': 0`(ok)和 `'failed': 2`(warn)的测试(f1ad09b)。
- **预防**: 新任务脚本不吞退出码。日志扫描排除计数器、按整词匹配;任务输出加了新计数器,就把它加进 health 测试的零失败用例。
- **已记录**: `docs/AGENT_PLAN.md` §9 S48(B 数据和运维)、S48 结果、S45 结果 + S46;AGENT.md §4 S47。

### 数据集悄悄停更或断档,依赖它的检查悄悄失效
- **在哪**: `agent/backfill.py` `backfill_factor_etfs`(index_daily 里 MTUM/VLUE/QUAL/USMV/AVUV 停在 2026-09-28);insider_tx(季度批量 sec_form345 + 每日索引爬虫)2026-07-01 到 09-16 没有行、6/30 后没有卖出,关注名单和复盘的内部人卖出检查失效。
- **原因**: 注释写"手动或周任务跑",实际没有任何定时任务调用它。季度批量止于 6/30,每日爬虫 9/17 才开始,中间没人接上;爬虫还只留公开市场买入。
- **修法**: postclose.sh 每天跑 `python -m agent.backfill factors`(S54);health 的指数新鲜度看 SPY/IWM/QQQ 中最旧的。insider_tx 按 10 天一段用 `crawl(days=10, today=<段末>)` 回填,约 63k 份申报,约 50 份因 SEC 临时错误丢失;爬虫现在也留公开市场卖出(5eacb23、3921c82)。
- **预防**: 加数据集时确认确实有任务调用它的加载器,并加进 health 新鲜度检查。两个数据源交接处,按营业日检查连续性。
- **已记录**: `docs/AGENT_PLAN.md` §9 S54 实施(口径修正)、§10 "9/29 晚的状态"(内部人数据缺口);`agent/backfill.py` L269 注释。

## 密封 LLM(claude -p)

### `Expecting , delimiter`(标题翻译整批丢失)
- **在哪**: 经密封 claude -p 的新闻标题翻译。现在是 `agent/translate.py`(`agent/watch.py`、`agent/review.py` 调用);当初是 `masters/translate.py`(S35 已删,2a14af2)。现在的表现:`translate skipped: Expecting ...`,或标题悄悄没翻译。
- **原因**: 模型输出含自由文本的 JSON 很脆:一个未转义的引号或长度不对的回答就丢掉整批。08:41 那次 35 条丢了 15 条(一句里一个引号毁掉一批 10 条)。
- **修法**: c8982a2 曾把 `masters/translate.py` 改成 `[n]` 编号标记,对批次漏掉的逐条单独重试,但那个文件已删。`agent/translate.py` 仍要求 JSON 数组(SYSTEM L17-18,L40 json.loads),失败的批次不缓存,下次运行重试。再出现就把 `[n]` 标记和逐条回退移植进 `agent/translate.py`。
- **预防**: 不要求 LLM 返回自由文本字符串的 JSON 数组;用编号标记加逐条回退。

### `claude -p` 非零退出、stderr 为空
- **在哪**: `integrations/claude_code_llm.py` `ClaudeCodeLLM.call`(`agent/narrator.py`、`agent/translate.py` 等密封调用者共用)。
- **原因**: 限流压力下出现(约 335 次调用里 128 次)。当时没有重试,失败和弃权看起来一样,那次运行悄悄产出几乎空仓的书。
- **修法**: d232e88 加了带抖动的指数退避重试。现在默认 `retries=3`:共 3 次,间隔 4 秒 × 2^(n−1) 加最多 2 秒,最后 `fail('exhausted', 'claude -p failed after 3 attempts: …')`。任何非零退出、错误信封或解析不了的信封都按暂时性处理。
- **预防**: LLM 调用失败必须是错误,不能变成中性答案。

### `Failed to authenticate: OAuth session expired and could not be refreshed`
- **在哪**: 无人值守的 `claude -p`:早报叙述、新闻翻译、周报(optradar `bin/run_weekly.sh`,周六 LLM 审计)。
- **原因**: 生产 Mac 上 Claude CLI 登录过期,launchd 任务无法刷新。早报叙述和翻译悄悄回退,9/26 周报 LLM 输出三次为空。
- **修法**: 用户重新登录(终端 `claude`,然后 `/login`),重跑周报。agent.health 每次跑一个极小的登录探测(S43,bf4c1b2),失效时显示 `Claude 登录 失效:… → 在终端运行 claude 然后 /login`。
- **预防**: health 每天验证 Claude 登录。出现模板或英文回退,当作 LLM 步骤失败。
- **已记录**: `docs/AGENT_PLAN.md` §9 S43;`docs/RUNBOOK.md` §8 第 8 步;`agent/health.py` L758。

### `ls: /Users/louis/.claude/local/claude: No such file or directory`
- **在哪**: 所有写死 `~/.claude/local/claude` 的地方:`integrations/claude_code_llm.py` CLAUDE_BIN(L45)、optradar `bin/run_and_notify.sh`(L55)和 `bin/run_weekly.sh`(L11)。
- **原因**: 新安装把 CLI 放在 `~/.local/bin/claude`,脚本写死 `~/.claude/local/claude`。
- **修法**: `mkdir -p ~/.claude/local && ln -s ~/.local/bin/claude ~/.claude/local/claude`,再验证 `~/.claude/local/claude -p "reply with: ok" --model opus --output-format json`。
- **预防**: 换机重建清单里有这一步。
- **已记录**: `docs/RUNBOOK.md` §8 第 8 步。

## 云端 routine

### 夏令时结束后云端 routine 早一小时触发(08:30 PST)
- **在哪**: Claude 云端 routine(美股早报、科技行业日报)。
- **原因**: cron 是 UTC:`30 16 * * *` 是 09:30 PDT。
- **修法**: 2026-11-01 之后两个都改成 `30 17 * * *`。
- **预防**: cron 按 UTC 写,改时间时按当时的 PT 偏移换算。

### `{"error_type":"EGRESS_BLOCKED","domain":"finance.yahoo.com","message":"Access to finance.yahoo.com is blocked by the network egress proxy."}`
- **在哪**: Claude 云端 routine(RemoteTrigger)美股早报 / 科技行业日报。
- **原因**: 云端环境的网络白名单挡了财经和新闻站。此外:stooq 对所有脚本返回 JS 机器人检查;Reuters、BBC 拒绝 WebFetch;CNBC、TheStreet、Benzinga 从云端回 403;github.com 只限配置的仓库;AV REALTIME_BULK_QUOTES 和 NEWS_SENTIMENT 是付费的,每秒限制容易撞。
- **修法**: 用户把云端环境网络设为 Full,之后没再出现 EGRESS_BLOCKED。提示词列出不要试的站点,改用 Yahoo chart API、免 key 的 Yahoo 新闻搜索和 RSS、Nasdaq RSS、financialmodelingprep 的 logo。AV 只用 EARNINGS_CALENDAR + TOP_GAINERS_LOSERS,最多 5 次、间隔 ≥ 2 秒。提示词要求遇到 403 或 EGRESS_BLOCKED 换源,503 或超时 30 秒后重试一次,永远不排查或绕过代理。用 RemoteTrigger `get_run_log` 排查。
- **预防**: 更新 routine 时提交完整 job_config 和新的 event uuid,然后跑一次、读 `get_run_log` 再汇报。

### `ModuleNotFoundError: No module named 'PIL'`(已经 `pip install -q pillow`)
- **在哪**: 云端 routine 美股早报的沙箱(经 RemoteTrigger `get_run_log` 看到)。
- **原因**: 裸 `pip` 装进的解释器和 `python3`(`/usr/local/bin/python3`)不是同一个。
- **修法**: `python3 -m pip install -q pillow`。
- **预防**: routine 提示词里一律用 `python3 -m pip` 安装。

## Claude Code 工具环境

### `Blocked: sleep 60 followed by: cat …/tasks/<id>.output.`
- **在哪**: Claude Code Bash 工具:任何以 `sleep N; <检查>` 开头的前台命令。
- **原因**: harness 拦截以长 sleep(见过 45、60、240 秒)开头、后接状态检查的前台命令。短的 `sleep 20;`、真正工作之后的 sleep 能通过。
- **修法**: 等条件本身:`until grep -q '<标记>' $LOG; do sleep 15; done; tail -2 $LOG`;或等进程:`while pgrep -f "<加载模块>" >/dev/null; do sleep 15; done; <下一步>`。自己启动的命令用 `run_in_background: true`,等任务通知。也可以用 Monitor,或过一会儿再查,不 sleep。
- **预防**: 命令不以 `sleep N;` 开头;在 until/while 循环里轮询条件,或用后台任务通知。

### `File has not been read yet. Read it first before writing to it.`
- **在哪**: Claude Code Edit/Write 改 hedge-fund 和 optradar 文件(`docs/AGENT_PLAN.md`、AGENT.md、`agent/execute.py`、`agent/bin/postclose.sh` 等)。同类:`File has been modified since read, either by the user or by a linter. Read it again before attempting to write it.`
- **原因**: 文件只通过 Bash(sed -n、cat、grep、git apply)看过,不算 Read;或 Read 之后被 python heredoc 编辑、别的提交或别的进程改过。一次连续三个 Edit 被拒;另一次同批并行发出的 `git add && git commit` 照样执行,什么也没提交(`no changes added to commit`)。
- **修法**: 用 Read 工具读一下(给 offset/limit 范围就够),再重做 Edit;或用 python 替换脚本并 `assert s.count(old)==1`。提交放到单独的后一步。
- **预防**: 本 session 里 Edit/Write 之前先 Read,哪怕已经用 sed 看过;shell 侧改过之后再 Read 一次。依赖 Edit 结果的提交不和 Edit 放在同一批。

### `Permission for this action was denied by the Claude Code auto mode classifier. Reason: [Unauthorized Persistence]`
- **在哪**: Bash:用 heredoc 写 `bin/power_policy.sh`(电源设置);`launchctl bootout gui/$(id -u)/com.louis.agent.chain`;在生产 `~/.hedge-fund/agent/panel.db` 上 `DELETE FROM bars WHERE ticker='NRG' AND trade_date=today`。同类原因:`[Interfere With Workloads]`、`[Irreversible Local Destruction]`。
- **原因**: 分类器把 launchd、pmset、sudo 改动当持久化或干扰工作负载,把对在用库的 DELETE/UPDATE 当破坏性操作,这些留给用户做。被拒后,同一并行批里无害的调用也被拒,下一个相关调用(只读 grep)也被拒;依赖它的调用接着报 `fatal: pathspec 'bin/power_policy.sh' did not match any files`。另:plist 复制进 LaunchAgents 但没加载,13:25 收盘后任务悄悄没跑。
- **修法**: 仓库文件用 Write 工具写(成功了)。特权步骤放进用户自己跑的脚本(optradar `bin/install_power.sh`:电源设置),单行命令写进 AGENT.md §6。`install_launchd.sh` 只打印每个任务的状态和安装命令。没加载的任务手动跑(`./agent/bin/postclose.sh`)。库不强行处理:那根不完整的日线由 13:25 postclose 同步覆盖;删库数据交给用户。
- **预防**: agent 不跑 launchctl、pmset、sudo,不计划在生产 DuckDB 上手动 DELETE/UPDATE。可能被拒的调用不和依赖它的调用放在同一批。health 检查任务是否已加载。
- **已记录**: AGENT.md §6(agent 无权改 launchd)、§8;`docs/RUNBOOK.md` §8 第 9–10 步;`docs/AGENT_PLAN.md` §9 S47b 结果、§10 等待中。

### `The server-side auto mode classifier gave no verdict (error), so auto mode cannot determine the safety of …`
- **在哪**: Claude Code auto mode 下的 Workflow、Bash、ScheduleWakeup 调用(包括夜间 /loop)。变体:`API Error: Your computer went to sleep mid-response. … the response ended before its verdict arrived`。
- **原因**: 分类器服务暂时故障,或 Mac 在回复中途睡眠、裁决没到;不是对操作本身的判断。连续失败可能让 /loop 的唤醒整个丢掉,之后要用户输入 "Try again"。
- **修法**: 原样重试一次。当时紧接着一个简单调用(`date`)能过,重试的 DuckDB 读也成功。期间继续做只读工作。出现 "went to sleep mid-response" 后,确认下一个 ScheduleWakeup 结果写着 `Next wakeup scheduled`;唤醒丢了,循环只有用户提示后才恢复。
- **预防**: 不改写或绕过这个调用。连续无裁决就转去做别的事并告诉用户。

### `[review:exec] failed: You've hit your session limit · resets 12pm (America/Los_Angeles)`
- **在哪**: Workflow 工具:13 个审查子 agent 并行的全系统审查,结果是 `{"dimensions":[],"dropped_reviewers":[…13]}`。
- **原因**: 13 个子 agent 同时跑,在任何一个返回之前就撞上用量上限;工作文件在磁盘上,workflow 结果却为空。
- **修法**: 最多 5 个并发重启;每个先读上次的 journal 和工作目录,发现即落盘,每个维度一个核查者。上限重置后删掉过期的 mkdir 锁(`rmdir …/review_snapshot/heavy.lock`),用 `Workflow({scriptPath, resumeFromRunId})` 续跑。
- **预防**: 限制并发,保存部分结果,重跑前读 journal.jsonl。

### `json.decoder.JSONDecodeError: Extra data: line 1 column 3 (char 2)`(解析 Workflow 输出文件)
- **在哪**: 解析 Workflow 工具的输出文件(`<session>/tasks/<id>.output`)。同类:`Expecting value: line 1 column 1 (char 0)`,或打印出来的是外壳 `{ "summary": ..., "agentCount": 18, "logs": [...]` 而不是数据。
- **原因**: 文件是一个格式化过的 JSON 对象 `{summary, agentCount, logs, result}`;从第一个 `[` 切,或搜紧凑的 `[{"key"` / `{"impl"` 字符串,会落在错的位置。
- **修法**: `d = json.load(open(path)); data = d.get('result', d)`,或用 `json.JSONDecoder().raw_decode` 扫描。
- **预防**: 一律 json.load 整个 .output 文件,再取 `['result']`。

### `Web search was not performed: this session has used its web search budget (200 of 200 WebSearch calls).`
- **在哪**: 长时间运行的 session 里的 WebSearch,包括 Workflow 的研究子 agent(共用一个 session 的额度)。
- **原因**: 每个 session 的 WebSearch 上限(`CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION`,默认 200)用完了。
- **修法**: 改用公开的 Google News RSS:`curl -sL -A 'Mozilla/5.0' 'https://news.google.com/rss/search?q=%22<公司名>%22+when:1d&hl=en-US&gl=US&ceid=US:en'`,解析 `<item>` 的 title、pubDate、source;正文再用 WebFetch 读。Bing News RSS 也能用。也可以请用户调高这个上限。
- **预防**: 长 session 里在 workflow 各 agent 之间分配 WebSearch 额度;已知 URL 优先 WebFetch;日常新闻检查用按代码的 RSS(Google News、Yahoo、Nasdaq)。

### `The server returned HTTP 403 Forbidden.`
- **在哪**: WebFetch 抓 GlobeNewswire 等新闻稿站;PDF 和图片表格。Reddit:`curl https://www.reddit.com/r/<sub>/about.json` 返回 403 和 `<title>Blocked</title>`,WebFetch 报 `Claude Code is unable to fetch from developers.reddit.com`。
- **原因**: 部分新闻稿和财经站屏蔽抓取器;WebFetch 也解析不了 PDF 和图片表格。Reddit 拦截未认证的 JSON,官方 API 2025-11 起关闭自助申请;Unusual Whales API 收费(401)。
- **修法**: 抓转载镜像(如 manilatimes.net 上 GlobeNewswire 新闻稿的 tmt-newswire 副本)、Bing/Google News RSS 或公司 IR 页面。PDF 和 JPG 读 WebFetch 自动存的本地副本,用只依赖标准库的 python 提取器或 Read 工具。Reddit 停止尝试:异常期权流用自己的 Alpaca 期权日线算(S32),散户热度用 ApeWisdom + Stocktwits(`agent/sources/retail_heat.py`)。
- **预防**: 遇到 403 换镜像,不重试。Reddit 的 JSON 端点和 API 都不试,Discord 抓取不做。
- **已记录**: AGENT.md §0 铁律 8;`docs/AGENT_PLAN.md` §10 明确不做。

## 都不管用时

1. 缩到最小的复现命令(一个模块、一张表、一个代码、一个日期),把原文和退出码存下来:`cmd > log 2>&1; r=$?; tail log`。
2. 涉及下单、对账、券商故障、坏数据日,先按 `docs/RUNBOOK.md` 处理;账本对不上不用交易去修(AGENT.md §0 铁律 7)。
3. 查清后把原因和修法写进 `docs/AGENT_PLAN.md` §9。
4. 在本文件对应小节按同一格式加一条(高频的放前面),和修复同一个 commit。

## 未收录

核对过的 89 条记录:78 条整理成上面 60 条(18 条并入相近条目);未收录 11 条:已修且不会再现 6,一次性或价值低 3,设计说明而非报错 1(AGENT.md 已写),其他 1。
