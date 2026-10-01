"""访客撤回认领班的端到端 + 并发测试（标准库）。前置：服务已运行在 http://127.0.0.1:8181。"""
import http.cookiejar
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://127.0.0.1:8181"
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def client():
    cj = http.cookiejar.CookieJar()
    return cj, urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def _jar_of(op):
    for h in op.handlers:
        if isinstance(h, urllib.request.HTTPCookieProcessor):
            return h.cookiejar
    return http.cookiejar.CookieJar()


def req(op, method, path, form=None, raw=False):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    try:
        if raw:
            opener = urllib.request.build_opener(
                NoRedirect, urllib.request.HTTPCookieProcessor(_jar_of(op)))
            resp = opener.open(r)
            return resp.status, resp.read().decode(), resp.headers.get("Location", "")
        resp = op.open(r)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return (e.code, body, e.headers.get("Location", "")) if raw else (e.code, body)


def raw_cookie(cookie_header, path, form):
    data = urllib.parse.urlencode(form).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST")
    r.add_header("Cookie", cookie_header)
    try:
        resp = urllib.request.build_opener(NoRedirect).open(r)
        return resp.status, resp.headers.get("Location", ""), resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", ""), e.headers.get_all("Set-Cookie") or []


def csrf(htmltext):
    m = re.search(r'name="csrf_token" value="([^"]+)"', htmltext)
    return m.group(1) if m else None


def flash_of(set_cookies):
    import base64, json
    for h in set_cookies:
        if h.startswith("lf_flash="):
            raw = h.split(";", 1)[0][len("lf_flash="):]
            try:
                kind, msg = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
                return kind, msg
            except Exception:
                return "", ""
    return "", ""


db = sqlite3.connect("/tmp/lfdata/lostfound.db")

# ---- 工作人员登录
jar, staff = client()
_, body = req(staff, "GET", "/staff/login")
tok = csrf(body)
req(staff, "POST", "/staff/login", form={"csrf_token": tok, "password": "secret123"}, raw=True)
_, dash = req(staff, "GET", "/staff")
SCSRF = csrf(dash)
COOKIE = "; ".join(f"{c.name}={c.value}" for c in jar)


def make_item(pub, priv):
    req(staff, "POST", "/staff/items",
        form={"csrf_token": SCSRF, "public_description": pub, "private_features": priv}, raw=True)
    _, b = req(staff, "GET", "/staff")
    return max(int(x) for x in re.findall(r'/staff/items/(\d+)', b))


def make_claim(item, answer, contact):
    _, v = client()
    st, b = req(v, "POST", f"/items/{item}/claims", form={"answer": answer, "contact": contact})
    m = re.search(r'<div class="tokenbox">([^<]+)</div>', b)
    assert st == 200 and m, (st, b)
    return m.group(1).strip()


def claim_id_of(token):
    h = __import__("hashlib").sha256(token.encode()).hexdigest()
    return db.execute("SELECT id FROM claims WHERE token_hash=?", (h,)).fetchone()[0]


def withdraw_raw(token):
    # 不经 CookieJar（避免其把一次性 flash 吞掉）；直接取 303 的 Set-Cookie。
    code, loc, sc = raw_cookie("", "/claims/withdraw", {"token": token})
    return code, loc, sc


def query(token):
    _, v = client()
    return req(v, "GET", "/query?token=" + urllib.parse.quote(token))


def status_of(token):
    h = __import__("hashlib").sha256(token.encode()).hexdigest()
    return db.execute("SELECT status FROM claims WHERE token_hash=?", (h,)).fetchone()[0]


# ============================================================ 1 待核对撤回
print("== 待核对申请撤回：仅改变本人状态 ==")
i1 = make_item("撤回-物品1-蓝色水杯", "私密-杯底刻字M")
ta = make_claim(i1, "答案甲", "cA")
tb = make_claim(i1, "答案乙", "cB")
st, b = query(ta)
check("待核对查询页有撤回表单", 'action="/claims/withdraw"' in b and 'name="token"' in b)

st, loc, _ = withdraw_raw(ta)
check("撤回应 303 回查询页", st == 303 and loc.startswith("/query?token="), (st, loc))
check("待核对撤回后状态 withdrawn", status_of(ta) == "withdrawn")
row = db.execute("SELECT status, withdrawn_at, decided_at FROM claims WHERE token_hash=?",
                 (__import__("hashlib").sha256(ta.encode()).hexdigest(),)).fetchone()
check("写入撤回时间且 decided_at 为空(未被选定过)", row[1] is not None and row[2] is None, str(row))

