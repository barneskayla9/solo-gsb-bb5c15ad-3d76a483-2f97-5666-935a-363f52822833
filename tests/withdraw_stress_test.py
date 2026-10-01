"""撤回并发压力：获选申请撤回与重发/撤下/核码多轮竞争。

每轮必须满足：
  - 申请最终态要么 selected(物品交还) 要么 withdrawn(物品保管中)，绝不矛盾；
  - 若撤回：该申请名下无任何有效交还码，物品 kept，落选申请按物品状态恢复或保持；
  - 若核码成功：物品 returned、恰一枚码核销；
  - 全物品「有效码数 == 获选人数」，无双获选、无游离有效码、无部分更新。
前置：服务运行在 http://127.0.0.1:8181，工作人员口令 secret123，LF_DATA_DIR=/tmp/lfdata。
"""
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
ROUNDS = 15


class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def opener(cj=None):
    return urllib.request.build_opener(
        NR, urllib.request.HTTPCookieProcessor(cj or http.cookiejar.CookieJar()))


def go(op, method, path, form=None, cookie=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if cookie:
        r.add_header("Cookie", cookie)
    try:
        x = op.open(r)
        return x.status, x.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


# 登录
cj = http.cookiejar.CookieJar()
login = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
b = login.open(urllib.request.Request(BASE + "/staff/login")).read().decode()
tok = re.search(r'name="csrf_token" value="([^"]+)"', b).group(1)
go(login, "POST", "/staff/login", {"csrf_token": tok, "password": "secret123"})
b = login.open(urllib.request.Request(BASE + "/staff")).read().decode()
CSRF = re.search(r'name="csrf_token" value="([^"]+)"', b).group(1)
COOKIE = "; ".join(f"{c.name}={c.value}" for c in cj)

db = sqlite3.connect("/tmp/lfdata/lostfound.db")
fails = 0


def make_item():
    st, loc = go(opener(), "POST", "/staff/items",
                 {"csrf_token": CSRF, "public_description": "撤回压测物品", "private_features": "压测私密"},
                 cookie=COOKIE)
    assert st == 303, (st, loc)
    return db.execute("SELECT MAX(id) FROM items").fetchone()[0]


def make_claim(iid, answer, contact):
    cj2 = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj2))
    r = urllib.request.Request(BASE + f"/items/{iid}/claims",
                               data=urllib.parse.urlencode({"answer": answer, "contact": contact}).encode(),
                               method="POST")
    b = op.open(r).read().decode()
    assert "查询凭证" in b, b[:200]
    return re.search(r'<div class="tokenbox">([^<]+)</div>', b).group(1).strip()


for rnd in range(1, ROUNDS + 1):
    iid = make_item()
    t_sel = make_claim(iid, "获选人", "sel")
    t_l1 = make_claim(iid, "落选甲", "l1")
    t_l2 = make_claim(iid, "落选乙", "l2")
    cid = db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id", (iid,)).fetchall()
    s_id, l1_id, l2_id = [r[0] for r in cid]
    go(opener(), "POST", f"/staff/claims/{s_id}/select", {"csrf_token": CSRF}, cookie=COOKIE)
    ver = db.execute("SELECT version FROM items WHERE id=?", (iid,)).fetchone()[0]

    # 取当前获选人有效码明文（供核码竞争方使用）
    from lostfound.security import decrypt_field, load_or_create_secret
    secret = load_or_create_secret("/tmp/lfdata/secret.key")
    enc = db.execute("SELECT code_enc FROM handover_codes WHERE claim_id=? ORDER BY id DESC LIMIT 1",
                     (s_id,)).fetchone()[0]
    code = decrypt_field(secret, enc)

    barrier = threading.Barrier(4)

    def w_withdraw():
        barrier.wait()
        return ("wd",) + go(opener(), "POST", "/claims/withdraw", {"token": t_sel})

    def w_resend():
        barrier.wait()
        return ("rs",) + go(opener(), "POST", f"/staff/claims/{s_id}/resend",
                            {"csrf_token": CSRF}, cookie=COOKIE)

    def w_remove():
        barrier.wait()
        return ("rm",) + go(opener(), "POST", f"/staff/items/{iid}/remove",
                            {"csrf_token": CSRF, "version": ver}, cookie=COOKIE)

    def w_verify():
        barrier.wait()
        return ("vf",) + go(opener(), "POST", "/staff/verify",
                            {"csrf_token": CSRF, "code": code}, cookie=COOKIE)

    with ThreadPoolExecutor(max_workers=4) as ex:
        outs = [f.result() for f in [
            ex.submit(w_withdraw), ex.submit(w_resend),
            ex.submit(w_remove), ex.submit(w_verify)]]

    istat = db.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()[0]
    sstat = db.execute("SELECT status FROM claims WHERE id=?", (s_id,)).fetchone()[0]
    nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                      (iid,)).fetchone()[0]
    nactive = db.execute(
        "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id "
        "WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL",
        (iid,)).fetchone()[0]
    nconsumed = db.execute(
        "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id "
        "WHERE c.item_id=? AND hc.consumed_at IS NOT NULL", (iid,)).fetchone()[0]

    ok = True
    problems = []
    # 核码与撤回互斥：物品 returned ⇒ 申请 selected 且有一枚核销；否则申请 withdrawn
    if istat == "returned":
        if not (sstat == "selected" and nconsumed == 1):
            ok = False; problems.append(f"returned 但 sstat={sstat} consumed={nconsumed}")
    else:
        if sstat != "withdrawn":
            ok = False; problems.append(f"未交还但获选申请未撤回 sstat={sstat}")
        # 撤回者名下绝无有效码
        nact_s = db.execute(
            "SELECT COUNT(*) FROM handover_codes WHERE claim_id=? "
            "AND consumed_at IS NULL AND invalidated_at IS NULL", (s_id,)).fetchone()[0]
        if nact_s != 0:
            ok = False; problems.append(f"撤回者留有效码 {nact_s}")
        if istat == "removed":
            # 撤下结局：无获选人、落选不恢复
            if nsel != 0:
                ok = False; problems.append("撤下却有获选人")
        elif istat == "kept":
            # 撤回且保管：落选恢复 pending；无获选人
            if nsel != 0:
                ok = False; problems.append("保管中却仍有获选人")
    # 全局不变量：
    #   - 未核销有效码绝不能多于获选人数（无游离有效码、无双获选）；
    #   - 物品已交还时，获选人的码处于已核销态，故 active=0、consumed=1、nsel=1；
    #   - 物品未交还时，若有获选人则其持有恰一枚有效码（active == nsel）。
    invariant_ok = nactive <= nsel and nconsumed <= 1
    if istat == "returned":
        invariant_ok = invariant_ok and nsel == 1 and nactive == 0 and nconsumed == 1
    elif nsel == 1:
        invariant_ok = invariant_ok and nactive == 1 and nconsumed == 0
    else:
        invariant_ok = invariant_ok and nactive == 0
    if not invariant_ok:
        ok = False
        problems.append(f"不变量违背 active={nactive} nsel={nsel} consumed={nconsumed} istat={istat}")
    fails += 0 if ok else 1
    print(f"round {rnd:2d}: item={istat} sel_claim={sstat} nsel={nsel} "
          f"active={nactive} consumed={nconsumed} -> {'OK' if ok else 'BAD ' + '; '.join(problems)}")

print("\n全部通过" if fails == 0 else f"\n{fails} 轮失败")
raise SystemExit(1 if fails else 0)
