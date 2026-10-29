# -*- coding: utf-8 -*-
"""llmbench-cn Web 面板 —— 零依赖本地面板(启动:python -m llmbench web)

面板直接复用 cli 的命令函数,与命令行行为完全一致。
不保存任何密钥:密钥只存在于浏览器与本次请求进程内存之间。
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import cli

HTML = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>llmbench-cn · Web 面板</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;--acc:#3fb950;--ok:#3fb950;--bad:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 system-ui,"Segoe UI","Microsoft YaHei",sans-serif;padding:20px}
h1{font-size:20px;margin:0}.sub{color:var(--dim);font-size:12px;margin:4px 0 18px}a{color:#58a6ff}
.wrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.card h2{font-size:15px;margin:0 0 12px}
label{display:block;font-size:12px;color:var(--dim);margin:8px 0 4px}
input,select{width:100%;background:#0d1117;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:7px 9px;font-size:13px}
.row3{display:flex;gap:8px;align-items:flex-end}.row3>*{flex:1}.row3 button{flex:0 0 auto}
button{background:#21262d;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:8px 14px;font-size:13px;cursor:pointer;margin-top:12px}
button:hover{border-color:var(--acc)}button.primary{background:rgba(63,185,80,.12);border-color:var(--acc)}
button.alt{background:rgba(88,166,255,.12);border-color:#58a6ff;margin-left:8px}
button.warn{background:rgba(248,81,73,.12);border-color:#f85149;margin-left:8px}
pre,.out{background:#010409;border:1px solid var(--line);border-radius:8px;padding:12px;font:12.5px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-all;max-height:420px;overflow:auto;margin:10px 0 0}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:10px}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left}th{color:var(--dim);font-weight:600}
.chips{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}
.chip{background:#010409;border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-size:12px}
.chip b{display:block;font-size:16px;color:var(--acc)}
.taskgrid{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;font-size:13px}
.taskgrid label{display:flex;gap:6px;align-items:center;margin:2px 0;color:var(--fg)}
.taskgrid input{width:auto}
.hint{color:var(--dim);font-size:12px;margin-top:8px}
.prow{display:flex;gap:8px;margin-top:10px}.prow>*{flex:1}.prow .del{flex:0 0 auto;background:#2d1517;border-color:#5c2d30;color:#f0a8a8}
.hidden{display:none}
.toast{position:fixed;right:16px;bottom:16px;background:#21262d;border:1px solid var(--acc);border-radius:8px;padding:10px 14px;font-size:13px;display:none}
.toast.err{border-color:var(--bad)}
</style></head><body>
<h1>llmbench-cn <span style="color:var(--acc)">Web 面板</span></h1>
<div class="sub">国产大模型自测选型:首 token 延迟 / 吞吐 / 成本 / 客观任务通过率 · 零依赖 · 端口 __PORT__ · 姊妹项目:<a href="__AICN_URL__" target="_blank">aicn 面板</a></div>
<div class="wrap">
<div class="card" style="grid-column:1/-1">
<h2>① 基准测试</h2>
<div id="prows"></div>
<button class="alt" onclick="addRow()">+ 添加供应商(对比测试)</button>
<label>任务(勾选,默认全跑)</label>
<div class="taskgrid" id="tasks"></div>
<div class="row3">
<div><label>重复次数</label><input id="r-repeat" type="number" min="1" max="10" value="2"></div>
<div><label>超时(秒)</label><input id="r-timeout" type="number" min="5" value="120"></div>
<div><label>覆盖 base_url(可选)</label><input id="r-base" placeholder="如 http://127.0.0.1:8788/v1"></div>
<div><label>重试次数</label><input id="r-retries" type="number" min="0" max="5" value="1"></div>
</div>
<button class="primary" onclick="runBench()">开始基准测试</button>
<button class="alt" onclick="fillMock()">一键填入本地 mock 网关</button>
<div id="bench-out"></div>
<div class="hint">密钥只进入本次请求内存,面板不保存、不落盘。真实网关限流(429)时请减少重复次数或换模型。</div>
</div>
<div class="card">
<h2>② 同步模型(/models)</h2>
<label>供应商</label><select id="f-provider"></select>
<label>覆盖 base_url(可选)</label><input id="f-base" placeholder="留空用该供应商默认地址">
<label>API Key(可选,缺省读环境变量)</label><input id="f-key" type="password">
<button class="primary" onclick="runRefresh()">拉取 /models 并更新数据文件</button>
<button class="alt" onclick="fillSyncMock()">填入本地 mock 网关</button>
<div id="refresh-out"></div>
<div class="hint">写入用户数据层 ~/.llmbench/providers.json(不会动 pip 安装目录);写入前自动备份 .bak。</div>
</div>
<div class="card">
<h2>③ 供应商 / 任务清单</h2>
<button class="primary" onclick="loadList()">加载清单</button>
<div id="list-box"></div>
</div>
</div>
<div class="toast" id="toast"></div>
<script>
const $=s=>document.getElementById(s);
async function api(path,body){const r=await fetch('/api/'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});return r.json()}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function render(el,obj){const pre=document.createElement('pre');let t='';if(obj.error)t+='错误: '+obj.error+'\n';if(obj.exit&&obj.exit!==0)t+='[退出码 '+obj.exit+']\n';if(obj.note)t+=obj.note+'\n';pre.textContent=t+(obj.out||'(无输出)');el.innerHTML='';el.appendChild(pre)}
function toast(msg,isErr){const t=$('toast');t.textContent=msg;t.className='toast'+(isErr?' err':'');t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',3200)}
let PROV={},TASKS=[];
function providerOptions(){const cats={};Object.keys(PROV).forEach(p=>{const c=PROV[p].category||'其他';(cats[c]=cats[c]||[]).push(p)});
const order=['国产直连','聚合平台','海外官方','自建网关','其他'];let h='';
order.filter(c=>cats[c]).forEach(c=>{h+='<optgroup label="'+esc(c)+'">'+cats[c].map(p=>'<option value="'+esc(p)+'">'+esc(PROV[p].cn||PROV[p].name||p)+'('+esc(p)+')</option>').join('')+'</optgroup>'});
const rest=Object.keys(cats).filter(c=>order.indexOf(c)<0);rest.forEach(c=>{h+='<optgroup label="'+esc(c)+'">'+cats[c].map(p=>'<option value="'+esc(p)+'">'+esc(PROV[p].cn||PROV[p].name||p)+'('+esc(p)+')</option>').join('')+'</optgroup>'});
return h}
function modelOptions(pid){const p=PROV[pid]||{};const ms=p.models||[];let h='<option value="">(默认第一个)</option>';
if(ms.length){const fams={};ms.forEach(m=>{const fam=(String(m.id).split(/[-\/.]/)[0]||'other').toUpperCase();(fams[fam]=fams[fam]||[]).push(m)});
const keys=Object.keys(fams);
if(keys.length>1){keys.forEach(f=>{h+='<optgroup label="'+esc(f)+'">'+fams[f].map(m=>'<option value="'+esc(m.id)+'">'+esc(m.id)+(m.input!=null?(' · ¥'+m.input):'')+'</option>').join('')+'</optgroup>'})}
else{ms.forEach(m=>{h+='<option value="'+esc(m.id)+'">'+esc(m.id)+(m.input!=null?(' · ¥'+m.input):'')+'</option>'})}}
h+='<option value="__custom__">✏️ 自定义…</option>';return h}
function addRow(){const d=document.createElement('div');d.className='prow';
d.innerHTML='<select class="psel">'+providerOptions()+'</select><select class="pmsel"></select><input class="pmodel hidden" placeholder="输入模型名"><input class="pkey" type="password" placeholder="API Key(可选)"><button class="del" onclick="this.parentNode.remove()">✕</button>';
const sel=d.querySelector('.psel'),msel=d.querySelector('.pmsel'),min=d.querySelector('.pmodel');
msel.innerHTML=modelOptions(sel.value);
sel.addEventListener('change',()=>{msel.innerHTML=modelOptions(sel.value);min.classList.add('hidden')});
msel.addEventListener('change',()=>{min.classList.toggle('hidden',msel.value!=='__custom__')});
$('prows').appendChild(d)}
function rowData(d){const msel=d.querySelector('.pmsel'),min=d.querySelector('.pmodel');
return {provider:d.querySelector('.psel').value,model:msel.value==='__custom__'?(min.value||null):(msel.value||null),key:d.querySelector('.pkey').value}}
function fillMock(){const rows=[...document.querySelectorAll('.prow')];rows.slice(1).forEach(r=>r.remove());
const d=rows[0];d.querySelector('.psel').value='gateway';d.querySelector('.pmsel').innerHTML=modelOptions('gateway');
d.querySelector('.pkey').value='mock-key';$('r-base').value='http://127.0.0.1:8799/v1';$('r-repeat').value='2'}
function fillSyncMock(){$('f-provider').value='gateway';$('f-base').value='http://127.0.0.1:8799/v1';$('f-key').value='mock-key'}
async function init(){const r=await api('providers');PROV=r.providers||{};TASKS=r.tasks||[];
const tg=$('tasks');tg.innerHTML='';TASKS.forEach(t=>{tg.innerHTML+='<label><input type="checkbox" class="tck" value="'+esc(t.id)+'" checked>'+esc(t.id)+' <span style="color:var(--dim)">'+esc(t.name)+'</span></label>'});
$('f-provider').innerHTML=providerOptions();
if(!document.querySelectorAll('.prow').length)addRow()}
function median(a){if(!a.length)return null;const s=[...a].sort((x,y)=>x-y);const m=s.length>>1;return s.length%2?s[m]:(s[m-1]+s[m])/2}
async function runBench(){const rows=[...document.querySelectorAll('.prow')].map(rowData);const tasks=[...document.querySelectorAll('.tck:checked')].map(x=>x.value);
if(!rows.length||!rows[0].provider){alert('请先添加至少一个供应商');return}
$('bench-out').innerHTML='<pre>运行中(真实网关可能需数十秒)...</pre>';
const body={rows,tasks,repeat:parseInt($('r-repeat').value)||1,timeout:parseInt($('r-timeout').value)||120,retries:parseInt($('r-retries').value)||0,base_url:$('r-base').value};
const r=await api('run',body);if(!r.ok){render($('bench-out'),r);return}
const data=r.data||{};const rs=data.rows||[];
let passed=0,total=0;const firsts=[],tpss=[];
rs.forEach(x=>{const m=String(x.passed).match(/(\d+)\/(\d+)/);if(m){passed+=+m[1];total+=+m[2]}if(x.first_ms!=null)firsts.push(x.first_ms);if(x.tps!=null)tpss.push(x.tps)});
const medF=median(firsts);const medT=median(tpss);
let h='<div class="chips">';
h+='<div class="chip">客观通过<b>'+passed+' / '+total+'</b></div>';
h+='<div class="chip">首token 中位数<b>'+(medF==null?'—':medF+' ms')+'</b></div>';
h+='<div class="chip">吞吐 中位数<b>'+(medT==null?'—':medT+' tok/s')+'</b></div>';
h+='<div class="chip">模型数<b>'+new Set(rs.map(x=>x.model)).size+'</b></div></div>';
h+='<table><tr><th>供应商</th><th>模型</th><th>任务</th><th>次数</th><th>首token(ms)</th><th>总时延(s)</th><th>tok/s</th><th>输入/输出</th><th>成本</th><th>通过</th></tr>';
rs.forEach(x=>{h+='<tr><td>'+esc(x.pid)+'</td><td>'+esc(x.model)+'</td><td>'+esc(x.task)+'</td><td>'+esc(x.ok_runs)+'/'+esc(x.runs)+'</td><td>'+(x.first_ms==null?'—':esc(x.first_ms))+'</td><td>'+(x.total_ms==null?'—':esc((x.total_ms/1000).toFixed(1)))+'</td><td>'+(x.tps==null?'—':esc(x.tps))+'</td><td>'+esc(x.usage_in==null?'—':x.usage_in)+'/'+esc(x.usage_out==null?'—':x.usage_out)+'</td><td>'+esc(x.cost_str)+'</td><td>'+esc(x.passed)+'</td></tr>'});
h+='</table><div class="hint">生成时间:'+esc(data.generated_at||'-')+' · 通过列 x/y=客观校验通过/可判定次数(- 表示无客观校验) · 首token 与 tok/s 取中位数</div>';
const btns='<button class="alt" onclick="copyMd()">复制 Markdown</button><button class="alt" onclick="downloadJson()">下载 JSON</button>';
$('bench-out').innerHTML=h+btns;window.__md=buildMd(data);window.__lastJson=data;}
function buildMd(data){const rs=data.rows||[];let m='# llmbench-cn 实测报告\n\n- 生成时间:'+(data.generated_at||'-')+'\n\n| 供应商 | 模型 | 任务 | 次数 | 首token(ms) | 总时延(s) | tok/s | 输入/输出 | 成本 | 通过 |\n|---|---|---|---|---|---|---|---|---|---|\n';
rs.forEach(x=>{m+='| '+[x.pid,x.model,x.task,x.ok_runs+'/'+x.runs,x.first_ms==null?'—':x.first_ms,x.total_ms==null?'—':(x.total_ms/1000).toFixed(1),x.tps==null?'—':x.tps,(x.usage_in==null?'—':x.usage_in)+'/'+(x.usage_out==null?'—':x.usage_out),x.cost_str,x.passed].map(v=>String(v).replace(/\|/g,'\\|')).join(' | ')+' |\n'});return m}
async function copyMd(){try{await navigator.clipboard.writeText(window.__md||'');toast('Markdown 已复制到剪贴板')}catch(e){toast('复制失败,请手动选择',true)}}
function downloadJson(){const blob=new Blob([JSON.stringify(window.__lastJson||{},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='llmbench-report.json';a.click();toast('已下载 JSON 报告')}
async function loadList(){const r=await api('list',{});if(!r.ok){render($('list-box'),r);return}
const data=r.data||{};const ps=data.providers||{};let h='<table><tr><th>分类</th><th>供应商</th><th>默认模型</th><th>协议</th><th>备注</th></tr>';
Object.keys(ps).forEach(pid=>{const p=ps[pid];const protos=[];if(p.base_url)protos.push('chat');if(p.anthropic_base_url)protos.push('anthropic');if(p.responses_api)protos.push('responses');const first=(p.models||[])[0];
h+='<tr><td>'+esc(p.category||'-')+'</td><td>'+esc(pid)+'</td><td>'+esc(first?first.id:'(无内置模型)')+'</td><td>'+esc(protos.join('/'))+'</td><td>'+esc((p.note||'').slice(0,48))+'</td></tr>'});
h+='</table><h2 style="margin-top:14px">内置任务</h2><table><tr><th>任务</th><th>名称</th><th>校验</th></tr>';
(data.tasks||[]).forEach(t=>{const ch={py_fib:'✓执行验证',json_keys:'✓结构校验',contains:'✓答案校验'}[t.check||'']||'-';h+='<tr><td>'+esc(t.id)+'</td><td>'+esc(t.name)+'</td><td>'+esc(ch)+'</td></tr>'});
h+='</table>';$('list-box').innerHTML=h}
async function runRefresh(){const r=await api('refresh',{provider:$('f-provider').value,base_url:$('f-base').value,key:$('f-key').value});
if(r.ok){render($('refresh-out'),r);await init();toast('模型列表已同步')}else{render($('refresh-out'),r)}}
init();
</script></body></html>"""