st, b = query(ta)
check("撤回后凭证仍可查询并显示已撤回", "已撤回" in b)
check("撤回页显示撤回时间", "撤回时间" in b)
check("撤回后不显示交还码", '<div class="codebox">' not in b)
check("撤回后不再显示撤回表单(防重复)", 'action="/claims/withdraw"' not in b)
# 另一申请不受影响，物品仍开放
check("同物品另一申请仍待核对", status_of(tb) == "pending")
st, _ = req(client()[1], "POST", f"/items/{i1}/claims", form={"answer": "新", "contact": "n"})
check("物品仍接受新申请(200)", st == 200)

# 工作人员可见撤回状态与撤回时间
st, b = req(staff, "GET", f"/staff/items/{i1}")
check("工作人员物品页显示已撤回徽标", "已撤回" in b)
check("工作人员物品页显示撤回时间列", "处理/撤回时间" in b)
cid_a = claim_id_of(ta)
st, b = req(staff, "GET", f"/staff/claims/{cid_a}")
check("工作人员申请页显示撤回时间", "撤回时间" in b and "已被申请人主动撤回" in b)
check("已撤回申请页无选定按钮", f"/staff/claims/{cid_a}/select" not in b)

# ============================================================ 2 重复撤回
print("== 重复撤回只显示已撤回，不重复写入 ==")
w1 = db.execute("SELECT withdrawn_at FROM claims WHERE id=?", (cid_a,)).fetchone()[0]
st, _, _ = withdraw_raw(ta)
st, _, sc = withdraw_raw(ta)
fl = flash_of(sc)
w2 = db.execute("SELECT withdrawn_at FROM claims WHERE id=?", (cid_a,)).fetchone()[0]
check("重复撤回仍 303", st == 303)
check("重复撤回提示已撤回(不报错)", fl[0] in ("info", "ok") and "已撤回" in fl[1], str(fl))
check("撤回时间未被重复写入(保持不变)", w1 == w2, (w1, w2))
n = db.execute("SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id WHERE c.item_id=?",
               (i1,)).fetchone()[0]
check("待核对撤回不产生交还码", n == 0)
# 工作人员不能再选定已撤回申请
st, _, _ = req(staff, "POST", f"/staff/claims/{cid_a}/select", form={"csrf_token": SCSRF}, raw=True)
st, b = req(staff, "GET", f"/staff/claims/{cid_a}")
check("选定已撤回申请被业务拒绝", "主动撤回" in b)

# ============================================================ 3 未获选撤回
print("== 未获选申请撤回：仅改变本人状态，不影响获选人 ==")
i2 = make_item("撤回-物品2-黑色雨伞", "私密-伞柄LF")
u1 = make_claim(i2, "赢家", "w1")
u2 = make_claim(i2, "输家", "w2")
wid, lid = claim_id_of(u1), claim_id_of(u2)
req(staff, "POST", f"/staff/claims/{wid}/select", form={"csrf_token": SCSRF}, raw=True)
check("落选者为 rejected", status_of(u2) == "rejected")
st, _, _ = withdraw_raw(u2)
check("未获选撤回后 withdrawn", status_of(u2) == "withdrawn")
check("获选人不受影响仍 selected", status_of(u1) == "selected")
check("物品仍有获选人(不开放新申请)",
      db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'", (i2,)).fetchone()[0] == 1)
_, v = client()
st, b = req(v, "POST", f"/items/{i2}/claims", form={"answer": "x", "contact": "y"})
check("有获选人时仍拒绝新申请", st == 400 and "已确定获选人" in b)

# ============================================================ 4 已获选撤回（物品保管中）
print("== 获选申请撤回：作废码 + 恢复未获选为待核对 + 重新开放 ==")
i3 = make_item("撤回-物品3-棕色钱包", "私密-借书卡姓名")
p1 = make_claim(i3, "获选人答案", "p1")
p2 = make_claim(i3, "落选答案甲", "p2")
p3 = make_claim(i3, "落选答案乙", "p3")
p4 = make_claim(i3, "之前主动撤回者", "p4")  # 这个先主动撤回(pending->withdrawn)
s1, s2, s3, s4 = (claim_id_of(x) for x in (p1, p2, p3, p4))
withdraw_raw(p4)
req(staff, "POST", f"/staff/claims/{s1}/select", form={"csrf_token": SCSRF}, raw=True)
check("选定后 s2/s3 rejected", status_of(p2) == "rejected" and status_of(p3) == "rejected")
check("s4 已主动撤回", status_of(p4) == "withdrawn")
# 获选人拿到码
st, b = query(p1)
m = re.search(r'<div class="codebox">(\d{8})</div>', b)
check("撤回前获选人有码", bool(m))
old_code = m.group(1) if m else ""
ver_before = db.execute("SELECT version FROM items WHERE id=?", (i3,)).fetchone()[0]

st, _, sc = withdraw_raw(p1)
fl = flash_of(sc)
check("获选撤回 303 且成功提示", st == 303 and fl[0] == "ok", str(fl))
check("获选人变 withdrawn", status_of(p1) == "withdrawn")
ninv = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND invalidated_at IS NOT NULL",
                  (s1,)).fetchone()[0]
