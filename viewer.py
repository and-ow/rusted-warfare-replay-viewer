#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_viewer -- 生成自包含的 HTML 回放查看器。

数据来源：rwreplay.parse_replay。
可视化内容：
  * 地图底图（若提供 APK assets 目录且为内置地图）
  * 各单位位置随时间移动（来自命令流中的额外动作项 d）
  * 命令目标点、聊天、队伍、规则、播放控制
"""

import base64
import json
import os
import re

from rwreplay import parse_replay

TEAM_COLORS = [
    "#e0503c", "#4a90e2", "#5ec26a", "#e2b93b", "#9b6bd6",
    "#3fc7c7", "#e07ab0", "#b08040", "#8fd14f", "#7f8c8d",
    "#d94f6a", "#5f9e6e", "#c0a24a", "#6a7fd6", "#d67a3f",
]

# cg 移动方式枚举（com.corrodinggames.rts.game.units.cg）常见项
CG_NAMES = {0: "none", 1: "land", 2: "water", 3: "air", 4: "hover",
            5: "land+water", 6: "all"}


def _png_size(path):
    try:
        with open(path, "rb") as fh:
            head = fh.read(33)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))
    except OSError:
        return None


def _tmx_world_size(path):
    """从 TMX 读取地图世界尺寸 = width*tilewidth（铁锈战争世界坐标单位）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(400)
    except OSError:
        return None
    m_w = re.search(r'width="(\d+)"', head)
    m_t = re.search(r'tilewidth="(\d+)"', head)
    m_h = re.search(r'height="(\d+)"', head)
    m_th = re.search(r'tileheight="(\d+)"', head)
    if not (m_w and m_t and m_h and m_th):
        return None
    return (int(m_w.group(1)) * int(m_t.group(1)),
            int(m_h.group(1)) * int(m_th.group(1)))


def _safe_join(root, rel):
    """把回放中的相对路径安全地拼到 assets 根目录下。

    map_path 来自回放文件本身，可能被构造成 "../../etc/passwd" 或绝对路径，
    因此解析后必须校验结果仍位于 root 之内，否则返回 None。
    """
    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_abs, rel))
    if target != root_abs and not target.startswith(root_abs + os.sep):
        return None
    return target


def _load_map(assets, map_path):
    """返回 (dataURI, png_w, png_h, world_w, world_h) 或 None。"""
    if not assets or not map_path:
        return None
    rel = map_path.replace("\\", "/")
    if not rel.lower().endswith(".tmx"):
        return None
    tmx = _safe_join(assets, rel)
    png = _safe_join(assets, rel[:-4] + "_map.png")
    if tmx is None or png is None or not os.path.isfile(png):
        return None
    size = _png_size(png)
    if size is None:
        return None
    world = _tmx_world_size(tmx) if os.path.isfile(tmx) else None
    with open(png, "rb") as fh:
        data = base64.b64encode(fh.read()).decode("ascii")
    return "data:image/png;base64," + data, size[0], size[1], \
        (world[0] if world else None), (world[1] if world else None)


