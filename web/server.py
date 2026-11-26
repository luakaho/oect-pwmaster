#!/usr/bin/env python3
"""OEC (RK3566) fan controller WebUI.
Serves a single-page UI and a small JSON API; the GPIO daemon (fanctl)
is the only component that touches the hardware.
Environment:
  FAN_DATA  data dir (default /data) - status.json, fan.log, config/fan.conf, manual.duty
  PORT      listen port (default 8090)
"""
import json, os, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DATA = os.environ.get("FAN_DATA", "/data")
CFGF = os.path.join(DATA, "config", "fan.conf")
CTRL = os.path.join(DATA, "manual.duty")
LOG  = os.path.join(DATA, "fan.log")
STAT = os.path.join(DATA, "status.json")
PORT = int(os.environ.get("PORT", "8090"))

DEFCFG = """cpu_curve=33:25,40:50,48:75,58:100
hdd_curve=35:0,40:40,45:65,50:100
hyst_start=33
hyst_stop=31
min_duty=0
max_duty=100
failsafe_duty=60
hdd_mode=max
update_sec=2
boost_gain=15
boost_max=30
freq=12000
spin_us=4
deadband_duty=12
"""

def read_cfg():
    try:
        with open(CFGF) as f: return f.read()
    except Exception:
        return DEFCFG

def status():
    try:
        with open(STAT) as f: return json.load(f)
    except Exception as e:
        return {"error": str(e)}

def history(n=180):
    out = []
    try:
        with open(LOG) as f: lines = f.readlines()[-n:]
        for ln in lines:
            m = re.match(r"(\S+ \S+),cpu=([-\d.]+),n=(\d+),dmax=([-\d.]+),davg=([-\d.]+),duty=(\d+),rpm=(-?\d+)", ln.strip())
            if m:
                out.append({"t": m.group(1), "cpu": float(m.group(2)), "n": int(m.group(3)),
                            "dmax": float(m.group(4)), "davg": float(m.group(5)),
                            "duty": int(m.group(6)), "rpm": int(m.group(7))})
    except Exception:
        pass
    return out

