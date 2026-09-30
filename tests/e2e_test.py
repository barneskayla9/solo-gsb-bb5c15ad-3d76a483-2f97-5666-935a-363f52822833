"""端到端 + 并发测试（标准库）。前置：服务已运行在 http://127.0.0.1:8181。"""
import http.cookiejar
import os
import re
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8181"
PASS = 0
FAIL = 0


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
    """raw=True 时不跟随 303，返回 (status, body, location)；否则返回最终页 (status, body)。"""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    try:
        if raw:
            opener = urllib.request.build_opener(
                NoRedirect, urllib.request.HTTPCookieProcessor(_jar_of(op))
            )
            resp = opener.open(r)
            return resp.status, resp.read().decode(), resp.headers.get("Location", "")
        resp = op.open(r)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if raw:
            return e.code, body, e.headers.get("Location", "")
        return e.code, body


def raw_with_cookie(cookie_header, path, form):
    """线程内手工带 Cookie 的无跳转 POST，返回 (status, location)。"""
    data = urllib.parse.urlencode(form).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST")
    r.add_header("Cookie", cookie_header)
    try:
        resp = urllib.request.build_opener(NoRedirect).open(r)
        return resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def raw_with_cookie_full(cookie_header, path, form):
    """同 raw_with_cookie，额外返回响应中的 Set-Cookie 头（用于读取一次性 flash）。"""
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


# --------------------------------------------------------------- 1 基本流程
print("== 访客与工作人员基本流程 ==")
_, visitor = client()
st, body = req(visitor, "GET", "/")
check("首页可访问", st == 200 and "保管中的失物" in body)

anonjar, anon = client()
st, body = req(anon, "GET", "/staff/login")
tok = csrf(body)
check("登录页含 CSRF", bool(tok))

st, _, _ = req(anon, "POST", "/staff/login", form={"csrf_token": tok, "password": "wrong"}, raw=True)
check("错误口令被拒(401)", st == 401)

st, body, _ = req(anon, "POST", "/staff/login", form={"csrf_token": tok, "password": "wrong"}, raw=True)
check("重放登录请求(CSRF已轮换)被拒", st == 400)

st, body = req(anon, "GET", "/staff/login")
tok = csrf(body)
st, _, loc = req(anon, "POST", "/staff/login", form={"csrf_token": tok, "password": "secret123"}, raw=True)
check("正确口令登录(303)", st == 303 and loc == "/staff")

st, dash = req(anon, "GET", "/staff")
staff_csrf = csrf(dash)
check("登录后进入登记台", st == 200 and "登记新物品" in dash and bool(staff_csrf))

PRIV1 = "私密特征-内袋蓝色姓名贴ZXQ"
PUB1 = "公开信息-黑色双肩包"
st, _, loc = req(anon, "POST", "/staff/items",
                 form={"csrf_token": staff_csrf, "public_description": PUB1, "private_features": PRIV1}, raw=True)
check("登记物品1(303)", st == 303 and loc == "/staff/items/1")
PRIV2 = "私密特征2-钥匙扣红色小熊"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": "公开信息2-银色钥匙串",
                     "private_features": PRIV2}, raw=True)
check("登记物品2(303)", st == 303)
PRIV3 = "私密特征3-水壶底部划痕"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": "公开信息3-白色保温杯",
                     "private_features": PRIV3}, raw=True)
check("登记物品3(303)", st == 303)

_, v0 = client()
st, body = req(v0, "GET", "/")
check("访客列表显示公开描述", PUB1 in body and "公开信息2" in body and "公开信息3" in body)
check("访客列表不泄露私密特征", PRIV1 not in body and PRIV2 not in body and PRIV3 not in body)

_, stranger = client()
st, _ = req(stranger, "GET", "/staff")
check("未登录访问 /staff 被拒(403)", st == 403)
st, _ = req(stranger, "GET", "/staff/items/1")
check("未登录访问物品管理页被拒(403)", st == 403)

# --------------------------------------------------------------- 2 提交认领
print("== 提交认领与凭证 ==")
_, item1_page = client()
st, body = req(item1_page, "GET", "/items/1")
check("访客物品页显示公开描述", st == 200 and PUB1 in body)
check("访客物品页不显示私密特征", PRIV1 not in body)

tokens = []
for i, ans in enumerate(["答案A-说有姓名贴", "答案B-答错了", "答案C-也答对了"]):
    _, v = client()
    st, body = req(v, "POST", "/items/1/claims", form={"answer": ans, "contact": f"1380000000{i}"})
    check(f"提交认领{i+1}", st == 200 and "查询凭证" in body)
    m = re.search(r'<div class="tokenbox">([^<]+)</div>', body)
    check(f"认领{i+1}返回凭证", bool(m))
    if m:
        tokens.append((m.group(1).strip(), ans, f"1380000000{i}"))

_, v = client()
st, _ = req(v, "POST", "/items/1/claims", form={"answer": "x", "contact": ""})
check("缺少联系方式被拒(400)", st == 400)

_, v = client()
st, _ = req(v, "GET", "/query")
check("查询页可打开", st == 200)
st, body = req(v, "GET", "/query?token=not-a-real-token")
check("错误凭证查询失败", "凭证不正确" in body)

tA, ansA, conA = tokens[0]
tB, ansB, conB = tokens[1]
tC = tokens[2][0]
_, v = client()
st, qA = req(v, "GET", f"/query?token={urllib.parse.quote(tA)}")
check("凭证持有人看到本人状态", "待核对" in qA and PUB1 in qA)
check("查询页不泄露私密特征", PRIV1 not in qA)
check("查询页不泄露他人答案/联系方式/自身联系方式", ansB not in qA and conB not in qA and conA not in qA)

# --------------------------------------------------------------- 3 工作人员视图
print("== 工作人员可见全部信息 ==")
st, body = req(anon, "GET", "/staff/items/1")
check("工作人员能看到私密特征", PRIV1 in body)
check("工作人员能看到答案与联系方式", ansA in body and conA in body)
claim_ids = [int(x) for x in re.findall(r'/staff/claims/(\d+)', body)]
check("物品1有3个申请", len(claim_ids) == 3, str(claim_ids))
cA, cB, cC = claim_ids

st, _, _ = req(anon, "POST", f"/staff/claims/{cA}/select", form={"csrf_token": "bad"}, raw=True)
check("伪造CSRF选定被拒(400)", st == 400)

# --------------------------------------------------------------- 4 并发选定
print("== 并发确认最多成功一次 ==")
cookie_header = "; ".join(f"{c.name}={c.value}" for c in anonjar)
barrier = threading.Barrier(3)


def select_task(cid):
    barrier.wait()
    return cid, raw_with_cookie(cookie_header, f"/staff/claims/{cid}/select",
                                {"csrf_token": staff_csrf})


with ThreadPoolExecutor(max_workers=3) as ex:
    outcomes = [f.result() for f in [ex.submit(select_task, c) for c in (cA, cB, cC)]]

n303 = sum(1 for _, (code, _loc) in outcomes if code == 303)
check("并发选定三个请求都被接受处理(303)", n303 == 3, str(outcomes))

db = sqlite3.connect("/tmp/lfdata/lostfound.db")
rows = db.execute("SELECT id, status FROM claims WHERE item_id=1 ORDER BY id").fetchall()
selected_ids = [cid for cid, s in rows if s == "selected"]
nrej = sum(1 for _, s in rows if s == "rejected")
check("DB 中 selected 恰好1个", selected_ids == [cA] or len(selected_ids) == 1, str(rows))
check("DB 中其余 rejected=2", nrej == 2, str(rows))
winner = selected_ids[0]
winner_token = {cA: tA, cB: tB, cC: tC}[winner]
loser_token = {cA: tA, cB: tB, cC: tC}[cA if winner != cA else cB]

_, v = client()
st, body = req(v, "GET", f"/query?token={urllib.parse.quote(loser_token)}")
check("落选者看到未获选", "未获选" in body)
check("落选者页面无交还码展示", '<div class="codebox">' not in body)

_, v = client()
st, wq = req(v, "GET", f"/query?token={urllib.parse.quote(winner_token)}")
check("获选者看到已获选", "已获选" in wq)
m = re.search(r'<div class="codebox">(\d{8})</div>', wq)
check("获选者取得8位交还码", bool(m))
code1 = m.group(1) if m else ""
check("查询页不泄露私密特征", PRIV1 not in wq)

_, v = client()
st, body = req(v, "POST", "/items/1/claims", form={"answer": "后来者", "contact": "x@y.z"})
check("已获选物品拒绝新申请", st == 400 and "已确定获选人" in body)

