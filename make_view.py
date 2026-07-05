#!/usr/bin/env python3
"""Turn a word-by-word TTML (with optional x-bg background vocals) into a
self-contained karaoke HTML page: blurred cover backdrop, glowing active line,
per-word gradient fill."""
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
<title>__TITLE__ · lyrics</title>
<style>
  :root{
    --lit:#fff; --dim:rgba(255,255,255,.3);
    --accent:#4aa3e8; --accent-soft:#8ec2ea;
    --ink:#0a121b; --muted:#93a5b6;
    --mono:"Cascadia Code",Consolas,ui-monospace,monospace;
  }
  *{box-sizing:border-box; margin:0; padding:0;}
  html,body{height:100%;}
  body{font-family:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
    background:#0b131c; color:#fff; overflow:hidden; user-select:none;}

  /* blurred cover backdrop, falls back to a slate gradient */
  #bg{position:fixed; inset:0; z-index:0;
    background:radial-gradient(1100px 700px at 50% -10%,#1a2836 0%,#0b131c 60%,#080e15 100%);}
  #bg img{position:absolute; inset:-12%; width:124%; height:124%; object-fit:cover;
    filter:blur(90px) saturate(1.4) brightness(.52); opacity:0; transition:opacity 1.2s ease;}
  #bg img.on{opacity:1;}
  #bg .veil{position:absolute; inset:0;
    background:linear-gradient(rgba(10,17,26,.32),rgba(9,15,23,.5) 60%,rgba(8,13,20,.78));}

  #top{position:fixed; top:0; left:0; right:0; z-index:10; display:flex; align-items:center;
    gap:14px; padding:16px 24px; background:linear-gradient(rgba(8,13,20,.82),transparent);}
  #cd{width:46px; height:46px; border-radius:10px; object-fit:cover; flex:none;
    border:1px solid rgba(255,255,255,.14); box-shadow:0 6px 18px -6px rgba(0,0,0,.6);}
  #top h1{font-size:16.5px; font-weight:700; line-height:1.2;}
  #top .sub{font-size:12.5px; color:var(--muted); margin-top:2px;}

  #stage{position:absolute; inset:0; z-index:1; overflow:hidden; pointer-events:none;
    -webkit-mask-image:linear-gradient(transparent 7%,#000 24%,#000 76%,transparent 93%);
            mask-image:linear-gradient(transparent 7%,#000 24%,#000 76%,transparent 93%);}
  #lyrics{position:absolute; top:0; left:0; right:0; margin:0 auto; width:min(920px,92vw);
    padding:0 8px; transition:transform .5s cubic-bezier(.22,.61,.36,1); will-change:transform;}

  .line{padding:12px 0; cursor:pointer; pointer-events:auto;
    transition:opacity .35s,filter .35s,transform .35s; transform-origin:left center;}
  .line .main{font-size:clamp(28px,4.4vw,44px); font-weight:800; line-height:1.34;
    letter-spacing:.1px; color:var(--dim);}
  .line .bg{font-size:clamp(16px,2.2vw,21px); font-weight:700; font-style:italic;
    margin-top:3px; color:var(--dim);}
  .line.future{opacity:.42;}
  .line.past{opacity:.25;}
  .line.active{opacity:1; transform:scale(1.02);}
  .line:not(.active){filter:blur(.6px);}
  .line:hover{opacity:.9;}

  /* padding-bottom + matching negative margin gives descenders (g, y, p) room:
     -webkit-background-clip:text clips the fill to the box, so a tight box would
     shave their tails. The negative margin keeps line spacing unchanged. */
  .word{display:inline-block; white-space:pre; padding-bottom:.16em; margin-bottom:-.16em;
    background:linear-gradient(90deg,var(--lit) var(--p,0%),var(--dim) var(--p,0%));
    -webkit-background-clip:text; background-clip:text; color:transparent;
    transition:transform .18s ease;}
  .bg .word{background:linear-gradient(90deg,var(--accent-soft) var(--p,0%),var(--dim) var(--p,0%));
    -webkit-background-clip:text; background-clip:text;}
  .line.past .word{--p:100%;}
  .line.active .word{filter:drop-shadow(0 0 15px rgba(255,255,255,.3));}
  .line.active .bg .word{filter:drop-shadow(0 0 12px rgba(142,194,234,.35));}
  .word.sing{transform:translateY(-2px) scale(1.05);}

  .hint{position:fixed; bottom:78px; left:0; right:0; z-index:10; text-align:center;
    font-size:11.5px; color:rgba(255,255,255,.34); pointer-events:none;}

  #bar{position:fixed; left:0; right:0; bottom:0; z-index:10; display:flex; align-items:center;
    gap:13px; padding:14px 24px 16px; background:linear-gradient(transparent,rgba(7,12,18,.94) 55%);}
  #play{width:44px; height:44px; border:none; border-radius:50%; background:var(--accent);
    color:var(--ink); cursor:pointer; flex:none; display:grid; place-items:center;
    transition:filter .15s,transform .1s;}
  #play:hover{filter:brightness(1.12);}
  #play:active{transform:scale(.94);}
  #play svg{width:16px; height:16px; fill:currentColor;}
  .t{font-family:var(--mono); font-variant-numeric:tabular-nums; font-size:11.5px;
    color:var(--muted); min-width:42px; text-align:center;}
  #seek{-webkit-appearance:none; appearance:none; flex:1; height:5px; border-radius:99px;
    background:rgba(255,255,255,.14); cursor:pointer; outline-offset:4px;}
  #seek::-webkit-slider-thumb{-webkit-appearance:none; width:13px; height:13px; border-radius:50%;
    background:#fff; border:none; transform:scale(0); transition:transform .15s;
    box-shadow:0 2px 8px rgba(0,0,0,.5);}
  #seek:hover::-webkit-slider-thumb,#seek:focus-visible::-webkit-slider-thumb{transform:scale(1);}
  #seek::-moz-range-thumb{width:13px; height:13px; border-radius:50%; background:#fff; border:none;}
  #mode,#nudge{flex:none; border:1px solid rgba(255,255,255,.18); background:rgba(255,255,255,.05);
    color:#c8d4de; border-radius:999px; padding:6.5px 14px; font-size:12px; cursor:pointer;
    white-space:nowrap; font-variant-numeric:tabular-nums; font-family:inherit;
    transition:border-color .15s,color .15s;}
  #mode:hover,#nudge:hover{border-color:var(--accent); color:#fff;}

  @media (prefers-reduced-motion: reduce){
    .line,.word,#lyrics{transition:none;}
    .word.sing{transform:none;}
    .line.active{transform:none;}
  }
  @media (max-width:640px){
    #top{padding:12px 14px;} .hint{display:none;}
    #bar{padding:10px 14px 12px; gap:9px;} #mode{display:none;}
  }