def _run(fn, ns) -> dict:
    """在线程中执行 cli 命令函数，捕获输出与 SystemExit。

    用 cli.capture_output（基于 contextvars）而非 contextlib.redirect_stdout：
    后者改的是全局 sys.stdout，Web 面板并发请求时会互相串台。
    stdout 与 stderr 分开保存——JSON 报告只从 stdout 解析，
    否则「重试提示」之类的 stderr 内容会混进 JSON 导致解析失败。
    """
    box = {}

    def worker():
        try:
            with cli.capture_output() as cap:
                try:
                    box["out"] = fn(ns) or ""
                except SystemExit as exc:
                    box["exit"] = exc.code or 0
                except Exception as exc:
                    box["error"] = "%s: %s" % (type(exc).__name__, exc)
                box["stdout"] = "\n".join(cap.out)
                box["stderr"] = "\n".join(cap.err)
        except Exception as exc:  # pragma: no cover - 兜底
            box["error"] = "%s: %s" % (type(exc).__name__, exc)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(timeout=900)
    if th.is_alive():
        box["note"] = "任务仍在后台执行,超 900s 未返回。"
    # cmd_* 返回值优先（它是命令的正式产物）；否则退回捕获到的 stdout
    stdout = box.get("out") or box.get("stdout", "")
    stderr = box.get("stderr", "")
    return {"ok": box.get("error") is None and box.get("exit", 0) == 0,
            "exit": box.get("exit", 0), "error": box.get("error", ""),
            "note": box.get("note", ""), "out": stdout.strip(),
            "stderr": stderr.strip()}