# 成功的那个再次选定（跟随跳转到申请页，应带错误提示）
st, body = req(anon, "POST", f"/staff/claims/{winner}/select", form={"csrf_token": staff_csrf})
check("重复选定不改变状态并提示", st == 200 and "已经处理" in body)
nsel2 = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=1 AND status='selected'").fetchone()[0]
check("重复选定后 selected 仍为1", nsel2 == 1)

# --------------------------------------------------------------- 5 撤下
print("== 撤下物品 ==")
_, v2 = client()
st, body = req(v2, "POST", "/items/2/claims", form={"answer": "待核对答案", "contact": "wechat-xyz"})
t2 = re.search(r'<div class="tokenbox">([^<]+)</div>', body).group(1).strip()
st, _, loc = req(anon, "POST", "/staff/items/2/remove", form={"csrf_token": staff_csrf}, raw=True)
check("撤下物品(303)", st == 303 and loc == "/staff/items/2")
_, v = client()
st, body = req(v, "GET", "/")
check("撤下后不在公开列表", "公开信息2" not in body)
st, body = req(v, "GET", "/items/2")
check("撤下物品页无申请表单", 'action="/items/2/claims"' not in body)
st, body = req(v, "POST", "/items/2/claims", form={"answer": "a", "contact": "b"})
check("撤下物品拒绝新申请", st == 400 and "撤下或已交还" in body)
st, body = req(v, "GET", f"/query?token={urllib.parse.quote(t2)}")
check("撤下后待核对申请变为未获选", "未获选" in body)
st, _, _ = req(anon, "POST", "/staff/items/2/restore", form={"csrf_token": staff_csrf}, raw=True)
check("恢复物品(303)", st == 303)
st, body = req(v, "GET", "/")
check("恢复后重回公开列表", "公开信息2" in body)

# --------------------------------------------------------------- 6 核销与并发
print("== 核码交还 / 重复核码 / 并发核码 ==")
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": "00000000"})
check("错误交还码被拒(200提示)", st == 200 and "交还码不正确" in body)

barrier2 = threading.Barrier(2)


def verify_attempt(code):
    barrier2.wait()
    return raw_with_cookie(cookie_header, "/staff/verify", {"csrf_token": vcsrf, "code": code})


with ThreadPoolExecutor(max_workers=2) as ex:
    o1 = ex.submit(verify_attempt, code1)
    o2 = ex.submit(verify_attempt, code1)
    r1, r2 = o1.result(), o2.result()

success = [o for o in (r1, r2) if o[0] == 303 and o[1] == "/staff/items/1"]
check("并发核码恰好1次成功(303到物品页)", len(success) == 1, f"{r1} {r2}")
st = db.execute("SELECT status FROM items WHERE id=1").fetchone()[0]
check("物品1已交还", st == "returned")
nconsumed = db.execute("SELECT COUNT(*) FROM handover_codes WHERE consumed_at IS NOT NULL").fetchone()[0]
check("恰好1枚码被核销", nconsumed == 1)

st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code1})
check("重复核码被拒并提示已使用", st == 200 and "已使用" in body)

_, v = client()
st, body = req(v, "POST", "/items/1/claims", form={"answer": "a", "contact": "b"})
check("已交还物品拒绝新申请", st == 400 and "撤下或已交还" in body)

st, body = req(v, "GET", f"/query?token={urllib.parse.quote(winner_token)}")
check("获选者查询页不再显示可用码", 'class="codebox"' not in body)

# --------------------------------------------------------------- 7 重发/过期
print("== 交还码过期与重发 ==")
_, v3a = client()
req(v3a, "POST", "/items/3/claims", form={"answer": "甲", "contact": "甲-contact"})
_, v3b = client()
st, body = req(v3b, "POST", "/items/3/claims", form={"answer": "乙", "contact": "乙-contact"})
t3 = re.search(r'<div class="tokenbox">([^<]+)</div>', body).group(1).strip()

st, body = req(anon, "GET", "/staff/items/3")
c3 = [int(x) for x in re.findall(r'/staff/claims/(\d+)', body)][-1]
st, _, _ = req(anon, "POST", f"/staff/claims/{c3}/select", form={"csrf_token": staff_csrf}, raw=True)
check("选定物品3申请(303)", st == 303)

_, v = client()
st, q3 = req(v, "GET", f"/query?token={urllib.parse.quote(t3)}")
code_a = re.search(r'<div class="codebox">(\d{8})</div>', q3).group(1)
check("物品3获选者取得码", bool(code_a))

# 直接把码改过期
db.execute("UPDATE handover_codes SET expires_at='2000-01-01T00:00:00+00:00'"
           " WHERE claim_id=? AND consumed_at IS NULL", (c3,))
db.commit()
st, q3 = req(v, "GET", f"/query?token={urllib.parse.quote(t3)}")
check("过期后查询页提示过期", "交还码已过期" in q3 and 'class="codebox"' not in q3)
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code_a})
check("过期码核销被拒", "已过期" in body)

st, body = req(anon, "GET", f"/staff/claims/{c3}")
ccsrf = csrf(body)
st, _, _ = req(anon, "POST", f"/staff/claims/{c3}/resend", form={"csrf_token": ccsrf}, raw=True)
check("重发新码(303)", st == 303)
ninvalid = db.execute("SELECT COUNT(*) FROM handover_codes WHERE invalidated_at IS NOT NULL").fetchone()[0]
check("旧码已标记失效", ninvalid >= 1)

st, q3 = req(v, "GET", f"/query?token={urllib.parse.quote(t3)}")
m3 = re.search(r'<div class="codebox">(\d{8})</div>', q3)
check("重发后查询页显示新码", bool(m3) and m3.group(1) != code_a)
code_b = m3.group(1)

st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code_a})
check("旧码(已重发)核销被拒", "已失效" in body)

# 再重发一次：新新码也使上一枚失效
st, body = req(anon, "GET", f"/staff/claims/{c3}")
ccsrf = csrf(body)
req(anon, "POST", f"/staff/claims/{c3}/resend", form={"csrf_token": ccsrf}, raw=True)
st, q3 = req(v, "GET", f"/query?token={urllib.parse.quote(t3)}")
code_c = re.search(r'<div class="codebox">(\d{8})</div>', q3).group(1)
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code_b})
check("第二枚码被重发后也失效", "已失效" in body)
st, body, loc = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code_c}, raw=True)
check("最新码核销成功(303)", st == 303 and loc == "/staff/items/3")
st3 = db.execute("SELECT status FROM items WHERE id=3").fetchone()[0]
check("物品3已交还", st3 == "returned")

# 已交还后：申请页不再提供重发按钮
st, body = req(anon, "GET", f"/staff/claims/{c3}")
check("已交还申请页不再提供重发按钮", f"/staff/claims/{c3}/resend" not in body)
codes_after = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?", (c3,)).fetchone()[0]
check("已交还未产生额外交还码", codes_after == 3)

# --------------------------------------------------------------- 8 凭证强度与健康检查
print("== 凭证强度与杂项 ==")
check("查询凭证长度>=40", len(tA) >= 40, tA)
check("两枚凭证不同", tA != tB)
_, v = client()
st, body = req(v, "GET", "/healthz")
check("健康检查200", st == 200 and body == "ok")
st, _ = req(v, "GET", "/no-such-page")
check("未知公共路径404", st == 404)
st, _ = req(v, "GET", "/staff/no-such-page")
check("未知工作人员路径对访客403", st == 403)

# --------------------------------------------------------------- 9 误选改判
print("== 误选改判 ==")
PRIV4 = "私密特征4-钱包内借书卡"
PUB4 = "公开信息4-棕色短款钱包"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": PUB4,
                     "private_features": PRIV4}, raw=True)
check("登记物品4(303)", st == 303)
st, body = req(anon, "GET", "/staff")
item4_id = max(int(x) for x in re.findall(r'/staff/items/(\d+)', body))

t4 = []
for i, ans in enumerate(["改-答案甲-说有现金", "改-答案乙-提到借书卡", "改-答案丙-答错"]):
    _, v = client()
    st, body = req(v, "POST", f"/items/{item4_id}/claims",
                   form={"answer": ans, "contact": f"1391111000{i}"})
    m = re.search(r'<div class="tokenbox">([^<]+)</div>', body)
    t4.append((m.group(1).strip(), ans))
    check(f"物品4提交认领{i+1}", st == 200 and bool(m))