check("获选人全部未核销码被作废", ninv >= 1)
nactive = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id "
    "WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL", (i3,)).fetchone()[0]
check("物品下不再有任何有效码", nactive == 0, str(nactive))
check("落选 s2 恢复待核对", status_of(p2) == "pending")
check("落选 s3 恢复待核对", status_of(p3) == "pending")
check("已主动撤回 s4 不恢复", status_of(p4) == "withdrawn")
r = db.execute("SELECT status, decided_at FROM claims WHERE id=?", (s2,)).fetchone()
check("恢复待核对时清空 decided_at", r[0] == "pending" and r[1] is None, str(r))
nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'", (i3,)).fetchone()[0]
check("物品已无获选人", nsel == 0)
ver_after = db.execute("SELECT version FROM items WHERE id=?", (i3,)).fetchone()[0]
check("获选撤回推进了物品版本号", ver_after == ver_before + 1, (ver_before, ver_after))

# 重新接受新申请
_, v = client()
st, b = req(v, "POST", f"/items/{i3}/claims", form={"answer": "新人申请", "contact": "new"})
check("撤回获选后物品重新接受新申请", st == 200 and "查询凭证" in b)
# 旧码核销被拒
_, b2 = req(staff, "GET", "/staff/verify")
vc = csrf(b2)
st, b = req(staff, "POST", "/staff/verify", form={"csrf_token": vc, "code": old_code})
check("撤回后旧交还码核销被拒", "已失效" in b)
check("旧码被拒后物品仍保管中",
      db.execute("SELECT status FROM items WHERE id=?", (i3,)).fetchone()[0] == "kept")
# 撤回者查询页无码、无重新获选
st, b = query(p1)
check("撤回获选人查询页不显示码", '<div class="codebox">' not in b and "已撤回" in b)
# 恢复的申请可再次被选定
req(staff, "POST", f"/staff/claims/{s2}/select", form={"csrf_token": SCSRF}, raw=True)
check("恢复的 s2 可重新获选", status_of(p2) == "selected")

# ============================================================ 5 物品已撤下时获选撤回
print("== 物品已撤下：获选撤回作废码，但不恢复申请、不开放提交 ==")
i4 = make_item("撤回-物品4-绿色饭盒", "私密-盒盖贴纸")
q1 = make_claim(i4, "获选", "q1")
q2 = make_claim(i4, "落选", "q2")
z1, z2 = claim_id_of(q1), claim_id_of(q2)
req(staff, "POST", f"/staff/claims/{z1}/select", form={"csrf_token": SCSRF}, raw=True)
ver4 = db.execute("SELECT version FROM items WHERE id=?", (i4,)).fetchone()[0]
req(staff, "POST", f"/staff/items/{i4}/remove", form={"csrf_token": SCSRF, "version": ver4}, raw=True)
# 撤下时待核对申请被拒（这里 s2 已在选定时 rejected，状态保持 rejected）
check("物品已 removed", db.execute("SELECT status FROM items WHERE id=?", (i4,)).fetchone()[0] == "removed")
check("撤下后落选仍 rejected", status_of(q2) == "rejected")
st, _, _ = withdraw_raw(q1)
check("撤下物品上获选申请可撤回", status_of(q1) == "withdrawn")
check("撤回作废其码",
      db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND invalidated_at IS NOT NULL",
                 (z1,)).fetchone()[0] >= 1)
check("撤下后不恢复落选申请", status_of(q2) == "rejected")
nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'", (i4,)).fetchone()[0]
check("撤下后无获选人但仍不开放", nsel == 0)
_, v = client()
st, b = req(v, "POST", f"/items/{i4}/claims", form={"answer": "x", "contact": "y"})
check("已撤下物品拒绝新申请", st == 400 and "撤下或已交还" in b)