</style></head>
<body>
  <div id="bg"><img src="__COVER__" alt="" onload="this.classList.add('on')" onerror="this.remove()"><div class="veil"></div></div>
  <header id="top">
    <img id="cd" src="__COVER__" alt="" onerror="this.style.display='none'">
    <div>
      <h1>__TITLE__</h1>
      <div class="sub">__ARTIST__</div>
    </div>
  </header>
  <div id="stage"><div id="lyrics"></div></div>
  <div class="hint">click a line to jump · space plays or pauses · arrow keys nudge sync · blue words are background vocals</div>
  <footer id="bar">
    <button id="play" aria-label="Play or pause"><svg viewBox="0 0 24 24"><path d="M7 4.5v15l13-7.5z"/></svg></button>
    <span class="t" id="cur">0:00</span>
    <input id="seek" type="range" min="0" max="100" value="0" step="0.01" aria-label="Seek">
    <span class="t" id="dur">0:00</span>
    <button id="nudge" title="Shift lyric timing against the audio (arrow keys, click to reset)">sync 0ms</button>
    <button id="mode" title="Sweep fills each word smoothly. Pop highlights it at onset.">Sweep</button>
  </footer>
  <audio id="audio" src="__AUDIO__" preload="auto"></audio>
<script>
const DATA = __JSON__;
const audio=document.getElementById('audio'), wrap=document.getElementById('lyrics'),
      stage=document.getElementById('stage'), playBtn=document.getElementById('play'),
      seek=document.getElementById('seek'), curT=document.getElementById('cur'), durT=document.getElementById('dur'),
      modeBtn=document.getElementById('mode');
const ICON_PLAY='<svg viewBox="0 0 24 24"><path d="M7 4.5v15l13-7.5z"/></svg>';
const ICON_PAUSE='<svg viewBox="0 0 24 24"><path d="M6 4h4v16H6zm8 0h4v16h-4z"/></svg>';
const fmt=s=>{s=Math.max(0,s|0);return (s/60|0)+':'+String(s%60).padStart(2,'0');};
const allWords=ln=>ln.bg&&ln.bg.length?ln.words.concat(ln.bg):ln.words;
const POP=0.10; // pop-fill duration (s)
// Sweep fill: a word fills over the SHORTER of its own sung length and the time until the
// next word starts, then holds. So a short word before a long gap (e.g. a held "to") fills
// crisply and waits, instead of crawling, while fast words still hand off seamlessly. A
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
      if(i!==idx){const p=i<idx?'100%':'0%';allWords(DATA.lines[i]).forEach(wd=>{
        wd.el.style.setProperty('--p',p);wd.el.classList.remove('sing');});}}
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
    wd.el.style.setProperty('--p',p.toFixed(1)+'%');
    wd.el.classList.toggle('sing', t>=wd.t0 && t<=wd.t1+0.12);}
  curT.textContent=fmt(t);
  if(audio.duration){const pc=t/audio.duration*100;seek.value=pc;
    seek.style.background=`linear-gradient(90deg,var(--accent) ${pc}%,rgba(255,255,255,.14) ${pc}%)`;}
  requestAnimationFrame(tick);
}
audio.addEventListener('loadedmetadata',()=>{durT.textContent=fmt(audio.duration);});
playBtn.addEventListener('click',()=>audio.paused?audio.play():audio.pause());
audio.addEventListener('play',()=>playBtn.innerHTML=ICON_PAUSE);
audio.addEventListener('pause',()=>playBtn.innerHTML=ICON_PLAY);
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
    ap.add_argument("--cover", default="", help="cover art url (e.g. /covers/<id>.jpg)")
    ap.add_argument("--gen-seconds", default="")
    a = ap.parse_args()
    lines = parse_ttml(open(a.ttml, encoding="utf-8").read())
    data = {"title": a.title, "artist": a.artist, "lines": lines}
    out = (TEMPLATE.replace("__TITLE__", html.escape(a.title))
                   .replace("__ARTIST__", html.escape(a.artist))
                   .replace("__AUDIO__", html.escape(a.audio))
                   .replace("__COVER__", html.escape(a.cover))
                   .replace("__JSON__", json.dumps(data)))
    open(a.out, "w", encoding="utf-8").write(out)
    nbg = sum(len(l["bg"]) for l in lines)
    print(f"wrote {a.out}: {len(lines)} lines, "
          f"{sum(len(l['words']) for l in lines)} main words, {nbg} background words")

if __name__ == "__main__":
    main()