st, body = req(anon, "GET", f"/staff/items/{item4_id}")
r4 = [int(x) for x in re.findall(r'/staff/claims/(\d+)', body)]
check("物品4有3个申请", len(r4) == 3, str(r4))
rA, rB, rC = r4
tokA, tokB, tokC = t4[0][0], t4[1][0], t4[2][0]
ansA4, ansB4 = t4[0][1], t4[1][1]

# 先选定甲
st, _, _ = req(anon, "POST", f"/staff/claims/{rA}/select",
               form={"csrf_token": staff_csrf}, raw=True)
check("选定物品4甲(303)", st == 303)

# 未获选申请页不应出现改判表单
st, body = req(anon, "GET", f"/staff/claims/{rB}")
check("未获选申请页无改判表单", f"/staff/claims/{rB}/rejudge" not in body)
# 原获选申请页出现改判表单，目标只列同物品未获选申请
st, body = req(anon, "GET", f"/staff/claims/{rA}")
check("获选申请页有改判表单", f'action="/staff/claims/{rA}/rejudge"' in body)
check("改判表单带物品版本号", 'name="version"' in body)
check("改判目标含同物品申请B", f'value="{rB}"' in body)
check("改判目标含同物品申请C", f'value="{rC}"' in body)
check("改判目标不含自己", f'<option value="{rA}">' not in body)
ver4 = db.execute("SELECT version FROM items WHERE id=?",
                  (item4_id,)).fetchone()[0]

# 拒绝项：理由为空（表单级校验，直接 400，不进入事务）
st, _, loc = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
                 form={"csrf_token": staff_csrf, "to_claim_id": str(rB),
                       "reason": "   ", "item_id": str(item4_id),
                       "version": ver4}, raw=True)
check("空理由改判被拒(400)", st == 400)
st, body = req(anon, "GET", f"/staff/claims/{rA}")
check("空理由被拒后仍有改判表单", f'action="/staff/claims/{rA}/rejudge"' in body)

# 拒绝项：目标是另一物品的申请
r_other = c3  # 物品3（已交还）的获选申请
st, _, loc = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
                 form={"csrf_token": staff_csrf, "to_claim_id": str(r_other),
                       "reason": "跨物品", "item_id": str(item4_id),
                       "version": ver4}, raw=True)
check("跨物品目标改判被拒", st == 303 and loc == f"/staff/claims/{rA}")

# 拒绝项：目标是原申请自己
st, _, _ = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
               form={"csrf_token": staff_csrf, "to_claim_id": str(rA),
                     "reason": "自己", "item_id": str(item4_id),
                     "version": ver4}, raw=True)
check("目标为自己改判被拒", st == 303)

# 拒绝项：从非获选申请发起（以B为原申请）
st, _, loc = req(anon, "POST", f"/staff/claims/{rB}/rejudge",
                 form={"csrf_token": staff_csrf, "to_claim_id": str(rC),
                       "reason": "B非获选", "item_id": str(item4_id),
                       "version": ver4}, raw=True)
check("非获选原申请发起改判被拒", st == 303 and loc == f"/staff/claims/{rB}")

# 上述拒绝均未改动状态
sel4 = db.execute("SELECT id FROM claims WHERE item_id=? AND status='selected'",
                  (item4_id,)).fetchall()
check("拒绝后仍只有甲获选", [r[0] for r in sel4] == [rA], str(sel4))

# 甲当前的码（改判前）
_, v = client()
st, qA0 = req(v, "GET", f"/query?token={urllib.parse.quote(tokA)}")
m = re.search(r'<div class="codebox">(\d{8})</div>', qA0)
check("甲改判前持有交还码", bool(m))
codeA0 = m.group(1) if m else ""

# 成功改判：甲 -> 乙
REASON = "现场二次核对：甲无法说出借书卡姓名，乙的答案与私密特征吻合"
st, _, loc = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
                 form={"csrf_token": staff_csrf, "to_claim_id": str(rB),
                       "reason": REASON, "item_id": str(item4_id),
                       "version": ver4}, raw=True)
check("改判成功(303到物品页)", st == 303 and loc == f"/staff/items/{item4_id}", loc)

rows4 = db.execute("SELECT id, status FROM claims WHERE item_id=? ORDER BY id",
                   (item4_id,)).fetchall()
check("改判后甲/丙未获选、乙获选",
      dict(rows4) == {rA: "rejected", rB: "selected", rC: "rejected"}, str(rows4))

# 原码全部失效 + 新码签发
ninv = db.execute("SELECT COUNT(*) FROM handover_codes hc WHERE hc.claim_id=?"
                  " AND hc.invalidated_at IS NOT NULL", (rA,)).fetchone()[0]
nnew = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?"
                  " AND consumed_at IS NULL AND invalidated_at IS NULL", (rB,)).fetchone()[0]
check("甲的旧码已失效", ninv >= 1)
check("乙获得一枚新有效码", nnew == 1)
check("改判留痕1条",
      db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?",
                 (item4_id,)).fetchone()[0] == 1)

# 甲的凭证：未获选，看不到任何码
_, v = client()
st, qA1 = req(v, "GET", f"/query?token={urllib.parse.quote(tokA)}")
check("甲查询页变为未获选", "未获选" in qA1 and "已获选" not in qA1)
check("甲查询页无交还码", '<div class="codebox">' not in qA1)
check("甲查询页不泄露改判理由与乙的信息", REASON not in qA1 and ansB4 not in qA1)
# 乙的凭证：已获选 + 新码（与旧码不同）
_, v = client()
st, qB1 = req(v, "GET", f"/query?token={urllib.parse.quote(tokB)}")
check("乙查询页变为已获选", "已获选" in qB1)
m = re.search(r'<div class="codebox">(\d{8})</div>', qB1)
check("乙取得新交还码且不同于甲旧码", bool(m) and m.group(1) != codeA0)
codeB1 = m.group(1) if m else ""
check("乙查询页不泄露改判理由与甲的信息", REASON not in qB1 and ansA4 not in qB1)
# 丙无变化
_, v = client()
st, qC1 = req(v, "GET", f"/query?token={urllib.parse.quote(tokC)}")
check("丙仍为未获选且无码", "未获选" in qC1 and '<div class="codebox">' not in qC1)

# 甲旧码核销被拒
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": codeA0})
check("甲旧码核销被拒(已失效)", "已失效" in body)
st4 = db.execute("SELECT status FROM items WHERE id=?", (item4_id,)).fetchone()[0]
check("旧码被拒后物品仍保管中", st4 == "kept")

# 乙的新码可核销成功（但为继续测试，先改用另一物品；此处直接验证新码正确）
# 留痕在物品页与双方申请页均可见
st, body = req(anon, "GET", f"/staff/items/{item4_id}")
check("物品页展示改判记录", "改判记录" in body and REASON in body
      and f'/staff/claims/{rA}' in body and f'/staff/claims/{rB}' in body)
st, bodyA = req(anon, "GET", f"/staff/claims/{rA}")
st, bodyB = req(anon, "GET", f"/staff/claims/{rB}")
check("甲申请页可追溯改判记录", REASON in bodyA)
check("乙申请页可追溯改判记录", REASON in bodyB)
check("乙申请页显示新码明文", codeB1 in bodyB)
check("甲申请页不显示乙新码明文", codeB1 not in bodyA)

# 甲被改判后不能再对其重发（页面无重发表单；用有效 CSRF 直打接口亦被业务拒绝）
st, _, _ = req(anon, "POST", f"/staff/claims/{rA}/resend",
               form={"csrf_token": staff_csrf}, raw=True)
check("对未获选的甲重发被拒(303业务错误)", st == 303)

# 再改判一次：乙 -> 甲（双重改判），版本号已被第一次改判推进
ver4b = db.execute("SELECT version FROM items WHERE id=?",
                   (item4_id,)).fetchone()[0]
check("第一次改判推进了物品版本号", ver4b != ver4)
st, _, _ = req(anon, "POST", f"/staff/claims/{rB}/rejudge",
               form={"csrf_token": staff_csrf, "to_claim_id": str(rA),
                     "reason": "再次复核后确认甲才是失主", "item_id": str(item4_id),
                     "version": ver4b}, raw=True)
check("二次改判(新版本号)被接受", st == 303)
# 旧版本号再改判会被拒绝（版本已推进）
st, body = req(anon, "GET", f"/staff/claims/{rA}")
st, _, loc = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
                 form={"csrf_token": staff_csrf, "to_claim_id": str(rB),
                       "reason": "旧版本号", "item_id": str(item4_id),
                       "version": ver4}, raw=True)
