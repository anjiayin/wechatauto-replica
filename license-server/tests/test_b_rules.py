# -*- coding: utf-8 -*-
"""B 组：业务规则（证伪重点）。

对齐依据：``docs/design.md`` §7.1 / §7.3 / §5.3、§1.3 的关键不变量（INV-1..INV-5）。
"""
from __future__ import annotations

import threading
from datetime import timedelta

import pymysql
import pytest

import _helpers as H


# ---------------------------------------------------------------------------
# B1 幂等：同机同码不叠加；同机新码才叠加
# ---------------------------------------------------------------------------
def test_b01_same_code_is_idempotent(admin):
    """同机 + 同码重复激活：到期日不叠加，返回同一张证。"""
    code = H.create_codes(admin, count=1, days=100)[0]
    first = H.activate_ok(H.M1, code)
    expires_1 = first["payload"]["expires_at"]

    resp = H.activate(H.M1, code)
    assert resp.status_code == 200, resp.text
    second = resp.json()["license"]
    assert second["payload"]["expires_at"] == expires_1, "重复激活把天数叠加了"
    assert second["payload"]["license_id"] == first["payload"]["license_id"]

    rows = H.q("SELECT license_id FROM license WHERE machine_code = %s", (H.M1,))
    assert len(rows) == 1, "重复激活多建了许可证：%s" % rows


def test_b01_new_code_stacks_from_max_expiry(admin):
    """同机换新码：从 max(旧到期, 今天) 起叠加，issued_at 不变。"""
    code_a = H.create_codes(admin, count=1, days=100)[0]
    first = H.activate_ok(H.M1, code_a)
    expires_1 = H.parse_iso(first["payload"]["expires_at"])

    code_b = H.create_codes(admin, count=1, days=50)[0]
    resp = H.activate(H.M1, code_b)
    assert resp.status_code == 200, resp.text
    second = resp.json()["license"]
    expires_2 = H.parse_iso(second["payload"]["expires_at"])

    assert expires_2 == expires_1 + timedelta(days=50), (
        "叠加基准不对：旧到期 %s，新到期 %s（期望 +50 天）" % (expires_1, expires_2))
    assert second["payload"]["issued_at"] == first["payload"]["issued_at"]
    assert second["payload"]["license_id"] == first["payload"]["license_id"]


# ---------------------------------------------------------------------------
# B2 吊销后重发
# ---------------------------------------------------------------------------
def test_b02_reissue_after_revoke(admin):
    """吊销后用未使用的新码重发：新证上线、旧证留库且仍吊销、reissued_from 指向旧证。"""
    code_a = H.create_codes(admin, count=1, days=100, phone="13900004444")[0]
    old = H.activate_ok(H.M1, code_a)
    old_id = old["payload"]["license_id"]
    assert admin.post("/admin/api/licenses/%s/revoke" % old_id,
                      {"reason": "换机"}).status_code == 200

    code_b = H.create_codes(admin, count=1, days=100, phone="13900004444")[0]
    resp = H.activate(H.M1, code_b)
    assert resp.status_code == 200, resp.text
    new_doc = resp.json()["license"]
    new_id = new_doc["payload"]["license_id"]

    assert new_id != old_id
    new_row = H.license_row(new_id)
    assert new_row["reissued_from"] == old_id, new_row["reissued_from"]
    assert new_row["current_key"] == H.M1
    assert new_row["revoked_at"] is None

    old_row = H.license_row(old_id)
    assert old_row is not None, "旧证被删了（INV-2：只新增不删除）"
    assert old_row["revoked_at"] is not None, "旧证吊销状态被清掉了"
    assert old_row["current_key"] is None

    # 新旧两张证一共 2 行，且只有 1 条 current_key
    rows = H.q("SELECT COUNT(*) AS c FROM license WHERE machine_code = %s", (H.M1,))
    assert rows[0]["c"] == 2
    cur_rows = H.q("SELECT COUNT(*) AS c FROM license WHERE current_key = %s", (H.M1,))
    assert cur_rows[0]["c"] == 1