# ============================================================ 6 物品已交还拒绝撤回
print("== 物品已交还：拒绝撤回任何申请 ==")
i5 = make_item("撤回-物品5-银色手表", "私密-表背划痕")
k1 = make_claim(i5, "获选人", "k1")
k2 = make_claim(i5, "待核对", "k2")
c1id = claim_id_of(k1)
req(staff, "POST", f"/staff/claims/{c1id}/select", form={"csrf_token": SCSRF}, raw=True)
st, b = query(k1)
code = re.search(r'<div class="codebox">(\d{8})</div>', b).group(1)
_, b = req(staff, "GET", "/staff/verify")
req(staff, "POST", "/staff/verify", form={"csrf_token": csrf(b), "code": code})
check("物品已交还", db.execute("SELECT status FROM items WHERE id=?", (i5,)).fetchone()[0] == "returned")
for label, tk in (("获选(已交还)", k1), ("未获选(已交还物品)", k2)):
    st, _, sc = withdraw_raw(tk)
    fl = flash_of(sc)
    check(f"{label}撤回被拒绝并提示已交还", st == 303 and "已交还" in fl[1] and fl[0] == "err", str(fl))
check("获选申请仍 selected(未被撤回)", status_of(k1) == "selected")
check("另一申请仍 rejected(未被撤回)", status_of(k2) == "rejected")
st, b = query(k2)
check("已交还物品查询页不出现撤回表单", 'action="/claims/withdraw"' not in b)

# 错误凭证
st, _, _ = req(client()[1], "POST", "/claims/withdraw", form={"token": "not-a-token"}, raw=True)
check("错误凭证撤回被拒", st in (400, 404))

# ============================================================ 7 撤回与工作人员操作并发
print("== 并发：撤回 vs 选定 / 改判 / 重发 / 撤下 / 核码 ==")


def setup_item(pub, n=3):
    iid = make_item(pub, "私密-" + pub)
    toks = [make_claim(iid, f"答案{j}", f"u{j}") for j in range(n)]
    ids = [claim_id_of(t) for t in toks]
    return iid, toks, ids


# 7a 撤回 vs 选定（同一份待核对申请）
iA, tA, idsA = setup_item("并发A-撤回对选定")
cid = idsA[0]
bar = threading.Barrier(2)


def task_withdraw(token):
    bar.wait()
    code, loc, sc = raw_cookie(COOKIE, "/claims/withdraw", {"token": token})
    return ("wd", code, loc, sc)


def task_select(c):
    bar.wait()
    code, loc, sc = raw_cookie(COOKIE, f"/staff/claims/{c}/select", {"csrf_token": SCSRF})
    return ("sel", code, loc, sc)


with ThreadPoolExecutor(max_workers=2) as ex:
    r1 = ex.submit(task_withdraw, tA[0])
    r2 = ex.submit(task_select, cid)
    o1, o2 = r1.result(), r2.result()
final = status_of(tA[0])
check("撤回/选定并发后该申请状态唯一(withdrawn 或 selected)",
      final in ("withdrawn", "selected"), final)
if final == "withdrawn":
    check("撤回胜出：物品无获选人(选定未作用于该申请)",
          db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                     (iA,)).fetchone()[0] == 0)
    # 工作人员随后得到明确提示而非双获选
    st, b = req(staff, "GET", f"/staff/claims/{cid}")
    check("撤回胜出：申请页提示已撤回", "已撤回" in b)
else:
    check("选定胜出：申请仍获选(撤回失败方无写入)", final == "selected")
nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'", (iA,)).fetchone()[0]
check("绝无双获选", nsel <= 1, str(nsel))

# 7b 撤回(获选人) vs 核码：最多一方生效，不得留可用旧码 + 物品半态
iB, tB, idsB = setup_item("并发B-撤回对核码", 1)
sB = idsB[0]
req(staff, "POST", f"/staff/claims/{sB}/select", form={"csrf_token": SCSRF}, raw=True)
st, b = query(tB[0])
codeB = re.search(r'<div class="codebox">(\d{8})</div>', b).group(1)
bar = threading.Barrier(2)


def task_verify(c):
    bar.wait()
    code, loc, sc = raw_cookie(COOKIE, "/staff/verify", {"csrf_token": SCSRF, "code": c})
    return ("vf", code, loc, sc)


with ThreadPoolExecutor(max_workers=2) as ex:
    r1 = ex.submit(task_withdraw, tB[0])
    r2 = ex.submit(task_verify, codeB)
    o1, o2 = r1.result(), r2.result()
