# ClawIndex VNext 1.0 运行与回滚手册

2026-10-13已升级到VNext 2.1；云端Compose、自动补数据、默认空仓、因子研究与一致性备份见[当前版本手册](VNext-云端自动维护与因子进化-2026-10-13.md)。schema 1→2步骤见[自动进化使用手册](VNext-自动进化使用手册.md)，2.1不新增schema。以下保留既有本机运行和旧版迁移说明。

本版本是独立实现：`app_next.py`、`clawindex_next/`、显式指定的新版数据库。旧 `app.py` 与旧库接口未修改。Python **3.12** 是本次验证版本。系统供本人和朋友使用，登录只需要账号名，新账号自动创建。2026-10-13易用性更新后，工作日自动行情更新默认开启；自动巡检和已有渠道开关保留原设置，新渠道默认关闭。日常操作见[快速上手](VNext-快速上手.md)。

Linux Compose、独立服务入口及CI已经实现，部署见当前版本手册；当前开发机无Docker/Linux，实际镜像及云端验收仍待CI确认。本手册以下描述本机运行方式。

## 本工作区已完成

- 已从 `quant_system20261012.db` 只读迁移到 `quant_system_next.db`。
- 完整归档10张业务表，包含275条旧巡检、11份旧提示词、10条旧持仓；逐行摘要校验通过。
- 旧持仓导入为待确认 UNKNOWN，旧比例保留为目标投入完成度。
- 已完成11条监控的离线巡检和10个指数、每个18种参数/成本/延迟情景的研究。
- 未调用真实行情或付费模型接口，未向任何Webhook发送消息，未切换生产入口。

研究报告并不证明策略值得投资。基线在多个行业落后于持有；国际指数旧历史有缺口/无效OHLC，比较被标为 INVALID_DATA_GAPS。报告带60/20/20时间切分；这些历史测试集已经被查看，后续据此调参不能继续宣称未使用的样本外结果。

## 安装及本机启动

**日常使用：先关闭原来启动页面的终端，再双击工作区里的 `一键启动新版.bat`。** 它会同时启动页面与后台巡检，浏览器打开 `http://127.0.0.1:8502`。输入原账号名即可使用；保留启动窗口运行，按Ctrl+C一起停止页面和后台。无需再开第二个终端。

如果健康页显示排队中，表示巡检尚未被后台取走，不是买卖判断。首页和巡检弹窗会自动刷新；后台未启动时页面会提醒。一时不方便重启，也可点击“立即处理我的待执行巡检”，仅处理本账号已提交的任务，新巡检会先同步行情；该按钮不投递通知。

在“信号与待执行”点击“提交全部巡检”，无需先单独同步；在“监控与数据”也可提交单指数巡检或仅更新行情。后台更新到各市场最近应已发布时段，再校验并分析。同步失败会重试并显示原因，旧卡片明确标为上次结果。Tushare密钥配置在 `.env.next`（从 `.env.next.example` 复制并填写）；当日未收盘或尚未到发布时间时，最新预期数据可以是前一交易日。

推荐新建独立虚拟环境，避免沿用旧系统的依赖：

```powershell
Set-Location D:\Develop\ClawIndex
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-next-tested.txt
```

`requirements-next-tested.txt` 固定本次直接依赖版本，`requirements-next.txt` 给出允许范围。新版不需要Akshare或APScheduler。现有工作区的 `.venv` 已安装和验证新版依赖。复制 `.env.next.example` 为 `.env.next` 后填写Tushare凭据；新版不读取旧 `.env`。AI解释密钥可留空。不要将密钥或个人数据库提交Git。

用命令启动页面与后台（等同于双击批处理）：

```powershell
.\.venv\Scripts\python.exe start_next.py
```