# ---------------------------------------------------------------------------
# B3 一机一证（应用层 + 数据库层双重防线）
# ---------------------------------------------------------------------------
def test_b03_one_active_license_per_machine(admin):
    """应用层：同机 current_key 非空的记录 ≤ 1；数据库层：手工插第二条必须 1062。"""
    code_a = H.create_codes(admin, count=1, days=365)[0]
    H.activate_ok(H.M1, code_a)
    code_b = H.create_codes(admin, count=1, days=365)[0]
    H.activate_ok(H.M1, code_b)          # 续期，不新建

    row = H.q1("SELECT COUNT(*) AS c FROM license WHERE current_key IS NOT NULL "
               "AND machine_code = %s", (H.M1,))
    assert int(row["c"]) <= 1, "同机出现多条生效证（INV-1 被破坏）"

    with pytest.raises(pymysql.err.IntegrityError) as excinfo:
        H.insert_license(machine_code=H.M1, current_key=H.M1,
                         license_id="LIC-DUP-0001")
    assert excinfo.value.args[0] == 1062, (
        "数据库没有拦住第二条生效证：%s" % (excinfo.value,))

    # 表还在、数据没被破坏
    assert H.q1("SELECT COUNT(*) AS c FROM license WHERE license_id = 'LIC-DUP-0001'")["c"] == 0


# ---------------------------------------------------------------------------
# B4 缩期 / 设到期日
# ---------------------------------------------------------------------------
def _active_license(admin, days: int = 100) -> str:
    """造一张生效证，返回 license_id。"""
    code = H.create_codes(admin, count=1, days=days)[0]
    doc = H.activate_ok(H.M1, code)
    return str(doc["payload"]["license_id"])


def test_b04_shrink_days_negative_and_audit(admin):
    """days=-10 生效，审计 days_delta=-10，记录动作为 extend。"""
    lic_id = _active_license(admin, 100)
    before = H.parse_iso(H.license_row(lic_id)["expires_at"])

    resp = admin.post("/admin/api/licenses/%s/extend" % lic_id, {"days": -10})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["item"]["days_delta"] == -10
    after = H.parse_iso(H.license_row(lic_id)["expires_at"])
    assert after == before - timedelta(days=10)

    rows = H.audit_rows("extend", lic_id)
    assert rows, "缩期没有写审计"
    row = rows[0]
    assert int(row["days_delta"]) == -10, row["days_delta"]
    assert row["before_json"] and row["after_json"], "审计缺少变更前后快照"
    assert H.has_chinese(row["note"])


def test_b04_shrink_below_issued_at_rejected(admin):
    """缩到早于签发日 → 409 expiry_too_early（force 也不能越过）。"""
    lic_id = _active_license(admin, 365)
    for force in (False, True):
        resp = admin.post("/admin/api/licenses/%s/extend" % lic_id,
                          {"days": -400, "force": force})
        assert resp.status_code == 409, resp.text
        assert H.error_of(resp)["code"] == "expiry_too_early", resp.text
        assert H.has_chinese(H.error_of(resp)["message"])
    # 数据没被改动
    row = H.license_row(lic_id)
    assert H.parse_iso(row["expires_at"]) > H.parse_iso(row["issued_at"])


def test_b04_set_expiry_in_past_needs_force(admin):
    """改到过去：无 force → 409 expiry_in_past；有 force → 200 且落库。"""
    lic_id = _active_license(admin, 100)
    # 让证看起来是 30 天前签发的，这样"昨天到期"才不是 expiry_too_early
    now = H.signing.now()
    H.x("UPDATE license SET issued_at = %s WHERE license_id = %s",
        (now - timedelta(days=30), lic_id))
    target = (now - timedelta(days=1)).date().isoformat()

    resp = admin.post("/admin/api/licenses/%s/expiry" % lic_id,
                      {"expires_at": target, "force": False})
    assert resp.status_code == 409, resp.text
    assert H.error_of(resp)["code"] == "expiry_in_past", resp.text

    resp = admin.post("/admin/api/licenses/%s/expiry" % lic_id,
                      {"expires_at": target, "force": True})
    assert resp.status_code == 200, resp.text
    stored = H.license_row(lic_id)["expires_at"]
    assert stored.strftime("%Y-%m-%d") == target, stored

    # 审计
    rows = H.audit_rows("set_expiry", lic_id)
    assert rows and int(rows[0]["days_delta"]) != 0