def build_payload(path, assets):
    d = parse_replay(path)
    setup = d.get("setup", {})
    meta = d.get("metadata") or {}

    teams = []
    for t in setup.get("teams", []):
        if not t.get("notNull"):
            continue
        teams.append({"id": t.get("teamId", t.get("index")), "name": t.get("name") or ""})

    # 轨迹事件：命令流中额外动作项携带 [unitId, 当前x, 当前y]
    events = []
    orders = []
    unit_ids = set()
    for c in d.get("commands", []):
        if c.get("type") != "command" or c.get("_error"):
            continue
        frame = c.get("frame", 0)
        team = c.get("teamId", -1)
        wp = c.get("waypoint")
        if wp:
            orders.append([frame, team, wp.get("actionName") or "?",
                           round(wp.get("x") or 0.0, 1), round(wp.get("y") or 0.0, 1),
                           len(c.get("unitIds") or [])])
        for a in c.get("extraActions") or []:
            if not a:
                continue
            uid = a.get("b")
            if uid is None:
                continue
            unit_ids.add(uid)
            events.append([frame, team, uid, round(a.get("c") or 0.0, 1),
                           round(a.get("d") or 0.0, 1)])
    events.sort(key=lambda e: e[0])

    # 坐标范围（用于无底图时自适应）
    xs = [e[3] for e in events] + [o[3] for o in orders]
    ys = [e[4] for e in events] + [o[4] for o in orders]
    extent = [min(xs), min(ys), max(xs), max(ys)] if xs else [0, 0, 1, 1]

    mapimg = _load_map(assets, setup.get("mapPath"))

    payload = {
        "file": d["file"],
        "fileBytes": d["fileBytes"],
        "header": d["header"],
        "map": {
            "path": setup.get("mapPath") or "",
            "image": mapimg[0] if mapimg else None,
            "pngW": mapimg[1] if mapimg else None,
            "pngH": mapimg[2] if mapimg else None,
            "worldW": mapimg[3] if mapimg else None,
            "worldH": mapimg[4] if mapimg else None,
            "extent": extent,
        },
        "rules": (setup.get("gameSetup") or {}).get("rules"),
        "camera": {"x": setup.get("camera_x"), "y": setup.get("camera_y"),
                   "zoom": setup.get("zoom")},
        "teams": teams,
        "unitCount": len(unit_ids),
        "objectCount": setup.get("objectCount"),
        "commands": orders,
        "events": events,
        "chats": [[c["frame"], c.get("authorTeamId"), c.get("from"), c.get("message")]
                  for c in d.get("chats", [])],
        "summary": d["summary"],
        "duration": meta.get("lastFrame"),
        "time": meta.get("time"),
        "warnings": d.get("warnings", []),
    }
    return payload


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>铁锈战争回放查看器</title>
<style>
  :root{
    --bg:#12161c; --panel:#1b2029; --line:#2b3340; --text:#dfe6ef;
    --muted:#8b97a8; --accent:#4a90e2;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:13px/1.5 -apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif}
  header{padding:12px 18px;border-bottom:1px solid var(--line);
         display:flex;flex-wrap:wrap;gap:6px 22px;align-items:baseline}
  header h1{font-size:15px;margin:0 12px 0 0;font-weight:600}
  header .kv{color:var(--muted)}
  header .kv b{color:var(--text);font-weight:600}
  main{display:flex;height:calc(100vh - 58px);min-height:420px}
  #stage{flex:1;position:relative;overflow:hidden;background:#0c0f13;
         display:flex;align-items:center;justify-content:center}
  #cv{max-width:100%;max-height:100%;background:#0c0f13}
  #side{width:320px;border-left:1px solid var(--line);background:var(--panel);
        display:flex;flex-direction:column;overflow:hidden}
  #side h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;
           color:var(--muted);margin:0;padding:10px 14px;border-bottom:1px solid var(--line)}
  #log{flex:1;overflow:auto;padding:8px 14px}
  .msg{margin:6px 0;padding:6px 8px;border-left:3px solid var(--line);
       background:#161b23;border-radius:4px}
  .msg .who{font-weight:600;color:var(--accent)}
  .msg .fr{color:var(--muted);font-size:11px}
  #bar{border-top:1px solid var(--line);padding:8px 12px;
       display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  button{background:#232b36;color:var(--text);border:1px solid var(--line);
         border-radius:5px;padding:5px 12px;cursor:pointer;font:inherit}
  button:hover{background:#2b3542}
  input[type=range]{flex:1;min-width:120px;accent-color:var(--accent)}
  .lbl{color:var(--muted);font-size:12px}
  .teams{padding:8px 14px;border-bottom:1px solid var(--line);max-height:130px;overflow:auto}
  .team{display:flex;align-items:center;gap:8px;padding:2px 0}
  .dot{width:10px;height:10px;border-radius:50%;flex:none}
  .warn{color:#e2b93b;padding:8px 14px;font-size:12px;border-top:1px solid var(--line)}
</style>
</head>
<body>
<header id="hdr"></header>
<main>
  <div id="stage"><canvas id="cv"></canvas></div>
  <aside id="side">
    <h2>队伍</h2>
    <div class="teams" id="teams"></div>
    <h2>聊天 / 事件</h2>
    <div id="log"></div>
    <div id="bar">
      <button id="play">▶ 播放</button>
      <input type="range" id="seek" min="0" max="100" value="0" step="1">
      <span class="lbl" id="frame">帧 0 / 0</span>
      <select id="speed">
        <option value="1">1×</option>
        <option value="2" selected>2×</option>
        <option value="4">4×</option>
        <option value="8">8×</option>
      </select>
      <label class="lbl"><input type="checkbox" id="trails" checked> 轨迹</label>
    </div>
  </aside>
</main>
<script>
const D = __DATA__;
const COLORS = __COLORS__;
const stage=document.getElementById('stage'), cv=document.getElementById('cv'),
      ctx=cv.getContext('2d');
const seek=document.getElementById('seek'), frameLbl=document.getElementById('frame'),
      playBtn=document.getElementById('play'), speedSel=document.getElementById('speed'),
      trailsChk=document.getElementById('trails'), logEl=document.getElementById('log');

const F0 = D.summary.firstFrame==null?0:D.summary.firstFrame;
const F1 = D.duration!=null?D.duration:(D.summary.lastFrame||1);
seek.min=0; seek.max=F1;

// ---- 头部信息 ----
(function(){
  const h=D.header||{}, parts=[];
  parts.push(`<h1>${esc(D.file)}</h1>`);
  parts.push(kv('版本', h.gameVersionString+' (构建 '+h.gameVersionCode+' / 格式 '+h.formatVersion+')'));
  parts.push(kv('地图', D.map.path||'(内嵌)'));
  parts.push(kv('时长', F1+' 帧 · '+(D.time? (D.time/60000).toFixed(1):'?')+' 分钟'));
  parts.push(kv('命令', D.summary.commandCount+' · 单位 '+D.unitCount+' · 聊天 '+D.summary.chatCount));
  const r=D.rules;
  if(r) parts.push(kv('规则', '资金 '+r.startingCredits+' · 迷雾 '+r.fogMode+' · 起始单位 '+r.startingUnits+' · 收入 '+r.incomeMultiplier));
  document.getElementById('hdr').innerHTML=parts.join('');
})();
function kv(k,v){return `<span class="kv">${k} <b>${esc(String(v))}</b></span>`;}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

// ---- 队伍 ----
const teamName={}, teamColor={};
D.teams.forEach((t,i)=>{teamName[t.id]=t.name||('队伍'+t.id);
  teamColor[t.id]=COLORS[i%COLORS.length];});
document.getElementById('teams').innerHTML = D.teams.map(t=>
  `<div class="team"><span class="dot" style="background:${teamColor[t.id]}"></span>
   <span>${t.id}. ${esc(teamName[t.id])}</span></div>`).join('')
  || '<div class="lbl">无</div>';
if(D.warnings&&D.warnings.length)
  document.querySelector('aside').insertAdjacentHTML('beforeend',
    `<div class="warn">⚠ ${D.warnings.length} 条解析警告</div>`);

// ---- 世界 -> 画布 坐标变换 ----
let sx=1, sy=1, ox=0, oy=0, CW=0, CH=0;
function setupView(){
  const m=D.map;
  let worldW=m.worldW, worldH=m.worldH;
  if(!worldW||!worldH){
    const e=m.extent; worldW=Math.max(1,e[2]-e[0]); worldH=Math.max(1,e[3]-e[1]);
    ox=-e[0]; oy=-e[1];
  }
  const maxDim=760;
  if(m.image){
    CW=worldW; CH=worldH;
    const scale=Math.min(maxDim/Math.max(worldW,worldH),2.2);
    cv.width=Math.round(worldW*scale); cv.height=Math.round(worldH*scale);
  } else {
    CW=worldW; CH=worldH;
    cv.width=680; cv.height=Math.round(680*worldH/worldW);
    if(cv.height>760){cv.height=760; cv.width=Math.round(760*worldW/worldH);}
  }
  sx=cv.width/worldW; sy=cv.height/worldH;
}
function px(x){return (x+ox)*sx;}
function py(y){return (y+oy)*sy;}

// ---- 单位状态 ----
// 预计算：unitId -> [{f,x,y,t}]，按帧升序
const unitSeries={};
D.events.forEach(e=>{ const [f,t,uid,x,y]=e;
  (unitSeries[uid] || (unitSeries[uid]=[])).push([f,x,y,t]); });
const uids=Object.keys(unitSeries);

function unitAt(series,frame){
  // 二分查找最后一个 f<=frame
  let lo=0,hi=series.length-1,ans=-1;
  while(lo<=hi){const m=(lo+hi)>>1; if(series[m][0]<=frame){ans=m;lo=m+1;}else hi=m-1;}
  return ans<0?null:series[ans];
}
// 轨迹：unitId -> 已走过的点（用于绘制，重算时截断）
let trailBuf={};

function draw(frame){
  ctx.setTransform(1,0,0,1,0,0);
  ctx.clearRect(0,0,cv.width,cv.height);
  ctx.fillStyle='#0c0f13'; ctx.fillRect(0,0,cv.width,cv.height);
  if(D.map.image){
    const img=imgCache;
    if(img) ctx.drawImage(img,0,0,cv.width,cv.height);
  } else {
    // 无底图：网格
    ctx.strokeStyle='#1c232c'; ctx.lineWidth=1;
    const step=100*sx;
    for(let x=0;x<cv.width;x+=step){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,cv.height);ctx.stroke();}
    for(let y=0;y<cv.height;y+=step){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(cv.width,y);ctx.stroke();}
  }
  // 命令目标（当前帧附近的）
  ctx.globalAlpha=0.55;
  D.commands.forEach(o=>{ const [f,team,act,x,y]=o;
    if(f>frame) return;
    if(frame-f>600) return;
    ctx.strokeStyle=teamColor[team]||'#888'; ctx.lineWidth=1.5;
    ctx.beginPath(); ctx.moveTo(px(x)-4,py(y)); ctx.lineTo(px(x)+4,py(y));
    ctx.moveTo(px(x),py(y)-4); ctx.lineTo(px(x),py(y)+4); ctx.stroke();
  });
  ctx.globalAlpha=1;
  // 单位
  D.events.forEach(e=>{ const [f,team,uid,x,y]=e;
    if(f>frame) return;
    });
  const showTrails=trailsChk.checked;
  uids.forEach(uid=>{
    const s=unitSeries[uid]; const cur=unitAt(s,frame);
    if(!cur) return;
    const [,x,y,team]=cur;
    const col=teamColor[team]||'#9aa';
    if(showTrails){
      ctx.strokeStyle=col; ctx.globalAlpha=0.35; ctx.lineWidth=1;
      ctx.beginPath(); let started=false;
      for(const p of s){ if(p[0]>frame) break;
        if(!started){ctx.moveTo(px(p[1]),py(p[2]));started=true;} else ctx.lineTo(px(p[1]),py(p[2])); }
      ctx.stroke(); ctx.globalAlpha=1;
    }
    ctx.fillStyle=col; ctx.beginPath();
    ctx.arc(px(x),py(y),3.2,0,Math.PI*2); ctx.fill();
    ctx.strokeStyle='#0c0f13'; ctx.lineWidth=1; ctx.stroke();
  });
}

// ---- 聊天按帧显示 ----
let lastShown=-1;
function updateLog(frame){
  if(frame<lastShown){ logEl.innerHTML=''; lastShown=-1; D.chats.forEach((c,i)=>c._done=false); }
  D.chats.forEach(c=>{ if(c._done) return; if(c[0]<=frame){ c._done=true;
    const div=document.createElement('div'); div.className='msg';
    div.innerHTML=`<span class="who">${esc(c[2]||teamName[c[1]]||'系统')}</span>
      <span class="fr"> [帧 ${c[0]}]</span><br>${esc(c[3]||'')}`;
    logEl.appendChild(div); lastShown=Math.max(lastShown,frame);} });
}

let imgCache=null;
if(D.map.image){ imgCache=new Image(); imgCache.onload=()=>draw(+seek.value); imgCache.src=D.map.image; }

// ---- 播放 ----
let playing=false, acc=0, lastT=0;
function tick(t){
  if(!playing) return;
  const dt=(t-lastT)/1000; lastT=t;
  acc += dt*(speedSel.value*60);
  if(acc>=1){ const nf=Math.min(+seek.value+Math.floor(acc), F1); acc-=Math.floor(acc);
    seek.value=nf; update(+seek.value); }
  if(+seek.value>=F1){ playing=false; playBtn.textContent='▶ 播放'; return; }
  requestAnimationFrame(tick);
}
function update(frame){
  frameLbl.textContent=`帧 ${frame} / ${F1}`;
  draw(frame); updateLog(frame);
}
playBtn.onclick=()=>{ playing=!playing;
  if(playing){ playBtn.textContent='⏸ 暂停'; lastT=performance.now(); acc=0;
    if(+seek.value>=F1) seek.value=0;
    requestAnimationFrame(tick);} else playBtn.textContent='▶ 播放'; };
seek.oninput=()=>{ playing=false; playBtn.textContent='▶ 播放'; update(+seek.value); };
trailsChk.onchange=()=>draw(+seek.value);

setupView();
update(F0);
</script>
</body>
</html>
"""


def _json_for_script(obj):
    """序列化为可安全内嵌进 <script> 的 JSON 文本。

    json.dumps 不转义 "<"，若回放中的聊天内容里出现 "</script>"，浏览器会在该处
    提前闭合脚本标签，其后的文本被当作 HTML 解析，从而可注入任意标签或脚本；
    即使只转义 "</"，残留的 "<!--" 也会把 tokenizer 带进 script-data-escaped 状态。
    因此这里把所有 "<" 写成 JSON 转义 "\\u003c"（解析后仍是 "<"，语义不变），
    并转义行分隔符 U+2028/U+2029。
    """
    text = json.dumps(obj, ensure_ascii=False)
    return (text.replace("<", "\\u003c")
                .replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029"))


def build_viewer(path, out, assets):
    payload = build_payload(path, assets)
    html = _TEMPLATE.replace("__DATA__", _json_for_script(payload))
    html = html.replace("__COLORS__", json.dumps(TEAM_COLORS))
    if not out:
        base = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(os.getcwd(), base + ".viewer.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("已生成查看器: %s (%d bytes)" % (out, len(html)))
    return out
