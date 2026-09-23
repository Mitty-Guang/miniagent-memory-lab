"""可视化监控面板：http://127.0.0.1:8899

数据源（自动选择，避免"从日志重建计划"的错位问题）：
1. **结果模式**：读取最新的 `results/sweep_*.json`（权威实验数据）→ 展示准确的
   成功率矩阵与汇总；
2. **实时动态**：读取 `progress.log` 的最近若干条 → 展示最近活动与是否正在运行。

运行：
    .\\.venv\\Scripts\\python.exe scripts\\monitor.py            # 默认端口 8899
    .\\.venv\\Scripts\\python.exe scripts\\monitor.py --port 9000
"""
import argparse
import html
import json
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PROGRESS = ROOT / "progress.log"
RESULTS = ROOT / "results"

LINE_RE = re.compile(
    r"^\[(?P<ts>\d{2}:\d{2}:\d{2})\]\s+policy=(?P<policy>\S+)\s+task=(?P<task>\S+)\s+"
    r"success=(?P<success>True|False)\s+(?P<rest>.*)$"
)


def parse_progress(limit: int = 2000):
    """解析 progress.log 的最后 limit 行。"""
    if not PROGRESS.exists():
        return []
    with open(PROGRESS, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()[-limit:]
    rows = []
    for line in lines:
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


def latest_sweep():
    files = sorted(RESULTS.glob("sweep_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    path = files[-1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    data["_file"] = path.name
    data["_mtime"] = datetime.fromtimestamp(path.stat().st_mtime)
    return data


def render_html():
    sweep = latest_sweep()
    rows = parse_progress()
    recent = rows[-15:][::-1]

    # 最近活动
    if rows:
        try:
            last_time = datetime.strptime(rows[-1]["ts"], "%H:%M:%S")
            idle = (datetime.now() - datetime.combine(datetime.today(), last_time.time())).total_seconds()
        except Exception:
            idle = 10**9
    else:
        idle = 10**9

    running = idle < 180
    status = f"有任务在跑（最后一条 {int(idle)} 秒前）" if running else f"空闲（最后一条 {int(idle)} 秒前）"

    # 结果矩阵（来自最新 sweep JSON）
    matrix_html = ""
    sweep_cards = """
      <div class="card"><div class="k">最近扫描文件</div><div class="v">（暂无 results/sweep_*.json）</div></div>
    """
    if sweep:
        budgets = sweep.get("budgets", [])
        policies = sweep.get("policies", [])
        table = sweep.get("table", {})
        n_tasks = sweep.get("n_tasks", 0)

        def cell(budget: str, policy: str) -> str:
            item = (table.get(budget) or {}).get(policy) or {}
            if not item:
                return '<td class="empty">—</td>'
            rate = item.get("success_rate", 0)
            cls = "good" if rate >= 0.95 else ("mid" if rate >= 0.8 else "bad")
            return (
                f'<td class="{cls}">{rate:.0%}'
                f'<div class="sub">{item.get("n_tasks", 0)} 任务</div></td>'
            )

        header = "".join(f"<th>预算 {b}</th>" for b in budgets)
        body = ""
        for policy in policies:
            cells = "".join(cell(str(b), policy) for b in budgets)
            body += f"<tr><th>{policy}</th>{cells}</tr>"
        matrix_html = f"""
          <table>
            <tr><th></th>{header}</tr>
            {body}
          </table>
        """
        total_runs = sum(
            item.get("n_tasks", 0)
            for row in table.values()
            for item in row.values()
        )
        avg_success = 0.0
        if total_runs:
            avg_success = sum(
                item.get("success_rate", 0) * item.get("n_tasks", 0)
                for row in table.values()
                for item in row.values()
            ) / total_runs
        sweep_cards = f"""
          <div class="card"><div class="k">最近扫描文件</div><div class="v" style="font-size:13px">{html.escape(sweep['_file'])}</div>
            <div class="k" style="margin-top:6px">{sweep['_mtime'].strftime('%m-%d %H:%M')} · {len(budgets)} 档预算 × {len(policies)} 策略 × {n_tasks} 任务</div></div>
          <div class="card"><div class="k">扫描总成功率</div><div class="v">{avg_success:.1%}</div></div>
        """

    feed = "".join(
        f'<div class="line"><span class="ts">{r["ts"]}</span> {"✅" if r["success"] else "❌"} '
        f'<b>{html.escape(r["policy"])}</b> · {html.escape(r["task"])} '
        f'<span class="sec">{str(r["seconds"])+"s" if r["seconds"] else ""}</span></div>'
        for r in recent
    ) or '<div class="muted">暂无记录</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>MiniAgent 实验监控</title>
<style>
  body {{ font-family: "Microsoft YaHei", sans-serif; margin: 24px; background:#f6f7f9; color:#1f2937; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .muted {{ color:#6b7280; font-size: 13px; }}
  .cards {{ display:flex; gap:12px; margin:16px 0; flex-wrap: wrap; }}
  .card {{ background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:12px 16px; min-width:170px; }}
  .card .k {{ font-size:12px; color:#6b7280; }}
  .card .v {{ font-size:18px; font-weight:700; margin-top:4px; }}
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
  <div class="muted">矩阵数据源：<b>results/sweep_*.json</b>（权威结果） ｜ 动态来自 progress.log ｜
    自动刷新每 5 秒 ｜ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>

  <div class="cards">
    <div class="card"><div class="k">状态</div><div class="v" style="font-size:15px">{status}</div></div>
    {sweep_cards}
  </div>

  <h2 style="font-size:16px; margin-top:18px;">最近一次预算扫描的成功率矩阵</h2>
  {matrix_html or '<div class="muted">暂无扫描结果（运行 scripts/run_sweep.py 后出现）</div>'}

  <h2 style="font-size:16px; margin-top:24px;">最近动态</h2>
  <div>{feed}</div>

  <script>setTimeout(function(){{ location.reload(); }}, 5000);</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = render_html().encode("utf-8")
            self.send_response(200)
        except Exception as exc:
            body = f"<pre>monitor error: {exc}</pre>".encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[monitor] http://127.0.0.1:{args.port} （Ctrl+C 停止）", flush=True)
    server.serve_forever()
