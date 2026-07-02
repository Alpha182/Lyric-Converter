#!/usr/bin/env python3
"""Turn a word-by-word TTML (with optional x-bg background vocals) into a
self-contained karaoke HTML page."""
import re, html, json, argparse

def t2s(t):
    m, s = t.split(":"); return round(int(m) * 60 + float(s), 3)

def parse_ttml(ttml):
    lines = []
    for p in re.findall(r"<p\b[^>]*>.*?</p>", ttml, re.S):
        inner = re.search(r"<p\b[^>]*>(.*)</p>", p, re.S).group(1)
        bg = []
        mbg = re.search(r'<span ttm:role="x-bg">(.*)</span>', inner, re.S)
        if mbg:
            bg = [{"t0": t2s(b), "t1": t2s(e), "w": html.unescape(w)}
                  for b, e, w in re.findall(r'<span begin="([^"]+)" end="([^"]+)">([^<]*)</span>', mbg.group(1))]
            inner = inner[:mbg.start()] + inner[mbg.end():]
        main = [{"t0": t2s(b), "t1": t2s(e), "w": html.unescape(w)}
                for b, e, w in re.findall(r'<span begin="([^"]+)" end="([^"]+)">([^<]*)</span>', inner)]
        allw = main + bg
        if allw:
            lines.append({"start": min(w["t0"] for w in allw),
                          "end": max(w["t1"] for w in allw),
                          "words": main, "bg": bg})
    return lines

TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ — lyrics</title>
<style>
  :root{ --lit:#fff; --dim:rgba(255,255,255,.22); --accent:#1db954; }
  *{box-sizing:border-box; margin:0; padding:0;}
  html,body{height:100%;}
  body{font-family:"Segoe UI",system-ui,sans-serif;
    background:radial-gradient(1200px 800px at 50% -10%,#1f2a24 0%,#0b0f0d 55%,#070907 100%);
    color:#fff; overflow:hidden; user-select:none;}
  #top{position:fixed;top:0;left:0;right:0;padding:18px 26px;z-index:10;
    background:linear-gradient(#070907cc,transparent);}
  #top h1{font-size:18px;font-weight:700;}
  #top .sub{font-size:12px;color:#9aa3a0;margin-top:2px;}
  #top .tag{display:inline-block;margin-top:6px;font-size:11px;color:var(--accent);
    border:1px solid #1db95455;border-radius:999px;padding:2px 9px;}
  #stage{position:absolute;inset:0;overflow:hidden;pointer-events:none;
    -webkit-mask-image:linear-gradient(transparent 6%,#000 26%,#000 74%,transparent 94%);
            mask-image:linear-gradient(transparent 6%,#000 26%,#000 74%,transparent 94%);}
  #lyrics{position:absolute;top:0;left:0;right:0;margin:0 auto;width:min(900px,92vw);padding:0 6px;
    transition:transform .45s cubic-bezier(.22,.61,.36,1);will-change:transform;}
  .line{padding:11px 0;cursor:pointer;pointer-events:auto;
    transition:opacity .3s,filter .3s,transform .3s;}
  .line .main{font-size:34px;font-weight:800;line-height:1.28;letter-spacing:.2px;color:var(--dim);}
  .line .bg{font-size:18px;font-weight:700;font-style:italic;margin-top:2px;opacity:.9;color:var(--dim);}
  .line.past{opacity:.45;} .line.future{opacity:.5;}
  .line.active{transform:scale(1.02);transform-origin:left;}
  .line:not(.active){filter:blur(.4px);}
  .word{background:linear-gradient(90deg,var(--lit) var(--p,0%),var(--dim) var(--p,0%));
    -webkit-background-clip:text;background-clip:text;color:transparent;transition:background .08s linear;}
  .bg .word{background:linear-gradient(90deg,var(--accent) var(--p,0%),var(--dim) var(--p,0%));
    -webkit-background-clip:text;background-clip:text;}
  .line.past .word{--p:100%;}
  #bar{position:fixed;left:0;right:0;bottom:0;z-index:10;display:flex;align-items:center;gap:14px;
    padding:14px 26px;background:linear-gradient(transparent,#070907ee);}
  #play{width:46px;height:46px;border:none;border-radius:50%;background:var(--accent);color:#04130a;
    font-size:18px;cursor:pointer;flex:none;}
  #seek{flex:1;accent-color:var(--accent);height:4px;cursor:pointer;}
  .t{font-variant-numeric:tabular-nums;font-size:12px;color:#9aa3a0;min-width:42px;text-align:center;}
  #mode,#nudge{flex:none;border:1px solid #ffffff30;background:transparent;color:#cfd6d3;border-radius:999px;
    padding:6px 13px;font-size:12px;cursor:pointer;white-space:nowrap;font-variant-numeric:tabular-nums;}
  #mode:hover,#nudge:hover{border-color:var(--accent);color:#fff;}
  .hint{position:fixed;bottom:70px;left:0;right:0;text-align:center;font-size:12px;color:#6b736f;
    z-index:10;pointer-events:none;}
</style></head>
<body>
  <div id="top">
    <h1>__TITLE__ <span style="color:#9aa3a0;font-weight:500">· __ARTIST__</span></h1>
    <div class="sub">word-by-word timing generated automatically (Demucs + forced alignment) — unverified__GENINFO__</div>
    <span class="tag">AI auto-aligned</span>
  </div>
  <div id="stage"><div id="lyrics"></div></div>
  <div class="hint">click any line to jump · spacebar = play/pause · ← / → nudge sync · green = background vocals · Sweep/Pop ▸</div>
  <div id="bar">
    <button id="play">▶</button>
    <span class="t" id="cur">0:00</span>
    <input id="seek" type="range" min="0" max="100" value="0" step="0.01">
    <span class="t" id="dur">0:00</span>
    <button id="nudge" title="Shift all lyric timing to match the audio (← / → keys, or click to reset)">sync 0ms</button>
    <button id="mode" title="Sweep = smooth fill across each word · Pop = crisp highlight at each word's onset">Sweep</button>
  </div>
  <audio id="audio" src="__AUDIO__" preload="auto"></audio>
<script>
const DATA = __JSON__;
const audio=document.getElementById('audio'), wrap=document.getElementById('lyrics'),
      stage=document.getElementById('stage'), playBtn=document.getElementById('play'),
      seek=document.getElementById('seek'), curT=document.getElementById('cur'), durT=document.getElementById('dur'),
      modeBtn=document.getElementById('mode');
const fmt=s=>{s=Math.max(0,s|0);return (s/60|0)+':'+String(s%60).padStart(2,'0');};
const allWords=ln=>ln.bg&&ln.bg.length?ln.words.concat(ln.bg):ln.words;
const POP=0.10; // pop-fill duration (s)
// Sweep fill: a word fills over the SHORTER of its own sung length and the time until the
// next word starts, then holds. So a short word before a long gap (e.g. a held "to…") fills
// crisply and waits — instead of crawling — while fast words still hand off seamlessly. A
// tiny floor stops crammed words strobing; the cap stops a held note crawling.
const SWEEP_MIN=0.05, SWEEP_MAX=0.75;
function computeFill(arr){for(let i=0;i<arr.length;i++){
  const nxt=i+1<arr.length?arr[i+1].t0:arr[i].t1;
  const cands=[arr[i].t1-arr[i].t0, nxt-arr[i].t0].filter(x=>x>0.001);
  const d=cands.length?Math.min(...cands):SWEEP_MIN;
  arr[i].fill=Math.min(SWEEP_MAX,Math.max(SWEEP_MIN,d));}}
DATA.lines.forEach(ln=>{computeFill(ln.words); if(ln.bg&&ln.bg.length)computeFill(ln.bg);});
let mode=localStorage.getItem('lyrmode')||'sweep';
modeBtn.textContent = mode==='pop'?'Pop':'Sweep';
modeBtn.addEventListener('click',()=>{mode=mode==='sweep'?'pop':'sweep';
  localStorage.setItem('lyrmode',mode); modeBtn.textContent=mode==='pop'?'Pop':'Sweep';});

// Live sync offset (seconds): positive = lyrics lead the audio (fixes a "starts late" feel).
const nudgeBtn=document.getElementById('nudge');
let OFFSET=parseFloat(localStorage.getItem('lyroffset')||'0')||0;
const showOffset=()=>{const ms=Math.round(OFFSET*1000);nudgeBtn.textContent='sync '+(ms>0?'+':'')+ms+'ms';};
const setOffset=v=>{OFFSET=Math.max(-2,Math.min(2,Math.round(v*100)/100));
  localStorage.setItem('lyroffset',OFFSET);showOffset();};
showOffset();
nudgeBtn.addEventListener('click',()=>setOffset(0));   // click to reset

const lineEls=[];
function mkWord(wd){const sp=document.createElement('span');sp.className='word';sp.textContent=wd.w;
  sp.style.setProperty('--p','0%');wd.el=sp;return sp;}
DATA.lines.forEach((ln,i)=>{
  const d=document.createElement('div');d.className='line future';
  const mw=document.createElement('div');mw.className='main';
  ln.words.forEach((wd,j)=>{mw.appendChild(mkWord(wd));if(j<ln.words.length-1)mw.appendChild(document.createTextNode(' '));});
  d.appendChild(mw);
  if(ln.bg&&ln.bg.length){const bw=document.createElement('div');bw.className='bg';
    ln.bg.forEach((wd,j)=>{bw.appendChild(mkWord(wd));if(j<ln.bg.length-1)bw.appendChild(document.createTextNode(' '));});
    d.appendChild(bw);}
  d.addEventListener('click',()=>{audio.currentTime=Math.max(0,ln.start-0.15);if(audio.paused)audio.play();});
  wrap.appendChild(d);lineEls.push(d);
});

let activeIdx=-2;
function findActive(t){let idx=-1;for(let i=0;i<DATA.lines.length;i++){if(DATA.lines[i].start<=t)idx=i;else break;}return idx;}
function setLineStates(idx){
  lineEls.forEach((el,i)=>{
    const cls=i<idx?'line past':(i===idx?'line active':'line future');
    if(el.className!==cls){el.className=cls;
      if(i!==idx){const p=i<idx?'100%':'0%';allWords(DATA.lines[i]).forEach(wd=>wd.el.style.setProperty('--p',p));}}
  });
  const el=lineEls[idx<0?0:idx];
  const target=el?el.offsetTop+el.offsetHeight/2:0;
  wrap.style.transform=`translateY(${stage.clientHeight/2-target}px)`;
}
function tick(){
  const t=audio.currentTime+OFFSET, idx=findActive(t);
  if(idx!==activeIdx){activeIdx=idx;setLineStates(idx);}
  if(idx>=0)for(const wd of allWords(DATA.lines[idx])){
    let p = mode==='pop'
      ? (t<wd.t0?0:Math.min(1,(t-wd.t0)/POP)*100)         // crisp: fill fast at onset, then hold
      : (t<wd.t0?0:Math.min(1,(t-wd.t0)/wd.fill)*100);    // smooth sweep at the sung cadence, then hold
    wd.el.style.setProperty('--p',p.toFixed(1)+'%');}
  curT.textContent=fmt(t);
  if(audio.duration)seek.value=(t/audio.duration*100);
  requestAnimationFrame(tick);
}
audio.addEventListener('loadedmetadata',()=>{durT.textContent=fmt(audio.duration);});
playBtn.addEventListener('click',()=>audio.paused?audio.play():audio.pause());
audio.addEventListener('play',()=>playBtn.textContent='⏸');
audio.addEventListener('pause',()=>playBtn.textContent='▶');
seek.addEventListener('input',()=>{if(audio.duration)audio.currentTime=seek.value/100*audio.duration;});
document.addEventListener('keydown',e=>{
  if(e.code==='Space'){e.preventDefault();playBtn.click();}
  else if(e.code==='ArrowRight'){e.preventDefault();setOffset(OFFSET+0.05);}  // lyrics earlier
  else if(e.code==='ArrowLeft'){e.preventDefault();setOffset(OFFSET-0.05);}   // lyrics later
});
window.__tick=tick;
requestAnimationFrame(tick);
</script></body></html>"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ttml", required=True)
    ap.add_argument("--audio", required=True, help="audio filename to reference (relative to the html)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--gen-seconds", default="")
    a = ap.parse_args()
    lines = parse_ttml(open(a.ttml, encoding="utf-8").read())
    data = {"title": a.title, "artist": a.artist, "lines": lines}
    geninfo = f" · generated in {round(float(a.gen_seconds))}s" if a.gen_seconds else ""
    out = (TEMPLATE.replace("__TITLE__", html.escape(a.title))
                   .replace("__ARTIST__", html.escape(a.artist))
                   .replace("__AUDIO__", html.escape(a.audio))
                   .replace("__GENINFO__", html.escape(geninfo))
                   .replace("__JSON__", json.dumps(data)))
    open(a.out, "w", encoding="utf-8").write(out)
    nbg = sum(len(l["bg"]) for l in lines)
    print(f"wrote {a.out}: {len(lines)} lines, "
          f"{sum(len(l['words']) for l in lines)} main words, {nbg} background words")

if __name__ == "__main__":
    main()