check("旧版本号改判被拒(303)", st == 303 and loc == f"/staff/claims/{rA}")
rows4 = db.execute("SELECT id, status FROM claims WHERE item_id=? ORDER BY id",
                   (item4_id,)).fetchall()
check("二次改判后甲获选、乙未获选",
      dict(rows4) == {rA: "selected", rB: "rejected", rC: "rejected"}, str(rows4))
ninv_b = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?"
                    " AND invalidated_at IS NOT NULL", (rB,)).fetchone()[0]
nnew_a = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?"
                    " AND consumed_at IS NULL AND invalidated_at IS NULL", (rA,)).fetchone()[0]
check("乙的码随二次改判失效", ninv_b >= 1)
check("甲重新获得新有效码", nnew_a == 1)
check("改判留痕共2条",
      db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?",
                 (item4_id,)).fetchone()[0] == 2)
_, v = client()
st, qB2 = req(v, "GET", f"/query?token={urllib.parse.quote(tokB)}")
check("乙二次改判后查询页无码", '<div class="codebox">' not in qB2 and "未获选" in qB2)

# 物品已交还后拒绝改判：用乙最后那枚新码核销
st, body = req(anon, "GET", f"/staff/claims/{rA}")
m = re.search(r'<div class="codebox">(\d{8})</div>', body)
codeA2 = m.group(1) if m else ""
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, _, loc = req(anon, "POST", "/staff/verify",
                form={"csrf_token": vcsrf, "code": codeA2}, raw=True)
check("甲最新码核销成功", st == 303 and loc == f"/staff/items/{item4_id}")
st, _, _ = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
               form={"csrf_token": staff_csrf, "to_claim_id": str(rB),
                     "reason": "交还后", "item_id": str(item4_id),
                     "version": ver4b}, raw=True)
check("已交还物品改判被拒", st == 303)
check("已交还后仍为2条留痕",
      db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?",
                 (item4_id,)).fetchone()[0] == 2)

# --------------------------------------------------------------- 10 改判并发
print("== 改判并发：只允许一次状态变更生效 ==")
PUB5 = "公开信息5-黑色雨伞"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": PUB5,
                     "private_features": "私密特征5-伞柄刻字LF"}, raw=True)
st, body = req(anon, "GET", "/staff")
item5_id = max(int(x) for x in re.findall(r'/staff/items/(\d+)', body))
t5 = []
for i in range(3):
    _, v = client()
    st, b = req(v, "POST", f"/items/{item5_id}/claims",
                form={"answer": f"伞答案{i}", "contact": f"u{i}"})
    t5.append(re.search(r'<div class="tokenbox">([^<]+)</div>', b).group(1).strip())
r5 = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id",
                               (item5_id,)).fetchall()]
s5, t5a, t5b = r5
req(anon, "POST", f"/staff/claims/{s5}/select", form={"csrf_token": staff_csrf}, raw=True)
ver5 = db.execute("SELECT version FROM items WHERE id=?",
                  (item5_id,)).fetchone()[0]


def rejudge_task(src, dst):
    return raw_with_cookie(
        cookie_header, f"/staff/claims/{src}/rejudge",
        {"csrf_token": staff_csrf, "to_claim_id": str(dst),
         "reason": f"并发改判 {src}->{dst}", "item_id": str(item5_id),
         "version": ver5},
    )


# 两个改判方向同时提交（同一物品），只有一次生效
barrier3 = threading.Barrier(2)


def rj_task(src, dst):
    barrier3.wait()
    return rejudge_task(src, dst)


with ThreadPoolExecutor(max_workers=2) as ex:
    o1 = ex.submit(rj_task, s5, t5a)
    o2 = ex.submit(rj_task, t5a, s5)
    cr1, cr2 = o1.result(), o2.result()

sel5 = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? AND status='selected'",
                                 (item5_id,)).fetchall()]
check("并发改判后 selected 仍恰好1个", len(sel5) == 1, str(sel5))
check("获选人只可能是原申请或首个目标", sel5[0] in (s5, t5a), str(sel5))
ncorr5 = db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?",
                    (item5_id,)).fetchone()[0]
check("并发改判只产生1条留痕", ncorr5 == 1, str(ncorr5))
nactive = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL",
    (item5_id,)).fetchone()[0]
check("并发改判后全物品仅1枚有效交还码", nactive == 1, str(nactive))

# 改判与撤下并发：状态只能朝一个方向变化，不得出现「撤下却签发新码」
PUB6 = "公开信息6-蓝色头盔"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": PUB6,
                     "private_features": "私密特征6-内衬贴纸"}, raw=True)
st, body = req(anon, "GET", "/staff")
item6_id = max(int(x) for x in re.findall(r'/staff/items/(\d+)', body))
for i in range(2):
    _, v = client()
    req(v, "POST", f"/items/{item6_id}/claims",
        form={"answer": f"盔答案{i}", "contact": f"h{i}"})
r6 = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id",
                               (item6_id,)).fetchall()]
s6, t6 = r6
req(anon, "POST", f"/staff/claims/{s6}/select", form={"csrf_token": staff_csrf}, raw=True)
# 记录选定前页面看到的版本号（模拟工作人员在改判发生前已打开撤下表单）
ver_before_select = db.execute("SELECT version FROM items WHERE id=?",
                               (item6_id,)).fetchone()[0]
# 选定会推进版本号；并发测试使用「当前页面版本」才公平，因此这里取选定后的最新版本
ver6 = db.execute("SELECT version FROM items WHERE id=?",
                  (item6_id,)).fetchone()[0]
barrier4 = threading.Barrier(2)


def mixed_task(kind):
    barrier4.wait()
    if kind == "rj":
        code, loc, sc = raw_with_cookie_full(
            cookie_header, f"/staff/claims/{s6}/rejudge",
            {"csrf_token": staff_csrf, "to_claim_id": str(t6),
             "reason": "并发改判：撤下同时发起", "item_id": str(item6_id),
             "version": ver6})
        return ("rj", code, loc, sc)
    code, loc, sc = raw_with_cookie_full(
        cookie_header, f"/staff/items/{item6_id}/remove",
        {"csrf_token": staff_csrf, "version": ver6})
    return ("rm", code, loc, sc)


with ThreadPoolExecutor(max_workers=2) as ex:
    m1 = ex.submit(mixed_task, "rj")
    m2 = ex.submit(mixed_task, "rm")
    mr1, mr2 = m1.result(), m2.result()


def flash_from(set_cookies):
    """从 303 响应各自的 Set-Cookie 中解出一次性 flash（不经过共享 Cookie 罐）。"""
    import base64 as _b64
    import json as _json
    for header in set_cookies:
        if not header.startswith("lf_flash="):
            continue
        raw = header.split(";", 1)[0][len("lf_flash="):]
        try:
            kind, msg = _json.loads(_b64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            return kind, msg
        except Exception:
            return "", ""
    return "", ""


rm_outcome = next(x for x in (mr1, mr2) if x[0] == "rm")
rj_outcome = next(x for x in (mr1, mr2) if x[0] == "rj")
rm_flash = flash_from(rm_outcome[3])
rj_flash = flash_from(rj_outcome[3])
rm_won = "物品已撤下" in rm_flash[1]
rj_won = "改判完成" in rj_flash[1]
# 写事务串行 + 条件式原子守卫：屏障保证同时发起，HTTP 端可能近串行处理，
# 但无论如何恰一方成功、另一方收到错误 flash。
check("撤下/改判并发恰好一方成功",
      rm_won != rj_won,
      f"rmflash={rm_flash} rjflash={rj_flash}")
check("双方都不会同时成功（无双重状态变更）", not (rm_won and rj_won))

istat6 = db.execute("SELECT status FROM items WHERE id=?", (item6_id,)).fetchone()[0]
nsel6 = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                   (item6_id,)).fetchone()[0]
nnew6 = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL",
    (item6_id,)).fetchone()[0]
ncorr6 = db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?",
                    (item6_id,)).fetchone()[0]
if rm_won:
    check("撤下方成功：物品已撤下", istat6 == "removed")
    check("撤下方成功：改判方收到错误提示", rj_flash[0] == "err")
    check("撤下方成功：仍只有1位获选人、1枚有效码", nsel6 == 1 and nnew6 == 1)
    check("撤下方成功：改判未留痕", ncorr6 == 0, str(ncorr6))
