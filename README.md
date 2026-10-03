# 车载智能数据授权治理

共享车辆引入车载智能助手后，语音偏好、行程摘要、训练样本等数据的授权边界必须
按 **车辆 / 账号 / 乘员会话** 分开管理。本服务用纯 Python（标准库，无第三方依赖）
实现一个数据授权治理后端：

- 所有访问判断以 **事件发生时点** 的同意版本为准；
- 撤回只追加一个新版本，只阻断后续处理，历史访问一律留痕；
- 删除追踪原始记录及可定位派生物，有期限合法保留到期后自动恢复处理；
- 导出/删除是带状态、可重试、崩溃可恢复的异步作业；
- 每次获准或拒绝都返回可读的理由解释。

## 领域模型

| 对象 | 说明 |
|---|---|
| `Vehicle` | 车辆（如 VIN） |
| `Account` | 用户账号 |
| `OccupantSession` | 乘员会话，把车辆、账号与一段时间绑定；同一辆车可有多个驾驶人的会话 |
| `DataPurpose` | 数据用途（voice-personalization / trip-summary / training 等），彼此独立 |
| `ConsentVersion` | 账号对某用途的授权版本，只增不改：granted → withdrawn → granted … |
| `DataRecord` | 被治理数据，带 `session_id`、`derived_from` 血缘、`locatable` 标记 |
| `AccessDecision` | 一次访问判断的完整解释（理由码 + 人话解释 + 依据的同意版本） |
| `Job` / `JobItem` | 导出/删除作业及逐项状态 |
| `legal hold` | 有期限保留令，生效区间 `[start_at, end_at)` |

## 核心规则

1. **时点判断**：取 `recorded_at <= 事件时间` 的最新同意版本。以历史时间重放访问
   判断，仍按当时版本获准/拒绝；撤回不追溯改写历史。
2. **会话隔离**：访问必须匹配账号与会话；不能拿 A 的会话访问 B 的记录，
   也不能访问采集自其他会话的记录；事件早于会话开始同样被拒。
3. **留痕**：采集/使用/共享/导出的每一次获准与拒绝都写 `access_log`，
   含理由码、依据的 consent_id/version、接收方等，可按账号/记录/操作查询。
4. **合法保留**：保留期内使用/导出/删除全部暂停（删除项记为 `held`，导出项记为
   `excluded/legal_hold`）；保留可按用途限定；`end_at` 到期后 `run_due()` 自动把
   等待中的作业重新置为 pending 并执行。
5. **删除血缘**：请求删除时从原始记录向下展开整条派生链——可定位派生物一并删除
   （逻辑删除，留墓碑可审计），已匿名聚合、无法定位的派生物标记 `non_locatable`
   并在结果中如实报告。
6. **作业重试与幂等**：单项瞬时故障 → 作业 `failed`，重试只影响未成功项；
   同一 `request_id` 重复请求返回原作业并标记 `duplicated`；
   崩溃时停在 `running` 的作业在服务重启时自动复位为 `pending`。

## 持久化与重启恢复

数据目录包含 `snapshot.json` 与 `events.jsonl`（WAL）：

- 每次变更先 `append` + `fsync` 写 WAL，才算生效；
- `checkpoint()` 原子写快照并截断 WAL；
- 启动时先读快照再重放日志；构造服务时 `recover_stale_jobs()` 把
  崩溃残留的 `running` 作业复位为 `pending`。

## 运行

```bash
python -m unittest discover -s tests -v   # 20 个测试
python -m compileall -q src tests run_cli.py
python run_cli.py                          # 命令行场景演示
```

## Python API 示例

```python
from consent_governance import GovernanceService, Operation

svc = GovernanceService("./data")
svc.register_vehicle("VIN-1")
svc.register_account("alice")
svc.register_purpose("trip-summary", "行程摘要")
svc.start_session("S1", "VIN-1", "alice")
svc.grant_consent("alice", "trip-summary")

d = svc.collect("alice", "trip-summary", "summary",
                session_id="S1", record_id="R1")
print(d.allowed, d.reason_code, d.explanation)

svc.withdraw_consent("alice", "trip-summary")
d = svc.evaluate_access(Operation.USE, "alice", "trip-summary", record_id="R1")
# d.allowed == False, d.reason_code == consent_withdrawn（撤回只阻止后续用途）

job = svc.request_delete("alice", "REQ-001")
svc.process_job(job.job_id)
```

## REST 接口

`python -c "from consent_governance.api import serve; serve('./data').serve_forever()"`
后可用：

| 方法/路径 | 作用 |
|---|---|
| `POST /vehicles` `/accounts` `/purposes` `/sessions` | 实体注册 |
| `POST /accounts/{id}/consents/{purpose}` | 追加同意版本（granted/denied/withdrawn） |
| `POST /holds` | 登记有期限合法保留 |
| `POST /access/evaluate` | 访问判断并留痕（拒绝返回 403 + 理由） |
| `POST /collect` | 采集：先判断，获准才落记录 |
| `POST /exports` `/deletions` | 幂等作业请求（需 `request_id`） |
| `POST /jobs/{id}/run` | 执行/重试作业 |
| `POST /run-due` | 到期恢复 + 执行所有待处理/失败作业 |
| `GET /jobs/{id}` | 作业状态与逐项结果 |
| `GET /requests/{request_id}` | 按幂等键查作业 |
| `GET /records/{id}/lineage` | 原始记录、派生链、不可定位派生物 |
| `GET /access-logs` | 历史访问留痕（支持账号/记录/操作过滤） |

## 自动化验证覆盖

- **多人共车**：同车两场会话两个驾驶人，跨账号/跨会话访问被拒；
- **同意换版**：授予→撤回→再授予，时点重放依据对应版本；
- **保留冲突**：保留期阻断使用与删除，到期自动恢复；按用途限定的保留不波及其他用途；
- **派生物删除**：原始记录 + 可定位派生物级联删除，不可定位聚合如实报告；
- **重复请求**：同 request_id 返回同一作业；
- **失败重试**：注入瞬时故障后重试成功；
- **重启恢复**：作业执行中模拟崩溃，新进程 WAL 重放后复位并完成。