def test_b04_invalid_days_rejected(admin):
    """days=0 / 超范围 → 400 invalid_days。"""
    lic_id = _active_license(admin, 100)
    for days in (0, 3651, -3651):
        resp = admin.post("/admin/api/licenses/%s/extend" % lic_id, {"days": days})
        assert resp.status_code == 400, "%s → %s %s" % (days, resp.status_code, resp.text)
        assert H.error_of(resp)["code"] == "invalid_days", resp.text


def test_b04_expiry_bad_format(admin):
    """到期日格式不可解析 → 400 invalid_expires_at。"""
    lic_id = _active_license(admin, 100)
    resp = admin.post("/admin/api/licenses/%s/expiry" % lic_id,
                      {"expires_at": "不是日期"})
    assert resp.status_code == 400, resp.text
    assert H.error_of(resp)["code"] == "invalid_expires_at", resp.text


# ---------------------------------------------------------------------------
# B5 恢复
# ---------------------------------------------------------------------------
def test_b05_restore_conflict_when_reissued(admin):
    """同机已有新证时恢复旧证 → 409 already_active。"""
    code_a = H.create_codes(admin, count=1, days=100)[0]
    old = H.activate_ok(H.M1, code_a)
    old_id = old["payload"]["license_id"]
    assert admin.post("/admin/api/licenses/%s/revoke" % old_id,
                      {"reason": "换证"}).status_code == 200

    code_b = H.create_codes(admin, count=1, days=100)[0]
    H.activate_ok(H.M1, code_b)

    resp = admin.post("/admin/api/licenses/%s/restore" % old_id)
    assert resp.status_code == 409, resp.text
    assert H.error_of(resp)["code"] == "already_active", resp.text
    assert H.license_row(old_id)["revoked_at"] is not None

    # 反向：恢复一张没吊销的证 → 409 not_revoked
    active = H.q1("SELECT license_id FROM license WHERE current_key = %s", (H.M1,))
    resp = admin.post("/admin/api/licenses/%s/restore" % active["license_id"])
    assert resp.status_code == 409
    assert H.error_of(resp)["code"] == "not_revoked"


def test_b05_restore_without_new_license_works(admin):
    """只吊销、没有新证时恢复应当成功，且心跳恢复正常。"""
    lic_id = _active_license(admin, 100)
    assert admin.post("/admin/api/licenses/%s/revoke" % lic_id,
                      {"reason": "误操作"}).status_code == 200
    assert H.heartbeat(H.M1, lic_id).json()["ok"] is False

    resp = admin.post("/admin/api/licenses/%s/restore" % lic_id)
    assert resp.status_code == 200, resp.text
    assert H.license_row(lic_id)["revoked_at"] is None
    assert H.heartbeat(H.M1, lic_id).json()["ok"] is True