else:
    check("改判方成功：物品仍保管中（撤下方未实际生效）", istat6 == "kept", istat6)
    check("改判方成功：恰好1条留痕", ncorr6 == 1, str(ncorr6))
    check("改判方成功：撤下方收到版本/状态冲突错误", rm_flash[0] == "err", str(rm_flash))
    check("改判方成功：目标获选且全物品1枚有效码", nsel6 == 1 and nnew6 == 1)
    # 工作人员刷新确认后可以再撤（冲突不会永久阻塞正常撤下流程）
    ver_fresh = db.execute("SELECT version FROM items WHERE id=?",
                           (item6_id,)).fetchone()[0]
    st, _, loc = req(anon, "POST", f"/staff/items/{item6_id}/remove",
                     form={"csrf_token": staff_csrf, "version": ver_fresh}, raw=True)
    check("冲突后凭新版本可正常撤下", st == 303 and loc == f"/staff/items/{item6_id}")
    # 清掉成功 flash，再确认最终态
    req(anon, "GET", f"/staff/items/{item6_id}")
    istat6 = db.execute("SELECT status FROM items WHERE id=?",
                        (item6_id,)).fetchone()[0]
check("并发结局后物品为撤下态", istat6 == "removed")

# 旧版本号撤下会被明确拒绝（即使无并发，页面过期也不改状态）
st, _, loc = req(anon, "POST", f"/staff/items/{item6_id}/remove",
                 form={"csrf_token": staff_csrf, "version": ver_before_select}, raw=True)
check("过期版本号撤下被拒", st == 303 and loc == f"/staff/items/{item6_id}")
req(anon, "GET", f"/staff/items/{item6_id}")  # 清除错误 flash，避免污染后续页面断言

# 改判与重发并发（同向）：原获选人的码不得被重发成新码，目标拿到唯一新码
PUB7 = "公开信息7-银色手表"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": PUB7,
                     "private_features": "私密特征7-表背划痕"}, raw=True)
st, body = req(anon, "GET", "/staff")
item7_id = max(int(x) for x in re.findall(r'/staff/items/(\d+)', body))
for i in range(2):
    _, v = client()
    req(v, "POST", f"/items/{item7_id}/claims",
        form={"answer": f"表答案{i}", "contact": f"w{i}"})
r7 = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id",
                               (item7_id,)).fetchall()]
s7, t7 = r7
req(anon, "POST", f"/staff/claims/{s7}/select", form={"csrf_token": staff_csrf}, raw=True)
ver7 = db.execute("SELECT version FROM items WHERE id=?",
                  (item7_id,)).fetchone()[0]
barrier5 = threading.Barrier(2)


def mixed2(kind):
    barrier5.wait()
    if kind == "rj":
        return ("rj",) + raw_with_cookie(
            cookie_header, f"/staff/claims/{s7}/rejudge",
            {"csrf_token": staff_csrf, "to_claim_id": str(t7),
             "reason": "改判与重发并发", "item_id": str(item7_id),
             "version": ver7})
    return ("rs",) + raw_with_cookie(
        cookie_header, f"/staff/claims/{s7}/resend", {"csrf_token": staff_csrf})


with ThreadPoolExecutor(max_workers=2) as ex:
    x1 = ex.submit(mixed2, "rj")
    x2 = ex.submit(mixed2, "rs")
    xr1, xr2 = x1.result(), x2.result()

nsel7 = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                   (item7_id,)).fetchone()[0]
codes7 = db.execute(
    "SELECT c.id AS cid, COUNT(*) AS n FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL"
    " GROUP BY c.id", (item7_id,)).fetchall()
check("改判/重发并发后仍1位获选人", nsel7 == 1)
check("唯一有效码只属于新获选人(目标)",
      len(codes7) == 1 and codes7[0][0] == t7 and codes7[0][1] == 1,
      str(codes7))

# --------------------------------------------------------------- 11 访客撤回认领
print("== 访客撤回认领 ==")


def setup_item(pub, priv, answers):
    """登记物品 + 提交若干申请，返回 (item_id, [(claim_id, token)], )。"""
    st, _, _ = req(anon, "POST", "/staff/items",
                   form={"csrf_token": staff_csrf, "public_description": pub,
                         "private_features": priv}, raw=True)
    assert st == 303
    st, body = req(anon, "GET", "/staff")
    iid = max(int(x) for x in re.findall(r'/staff/items/(\d+)', body))
    out = []
    for ans in answers:
        _, v = client()
        st, b = req(v, "POST", f"/items/{iid}/claims",
                    form={"answer": ans, "contact": ans + "-contact"})
        assert st == 200 and "查询凭证" in b
        tok = re.search(r'<div class="tokenbox">([^<]+)</div>', b).group(1).strip()
        out.append(tok)
    st, body = req(anon, "GET", f"/staff/items/{iid}")
    cids = [int(x) for x in re.findall(r'/staff/claims/(\d+)', body)]
    return iid, list(zip(cids, out))


def withdraw_raw(tok):
    """无 Cookie、无 CSRF（访客凭凭证）：原始 POST，返回 (status, location, setcookies)。"""
    data = urllib.parse.urlencode({"token": tok}).encode()
    r = urllib.request.Request(BASE + "/withdraw", data=data, method="POST")
    try:
        resp = urllib.request.build_opener(NoRedirect).open(r)
        return resp.status, resp.headers.get("Location", ""), resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", ""), e.headers.get_all("Set-Cookie") or []


# ---- 11a 待核对申请撤回：仅改变本人状态 ----
i8, c8 = setup_item("公开信息8-蓝色笔记本", "私密特征8-扉页签名W", ["撤回答案甲", "撤回答案乙", "撤回答案丙"])
(w8a, tk8a), (w8b, tk8b), (w8c, tk8c) = c8
_, v = client()
st, body = req(v, "GET", f"/query?token={urllib.parse.quote(tk8a)}")
check("待核对申请查询页有撤回入口", 'href="/withdraw/confirm?token=' in body)

st, body = req(v, "GET", f"/withdraw/confirm?token={urllib.parse.quote(tk8a)}")
check("撤回确认页可打开", st == 200 and "确认撤回认领" in body and "撤回答案甲" not in body)
check("确认页不泄露联系方式", "撤回答案甲-contact" not in body)
st, body = req(v, "GET", f"/withdraw/confirm?token=not-a-real-token")
check("错误凭证打开确认页回查询表单", st == 200 and "凭证不正确" in body)

code, loc, sc = withdraw_raw(tk8a)
check("待核对申请撤回成功(303到查询页)", code == 303 and loc.startswith("/query?token="), str((code, loc)))
fl = flash_from(sc)
check("撤回成功 flash 为 ok", fl[0] == "ok", str(fl))

rows8 = dict(db.execute("SELECT id, status FROM claims WHERE item_id=?", (i8,)).fetchall())
check("撤回后本人为 withdrawn", rows8[w8a] == "withdrawn", str(rows8))
check("其余申请仍为 pending", rows8[w8b] == "pending" and rows8[w8c] == "pending", str(rows8))
wtime = db.execute("SELECT withdrawn_at FROM claims WHERE id=?", (w8a,)).fetchone()[0]
check("撤回时间已记录", bool(wtime))

st, body = req(v, "GET", f"/query?token={urllib.parse.quote(tk8a)}")
check("撤回后凭证仍可查询", "已撤回" in body)
check("撤回后显示撤回时间", "撤回时间" in body)
check("撤回后无交还码、无撤回入口", 'class="codebox"' not in body and "/withdraw/confirm" not in body)

# 重复撤回：幂等，不重复写入
code2, _, sc2 = withdraw_raw(tk8a)
fl2 = flash_from(sc2)
check("重复撤回返回303且提示此前已撤回", code2 == 303 and fl2[0] == "info" and "已撤回" in fl2[1], str(fl2))
check("重复撤回不更新撤回时间",
      db.execute("SELECT withdrawn_at FROM claims WHERE id=?", (w8a,)).fetchone()[0] == wtime)
check("重复撤回不新增交还码",
      db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?", (w8a,)).fetchone()[0] == 0)

# 工作人员侧可见撤回状态与时间
st, body = req(anon, "GET", f"/staff/items/{i8}")
check("工作人员物品页显示已撤回徽标", "已撤回" in body)
st, body = req(anon, "GET", f"/staff/claims/{w8a}")
check("工作人员申请页可查看撤回时间", "撤回时间" in body and "主动撤回" in body)
check("撤回申请页无选定/改判/重发按钮",
      f"/staff/claims/{w8a}/select" not in body and f"/staff/claims/{w8a}/rejudge" not in body
      and f"/staff/claims/{w8a}/resend" not in body)
st, _, _ = req(anon, "POST", f"/staff/claims/{w8a}/select",
               form={"csrf_token": staff_csrf}, raw=True)
check("工作人员不能选定已撤回申请", st == 303)
req(anon, "GET", f"/staff/claims/{w8a}")  # 清 flash
check("撤回选定后仍无获选人",
      db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                 (i8,)).fetchone()[0] == 0)

