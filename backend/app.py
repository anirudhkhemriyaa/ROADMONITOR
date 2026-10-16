"""Road Condition Monitoring - minimal backend (step 1).
Receives drives from the phone, shows live data, serves the phone installer.
Run:  python app.py      then open http://localhost:8000 on the laptop."""
import csv, hashlib, io, json, socket, time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
PHONE = ROOT / "phone"
RAW.mkdir(parents=True, exist_ok=True)
PHONE_FILES = {"rcm_logger.py", "marker.py", "check_drive.py", "upload.py", "rate_test.sh"}

app = FastAPI(title="Road Condition Monitoring")

# ---------------- live (in memory, lost on restart - that's fine) ----------------
live = {"rows": deque(maxlen=1500), "track": deque(maxlen=600), "batches": deque(maxlen=200),
        "total": 0, "last_seen": None, "device": None}


class Batch(BaseModel):
    device: str = "phone"
    rows: List[List[Optional[float]]]   # [ts_ms, ax, ay, az, lat, lon, speed_kmh, acc_m, bearing, gps_age_ms]


@app.post("/api/live")
def live_in(b: Batch):
    now = time.time()
    for r in b.rows:
        if len(r) >= 4:
            live["rows"].append(r)
            if len(r) >= 6 and r[4] is not None and r[5] is not None:
                if not live["track"] or live["track"][-1][:2] != [r[4], r[5]]:
                    live["track"].append([r[4], r[5]])
    live["batches"].append((now, len(b.rows)))
    live["total"] += len(b.rows)
    live["last_seen"], live["device"] = now, b.device
    return {"ok": True, "received": len(b.rows)}


@app.get("/api/live")
def live_out():
    now = time.time()
    recent = sum(n for t, n in live["batches"] if now - t <= 10)
    rows = list(live["rows"])
    last = rows[-1] if rows else None
    return {"age_s": None if live["last_seen"] is None else round(now - live["last_seen"], 1),
            "device": live["device"], "total": live["total"], "rows_per_s": round(recent / 10, 1),
            "latest": last, "chart": [r[:4] for r in rows[-300:]], "track": list(live["track"])}


