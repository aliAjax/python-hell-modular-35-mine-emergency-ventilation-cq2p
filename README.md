# 矿井应急避险与通风协调

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8335`。领域对象包括矿井人员、气体传感、通风设备、逃生通道、避险硐室、事件和处置任务。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8335
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8335/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建矿井事件、人员和设备记录后，依次执行撤离、搜救、通风恢复和事件关闭。`POST /api/offline-records` 用于合并现场离线记录，`source_id + record_id` 相同会幂等返回原记录。

## 规则重点

- 活跃任务按 `dedupe_key` 防止重复派工。
- 气体读数按阈值计算`severity`。
- 事件关闭前必须没有失联或已定位人员、没有活跃任务，所有通风设备恢复运行，并且**每个曾停运的区域都留有审批通过的送风许可**。

## 井下送风许可

井下恢复送风不再只看检测时间。停运风机重启前必须先由**安全员（safety/admin）**开具送风许可：

```
POST /api/ventilation-permits
{
  "ventilation_id": "停运风机ID",
  "tested_at": "2026-09-28T11:55:00Z",   // 气体检测时间
  "oxygen_pct": 20.9,                     // 氧气读数 %
  "methane_pct": 0.1                      // 甲烷读数 %
}
```

系统逐项核查并返回 `approved`/`denied` 及每项的原因（`data.checks`）：

| 核查项 | 不放行条件 |
| --- | --- |
| 检测时序 | 检测时间早于设备停机时间（停机时间由系统在`stop`时加盖，不可伪造） |
| 检测时效 | 检测距当前超过 30 分钟，必须重新检测 |
| 氧气 | 低于 19.5% |
| 甲烷 | 达到 1%（含） |
| 传感器 | 同区域传感器处于 `alarm` 或 `faulty`；仅 `warning` 时给预警但不阻断 |
| 人员撤离 | 同区域仍有 `active`/`missing`/`located` 状态人员 |

许可即使被拒绝也会留档（状态为`denied`），可在 `GET /api/ventilation-permits` 或 `GET /api/entities/<id>` 查看逐项结果。启动停运风机时必须引用通过的许可，系统会再次复查时效（超过30分钟需重做）与现场传感器、人员状态：

```
POST /api/entities/<vent_id>/actions
{"action": "restore", "data": {"permit_id": "<许可ID>"}}
```

降级（`degraded`）设备恢复不涉及停机送风，无需许可。根页面 `/` 提供选择停运设备、录入读数、查看逐项原因和凭许可启风机的操作界面。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
