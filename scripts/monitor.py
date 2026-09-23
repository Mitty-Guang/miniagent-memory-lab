"""可视化监控面板：http://127.0.0.1:8899

读取 progress.log（+ results/*.json），实时展示预算扫描进度：
- 总体进度条 / 当前档位 / 运行状态
- budget × policy 的成功率矩阵（含已完成数）
- 最近进度流 / 速率 / 预计剩余时间

运行：
    .\\.venv\\Scripts\\python.exe monitor.py            # 默认端口 8899
    .\\.venv\\Scripts\\python.exe monitor.py --port 9000
停止：Ctrl+C（或结束该 python 进程）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import html
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROGRESS = HERE.parent / "progress.log"
RESULTS = HERE.parent / "results"

PLAN_BUDGETS = [300, 500, 800, 1200]
PLAN_POLICIES = ["recent", "relevance", "impact"]

LINE_RE = re.compile(
    r"^\[(?P<ts>\d{2}:\d{2}:\d{2})\]\s+policy=(?P<policy>\S+)\s+task=(?P<task>\S+)\s+"
    r"success=(?P<success>True|False)\s+(?P<rest>.*)$"
)


def plan_task_ids():
    from mini_agent.task_suite import TASKS

    return [t["id"] for t in TASKS]


def parse_progress():
    if not PROGRESS.exists():
        return []
    rows = []
    with open(PROGRESS, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            match = LINE_RE.match(line.strip())
            if not match:
                continue
            rest = match.group("rest")
            seconds = None
            sec_match = re.search(r"seconds=([\d.]+)", rest)
            if sec_match:
                seconds = float(sec_match.group(1))
            rows.append(
                {
                    "ts": match.group("ts"),
                    "policy": match.group("policy"),
                    "task": match.group("task"),
                    "success": match.group("success") == "True",
                    "seconds": seconds,
                }
            )
    return rows


def sweep_rows(rows, task_ids):
    """定位本轮扫描的起点：最后一次 (policy=recent, 第一个任务)。"""
    first_task = task_ids[0]
    start = None
    for index, row in enumerate(rows):
        if row["policy"] == "recent" and row["task"] == first_task:
            start = index
    if start is None:
        return []
    return [r for r in rows[start:] if r["policy"] in PLAN_POLICIES]


def build_matrix(rows, task_ids):
    total_per_cell = len(task_ids)
    matrix = {}
    for budget in PLAN_BUDGETS:
        matrix[budget] = {}
        for policy in PLAN_POLICIES:
            matrix[budget][policy] = {"done": 0, "success": 0, "total": total_per_cell}
    # 计划顺序：budget → policy → 每个任务
    index = 0
    for budget in PLAN_BUDGETS:
        for policy in PLAN_POLICIES:
            for _task in task_ids:
                if index >= len(rows):
                    return matrix
                row = rows[index]
                cell = matrix[budget][policy]
                cell["done"] += 1
                cell["success"] += 1 if row["success"] else 0
                index += 1
    return matrix


def render_html():
    task_ids = plan_task_ids()
    rows = sweep_rows(parse_progress(), task_ids)
    matrix = build_matrix(rows, task_ids)

    total_runs = len(PLAN_BUDGETS) * len(PLAN_POLICIES) * len(task_ids)
    done = len(rows)
    percent = round(done / total_runs * 100, 1) if total_runs else 0

    # 速率与预计剩余
    eta_text = "—"
    rate_text = "—"
    status = "等待启动"
    if rows:
        try:
            t0 = datetime.strptime(rows[0]["ts"], "%H:%M:%S")
            t1 = datetime.strptime(rows[-1]["ts"], "%H:%M:%S")
            elapsed = max((t1 - t0).total_seconds(), 1)
            rate = done / elapsed * 60
            rate_text = f"{rate:.1f} 任务/分钟"
            remaining = (total_runs - done) / max(rate, 0.01)
            eta_text = f"{remaining:.0f} 分钟"
            idle = (datetime.now() - datetime.combine(datetime.today(), t1.time())).total_seconds()
            status = f"运行中（最后一条 {int(idle)} 秒前）" if idle < 120 else f"可能已停止（最后一条 {int(idle)} 秒前）"
        except Exception:
            pass

    task_done = {task: 0 for task in task_ids}
    for row in rows:
        task_done[row["task"]] = task_done.get(row["task"], 0) + 1

    success_total = sum(1 for r in rows if r["success"])
    success_text = f"{success_total}/{done}" if done else "0/0"

    # 当前档位
    current = "—"
    index = 0
    for budget in PLAN_BUDGETS:
        for policy in PLAN_POLICIES:
            if index + len(task_ids) > done:
                current = f"预算 {budget} · 策略 {policy}"
                break
            index += len(task_ids)
        if current != "—":
            break

    def cell_html(cell):
        if cell["done"] == 0:
            return '<td class="empty">—</td>'
        rate = cell["success"] / cell["done"]
        cls = "good" if rate >= 0.95 else ("mid" if rate >= 0.8 else "bad")
        return f'<td class="{cls}">{rate:.0%}<div class="sub">{cell["done"]}/{cell["total"]}</div></td>'

    matrix_html = ""
    for policy in PLAN_POLICIES:
        cells = "".join(cell_html(matrix[b][policy]) for b in PLAN_BUDGETS)
        matrix_html += f"<tr><th>{policy}</th>{cells}</tr>"

    recent_html = ""
    for row in rows[-15:][::-1]:
        mark = "✅" if row["success"] else "❌"
        seconds = f'{row["seconds"]}s' if row["seconds"] else ""
        recent_html += (
            f'<div class="line"><span class="ts">{row["ts"]}</span> {mark} '
            f'<b>{html.escape(row["policy"])}</b> · {html.escape(row["task"])} '
            f'<span class="sec">{seconds}</span></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>MiniAgent 实验监控</title>
<style>
  body {{ font-family: "Microsoft YaHei", sans-serif; margin: 24px; background:#f6f7f9; color:#1f2937; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .muted {{ color:#6b7280; font-size: 13px; }}
  .cards {{ display:flex; gap:12px; margin:16px 0; flex-wrap: wrap; }}
  .card {{ background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:12px 16px; min-width:150px; }}
  .card .k {{ font-size:12px; color:#6b7280; }}
  .card .v {{ font-size:18px; font-weight:700; margin-top:4px; }}
  .bar {{ height:14px; background:#e5e7eb; border-radius:7px; overflow:hidden; margin:10px 0 4px; }}
  .bar > div {{ height:100%; background:#2563eb; }}
  table {{ border-collapse: collapse; background:#fff; border:1px solid #e5e7eb; border-radius:10px; overflow:hidden; }}
  th, td {{ padding:10px 16px; text-align:center; border-bottom:1px solid #f0f1f3; font-size:14px; }}
  th {{ background:#f9fafb; }}
  td.good {{ background:#ecfdf5; color:#065f46; font-weight:700; }}
  td.mid {{ background:#fffbeb; color:#92400e; font-weight:700; }}
  td.bad {{ background:#fef2f2; color:#991b1b; font-weight:700; }}
  td.empty {{ color:#d1d5db; }}
  .sub {{ font-weight:400; font-size:11px; color:#6b7280; }}
  .line {{ font-size:13px; padding:3px 0; border-bottom:1px dashed #eee; }}
  .ts {{ color:#9ca3af; font-family:Consolas, monospace; }}
  .sec {{ color:#9ca3af; }}
</style></head>
<body>
  <h1>MiniAgent 实验监控</h1>
  <div class="muted">数据源：{html.escape(str(PROGRESS))} ｜ 自动刷新每 5 秒 ｜ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>

  <div class="cards">
    <div class="card"><div class="k">总体进度</div><div class="v">{done} / {total_runs}</div></div>
    <div class="card"><div class="k">成功率（累计）</div><div class="v">{success_text}</div></div>
    <div class="card"><div class="k">状态</div><div class="v">{status}</div></div>
    <div class="card"><div class="k">速度</div><div class="v">{rate_text}</div></div>
    <div class="card"><div class="k">预计剩余</div><div class="v">{eta_text}</div></div>
    <div class="card"><div class="k">当前档位</div><div class="v">{current}</div></div>
  </div>

  <div class="bar"><div style="width:{percent}%"></div></div>
  <div class="muted">{percent}% 完成</div>

  <h2 style="font-size:16px; margin-top:24px;">成功率矩阵（行=策略，列=预算字符）</h2>
  <table>
    <tr><th></th>{"".join(f"<th>预算 {b}</th>" for b in PLAN_BUDGETS)}</tr>
    {matrix_html}
  </table>

  <h2 style="font-size:16px; margin-top:24px;">最近进度</h2>
  <div>{recent_html or '<div class="muted">暂无记录</div>'}</div>

  <script>setTimeout(function(){{ location.reload(); }}, 5000);</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = render_html().encode("utf-8")
            self.send_response(200)
        except Exception as exc:  # 出错了也要能看见
            body = f"<pre>monitor error: {exc}</pre>".encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 静默


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[monitor] http://127.0.0.1:{args.port} （Ctrl+C 停止）", flush=True)
    server.serve_forever()