# ---------------------------------------------------------------------------
# B6 并发：同一激活码被两台机器同时兑换
# ---------------------------------------------------------------------------
def test_b06_concurrent_activate_same_code(admin):
    """两线程同时用同一码激活两台机器：恰好一个成功，另一个 **409 code_used**。

    注意：失败的一方**不允许**是 500。实测（200 条历史证 + 30 轮）约 7% 概率
    抛出 InnoDB 1213 死锁 → 500 ``internal_error``（详见复验报告）。
    用例跑 6 轮以提高命中率；若出现 500，断言消息里会带出完整回包。
    """
    now = H.signing.now()
    for seed in range(100):          # 预置历史数据：贴合生产库，也更容易触发 gap lock 冲突
        machine = "%04X-0000-0000-0000-0000-0000-0000-0000" % seed
        H.insert_license(license_id="LIC-SEED-%04d" % seed,
                         machine_code=machine, current_key=machine,
                         expires_at=now + timedelta(days=100))

    for round_no in range(6):
        code = H.create_codes(admin, count=1, days=30)[0]
        machine_a = "1A00-0000-0000-0000-0000-0000-0000-%04d" % (round_no * 2 + 1)
        machine_b = "2B00-0000-0000-0000-0000-0000-0000-%04d" % (round_no * 2 + 2)
        results = {}
        barrier = threading.Barrier(2)

        def worker(name: str, machine: str) -> None:
            barrier.wait(timeout=15)
            resp = H.activate(machine, code)
            results[name] = (resp.status_code, resp.json())

        threads = [threading.Thread(target=worker, args=("a", machine_a)),
                   threading.Thread(target=worker, args=("b", machine_b))]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=40)

        assert len(results) == 2, results
        success = [k for k, v in results.items() if v[1].get("ok") is True]
        assert len(success) == 1, (
            "第 %d 轮：成功 %d 个（期望恰好 1 个）：%s"
            % (round_no + 1, len(success), results))
        loser_name = [k for k in results if k not in success][0]
        loser_status, loser_body = results[loser_name]
        assert loser_status == 409, (
            "第 %d 轮：失败方返回 %s（期望 409 code_used，500 说明事务死锁没被处理）：%s"
            % (round_no + 1, loser_status, loser_body))
        assert loser_body["error"]["code"] == "code_used", loser_body

        used = H.q("SELECT used_machine FROM license_code WHERE code = %s", (code,))
        assert len(used) == 1 and used[0]["used_machine"] in (machine_a, machine_b)
        licenses = H.q("SELECT COUNT(*) AS c FROM license WHERE activation_code = %s",
                       (code,))
        assert int(licenses[0]["c"]) == 1, "同一码兑换出了多张证"


def test_b01_new_code_base_is_today_when_expired(admin):
    """同机换新码且旧证已过期：基准取"今天"（不出现负叠加、不白送天数）。"""
    code_a = H.create_codes(admin, count=1, days=100)[0]
    doc = H.activate_ok(H.M1, code_a)
    lic_id = doc["payload"]["license_id"]

    now = H.signing.now()
    H.x("UPDATE license SET issued_at = %s, expires_at = %s WHERE license_id = %s",
        (now - timedelta(days=200), now - timedelta(days=100), lic_id))

    code_b = H.create_codes(admin, count=1, days=30)[0]
    resp = H.activate(H.M1, code_b)
    assert resp.status_code == 200, resp.text
    payload = resp.json()["license"]["payload"]
    new_expires = H.parse_iso_naive(payload["expires_at"])
    expect = now + timedelta(days=30)
    assert abs((new_expires - expect).total_seconds()) < 300, (
        "过期后续期基准不是今天：期望 %s 附近，实际 %s" % (expect, new_expires))
    assert payload["license_id"] == lic_id, "续期不应新建许可证"