istat = db.execute("SELECT status FROM items WHERE id=?", (iB,)).fetchone()[0]
cstat = status_of(tB[0])
check("撤回/核码并发结局一致：要么交还要么撤回，不矛盾",
      (istat == "returned" and cstat == "selected") or (istat == "kept" and cstat == "withdrawn"),
      f"item={istat} claim={cstat}")
if istat == "returned":
    check("核码胜出：码已核销",
          db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND consumed_at IS NOT NULL",
                     (sB,)).fetchone()[0] == 1)
else:
    check("撤回胜出：旧码已作废无可用码",
          db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=? "
                     "AND consumed_at IS NULL AND invalidated_at IS NULL", (sB,)).fetchone()[0] == 0)
    check("撤回胜出：物品仍保管中可继续流程", istat == "kept")

# 7c 撤回(获选人) vs 撤下：互不产生矛盾状态
iC, tC, idsC = setup_item("并发C-撤回对撤下", 2)
sC = idsC[0]
req(staff, "POST", f"/staff/claims/{sC}/select", form={"csrf_token": SCSRF}, raw=True)
verC = db.execute("SELECT version FROM items WHERE id=?", (iC,)).fetchone()[0]
bar = threading.Barrier(2)


def task_remove(iid, ver):
    bar.wait()
    code, loc, sc = raw_cookie(COOKIE, f"/staff/items/{iid}/remove",
                               {"csrf_token": SCSRF, "version": ver})
    return ("rm", code, loc, sc)


with ThreadPoolExecutor(max_workers=2) as ex:
    r1 = ex.submit(task_withdraw, tC[0])
    r2 = ex.submit(task_remove, iC, verC)
    o1, o2 = r1.result(), r2.result()
istat = db.execute("SELECT status FROM items WHERE id=?", (iC,)).fetchone()[0]
cstat = status_of(tC[0])
# 两种结局都必须自洽
if istat == "removed":
    check("撤下胜出：撤回若也提交，申请为 withdrawn；否则保持 selected",
          cstat in ("withdrawn", "selected"), cstat)
    # 物品撤下后即便撤回也未恢复落选申请
    check("撤下结局：落选申请未被恢复为 pending",
          db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='pending'",
                     (iC,)).fetchone()[0] == 0)
else:
    check("撤回胜出(物品仍保管中)：获选申请 withdrawn", cstat == "withdrawn", cstat)
    check("撤回胜出：落选恢复 pending", status_of(tC[1]) == "pending")

# 7d 撤回(获选人) vs 改判 / 重发：不得双获选、不得给已撤回者留新码
iD, tD, idsD = setup_item("并发D-撤回对改判重发", 2)
sD, oD = idsD
req(staff, "POST", f"/staff/claims/{sD}/select", form={"csrf_token": SCSRF}, raw=True)
verD = db.execute("SELECT version FROM items WHERE id=?", (iD,)).fetchone()[0]
bar = threading.Barrier(3)


def task_rejudge(src, dst, ver):
    bar.wait()
    code, loc, sc = raw_cookie(
        COOKIE, f"/staff/claims/{src}/rejudge",
        {"csrf_token": SCSRF, "to_claim_id": str(dst), "reason": "并发撤回",
         "item_id": str(iD), "version": ver})
    return ("rj", code, loc, sc)


def task_resend(c):
    bar.wait()
    code, loc, sc = raw_cookie(COOKIE, f"/staff/claims/{c}/resend", {"csrf_token": SCSRF})
    return ("rs", code, loc, sc)


with ThreadPoolExecutor(max_workers=3) as ex:
    f1 = ex.submit(task_withdraw, tD[0])
    f2 = ex.submit(task_rejudge, sD, oD, verD)
    f3 = ex.submit(task_resend, sD)
    res = [f.result() for f in (f1, f2, f3)]
nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'", (iD,)).fetchone()[0]
check("三方并发后 selected 至多 1（无双获选）", nsel <= 1, str(nsel))
# 原获选人若已撤回，则其名下不得有任何有效码
if status_of(tD[0]) == "withdrawn":
    nact = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=? "
                      "AND consumed_at IS NULL AND invalidated_at IS NULL", (sD,)).fetchone()[0]
    check("已撤回的原获选人名下无有效码(重发未给旧状态留码)", nact == 0, str(nact))
ncorr = db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?", (iD,)).fetchone()[0]
check("若改判与撤回竞争，留痕不超过 1 条", ncorr <= 1, str(ncorr))
# 全物品有效码总数必须与获选人数一致（0 或 1）
nactive = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id "
    "WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL", (iD,)).fetchone()[0]
check("有效码数与获选人数一致(无游离有效码)", nactive == nsel, f"codes={nactive} sel={nsel}")

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