def api_providers() -> dict:
    tasks = cli.load_json(cli.TASKS_FILE)
    return {"ok": True, "providers": cli.load_providers(),
            "tasks": tasks.get("tasks", [])}


def api_list(body: dict) -> dict:
    ns = argparse.Namespace(tasks=False, json=True, provider=None)
    res = _run(cli.cmd_list, ns)
    if res["ok"]:
        try:
            res["data"] = json.loads(res["out"])
            res["out"] = ""
        except Exception:
            pass
    return res


def api_run(body: dict) -> dict:
    rows = [r for r in body.get("rows", []) if r.get("provider")]
    if not rows:
        return {"ok": False, "error": "请至少提供一个供应商"}
    provider_tokens, models = [], []
    for r in rows:
        token = r["provider"] + (":" + str(r.get("key")) if r.get("key") else "")
        provider_tokens.append(token)
        models.append(r.get("model") or None)
    tasks = body.get("tasks") or None
    ns = argparse.Namespace(provider=provider_tokens, model=models,
                            task=tasks, repeat=int(body.get("repeat") or 1),
                            max_tokens=0, base_url=body.get("base_url") or None,
                            timeout=int(body.get("timeout") or 120),
                            retries=int(body.get("retries") or 0),
                            json=True, save=None, markdown=None, verbose=False)
    res = _run(cli.cmd_run, ns)
    if res["ok"]:
        try:
            res["data"] = json.loads(res["out"])
            res["out"] = ""
        except Exception:
            pass
    return res