# ---------------- uploads ----------------
def summarize(data: bytes) -> dict:
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace"))))
    if len(rows) < 10:
        raise HTTPException(400, "CSV has fewer than 10 rows or wrong format")
    try:
        ts = [int(r["timestamp_ms"]) for r in rows]
    except (KeyError, ValueError):
        raise HTTPException(400, "CSV must have an integer 'timestamp_ms' column")
    dts = [b - a for a, b in zip(ts, ts[1:])]
    dur = max((ts[-1] - ts[0]) / 1000.0, 1e-9)
    same, prev = 0, None
    for r in rows:
        k = (r.get("ax"), r.get("ay"), r.get("az"))
        same += (k == prev)
        prev = k
    gps = [r for r in rows if r.get("lat")]
    fresh = [r for r in gps if r.get("gps_age_ms") and int(float(r["gps_age_ms"])) <= 2000]
    accs = sorted(float(r["gps_accuracy_m"]) for r in gps if r.get("gps_accuracy_m") not in (None, "", "None"))
    hz = len(rows) / dur
    gaps = [d for d in dts if d > 1000]
    s = {"rows": len(rows), "duration_s": round(dur, 1), "hz": round(hz, 1),
         "gaps_over_1s": len(gaps), "longest_gap_s": round(max(gaps) / 1000, 1) if gaps else 0,
         "duplicate_pct": round(100 * same / len(rows), 1),
         "gps_fresh_pct": round(100 * len(fresh) / len(rows)),
         "gps_median_acc_m": accs[len(accs) // 2] if accs else None,
         "backwards_steps": sum(1 for d in dts if d < 0)}
    warns = []
    if hz < 15: warns.append("low rate")
    if gaps: warns.append("gaps")
    if s["duplicate_pct"] >= 5: warns.append("repeated values")
    if s["gps_fresh_pct"] < 90: warns.append("weak GPS")
    if s["backwards_steps"]: warns.append("clock jumps")
    s["status"] = "FAIL" if (hz < 8 or s["backwards_steps"]) else ("WARN" if warns else "OK")
    s["warnings"] = warns
    return s


@app.post("/upload")
async def upload(file: UploadFile = File(...), markers: Optional[UploadFile] = File(None),
                 vehicle_type: str = Form("unknown"), phone_model: str = Form(""), meta: str = Form("")):
    data = await file.read()
    sha = hashlib.sha256(data).hexdigest()
    for sc in RAW.glob("*.summary.json"):
        old = json.loads(sc.read_text())
        if old.get("sha256") == sha:
            return {"status": "already_uploaded", "file": old["file"]}
    name = Path(file.filename or "drive.csv").name
    if (RAW / name).exists():
        name = "%s_%s%s" % (Path(name).stem, sha[:6], Path(name).suffix)
    summary = summarize(data)               # validates before anything is saved
    (RAW / name).write_bytes(data)
    if markers is not None:
        (RAW / Path(markers.filename or name + ".markers.csv").name).write_bytes(await markers.read())
    rec = {"file": name, "sha256": sha, "vehicle_type": vehicle_type, "phone_model": phone_model,
           "uploaded_at": datetime.now().isoformat(timespec="seconds"), "has_markers": markers is not None,
           "summary": summary}
    (RAW / (name + ".summary.json")).write_text(json.dumps(rec, indent=2))
    return {"status": "saved", **rec}


@app.get("/api/drives")
def drives():
    out = [json.loads(p.read_text()) for p in RAW.glob("*.summary.json")]
    return sorted(out, key=lambda r: r["uploaded_at"], reverse=True)


# ---------------- connection helper + drive preview ----------------
clients = {}   # phones (non-local callers) that have contacted this server


@app.middleware("http")
async def track_clients(request: Request, call_next):
    ip = request.client.host if request.client else "?"
    if ip not in ("127.0.0.1", "::1"):
        c = clients.setdefault(ip, {"count": 0})
        c.update(last_seen=time.time(), last_path="%s %s" % (request.method, request.url.path))
        c["count"] += 1
    return await call_next(request)


def lan_ips():
    ips = set()
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass
    ips.add(lan_ip())
    return sorted(i for i in ips if not i.startswith("127.") and not i.startswith("169.254."))


@app.get("/api/info")
def info():
    now = time.time()
    return {"urls": ["http://%s:8000" % ip for ip in lan_ips()],
            "clients": [{"ip": ip, "seconds_ago": round(now - c["last_seen"]), "count": c["count"],
                         "last": c["last_path"]} for ip, c in clients.items()]}


@app.get("/api/drives/{name}/preview")
def preview(name: str):
    p = RAW / Path(name).name
    if p.suffix != ".csv" or not p.exists():
        raise HTTPException(404, "no such drive file")
    with open(p, newline="") as fh:
        rows = list(csv.DictReader(fh))
    step_c, step_t = max(1, len(rows) // 1500), max(1, len(rows) // 500)
    chart, track = [], []
    for i, r in enumerate(rows):
        try:
            if i % step_c == 0:
                chart.append([int(r["timestamp_ms"]), float(r["ax"]), float(r["ay"]), float(r["az"])])
            if r.get("lat") and i % step_t == 0:
                track.append([float(r["lat"]), float(r["lon"])])
        except (KeyError, ValueError):
            continue
    return {"file": p.name, "rows": len(rows), "chart": chart, "track": track}


# ---------------- phone installer ----------------
@app.get("/ping")
def ping():
    return {"ok": True, "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/phone/install.sh", response_class=PlainTextResponse)
def install_sh(request: Request):
    return (PHONE / "install.sh").read_text().replace("__BASE__", str(request.base_url).rstrip("/"))


@app.get("/phone/{name}", response_class=PlainTextResponse)
def phone_file(name: str):
    if name not in PHONE_FILES:
        raise HTTPException(404)
    return (PHONE / name).read_text()


# ---------------- dashboard ----------------
PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Road Monitor</title><style>
body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#f4f5f7;color:#1c1e21}
h1{font-size:20px;margin:0 0 12px} h2{font-size:15px;margin:14px 0 8px}
.row{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:12px}
.card{background:#fff;border-radius:10px;padding:12px 14px;box-shadow:0 1px 3px #0002;flex:1;min-width:150px}
.big{font-size:26px;font-weight:600} .sm{font-size:12px;color:#666}
.badge{display:inline-block;padding:3px 10px;border-radius:12px;color:#fff;font-weight:600;font-size:13px}
canvas{width:100%;background:#fff;border-radius:10px;box-shadow:0 1px 3px #0002}
table{width:100%;border-collapse:collapse;font-size:13px;background:#fff;border-radius:10px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #eee} th{color:#666}
.OK{color:#1a8f3c}.WARN{color:#c77700}.FAIL{color:#c62828}
.drop{border:2px dashed #999;border-radius:10px;padding:16px;text-align:center;background:#fff}
.drop.over{background:#e8f0fe;border-color:#1e88e5}
#viewing{display:none;background:#fff3cd;border-radius:10px;padding:8px 12px;margin-bottom:10px}
button{padding:4px 10px;cursor:pointer}
</style></head><body>
<h1>Road Condition Monitor <span id="badge" class="badge" style="background:#999">NO DATA</span></h1>
<div class="row">
 <div class="card"><h2 style="margin-top:0">1. Type this in the phone app</h2>
  <div id="urls" style="font-size:18px">checking...</div>
  <div class="sm">Use the address of your Wi-Fi (or hotspot) adapter. The phone must be on the same network.</div></div>
 <div class="card"><h2 style="margin-top:0">2. Phones that reached this server</h2>
  <table><thead><tr><th>Phone IP</th><th>Last seen</th><th>Requests</th><th>Last call</th></tr></thead>
  <tbody id="clients"><tr><td colspan="4">none yet</td></tr></tbody></table>
  <div class="sm">Empty after pressing Test connection = the phone cannot reach the laptop (network or firewall).</div></div>
</div>
<div class="row">
 <div class="card"><div class="sm">Readings per second</div><div class="big" id="rate">-</div></div>
 <div class="card"><div class="sm">Total received</div><div class="big" id="total">0</div></div>
 <div class="card"><div class="sm">Last data</div><div class="big" id="age">-</div><div class="sm" id="dev"></div></div>
 <div class="card"><div class="sm">Acceleration (m/s2)</div><div id="acc">-</div></div>
 <div class="card"><div class="sm">GPS</div><div id="gps">-</div></div>
</div>
<div id="viewing">Viewing saved drive: <b id="vname"></b> <button onclick="backLive()">Back to live</button></div>
<h2>Accelerometer <span class="sm">(red x, green y, blue z)</span></h2>
<canvas id="chart" width="1000" height="220"></canvas>
<h2>GPS path</h2>
<canvas id="map" width="1000" height="260"></canvas>
<h2>Import drive files (copied from the phone by USB)</h2>
<div class="drop" id="drop">Drop files here, or <input type="file" id="files" multiple accept=".csv,.json"><br>
<span class="sm">Select the drive CSV together with its _markers.csv and _meta.json (from Android/data/com.roadmonitor/files/drives)</span>
<div id="importlog" class="sm" style="margin-top:8px"></div></div>
<h2>Uploaded drives</h2>
<table><thead><tr><th>File</th><th>Vehicle</th><th>Rows</th><th>Min</th><th>Hz</th><th>Gaps</th><th>GPS ok</th><th>Quality</th><th>Uploaded</th><th></th></tr></thead>
<tbody id="drives"><tr><td colspan="10">none yet</td></tr></tbody></table>
<script>
const $=id=>document.getElementById(id);
let viewing=null;
function chart(rows){
 const c=$('chart'),g=c.getContext('2d');g.clearRect(0,0,c.width,c.height);
 if(rows.length<2)return;
 let lo=1e9,hi=-1e9;rows.forEach(r=>{for(let k=1;k<=3;k++){lo=Math.min(lo,r[k]);hi=Math.max(hi,r[k]);}});
 const pad=(hi-lo)*0.1||1;lo-=pad;hi+=pad;
 g.strokeStyle='#ddd';g.beginPath();g.moveTo(0,c.height/2);g.lineTo(c.width,c.height/2);g.stroke();
 g.fillStyle='#888';g.font='12px sans-serif';g.fillText(hi.toFixed(1),4,12);g.fillText(lo.toFixed(1),4,c.height-4);
 ['#e53935','#43a047','#1e88e5'].forEach((col,i)=>{
  g.strokeStyle=col;g.lineWidth=1.5;g.beginPath();
  rows.forEach((r,j)=>{const x=j/(rows.length-1)*c.width,y=c.height-(r[i+1]-lo)/(hi-lo)*c.height;j?g.lineTo(x,y):g.moveTo(x,y);});
  g.stroke();});
}
function map(tr){
 const c=$('map'),g=c.getContext('2d');g.clearRect(0,0,c.width,c.height);
 if(!tr.length){g.fillStyle='#888';g.font='14px sans-serif';g.fillText('No GPS fix received yet',12,24);return;}
 const lats=tr.map(p=>p[0]),lons=tr.map(p=>p[1]);
 const la0=Math.min(...lats),la1=Math.max(...lats),lo0=Math.min(...lons),lo1=Math.max(...lons);
 const k=Math.cos(la0*Math.PI/180),w=Math.max((lo1-lo0)*k,1e-5),h=Math.max(la1-la0,1e-5);
 const s=Math.min((c.width-40)/w,(c.height-40)/h);
 const X=p=>20+((p[1]-lo0)*k)*s,Y=p=>c.height-20-(p[0]-la0)*s;
 g.strokeStyle='#1e88e5';g.lineWidth=2;g.beginPath();tr.forEach((p,i)=>i?g.lineTo(X(p),Y(p)):g.moveTo(X(p),Y(p)));g.stroke();
 const e=tr[tr.length-1];g.fillStyle='#e53935';g.beginPath();g.arc(X(e),Y(e),6,0,7);g.fill();
}
async function tickLive(){
 try{const d=await (await fetch('/api/live')).json();
  const on=d.age_s!==null&&d.age_s<3;
  $('badge').textContent=on?'LIVE':(d.age_s===null?'NO DATA':'STOPPED');
  $('badge').style.background=on?'#1a8f3c':(d.age_s===null?'#999':'#c77700');
  $('rate').textContent=d.rows_per_s;$('total').textContent=d.total;
  $('age').textContent=d.age_s===null?'-':d.age_s+' s ago';$('dev').textContent=d.device||'';
  const L=d.latest;
  if(L){$('acc').innerHTML='x '+L[1]+'<br>y '+L[2]+'<br>z '+L[3];
   $('gps').innerHTML=L[4]===null?'no fix yet':L[4].toFixed(5)+', '+L[5].toFixed(5)+'<br>speed '+(L[6]??'-')+' km/h<br>accuracy '+(L[7]??'-')+' m';}
  if(!viewing){chart(d.chart);map(d.track);}}catch(e){$('badge').textContent='SERVER?';}
}
async function tickInfo(){
 try{const d=await (await fetch('/api/info')).json();
  $('urls').innerHTML=d.urls.length?d.urls.map(u=>'<div><b>'+u+'</b></div>').join(''):'not detected - run ipconfig and use http://YOUR_IP:8000';
  $('clients').innerHTML=d.clients.length?d.clients.map(c=>'<tr><td>'+c.ip+'</td><td>'+c.seconds_ago+' s ago</td><td>'+c.count+'</td><td>'+c.last+'</td></tr>').join(''):'<tr><td colspan="4">none yet</td></tr>';}catch(e){}
}
async function tickDrives(){
 const d=await (await fetch('/api/drives')).json();if(!d.length)return;
 $('drives').innerHTML=d.map(r=>{const s=r.summary;return '<tr><td>'+r.file+'</td><td>'+r.vehicle_type+'</td><td>'+s.rows+'</td><td>'+(s.duration_s/60).toFixed(1)+'</td><td>'+s.hz+'</td><td>'+s.gaps_over_1s+'</td><td>'+s.gps_fresh_pct+'%</td><td class="'+s.status+'">'+s.status+' '+s.warnings.join(', ')+'</td><td>'+r.uploaded_at.replace('T',' ')+'</td><td><button onclick="viewDrive(&quot;'+r.file+'&quot;)">View</button></td></tr>'}).join('');
}
async function viewDrive(f){
 const d=await (await fetch('/api/drives/'+encodeURIComponent(f)+'/preview')).json();
 viewing=f;$('viewing').style.display='block';$('vname').textContent=f+' ('+d.rows+' rows)';
 chart(d.chart);map(d.track);window.scrollTo(0,0);
}
function backLive(){viewing=null;$('viewing').style.display='none';tickLive();}
async function doImport(files){
 const out=$('importlog');out.innerHTML='';
 const by={};
 for(const f of files){
  const id=f.name.split('_')[0];by[id]=by[id]||{};
  if(f.name.endsWith('_markers.csv'))by[id].markers=f;
  else if(f.name.endsWith('_meta.json'))by[id].meta=f;
  else if(f.name.endsWith('.csv'))by[id].csv=f;
 }
 for(const id of Object.keys(by)){
  const g=by[id];
  if(!g.csv){out.innerHTML+=id+': no drive CSV selected, skipped<br>';continue;}
  const fd=new FormData();fd.append('file',g.csv);
  if(g.markers)fd.append('markers',g.markers);
  if(g.meta){const t=await g.meta.text();fd.append('meta',t);
   try{const m=JSON.parse(t);fd.append('vehicle_type',m.vehicle_type||'unknown');fd.append('phone_model',m.phone_model||'');}catch(e){}}
  try{const r=await fetch('/upload',{method:'POST',body:fd});const j=await r.json();
   out.innerHTML+=id+': '+(j.status||j.detail||r.status)+'<br>';}
  catch(e){out.innerHTML+=id+': failed ('+e+')<br>';}
 }
 tickDrives();
}
$('files').addEventListener('change',e=>doImport(e.target.files));
const dz=$('drop');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('over');});
dz.addEventListener('dragleave',()=>dz.classList.remove('over'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('over');doImport(e.dataTransfer.files);});
setInterval(tickLive,1000);setInterval(tickDrives,5000);setInterval(tickInfo,3000);
tickLive();tickDrives();tickInfo();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return PAGE


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    import uvicorn
    print("\n  Dashboard (laptop):  http://localhost:8000")
    print("  Type ONE of these in the phone app (the one on your Wi-Fi / hotspot network):")
    for ip in lan_ips():
        print("    http://%s:8000" % ip)
    print()
    uvicorn.run(app, host="0.0.0.0", port=8000)