# 待核对撤回不影响物品继续核对/接受申请：选定乙成功
st, _, _ = req(anon, "POST", f"/staff/claims/{w8b}/select",
               form={"csrf_token": staff_csrf}, raw=True)
check("撤回不阻塞工作人员选定其他申请", st == 303)
check("已撤回申请未因他人获选改变状态",
      db.execute("SELECT status FROM claims WHERE id=?", (w8a,)).fetchone()[0] == "withdrawn")

# ---- 11b 未获选申请撤回：仅改变本人状态，不影响获选人 ----
st, q8b = req(v, "GET", f"/query?token={urllib.parse.quote(tk8c)}")
check("丙看到未获选", "未获选" in q8b)
code, _, _ = withdraw_raw(tk8c)
check("未获选申请可撤回", code == 303)
check("未获选撤回后物品仍有获选人",
      db.execute("SELECT status FROM claims WHERE id=?", (w8b,)).fetchone()[0] == "selected")
check("丙撤回后为 withdrawn",
      db.execute("SELECT status FROM claims WHERE id=?", (w8c,)).fetchone()[0] == "withdrawn")

# ---- 11c 获选申请撤回（物品保管中）：作废码 + 恢复未获选 + 重新开放 ----
i9, c9 = setup_item("公开信息9-灰色手套", "私密特征9-内衬补丁", ["手套甲", "手套乙", "手套丙"])
(g9a, tg9a), (g9b, tg9b), (g9c, tg9c) = c9
req(anon, "POST", f"/staff/claims/{g9a}/select", form={"csrf_token": staff_csrf}, raw=True)
ver9_before = db.execute("SELECT version FROM items WHERE id=?", (i9,)).fetchone()[0]
_, v = client()
st, body = req(v, "GET", f"/query?token={urllib.parse.quote(tg9a)}")
m = re.search(r'<div class="codebox">(\d{8})</div>', body)
code9a = m.group(1) if m else ""
check("获选人撤回前持有码", bool(code9a))

code, loc, sc = withdraw_raw(tg9a)
fl = flash_from(sc)
check("获选申请撤回成功", code == 303 and fl[0] == "ok" and "交还码已立即作废" in fl[1]
      and "恢复待核对" in fl[1], str(fl))
rows9 = dict(db.execute("SELECT id, status FROM claims WHERE item_id=?", (i9,)).fetchall())
check("获选人变 withdrawn", rows9[g9a] == "withdrawn", str(rows9))
check("其余未获选申请恢复 pending", rows9[g9b] == "pending" and rows9[g9c] == "pending", str(rows9))
check("decided_at 已清空（恢复待核对）",
      db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='pending' AND decided_at IS NOT NULL",
                 (i9,)).fetchone()[0] == 0)
ninv = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND invalidated_at IS NOT NULL",
                  (g9a,)).fetchone()[0]
check("获选人全部未核销码已作废", ninv == 1)
check("无有效码残留",
      db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND consumed_at IS NULL"
                 " AND invalidated_at IS NULL", (g9a,)).fetchone()[0] == 0)
ver9_after = db.execute("SELECT version FROM items WHERE id=?", (i9,)).fetchone()[0]
check("获选撤回不推进物品版本号（仅选定时推进）", ver9_after == ver9_before)
istat9 = db.execute("SELECT status FROM items WHERE id=?", (i9,)).fetchone()[0]
check("物品仍保管中", istat9 == "kept")

st, body = req(v, "GET", f"/query?token={urllib.parse.quote(tg9a)}")
check("撤回后查询页不显示旧码", 'class="codebox"' not in body and code9a not in body)
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code9a})
check("旧码核销被拒（已失效）", "已失效" in body)
check("旧码被拒后物品仍保管中",
      db.execute("SELECT status FROM items WHERE id=?", (i9,)).fetchone()[0] == "kept")

# 重新接受新申请
_, vn = client()
st, body = req(vn, "POST", f"/items/{i9}/claims", form={"answer": "新手套丁", "contact": "d-contact"})
check("撤回后物品重新接受新申请", st == 200 and "查询凭证" in body)

# 工作人员不能对已撤回的原获选人重发
st, _, _ = req(anon, "POST", f"/staff/claims/{g9a}/resend",
               form={"csrf_token": staff_csrf}, raw=True)
check("原获选人撤回后重发被拒", st == 303)
st, body = req(anon, "GET", f"/staff/claims/{g9a}")
check("撤回页提示已主动撤回", "主动撤回" in body)

# 工作人员可选恢复待核对的乙
st, _, _ = req(anon, "POST", f"/staff/claims/{g9b}/select",
               form={"csrf_token": staff_csrf}, raw=True)
check("恢复待核对后可重新选定", st == 303)
sel9 = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? AND status='selected'",
                                 (i9,)).fetchall()]
check("重新选定后仅乙获选", sel9 == [g9b], str(sel9))
# 已主动撤回的甲不参与批量拒绝、保持 withdrawn
check("已撤回的甲不恢复、仍 withdrawn",
      db.execute("SELECT status FROM claims WHERE id=?", (g9a,)).fetchone()[0] == "withdrawn")

# ---- 11d 获选申请撤回（物品已撤下）：只作废码，不恢复、不开放 ----
i10, c10 = setup_item("公开信息10-红色围巾", "私密特征10-流苏磨损", ["围巾甲", "围巾乙"])
(h10a, th10a), (h10b, th10b) = c10
req(anon, "POST", f"/staff/claims/{h10a}/select", form={"csrf_token": staff_csrf}, raw=True)
req(anon, "POST", f"/staff/items/{i10}/remove",
    form={"csrf_token": staff_csrf, "version": db.execute(
        "SELECT version FROM items WHERE id=?", (i10,)).fetchone()[0]}, raw=True)
check("物品10已撤下", db.execute("SELECT status FROM items WHERE id=?", (i10,)).fetchone()[0] == "removed")
# 撤下时乙（rejected）保持 rejected；甲仍 selected（撤下不改 selected）
code, _, sc = withdraw_raw(th10a)
fl = flash_from(sc)
check("撤下物品的获选申请可撤回", code == 303 and fl[0] == "ok")
check("撤下撤回提示不再开放申请", "已撤下" in fl[1], str(fl))
check("甲撤回为 withdrawn",
      db.execute("SELECT status FROM claims WHERE id=?", (h10a,)).fetchone()[0] == "withdrawn")
check("乙不恢复（仍 rejected）",
      db.execute("SELECT status FROM claims WHERE id=?", (h10b,)).fetchone()[0] == "rejected")
check("甲的码已作废",
      db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND invalidated_at IS NOT NULL",
                 (h10a,)).fetchone()[0] == 1)
_, vx = client()
st, body = req(vx, "POST", f"/items/{i10}/claims", form={"answer": "x", "contact": "y"})
check("撤下物品撤回后仍不接受新申请", st == 400 and "撤下或已交还" in body)
# 乙撤回仅改本人状态
code, _, _ = withdraw_raw(th10b)
check("撤下物品的未获选申请也可撤回", code == 303)
check("乙撤回为 withdrawn、物品仍 removed",
      db.execute("SELECT status FROM claims WHERE id=?", (h10b,)).fetchone()[0] == "withdrawn"
      and db.execute("SELECT status FROM items WHERE id=?", (i10,)).fetchone()[0] == "removed")

# ---- 11e 物品已交还：拒绝撤回任何申请 ----
i11, c11 = setup_item("公开信息11-黑色墨镜", "私密特征11-镜盒贴纸", ["墨镜甲", "墨镜乙"])
(k11a, tk11a), (k11b, tk11b) = c11
req(anon, "POST", f"/staff/claims/{k11a}/select", form={"csrf_token": staff_csrf}, raw=True)
st, body = req(anon, "GET", f"/staff/claims/{k11a}")
code11 = re.search(r'<div class="codebox">(\d{8})</div>', body).group(1)
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, _, _ = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code11}, raw=True)
check("物品11已交还", db.execute("SELECT status FROM items WHERE id=?", (i11,)).fetchone()[0] == "returned")