# ---------------------------------------------------------------------------
# B18 无客户信息的激活码激活后补录客户绑定
# ---------------------------------------------------------------------------
def test_b18_bind_customer_after_activation(admin):
    """无手机号的码激活后补录：许可证快照 + 客户档案 + 审计三处齐全。"""
    code = H.create_codes(admin, count=1, days=100, customer="", phone="")[0]
    lic_id = H.activate_ok(H.M1, code)["payload"]["license_id"]
    # 该码没带客户信息：许可证行客户字段为空、客户档案 0 条
    row = H.q1("SELECT customer_id, customer_name, customer_phone FROM license "
               "WHERE license_id = %s", (lic_id,))
    assert row["customer_name"] == "" and row["customer_phone"] == ""
    assert H.q1("SELECT COUNT(*) c FROM customer")["c"] == 0

    resp = admin.post("/admin/api/licenses/%s/customer" % lic_id,
                      {"customer": "某某汽配", "phone": "13900007777"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["item"]["customer"] == "某某汽配"
    assert body["item"]["phone"] == "13900007777"
    assert body["item"]["customer_id"] > 0

    row = H.q1("SELECT customer_id, customer_name, customer_phone FROM license "
               "WHERE license_id = %s", (lic_id,))
    assert row["customer_name"] == "某某汽配"
    assert row["customer_phone"] == "13900007777"
    assert row["customer_id"] == body["item"]["customer_id"]
    # 客户档案按手机号建档一次
    assert H.q1("SELECT COUNT(*) c FROM customer WHERE phone = %s",
                ("13900007777",))["c"] == 1
    # 审计含 before/after
    audits = H.q("SELECT before_json, after_json FROM audit_log "
                 "WHERE action = 'bind_customer'")
    assert len(audits) == 1
    assert '"customer": ""' in audits[0]["before_json"]
    assert '"phone": "13900007777"' in audits[0]["after_json"]


def test_b18_bind_customer_validations(admin):
    """校验与多机多档案：全空 400；同手机号不重复建档；换手机号建新档案。"""
    code = H.create_codes(admin, count=1, days=100)[0]
    lic_id = H.activate_ok(H.M2, code)["payload"]["license_id"]

    # 两个都为空 → 400 invalid_param
    resp = admin.post("/admin/api/licenses/%s/customer" % lic_id,
                      {"customer": "", "phone": ""})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_param"

    # 不存在的许可证 → 404
    resp = admin.post("/admin/api/licenses/LIC-NOT-EXIST/customer",
                      {"customer": "甲", "phone": "13900008888"})
    assert resp.status_code == 404, resp.text

    # 第一次绑定
    resp = admin.post("/admin/api/licenses/%s/customer" % lic_id,
                      {"customer": "甲客户", "phone": "139 0000-8888"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["item"]["phone"] == "13900008888", "手机号应归一化"

    # 同手机号再绑定（名称留空）：不重复建档，名称沿用档案
    resp = admin.post("/admin/api/licenses/%s/customer" % lic_id,
                      {"customer": "", "phone": "13900008888"})
    assert resp.status_code == 200, resp.text
    row = H.q1("SELECT customer_id, customer_name FROM license "
               "WHERE license_id = %s", (lic_id,))
    assert row["customer_name"] == "甲客户"
    assert H.q1("SELECT COUNT(*) c FROM customer")["c"] == 1

    # 换手机号 → 新档案，customer_id 回写
    resp = admin.post("/admin/api/licenses/%s/customer" % lic_id,
                      {"customer": "乙客户", "phone": "13900009999"})
    assert resp.status_code == 200, resp.text
    row = H.q1("SELECT customer_id, customer_name, customer_phone FROM license "
               "WHERE license_id = %s", (lic_id,))
    assert row["customer_name"] == "乙客户"
    assert row["customer_phone"] == "13900009999"
    assert row["customer_id"] != 0
    assert H.q1("SELECT COUNT(*) c FROM customer")["c"] == 2


# ---------------------------------------------------------------------------
# B19 激活码作废 / 导出筛选 / 客户编辑
# ---------------------------------------------------------------------------
def test_b19_void_unused_code(admin):
    """作废未使用的码：不能再激活、重复作废 409、已使用的码不能作废。"""
    code = H.create_codes(admin, count=1, days=100)[0]

    resp = admin.post("/admin/api/codes/%s/void" % code, {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["item"]["voided_at"]

    row = H.q1("SELECT voided_at FROM license_code WHERE code = %s", (code,))
    assert row["voided_at"] is not None, "作废未写库"

    # 已作废的码激活 → 400 code_voided（客户端能看到明确的中文原因）
    resp = H.activate(H.M1, code)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "code_voided"

    # 重复作废 → 409 already_voided
    resp = admin.post("/admin/api/codes/%s/void" % code, {})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "already_voided"

    # 已使用的码 → 409 code_used
    code2 = H.create_codes(admin, count=1, days=100)[0]
    H.activate_ok(H.M1, code2)
    resp = admin.post("/admin/api/codes/%s/void" % code2, {})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "code_used"

    # 不存在的码 → 404
    resp = admin.post("/admin/api/codes/KF-XXXX-YYYY-ZZZZ-1111/void", {})
    assert resp.status_code == 404, resp.text

    # 审计
    assert H.q1("SELECT COUNT(*) c FROM audit_log "
                "WHERE action = 'void_code'")["c"] == 1


def test_b19_voided_codes_hidden_from_unused_filter_and_stats(admin):
    """作废后：未使用筛选不含它；stats.voided 计数正确。"""
    c_void = H.create_codes(admin, count=1, days=100)[0]
    c_used = H.create_codes(admin, count=1, days=100)[0]
    admin.post("/admin/api/codes/%s/void" % c_void, {})
    H.activate_ok(H.M1, c_used)

    resp = admin.get("/admin/api/codes", params={"status": "unused"})
    codes = [r["code"] for r in resp.json()["items"]]
    assert c_void not in codes and c_used not in codes

    resp = admin.get("/admin/api/codes", params={"status": "voided"})
    codes = [r["code"] for r in resp.json()["items"]]
    assert codes == [c_void]

    stats = admin.get("/admin/api/dashboard").json()["codes"]
    assert stats.get("voided") == 1, stats
    assert stats["unused"] == 0, "作废的码不应计入未使用"


def test_b19_export_csv_follows_filters(admin):
    """导出跟随筛选条件，且包含天数/客户/手机号/状态/机器码/使用时间/生成时间。"""
    c_hit = H.create_codes(admin, count=1, days=100, customer="甲客户",
                           phone="13900001111")[0]
    c_other = H.create_codes(admin, count=1, days=30, customer="乙客户",
                             phone="13900002222")[0]
    lic = H.activate_ok(H.M1, c_hit)["payload"]["license_id"]
    H.activate_ok(H.M2, c_other)
    admin.post("/admin/api/codes/%s/void" % (
        H.create_codes(admin, count=1, days=10, customer="甲客户",
                       phone="13900001111")[0]), {})

    # 只导出手机号 13900001111 的（含已使用 + 已作废，不含乙客户）
    resp = admin.get("/admin/api/codes/export",
                     params={"q": "", "status": "all", "phone": "13900001111",
                             "batch": ""})
    assert resp.status_code == 200, resp.text
    text = resp.text.lstrip("\ufeff")
    lines = [l for l in text.strip().split("\r\n") if l]
    assert len(lines) == 3, "应含表头 + 2 行：%r" % lines
    header = lines[0]
    for col in ("激活码", "天数", "客户", "手机号", "状态", "绑定机器码",
                "使用时间", "生成时间"):
        assert col in header, "缺列：%s" % col
    assert c_hit in text and c_other not in text
    hit_line = [l for l in lines if c_hit in l][0]
    assert "已使用" in hit_line and H.M1 in hit_line and "100" in hit_line
    assert "甲客户" in hit_line and "13900001111" in hit_line

    # 状态筛选也生效：只导已使用
    resp = admin.get("/admin/api/codes/export",
                     params={"q": "", "status": "used",
                             "phone": "13900001111", "batch": ""})
    text = resp.text.lstrip("\ufeff")
    lines = [l for l in text.strip().split("\r\n") if l]
    assert len(lines) == 2 and c_hit in text, lines

    # 空筛选导出全部（3 行数据）
    resp = admin.get("/admin/api/codes/export",
                     params={"q": "", "status": "all", "phone": "",
                             "batch": ""})
    lines = [l for l in resp.text.strip().split("\r\n") if l]
    assert len(lines) == 4, "空筛选应导出全部 3 条 + 表头"


def test_b19_customer_edit(admin):
    """客户编辑：同名手机号 upsert 更新名称与备注，审计记 update_customer。"""
    body = {"name": "甲客户", "phone": "13900001111", "note": ""}
    resp = admin.post("/admin/api/customers", body)
    assert resp.status_code == 200, resp.text

    resp = admin.post("/admin/api/customers",
                      {"name": "甲客户改", "phone": "13900001111",
                       "note": "新备注"})
    assert resp.status_code == 200, resp.text
    row = H.q1("SELECT name, note FROM customer WHERE phone = %s",
               ("13900001111",))
    assert row["name"] == "甲客户改" and row["note"] == "新备注"
    assert H.q1("SELECT COUNT(*) c FROM customer")["c"] == 1, "不应重复建档"
    assert H.q1("SELECT COUNT(*) c FROM audit_log "
                "WHERE action = 'update_customer'")["c"] == 1
