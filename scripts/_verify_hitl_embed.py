"""验证：① embedding 检索提升跨会话命中 ② LangGraph 路径的 interrupt 人工审批（临时脚本）。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8901"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def run(task, runtime, max_steps=8, approval="auto", decide=None, timeout_s=900):
    post("/api/run", {
        "task": task, "policy": "relevance", "budget": 800, "approval_mode": approval,
        "max_steps": max_steps, "auto_params": False, "chat": False, "runtime": runtime,
    })
    state = get("/api/state")
    deadline = time.time() + timeout_s
    saw_pending = False
    while time.time() < deadline and state.get("status") == "running":
        if state.get("pending") and not saw_pending:
            saw_pending = True
            print(f"    ⏸ 审批请求: {state['pending'].get('tool')} args={str(state['pending'].get('args'))[:60]}")
            if decide is not None:
                post("/api/approve", {"approved": decide})
                print(f"    → 已回复 {'批准' if decide else '拒绝'}")
        time.sleep(2)
        state = get("/api/state")
    return state, saw_pending


print("=== ① embedding 检索：跨会话命中 ===")
s1, _ = run("记住：我的项目代号是 ORION，报告放 reports 目录。", "handwritten", max_steps=6)
print(f"  轮1 status={s1.get('status')} answer={str(s1.get('answer') or '')[:50].replace(chr(10), ' ')}")

time.sleep(2)
s2, _ = run("我上一轮说的项目代号是什么？只回答代号。", "langgraph", max_steps=8)
hits = [h.get("text", "")[:40] for h in (s2.get("lg_retrieved") or [])]
print(f"  轮2 status={s2.get('status')} lg_mode={s2.get('lg_mode')}")
print(f"  注入记忆({len(hits)}): {hits}")
print(f"  回答: {str(s2.get('answer') or '')[:80].replace(chr(10), ' ')}")
print(f"  ✅ 命中 ORION: {'ORION' in str(s2.get('answer') or '').upper() or any('ORION' in h.upper() for h in hits)}")

print("\n=== ② LangGraph 路径 interrupt 审批（先拒绝）===")
s3, saw3 = run("用 bash 列出当前目录文件", "langgraph", max_steps=8, approval="all", decide=False, timeout_s=600)
print(f"  status={s3.get('status')} 出现过审批请求={saw3}")
print(f"  事件流审批记录: {[(e.get('tool'), e.get('approved')) for e in (s3.get('trace') or []) if e.get('event') == 'approval']}")
print(f"  结果: {str(s3.get('answer') or '')[:90].replace(chr(10), ' ')}")

print("\n=== ③ LangGraph 路径 interrupt 审批（批准）===")
s4, saw4 = run("创建 hello_approve.txt，内容写 OK", "langgraph", max_steps=10, approval="all", decide=True, timeout_s=600)
print(f"  status={s4.get('status')} 出现过审批请求={saw4}")
print(f"  事件流审批记录: {[(e.get('tool'), e.get('approved')) for e in (s4.get('trace') or []) if e.get('event') == 'approval']}")
print(f"  结果: {str(s4.get('answer') or '')[:90].replace(chr(10), ' ')}")