for tk, cid, who in ((tk11a, k11a, "获选"), (tk11b, k11b, "未获选")):
    code, loc, sc = withdraw_raw(tk)
    fl = flash_from(sc)
    check(f"已交还物品拒绝撤回{who}申请", code == 303 and fl[0] == "err" and "已交还" in fl[1], str(fl))
    stt = db.execute("SELECT status FROM claims WHERE id=?", (cid,)).fetchone()[0]
    check(f"{who}申请状态未被改动", stt in ("selected", "rejected"), stt)
    _, v = client()
    st, body = req(v, "GET", f"/query?token={urllib.parse.quote(tk)}")
    check(f"已交还后{who}查询页无撤回入口", "/withdraw/confirm" not in body)
    st, body = req(v, "GET", f"/withdraw/confirm?token={urllib.parse.quote(tk)}")
    check(f"已交还后直接访问确认页被拦回查询", "已交还" in body or "完成交还" in body)

# ---- 11f 撤回与选定并发：状态裁决一致、无双获选/旧码/部分更新 ----
# 两种合法串行历史：
#   A) 选定先提交、撤回后提交：撤回的是「已获选申请」（合法，作废旧码、
#      恢复其他申请）——双方都得到各自操作的明确结果，终态一致；
#   B) 撤回先提交、选定后提交：选定对 withdrawn 申请条件不匹配而失败，
#      撤回方成功。两种历史下都不得出现双获选、可用旧码或部分更新。
i12, c12 = setup_item("公开信息12-绿色腰包", "私密特征12-内层标签", ["腰包甲", "腰包乙"])
(x12a, tx12a), (x12b, tx12b) = c12
barrierW = threading.Barrier(2)


def w_select():
    barrierW.wait()
    return raw_with_cookie_full(cookie_header, f"/staff/claims/{x12a}/select",
                                {"csrf_token": staff_csrf})


def w_withdraw():
    barrierW.wait()
    return withdraw_raw(tx12a)


with ThreadPoolExecutor(max_workers=2) as ex:
    o1 = ex.submit(w_select)
    o2 = ex.submit(w_withdraw)
    rs1, rs2 = o1.result(), o2.result()

sel_flash = flash_from(rs1[2])
wd_flash = flash_from(rs2[2])
sel_ok = sel_flash[0] == "ok"
wd_ok = wd_flash[0] == "ok"
status_a = db.execute("SELECT status FROM claims WHERE id=?", (x12a,)).fetchone()[0]
nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                  (i12,)).fetchall()[0][0]
nactive = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL",
    (i12,)).fetchone()[0]

if wd_ok and not sel_ok:
    # 历史 B：撤回先生效，选定被明确拒绝
    check("历史B-撤回先成功、选定被拒", True)
    check("历史B-甲为 withdrawn", status_a == "withdrawn", status_a)
    check("历史B-选定方收到明确错误提示", "已被申请人撤回" in sel_flash[1], str(sel_flash))
    check("历史B-无获选人", nsel == 0 and nactive == 0)
    st, body = req(anon, "GET", f"/staff/claims/{x12a}")
    check("历史B-申请页提示已撤回", "已撤回" in body)
else:
    # 历史 A：选定先生效，随后对已获选申请的撤回合法成功
    check("历史A-选定成功", sel_ok, str(sel_flash))
    check("历史A-撤回成功", wd_ok, str(wd_flash))
    check("历史A-甲最终 withdrawn", status_a == "withdrawn", status_a)
    check("历史A-无双获选", nsel == 0, f"nsel={nsel}")
    check("历史A-旧码已作废、无可用旧码", nactive == 0, f"nactive={nactive}")
    check("历史A-乙恢复待核对",
          db.execute("SELECT status FROM claims WHERE id=?", (x12b,)).fetchone()[0] == "pending")
    # 工作人员再选定乙可正常完成（终态可继续推进，非部分更新卡死）
    st, _, _ = req(anon, "POST", f"/staff/claims/{x12b}/select",
                   form={"csrf_token": staff_csrf}, raw=True)
    check("历史A-终态可继续选定其他申请", st == 303)
    nsel2 = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                       (i12,)).fetchone()[0]
    check("历史A-重新选定后恰好1位获选人", nsel2 == 1)
    check("历史A-甲保持 withdrawn 不复活",
          db.execute("SELECT status FROM claims WHERE id=?", (x12a,)).fetchone()[0] == "withdrawn")

# ---- 11g 撤回与核码并发：不得出现「交还又撤回」/旧码可用 ----
i13, c13 = setup_item("公开信息13-黄色雨披", "私密特征13-帽绳记号", ["雨披甲"])
(y13a, ty13a), = c13
req(anon, "POST", f"/staff/claims/{y13a}/select", form={"csrf_token": staff_csrf}, raw=True)
code13 = db.execute("SELECT code_hash FROM handover_codes WHERE claim_id=?", (y13a,)).fetchone()[0]
from lostfound.security import decrypt_field as _df, load_or_create_secret as _locs
_secret = _locs("/tmp/lfdata/secret.key")
_enc = db.execute("SELECT code_enc FROM handover_codes WHERE claim_id=?", (y13a,)).fetchone()[0]
plain13 = _df(_secret, _enc)
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
barrierV = threading.Barrier(2)


def v_verify():
    barrierV.wait()
    return raw_with_cookie(cookie_header, "/staff/verify", {"csrf_token": vcsrf, "code": plain13})


def v_withdraw():
    barrierV.wait()
    return withdraw_raw(ty13a)


with ThreadPoolExecutor(max_workers=2) as ex:
    p1 = ex.submit(v_verify)
    p2 = ex.submit(v_withdraw)
    vr, wr = p1.result(), p2.result()

verify_won = vr[0] == 303 and vr[1] == f"/staff/items/{i13}"
withdraw_won = wr[0] == 303 and wr[1].startswith("/query?token=") and flash_from(wr[2])[0] == "ok"
check("核码/撤回并发恰好一方生效", verify_won != withdraw_won, f"verify={vr} withdraw={wr}")
istat13 = db.execute("SELECT status FROM items WHERE id=?", (i13,)).fetchone()[0]
stt13 = db.execute("SELECT status FROM claims WHERE id=?", (y13a,)).fetchone()[0]
nconsumed = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND consumed_at IS NOT NULL",
                       (y13a,)).fetchone()[0]
ninvalid = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND invalidated_at IS NOT NULL",
                      (y13a,)).fetchone()[0]
if verify_won:
    check("核码胜：物品已交还", istat13 == "returned" and stt13 == "selected")
    check("核码胜：码恰好核销1枚", nconsumed == 1 and ninvalid == 0)
else:
    check("撤回胜：物品仍保管中、申请已撤回", istat13 == "kept" and stt13 == "withdrawn")
    check("撤回胜：码已作废且未核销", ninvalid == 1 and nconsumed == 0)
    # 再核旧码必被拒，旧码不可用
    st, body = req(anon, "GET", "/staff/verify")
    vcsrf2 = csrf(body)
    st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf2, "code": plain13})
    check("撤回胜后旧码核销被拒", "已失效" in body)
    check("撤回胜后物品仍未交还",
          db.execute("SELECT status FROM items WHERE id=?", (i13,)).fetchone()[0] == "kept")

# ---- 11h 撤回与改判并发：无双获选、无旧码可用、无部分更新 ----
# 两种合法串行历史：
#   A) 撤回先提交：甲 withdrawn、乙恢复 pending，改判随后被明确拒绝；
#   B) 改判先提交：乙 selected 并获得唯一新码、甲变 rejected 且旧码作废；
#      甲随后的撤回作为「未获选撤回」合法成功，仅改本人状态，不影响新获选人。
i14, c14 = setup_item("公开信息14-紫色卡包", "私密特征14-夹层硬币", ["卡包甲", "卡包乙"])
(z14a, tz14a), (z14b, tz14b) = c14
req(anon, "POST", f"/staff/claims/{z14a}/select", form={"csrf_token": staff_csrf}, raw=True)
ver14 = db.execute("SELECT version FROM items WHERE id=?", (i14,)).fetchone()[0]
_enc = db.execute("SELECT code_enc FROM handover_codes WHERE claim_id=?", (z14a,)).fetchone()[0]
plain14 = _df(_secret, _enc)
barrierR = threading.Barrier(2)


def r_rejudge():
    barrierR.wait()
    return raw_with_cookie_full(cookie_header, f"/staff/claims/{z14a}/rejudge",
                                {"csrf_token": staff_csrf, "to_claim_id": str(z14b),
                                 "reason": "撤回改判并发", "item_id": str(i14), "version": ver14})


def r_withdraw():
    barrierR.wait()
    return withdraw_raw(tz14a)


