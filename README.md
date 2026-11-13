# 分阶段筛查推广的触达与转介效应评估

项目使用 Python 3.11 标准库与 SQLite，现有逐案处理入口为 `colorectal_cascade.commands.cascade`。保留原有处理器、持久化与测试；分析业务使用本地合成资料，不连接生产系统。无需 pip 安装、下载模型或启动额外服务。

## 现有程序

在仓库根目录运行 `bash scripts/demo.sh`，依次完成现有产品回归、分析输入核对、语法构建和真实业务入口调用。所有运行文件位于临时目录，结束后清理；运行失败立即退出，不使用预存输出代替执行。

单独测试：`python3 -m unittest discover -s tests/cascade_states -p 'test_*.py' -v`。

输入检查：`python3 -m unittest discover -s tests -p test_analysis_inputs.py -v`。

构建检查：`python3 -m compileall -q colorectal_cascade tests`。

业务调用：`python3 -m colorectal_cascade.commands.cascade fixtures/demo.json --db /tmp/colorectal_cascade-demo.sqlite3`。

## 分析资料与联调

`fixtures/analysis/input.json` 保存业务演练数据，字段解释在 `fixtures/analysis/fields.md`。以下已核对的数据形态用于分析回归：cluster_assignment、immature_followup、shared_capacity。现有输入测试检查这些形态是否真实存在，不代替分析方法或数值正确性的验证。

分析入口接通后执行 `bash scripts/feature_demo.sh`：先重跑现有产品，再从干净数据库计算分析、同输入重放、核对输出结构并打印完整 JSON。尚未接通时该命令会报模块不存在，不会用静态样例冒充成功。分析 ID 为 `trial_analysis_id`，其余输出包括 `assignment_checks, stage_effects, followup_support, interference, limitations`。方法正确性还需覆盖业务说明中的反例，结构核验不能代替统计验证。

## 分析方法（给运营同事）

主要结论按原随机分配比较（意向性）：未收到或未响应提醒的人仍计入原分配组，参与者亚群中的比例只作描述，不冒充随机化效果。随机单位是社区：推断在区组内保持分配数不变的重随机化检验上进行，可枚举时精确枚举全部分配，否则用固定种子蒙特卡洛；区间由检验反演得到，不假设正态分布，社区少、零事件、规模不等都可处理。多终点按研究声明的主要终点顺序做固定顺序检验，并附 Holm 校正 p 值作敏感性对照。观察窗口未关闭者与等待结果者记为待定，不计入分母也不计为失败；截止时刻后才可用的事实暂不采用。跨社区共用服务点容量作为适用性限制报告，不事后删除社区。分析只评价服务流程，不判断个体病情，也不生成医疗建议。

## 离线环境

使用 Python 3.11.2 的 Linux 环境即可复现。容器环境预置解释器后，以只读方式挂载仓库，将文件复制到容器临时目录再运行脚本；运行时关闭网络。基础测试和小规模本地数据适合在 90 秒内展示，正式展示命令为 `bash scripts/feature_demo.sh`。不要在展示脚本里安装依赖或等待外部服务。
