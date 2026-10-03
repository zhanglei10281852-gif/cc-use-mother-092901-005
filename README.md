# 车载智能数据授权治理后端

共享车辆引入车载智能助手后，语音偏好、行程摘要与训练样本分属不同用途、不同驾驶人；
同意可撤回，事故调查又要求部分数据暂时保留。本后端以 **事件发生时点的授权版本**
为唯一判定依据，管理车辆、账号、乘员会话、数据用途与同意版本，并对采集、使用、
共享、导出、删除五类请求统一判定、统一留痕。

## 治理规则

1. **多人共车边界**：数据记录归属到账号；一次访问必须由归属人本人在该时点有效的
   乘员会话支撑。同一辆车的另一驾驶人不能依据自己的同意使用前一人的数据；
   采集时账号必须是该车辆当时的活动乘员。
2. **用途隔离**：每个用途（`Purpose`）声明其授权的操作集合（collect/use/share/
   export）。语音偏好、行程摘要、训练样本分别授权，互不串用。
3. **同意换版与时点判定**：授予/拒绝/撤回都产生不可变的新版本（版本号递增，
   `effective_at` 生效）。任何判定都解析 *事件时点* 最新有效版本。
   **撤回只阻止撤回时点之后的用途，不溯及既往**；历史判定保存在审计日志中，
   可随时按审计编号还原“为何获准/拒绝”。
4. **派生谱系**：派生物（如训练样本）记录 `derived_from`。删除请求沿谱系闭包
   追踪原始记录与全部可定位派生物；派生物不允许混入其他账号的来源。
5. **有期限合法保留**：事故调查可对记录（自动传播到派生物）设置带到期时间的保留。
   保留期内普通用途访问与删除均被阻止（删除条目进入 `held`，作业进入
   `awaiting_hold`），仅标记为法定调查的用途可访问；到期后 `tick()` 自动释放保留、
   续处理挂起的删除作业。
6. **作业语义**：导出/删除请求以 `request_id` 幂等去重；执行故障进入 `failed`，
   可安全重试且删除从上次进度继续；全部状态落 SQLite，进程重启时中断在
   `running` 的作业恢复为 `failed`，随后重试或 `tick()` 续处理。

## 模块结构

```
src/consent_governance/
  contracts.py   # 领域契约：Vehicle/Account/OccupantSession/Purpose/ConsentVersion/
                 # DataRecord/LegalHold/AccessDecision/AuditEntry/ExportJob/DeleteJob
  storage.py     # SQLite 仓储（含审计日志、作业与删除条目，时间统一存 UTC ISO-8601）
  engine.py      # GovernanceBackend：时点判定、解释、采集/派生、保留、导出/删除、恢复
tests/
  test_contracts.py   # 契约层冒烟
  test_governance.py  # 20 个用例：多人共车、同意换版、保留冲突、谱系删除、
                      #              重复请求、故障重试、重启恢复
run_cli.py       # 命令行端到端冒烟
```

## 快速开始

```python
from datetime import datetime, timezone
from consent_governance import GovernanceBackend, Operation, Purpose

gov = GovernanceBackend(":memory:")
gov.register_purpose(Purpose("voice-personalization", "语音偏好",
    frozenset({Operation.COLLECT, Operation.USE, Operation.SHARE, Operation.EXPORT})))
gov.register_vehicle("VIN-7")
gov.register_account("user-8")

gov.grant("user-8", "voice-personalization", datetime(2026,10,3,tzinfo=timezone.utc))
gov.start_session("S-8", "VIN-7", "user-8", datetime(2026,10,3,tzinfo=timezone.utc))
gov.collect("R-1", "user-8", "VIN-7", "S-8", "voice-personalization", "voice-profile")

d = gov.evaluate_access("user-8", Operation.SHARE, "voice-personalization", record_id="R-1")
assert d.allowed
print(d.reasons)            # 命中同意版本 v1 ...：GRANTED
print(gov.explain(d.audit_id).reasons)  # 事后仍可还原这次判定
```

删除与合法保留：

```python
job = gov.request_delete("user-8", "req-1")
gov.place_hold("R-1", until, "事故调查 #A21")
gov.run_delete(job.job_id)          # awaiting_hold：R-1 及其派生物挂起
gov.tick(after_hold_expiry)         # 保留到期自动续处理 → completed
```

## 运行

```bash
python3 -m unittest discover -s tests -v   # 自动化验证（20 用例）
python3 -m compileall -q src tests run_cli.py
python3 run_cli.py                          # 端到端冒烟
```

持久化只需把文件路径传给 `GovernanceBackend("/path/to/gov.db")`；
重新打开即完成重启恢复。