PAGE = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>OECT-PWMaster</title>
<style>
:root{--bg:#0e1015;--fg:#e8ebf2;--dim:#8b93a7;--card:#161a22;--line:#232936;--acc:#4ea1ff;--ok:#57d38c;--warn:#ffb020}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:14px;max-width:760px;margin:auto}
h1{font-size:18px;margin:0 0 4px}.sub{color:var(--dim);font-size:12px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px}
.k{color:var(--dim);font-size:12px}.v{font-size:20px;font-weight:600;margin-top:2px}
.v.small{font-size:14px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:12px}
.panel h2{font-size:14px;margin:0 0 10px;display:flex;align-items:center;gap:6px}
.help{display:inline-flex;width:18px;height:18px;border-radius:50%;background:#2b3343;color:#9fb0cc;font-size:12px;align-items:center;justify-content:center;cursor:pointer;flex:0 0 auto;user-select:none}
.help:active{background:var(--acc);color:#fff}
table{border-collapse:collapse;width:100%}td,th{padding:5px 4px;font-size:13px;text-align:left}
th{color:var(--dim);font-weight:500}
input[type=number],select{width:76px;background:#0b0e14;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:5px 6px;font-size:14px}
input[type=range]{width:100%}
button{background:var(--acc);color:#fff;border:0;border-radius:9px;padding:9px 14px;font-size:14px;cursor:pointer}
button.gray{background:#2a3140}button.sm{padding:6px 10px;font-size:13px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
#manualPanel.off #manualRow{opacity:.35}
#manualPanel.off #mstate{color:#ffb020}
button:disabled{opacity:.45;cursor:not-allowed}
canvas{width:100%;height:auto;display:block}
.legend{color:var(--dim);font-size:12px;margin-top:4px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
#tip{position:fixed;max-width:280px;background:#1e2532;border:1px solid #33405a;color:#e8ebf2;font-size:13px;padding:10px 12px;border-radius:10px;display:none;z-index:99;box-shadow:0 8px 24px #0008;line-height:1.5}
.presets button{background:#232936;border:1px solid var(--line)}
.presets button.active{background:var(--acc);border-color:var(--acc)}
</style></head><body>
<h1>OECT-PWMaster</h1>
<div class=sub>点每个标题后面的 <span class=help>?</span> 看说明</div>

<div class=grid>
 <div class=card><div class=k>CPU 温度</div><div class=v id=t_cpu>--</div></div>
 <div class=card><div class=k>硬盘温度</div><div class="v small" id=t_disk>--</div></div>
 <div class=card><div class=k>风扇档位</div><div class=v id=duty>--</div></div>
 <div class=card><div class=k>转速</div><div class="v small" id=rpm>--</div></div>
 <div class=card><div class=k>模式</div><div class="v small" id=mode>--</div></div>
</div>

<div class=panel><canvas id=chart width=900 height=300></canvas><div class=legend>
<span><i class=dot style="background:#4ea1ff"></i>CPU</span>
<span><i class=dot style="background:#ffb020"></i>硬盘最高温</span>
<span><i class=dot style="background:#57d38c"></i>风扇档位</span></div></div>

<div class=panel>
 <h2>一键方案 <span class=help data-tip="把两条曲线换成预设值。静音：温度高一些才开始转、转得温和；均衡：推荐日常；强冷：低一点就转、给风更足。">?</span></h2>
 <div class="row presets">
  <button id=btn_quiet onclick="preset('quiet')">静音</button>
  <button id=btn_balanced onclick="preset('balanced')">均衡</button>
  <button id=btn_cool onclick="preset('cool')">强冷</button>
 </div>
</div>
<details class=pro><summary>专业设置（曲线 / 手动 / 阈值）</summary>
<div class=panel id=manualPanel>
 <h2>手动控制 <span class=help data-tip="自动模式下（包括用了一键方案）这里会置灰不可操作。想手动指定档位，先点「启用手动」，拖滑块后点「应用」；点「回到自动」恢复温度调速。">?</span></h2>
 <div class=row style="margin-bottom:8px"><span id=mstate style="color:var(--dim);font-size:13px">当前：自动（温度调速）</span></div>
 <div class=row id=manualRow>
   <input id=sl type=range min=0 max=100 value=0 disabled>
   <span id=slv style="width:44px">0%</span>
   <button id=btnEnable onclick=enableManual()>启用手动</button>
   <button id=btnApply onclick=applyManual() disabled>应用</button>
   <button id=btnAuto class=gray onclick=setAuto()>回到自动</button>
 </div>
</div>


<div class=panel>
 <h2>CPU 温度曲线 <span class=help data-tip="左边填温度(℃)，右边填风扇档位(%)。风扇会按这两列做线性插值：例如 40℃→50%、48℃→75%，那 44℃ 就是约 62%。最多 6 个点，留空的行会被忽略。">?</span></h2>
 <table id=tbl_cpu><tr><th>#</th><th>温度 ℃</th><th>风扇档位 %</th></tr></table>
</div>

<div class=panel>
 <h2>硬盘温度曲线 <span class=help data-tip="硬盘温度按「硬盘取值方式」选出的那个值来套这条曲线。2.5 寸盘建议长期不超过 45℃，所以曲线在 45℃ 以上要明显加风。">?</span></h2>
 <table id=tbl_hdd><tr><th>#</th><th>温度 ℃</th><th>风扇档位 %</th></tr></table>
</div>

<div class=panel>
 <h2>其它设置 <span class=help data-tip="这些是全局规则，和上面两条曲线一起决定最终档位。改完点保存，2 秒内生效。">?</span></h2>
 <table>
  <tr><td>起转温度 <span class=help data-tip="CPU 低于这个温度时风扇完全停（节能、静音）；达到或超过才开始按曲线转。">?</span></td><td><input id=hyst_start type=number step=1></td></tr>
  <tr><td>停止温度 <span class=help data-tip="温度降到这个值以下才停风扇。和「起转温度」之间的差叫回差，用来避免温度在临界点来回跳、风扇忽转忽停。">?</span></td><td><input id=hyst_stop type=number step=1></td></tr>
  <tr><td>最低档位 <span class=help data-tip="不管曲线算出多少，都不会低于这个档位。想「常年低速常转、压住灰尘和湿气」就设成 20-25。">?</span></td><td><input id=min_duty type=number step=1></td></tr>
  <tr><td>最高档位 <span class=help data-tip="封顶值。嫌吵可以设 80，代价是极端高温时散热余量变小。">?</span></td><td><input id=max_duty type=number step=1></td></tr>
  <tr><td>读不到温度时 <span class=help data-tip="传感器异常、读不到温度时风扇给这个档位，防止过热。建议 60。">?</span></td><td><input id=failsafe_duty type=number step=1></td></tr>
  <tr><td>硬盘取值方式 <span class=help data-tip="三块盘的温度怎么合成一个：取最高（保守、推荐）或取平均（更安静，但可能让某块盘偏热）。">?</span></td><td><select id=hdd_mode><option value=max>取最高</option><option value=avg>取平均</option></select></td></tr>
  <tr><td>升温加速 <span class=help data-tip="温度在上升时，按上升速度额外加风，提前压住温度。数值越大加得越猛；设 0 = 关闭该功能。">?</span></td><td><input id=boost_gain type=number step=1></td></tr>
  <tr><td>加速上限 <span class=help data-tip="额外加风最多加多少个百分点（不超过总上限）。比如 30 就代表最多在曲线档位上再加 30%。">?</span></td><td><input id=boost_max type=number step=1></td></tr>
  <tr><td>PWM 频率 <span class=help data-tip="软件 PWM 的开关频率（Hz）。12kHz 风扇基本听不到、CPU 约 1/4 核；25kHz 完全无声但会吃满一个核。">?</span></td><td><input id=freq type=number step=1000></td></tr>
  <tr><td>自旋窗口 <span class=help data-tip="每个电平边沿前自旋多少微秒来对时。越大越准、越费 CPU，一般 3-6 即可。">?</span></td><td><input id=spin_us type=number step=1></td></tr>
  <tr><td>死区档位 <span class=help data-tip="低于这个占空比直接当 0（4 线扇低于约 15% 根本转不起来），能省掉无效自旋。">?</span></td><td><input id=deadband_duty type=number step=1></td></tr>
  <tr><td>刷新间隔 <span class=help data-tip="多久采样一次温度并更新风扇档位（秒）。越小反应越快，但更耗一点 CPU。推荐 2 秒。">?</span></td><td><input id=update_sec type=number step=1></td></tr>
 </table>
 <div style="margin-top:12px" class=row><button onclick=saveCfg()>保存设置</button><span id=savenote style="color:var(--dim)"></span></div>
</div>
</details>

<div id=tip></div>
<script>
const PRESETS={
 quiet:{cpu:[38,25,46,45,56,70,66,100],hdd:[38,0,43,40,48,65,53,100],hyst_start:38,hyst_stop:35,min_duty:0},
 balanced:{cpu:[33,25,40,50,48,75,58,100],hdd:[35,0,40,40,45,65,50,100],hyst_start:33,hyst_stop:31,min_duty:0},
 cool:{cpu:[30,30,36,55,44,80,52,100],hdd:[32,0,36,45,42,70,48,100],hyst_start:30,hyst_stop:28,min_duty:0}};
let cfg="",hist=[];
function rows(tbl,pairs){const t=document.getElementById(tbl);
 t.querySelectorAll('tr:not(:first-child)').forEach(r=>r.remove());
 for(let i=0;i<6;i++){const p=pairs[i]||['',''];
  const tr=document.createElement('tr');
  tr.innerHTML='<td>'+(i+1)+'</td><td><input type=number step=1 value="'+p[0]+'"></td><td><input type=number step=1 value="'+p[1]+'"></td>';
  t.appendChild(tr);}}
function get(k){const m=cfg.match(new RegExp('^'+k+'=(.*)$','m'));return m?m[1].trim():''}
function fill(){rows('tbl_cpu',(get('cpu_curve')||'').split(',').filter(Boolean).map(x=>x.split(':')));
 rows('tbl_hdd',(get('hdd_curve')||'').split(',').filter(Boolean).map(x=>x.split(':')));
 ['hyst_start','hyst_stop','min_duty','max_duty','failsafe_duty','update_sec','boost_gain','boost_max','freq','spin_us','deadband_duty'].forEach(k=>{const e=document.getElementById(k);if(e)e.value=get(k)});
 const m=get('hdd_mode');document.getElementById('hdd_mode').value=(m==='avg'?'avg':'max');}
function flat2pairs(a){const o=[];for(let i=0;i<a.length;i+=2)o.push(a[i]+":"+a[i+1]);return o.join(",")}
function markActive(){for(const k of ['quiet','balanced','cool']){const b=document.getElementById('btn_'+k);if(!b)continue;const p=PRESETS[k];
 const same=(flat2pairs(p.cpu)===pairsFrom('tbl_cpu'))&&(flat2pairs(p.hdd)===pairsFrom('tbl_hdd'))&&(String(p.hyst_start)===document.getElementById('hyst_start').value);
 b.classList.toggle('active',same);}}
async function loadCfg(){cfg=await (await fetch('api/config')).text();fill();markActive()}
function pairsFrom(tbl){const o=[];document.getElementById(tbl).querySelectorAll('tr:not(:first-child)').forEach(r=>{
 const i=r.querySelectorAll('input');const t=i[0].value.trim(),d=i[1].value.trim();
 if(t!==''&&d!=='')o.push(t+':'+d)});return o.join(',')}
function setPairs(tbl,arr){const t=document.getElementById(tbl);
 const inps=t.querySelectorAll('tr:not(:first-child) input');
 for(let r=0;r<6;r++){const tI=inps[r*2],dI=inps[r*2+1];
  if(arr[r*2]!==undefined){tI.value=arr[r*2];dI.value=arr[r*2+1]}else{tI.value='';dI.value=''}}}
function preset(name){const p=PRESETS[name];
 setPairs('tbl_cpu',p.cpu); setPairs('tbl_hdd',p.hdd);
 document.getElementById('hyst_start').value=p.hyst_start;
 document.getElementById('hyst_stop').value=p.hyst_stop;
 document.getElementById('min_duty').value=p.min_duty;
 saveCfg();}
async function saveCfg(){
 const g=id=>document.getElementById(id).value.trim();
 const txt=['cpu_curve='+pairsFrom('tbl_cpu'),'hdd_curve='+pairsFrom('tbl_hdd'),
  'hyst_start='+g('hyst_start'),'hyst_stop='+g('hyst_stop'),'min_duty='+g('min_duty'),
  'max_duty='+g('max_duty'),'failsafe_duty='+g('failsafe_duty'),
  'hdd_mode='+document.getElementById('hdd_mode').value,'update_sec='+g('update_sec'),'boost_gain='+g('boost_gain'),'boost_max='+g('boost_max'),'freq='+g('freq'),'spin_us='+g('spin_us'),'deadband_duty='+g('deadband_duty')].join('\n')+'\n';
 const r=await fetch('api/config',{method:'POST',body:txt});
 document.getElementById('savenote').textContent=r.ok?'已保存，2 秒内生效':'保存失败';
 if(r.ok){cfg=txt;markActive();}
 setTimeout(()=>document.getElementById('savenote').textContent='',4000);
}
async function tick(){try{
 const s=await (await fetch('api/status')).json(); if(s.error)return;
 document.getElementById('t_cpu').textContent=(s.cpu_c??'--')+'℃';
 document.getElementById('t_disk').textContent=(s.disk_n?('最高 '+s.disk_max_c+'℃ · '+s.disk_n+' 块'):'--');
 document.getElementById('duty').textContent=s.duty+'%';
 document.getElementById('rpm').textContent=(s.rpm===undefined||s.rpm<0)?'未启用（安全模式）':(s.rpm>0?s.rpm+' 转':'无信号');
 document.getElementById('mode').textContent=s.mode||'自动';
 const manual=(s.mode||'')==='手动';
 const mp=document.getElementById('manualPanel');
 if(mp) mp.classList.toggle('off',!manual);
 const st=document.getElementById('mstate');
 if(st) st.textContent=manual?'当前：手动（已锁定档位）':'当前：自动（温度调速，手动已锁定）';
 const be=document.getElementById('btnEnable'); if(be) be.disabled=manual;
 const ba=document.getElementById('btnApply'); if(ba) ba.disabled=!manual;
 const slEl=document.getElementById('sl');
 if(slEl){slEl.disabled=!manual; if(manual&&Date.now()>(window.__slPause||0)){slEl.value=s.duty;document.getElementById('slv').textContent=s.duty+'%';}}
 hist=await (await fetch('api/history')).json();draw();
}catch(e){}}
function draw(){try{
 const cv=document.getElementById('chart');if(!cv)return;const g=cv.getContext('2d');
 const W=cv.width,H=cv.height;g.clearRect(0,0,W,H);
 if(!hist||!hist.length)return;
 const maxT=Math.max(70,Math.max(...hist.map(h=>Math.max(h.cpu,h.dmax||0)))+5);
 const X=i=>i*(W-20)/(hist.length-1)+10,Y=t=>H-20-(t/maxT)*(H-40);
 const line=(f,c)=>{g.beginPath();g.strokeStyle=c;g.lineWidth=3;
   hist.forEach((h,idx)=>{const v=Math.min(f(h),maxT);idx?g.lineTo(X(idx),Y(v)):g.moveTo(X(idx),Y(v));});
   g.stroke();};
 line(h=>h.cpu,'#4ea1ff');
 line(h=>h.dmax||0,'#ffb020');
 line(h=>h.duty*maxT/100,'#57d38c');
}catch(e){console.error('draw failed',e);}}
async function enableManual(){
 const sl=document.getElementById('sl');
 const v=sl?sl.value:(50);
 await fetch('api/duty?v='+v,{method:'POST'});
 window.__slPause=Date.now()+8000;
 tick();
}
async function applyManual(){const v=document.getElementById('sl').value;await fetch('api/duty?v='+v,{method:'POST'});tick()}
async function setAuto(){await fetch('api/duty',{method:'DELETE'});tick()}
document.getElementById('sl').addEventListener('input',e=>{window.__slPause=Date.now()+10000;document.getElementById('slv').textContent=e.target.value+'%';});
const tip=document.getElementById('tip');
document.addEventListener('click',e=>{
 const h=e.target.closest('.help');
 const t=document.getElementById('tip');
 if(h){t.textContent=h.dataset.tip;t.style.display='block';
  const r=h.getBoundingClientRect();const w=Math.min(300,window.innerWidth-24);
  t.style.maxWidth=w+'px';t.style.left=Math.max(8,Math.min(r.left,window.innerWidth-w-8))+'px';
  t.style.top=(r.bottom+8)+'px';e.stopPropagation();}
 else if(!e.target.closest('#tip')){t.style.display='none';}
});
loadCfg();tick();setInterval(tick,3000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _s(self, body, ct="application/json; charset=utf-8", code=200):
        if isinstance(body, str): body = body.encode()
        self.send_response(code); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"): return self._s(PAGE, "text/html; charset=utf-8")
        if p == "/api/status":  return self._s(json.dumps(status(), ensure_ascii=False))
        if p == "/api/history": return self._s(json.dumps(history()))
        if p == "/api/config":  return self._s(read_cfg(), "text/plain; charset=utf-8")
        if p == "/healthz":     return self._s("ok", "text/plain")
        self._s(json.dumps({"error": "not found"}), code=404)

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/config":
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n).decode()
            try:
                os.makedirs(os.path.dirname(CFGF), exist_ok=True)
                old = {}
                for line in read_cfg().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        old[k.strip()] = v.strip()
                for line in body.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        old[k.strip()] = v.strip()
                merged = "".join("%s=%s\n" % (k, v) for k, v in old.items())
                with open(CFGF, "w") as f: f.write(merged)
                self._s(json.dumps({"ok": True}))
            except Exception as e:
                self._s(json.dumps({"error": str(e)}), code=500)
            return
        if p == "/api/duty":
            q = parse_qs(urlparse(self.path).query)
            try:
                v = max(0, min(100, int(q.get("v", ["50"])[0])))
                with open(CTRL, "w") as f: f.write(str(v))
                self._s(json.dumps({"ok": True, "duty": v}))
            except Exception as e:
                self._s(json.dumps({"error": str(e)}), code=400)
            return
        self._s(json.dumps({"error": "not found"}), code=404)

    def do_DELETE(self):
        if self.path.split("?")[0] == "/api/duty":
            try: os.remove(CTRL)
            except FileNotFoundError: pass
            self._s(json.dumps({"ok": True}))
        else:
            self._s(json.dumps({"error": "not found"}), code=404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("fan webui on :%d, data=%s" % (PORT, DATA), flush=True)
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