def api_refresh(body: dict) -> dict:
    ns = argparse.Namespace(provider=body.get("provider") or "gateway",
                            base_url=body.get("base_url") or None,
                            key=body.get("key") or None,
                            dry_run=bool(body.get("dry_run")),
                            no_prune=bool(body.get("no_prune")))
    return _run(cli.cmd_refresh, ns)


API = {"run": api_run, "list": api_list, "refresh": api_refresh}


class Handler(BaseHTTPRequestHandler):
    server_version = "llmbench-web/0.3"
    port = 8789
    aicn_url = "http://127.0.0.1:8787/"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else \
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _check_origin(self) -> bool:
        """简单的同源校验：拒绝跨站 POST（面板本地使用，防 DNS rebinding 滥用）。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        try:
            o_host = origin.split("//", 1)[1].split("/", 1)[0]
        except IndexError:
            return False
        return o_host == host or o_host.split(":")[0] == host.split(":")[0]

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = HTML.replace("__PORT__", str(self.port)) \
                       .replace("__AICN_URL__", self.aicn_url)
            self._send(200, page, "text/html; charset=utf-8")
            return
        if self.path == "/api/providers":
            self._send(200, api_providers())
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/api/"):
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._check_origin():
            self._send(403, {"ok": False, "error": "跨站请求被拒绝"})
            return
        name = self.path[5:]
        if name == "providers":
            self._send(200, api_providers())
            return
        fn = API.get(name)
        if not fn:
            self._send(404, {"ok": False, "error": "unknown api: " + name})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send(400, {"ok": False, "error": "bad json"})
            return
        try:
            self._send(200, fn(body))
        except Exception as exc:
            self._send(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})


def cmd_web(args: argparse.Namespace) -> int:
    Handler.port = args.port
    Handler.aicn_url = getattr(args, "aicn_url", None) or "http://127.0.0.1:8787/"
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print("面板启动失败:%s（端口 %d 可能已被占用，可换 --port）" % (exc, args.port))
        return 1
    url = "http://%s:%d/" % (args.host, srv.server_address[1])
    print("llmbench Web 面板:%s(Ctrl+C 停止)" % url)
    if not args.no_open:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n面板已停止")
    finally:
        srv.server_close()
    return 0