需要分开管理服务时，也可分别启动页面与worker：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app_next.py --server.address 127.0.0.1 --server.port 8502 --browser.gatherUsageStats false
.\.venv\Scripts\python.exe -m clawindex_next --db quant_system_next.db worker
```

一键启动会自动带上worker；若采用上述分开管理方式，两条命令需在各自终端中运行。界面提交的是持久任务，关闭弹窗或网页不会取消。后台默认工作日更新全部用户的关注指数，按共享指数去重；早间和收盘发布后对应的预期时段不同，可分别更新，成功时段不反复拉取，失败至少间隔30分钟再尝试。香港时间周六新发布的美股周五数据也会更新。管理员可暂停独立自动行情更新；若自动巡检开启，它仍需先更新行情。重启后过期任务租约会被回收；巡检任务最多尝试3次，可用 `worker --once` 单批运行。

账号名用于区分各人的监控、持仓、预算与历史，会话8小时过期。名字前后的空白会去除，其余字符和大小写保留；请一直使用同一个账号名。系统不验证账号持有人身份，知道账号名即可进入该账号，符合本人与朋友共同使用的要求。已有库的 `password_hash` 字段仅保留兼容，不读取或校验；无需改库或重新迁移，bcrypt依赖已移除。

新账号默认普通权限，登录不会提升管理员权限。需要管理全局自动同步开关时，可在本机配置管理员账号（不会要求密码）：

```powershell
.\.venv\Scripts\python.exe -m clawindex_next --db quant_system_next.db create-user my-account --admin
```

该命令保留账号原有数据并撤销该账号旧会话，重新输入账号名进入即可。原有管理员标记沿用，不会因登录改变。

## 首次使用顺序

1. 登录后检查监控的身份。黄金850531.SI是申万股票行业指数，不能使用现货黄金资料替代；旧category不再决定标的类型。
2. 已有监控无需重新添加；巡检会自动更新近期行情。国际指数若旧历史有缺口，仍需在监控与数据页从较早日期补全历史，不能只更新近期后就使用有缺口的历史比较。
3. 在持仓与预算页明确确认 UNKNOWN/EMPTY/HELD。状态可单独维护，金额、份额与基金映射均可留空。超过7个自然日未确认，个人操作降级为CONTEXT_REQUIRED；阅读信号不会刷新时间。
4. 提交巡检，弹窗显示更新、分析、可选AI解读的进度，完成后自动显示结果。缺少账户信息仍可显示有效指数信号，金额保持未知。勾选“巡检时同时生成AI解读”会调用已配置的模型，可能消耗额度；也可在卡片下点击“生成AI解释”，解读保存后无需每次重新生成。
5. 在策略与研究页研究该规则。邻近均线90/120/150、每边0/10/30基点、延迟1/2时段，默认共18情景；使用共同评估区间，各参数状态由完整历史前缀推进。市场特征UP/DOWN/RANGE只是描述，不自动切换策略。
6. 确认规则和研究限制后，按需求选择RESEARCH/SHADOW/ENABLED。存在数据缺口或共同评估区间少于252时段时，禁止进入ENABLED；一年并不代表统计有效或有优势。RESEARCH/SHADOW仍能看明确的观察信号。
7. 需要推送时，开启本人的飞书渠道。只有ENABLED、数据通过、持仓有效且个人动作BUY/SELL时入队。关闭渠道后队列停止发送。导入不自动开启或发送测试消息。
8. 对提醒反馈执行、暂缓或取消。反馈不改变持仓；成交后另行确认实际状态。金额依赖同币种预算/现金和人工核实的基金映射，指数点位不会被当作基金成交价。

## 数据与规则口径

- 规则是 `trend_confirm_v1`：默认MA120，连续3日确认；向上入场同时要求均线5时段斜率为正，退出优先。没有估值条件阻止退出，没有每天自动加仓。相等会打断计数。
- 缺少应有交易日不会前向填充；策略需要均线与斜率窗口全部有效。MA60够而MA120不够时，前者仍可展示，后者不能生成有效信号。
- 股票指数按所在市场交易时段计算，包含节假日、美国夏令时与半日市。CN使用XSHG，HK使用XHKG，IXIC采用XNYS共同交易日作为现版日历代理；极少数交易所特殊差异需核实后升级日历。
- Tushare `sw_daily` 成交额万元转人民币元；`index_daily` 千元转元；国际成交额缺失保持未知。申万采用18:30发布时间（收盘后210分钟），依据[官方sw_daily文档](https://tushare.pro/document/2?doc_id=327)。国际接口[官方index_global文档](https://tushare.pro/document/2?doc_id=211)未承诺发布时刻，本版收盘后150分钟属于明确估计，返回延迟会阻断信号，不能当作休市。
- 旧成交额单位、估值来源和历史利率来源未完整记录，因此归档原值，新指标中保留未知，不复用1.72%利率。新接口提供的PE/PB在同日行情中计算历史正值样本分位；PB/PE只是隐含ROE代理。
- 行情修订、数据集、特征、规则和信号均追加版本，快照保留输入正文和摘要。历史日期或较早修订不能覆盖较新的当前投影。取消监控不删除历史。
- AI结构化结果在界面显示为中文摘要、反面观点、局限与情景；失败、缺字段、未知引用或交易命令触发明确标注的程序解读，允许重试。程序直接展示动作和数字。语义检查无法证明AI所有文字真实，不使用AI置信度作为概率或启用依据。
- 默认回测在盘后生成信号，于之后可用的开盘执行；缺失开盘等待，最后待成交订单不计成交。初始共同研究区间如模型已LONG，用下一开盘对齐模型敞口并明确保留完整回放起点；持续持有基准也用同样延迟和费用。
- 回测是价格指数研究，现金收益设0，不含实际产品净值、分红、汇率或申赎规则。缺口下的曲线仅作诊断，不能用于比较优势。

## 在其他环境迁移

迁移前停止旧系统写入，使用真实路径。先dry-run，再写**不存在的新目标路径**：

```powershell
.\.venv\Scripts\python.exe -m clawindex_next --db quant_system_next.db migrate --source quant_system20261012.db --dry-run
.\.venv\Scripts\python.exe -m clawindex_next --db quant_system_next.db migrate --source quant_system20261012.db
.\.venv\Scripts\python.exe -m clawindex_next --db quant_system_next.db doctor
```

本工作区目标已存在，不要再次运行实际迁移来覆盖它。迁移使用只读源连接与SQLite Online Backup建立一致性内存副本，适用于WAL；逐表归档、逐行摘要与外键校验后再发布目标文件。失败保留 `.migrating` 暂存库供检查，不继续启动。源主文件若被并发写入，迁移会记录前后摘要变化，不谎报源文件未变化。

## 测试与复核

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q clawindex_next app_next.py
.\.venv\Scripts\python.exe scripts/validate_next_release.py --db quant_system_next.db --source quant_system20261012.db --output reports/2026-10-12-vnext-release-evidence.json
```