with ThreadPoolExecutor(max_workers=2) as ex:
    q1 = ex.submit(r_rejudge)
    q2 = ex.submit(r_withdraw)
    jr, wr2 = q1.result(), q2.result()

rj_flash = flash_from(jr[2])
wd_flash = flash_from(wr2[2])
rj_ok = jr[0] == 303 and jr[1] == f"/staff/items/{i14}" and rj_flash[0] == "ok"
wd_ok = wr2[0] == 303 and wr2[1].startswith("/query?token=") and wd_flash[0] == "ok"
st_a = db.execute("SELECT status FROM claims WHERE id=?", (z14a,)).fetchone()[0]
st_b = db.execute("SELECT status FROM claims WHERE id=?", (z14b,)).fetchone()[0]
ncorr14 = db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?", (i14,)).fetchone()[0]
nsel14 = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                    (i14,)).fetchone()[0]
nactive14 = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL",
    (i14,)).fetchone()[0]

if ncorr14 == 1:
    # 历史 B：改判先生效
    check("历史B-改判成功", rj_ok, str(rj_flash))
    check("历史B-乙获选、乙恰有1枚有效码", st_b == "selected" and nsel14 == 1 and nactive14 == 1,
          f"{st_b},{nsel14},{nactive14}")
    check("历史B-唯一有效码属于乙",
          db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?"
                     " AND consumed_at IS NULL AND invalidated_at IS NULL", (z14b,)).fetchone()[0] == 1)
    check("历史B-甲不再获选（rejected 或随后已撤回）", st_a in ("rejected", "withdrawn"), st_a)
    if st_a == "withdrawn":
        check("历史B-甲随后撤回也成功（未获选撤回不影响新获选人）", wd_ok, str(wd_flash))
    else:
        check("历史B-改判后撤回未必排上，但改判方成功即可", True)
else:
    # 历史 A：撤回先生效，改判被明确拒绝
    check("历史A-撤回成功、改判被拒", wd_ok and not rj_ok and rj_flash[0] == "err",
          f"rj={rj_flash} wd={wd_flash}")
    check("历史A-甲 withdrawn、乙恢复 pending", st_a == "withdrawn" and st_b == "pending",
          f"{st_a},{st_b}")
    check("历史A-无获选人、无有效码、不留痕", nsel14 == 0 and nactive14 == 0 and ncorr14 == 0)

# 甲的旧码在两种结局下都不可用（改判或撤回都已将其作废：invalidated 行数为 1）
check("甲的旧码均已作废",
      db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?"
                 " AND invalidated_at IS NOT NULL AND consumed_at IS NULL",
                 (z14a,)).fetchone()[0] == 1)
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": plain14})
check("并发结局后甲旧码核销被拒", "已失效" in body)
check("并发结局后无双获选", nsel14 <= 1)

# ---- 11i 确定性串行：改判后原获选人（未获选）撤回，不影响新获选人 ----
i15, c15 = setup_item("公开信息15-橙色U盘", "私密特征15-标签姓名LH", ["U盘甲", "U盘乙"])
(u15a, tu15a), (u15b, tu15b) = c15
req(anon, "POST", f"/staff/claims/{u15a}/select", form={"csrf_token": staff_csrf}, raw=True)
ver15 = db.execute("SELECT version FROM items WHERE id=?", (i15,)).fetchone()[0]
st, _, _ = req(anon, "POST", f"/staff/claims/{u15a}/rejudge",
               form={"csrf_token": staff_csrf, "to_claim_id": str(u15b),
                     "reason": "改判后原申请人再撤回", "item_id": str(i15), "version": ver15}, raw=True)
check("串行-先改判甲->乙成功", st == 303)
code, _, _ = withdraw_raw(tu15a)
check("串行-改判后甲作为未获选申请撤回成功", code == 303)
check("串行-甲 withdrawn",
      db.execute("SELECT status FROM claims WHERE id=?", (u15a,)).fetchone()[0] == "withdrawn")
check("串行-乙仍 selected 不受影响",
      db.execute("SELECT status FROM claims WHERE id=?", (u15b,)).fetchone()[0] == "selected")
check("串行-乙的有效码仍在",
      db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?"
                 " AND consumed_at IS NULL AND invalidated_at IS NULL", (u15b,)).fetchone()[0] == 1)
check("串行-未把其他申请恢复（乙是获选人，甲是主动撤回，无 pending 恢复对象）",
      db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='pending'",
                 (i15,)).fetchone()[0] == 0)
_, v = client()
st, body = req(v, "GET", f"/query?token={urllib.parse.quote(tu15b)}")
check("串行-乙查询页仍显示已获选与交还码", "已获选" in body and 'class="codebox"' in body)

# ---- 11j 获选撤回与撤下并发：不恢复申请、不开放提交 / 恢复只按最新状态 ----
i16, c16 = setup_item("公开信息16-米色手套", "私密特征16-腕口刺绣星", ["手套甲j", "手套乙j"])
(x16a, tx16a), (x16b, tx16b) = c16
req(anon, "POST", f"/staff/claims/{x16a}/select", form={"csrf_token": staff_csrf}, raw=True)
ver16 = db.execute("SELECT version FROM items WHERE id=?", (i16,)).fetchone()[0]
barrierX = threading.Barrier(2)


def x_remove():
    barrierX.wait()
    return raw_with_cookie_full(cookie_header, f"/staff/items/{i16}/remove",
                                {"csrf_token": staff_csrf, "version": ver16})


def x_withdraw():
    barrierX.wait()
    return withdraw_raw(tx16a)


with ThreadPoolExecutor(max_workers=2) as ex:
    q1 = ex.submit(x_remove)
    q2 = ex.submit(x_withdraw)
    rm3, wd3 = q1.result(), q2.result()

rm_fl = flash_from(rm3[2])
wd_fl3 = flash_from(wd3[2])
istat16 = db.execute("SELECT status FROM items WHERE id=?", (i16,)).fetchone()[0]
st_a16 = db.execute("SELECT status FROM claims WHERE id=?", (x16a,)).fetchone()[0]
st_b16 = db.execute("SELECT status FROM claims WHERE id=?", (x16b,)).fetchone()[0]
check("撤下/撤回并发：撤下结果明确（成功或被拒）", rm3[0] == 303, str(rm3))
check("撤下/撤回并发：撤回结果明确（成功或被拒）", wd3[0] == 303, str(wd3))

if istat16 == "removed":
    # 撤下在两种串行顺序下都会生效（撤回不推进 version，不影响撤下乐观锁）：
    #   撤下先：乙随撤下被拒，甲随后按「已撤下物品」撤回（仅作废码）；
    #   撤回先：甲撤回、乙恢复 pending，撤下随后生效再把乙标记 rejected。
    # 终态确定一致：物品 removed、甲 withdrawn、乙 rejected、不开放提交。
    check("并发结局：物品已撤下", True)
    check("甲已撤回", st_a16 == "withdrawn", st_a16)
    check("撤下后不恢复申请（乙为 rejected，不是 pending）", st_b16 == "rejected", st_b16)
    check("撤下后不开放新申请",
          db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='pending'",
                     (i16,)).fetchone()[0] == 0)
    check("甲的交还码已作废",
          db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?"
                     " AND invalidated_at IS NOT NULL AND consumed_at IS NULL",
                     (x16a,)).fetchone()[0] == 1)
else:
    check("不应出现物品未撤下的结局（撤下守卫不受撤回影响）", False, istat16)

# 无可用旧码、无双获选（两种结局共同不变量）
check("11j 无有效交还码",
      db.execute("SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
                 " WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL",
                 (i16,)).fetchone()[0] == 0)
check("11j selected 至多 1（撤下后应为 0）",
      db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                 (i16,)).fetchone()[0] <= 1)

# 若物品仍 kept（撤回先生效），最终由工作人员再撤下，得到确定的 removed 终态
if istat16 != "removed":
    ver16b = db.execute("SELECT version FROM items WHERE id=?", (i16,)).fetchone()[0]
    req(anon, "POST", f"/staff/items/{i16}/remove",
        form={"csrf_token": staff_csrf, "version": ver16b}, raw=True)
    istat16 = db.execute("SELECT status FROM items WHERE id=?", (i16,)).fetchone()[0]
check("11j 最终物品为撤下态", istat16 == "removed", istat16)
_, vx16 = client()
st, body = req(vx16, "POST", f"/items/{i16}/claims", form={"answer": "新", "contact": "n"})
check("11j 撤下后不接受新申请", st == 400 and "撤下或已交还" in body)

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