自动测试不调用真实Tushare、LLM或Webhook。覆盖真实生产快照迁移、确定性规则、未来数据隔离、下一开盘执行、跨用户权限、并发任务与通知幂等、业务码失败、队列失效、模型故障、Streamlit登录与全部页面。没有本地生产快照时，私有输入迁移测试会明确跳过。实时接口成功与长期投资有效性需要单独验证。

外部Webhook没有事务提交或幂等确认接口，因此这里只能保证本地事件唯一和有界重试；若远端已经收到消息但本地在落成功状态前崩溃，重试仍可能再次发送。不能宣称外部端到端exactly-once。

## 回滚

1. 在新版关闭自动同步与渠道，停止新版worker及Streamlit进程。
2. 从界面下载新增持仓/反馈，或在本机导出：

```powershell
.\.venv\Scripts\python.exe -m clawindex_next --db quant_system_next.db export-feedback --user my-account --output vnext-feedback-private.json
```

3. 保留新版数据库与证据，启动旧 `app.py` 使用其原来数据库。旧系统不包含VNext期间的新报告，不自动将新字段灌回旧库。
4. 做数据库备份时也使用SQLite Online Backup；运行中的WAL库不能只复制主文件。

本版没有自动下单、账户联网接入、ETF 8000积分接口依赖、真实基金绩效计算或自动环境策略路由。它实现的是设计中的首版可追溯择时、人工上下文和研究工作流；新策略仍须编写受控规则、登记依赖、回放验证后发布，不能靠自由提示词直接运行。
