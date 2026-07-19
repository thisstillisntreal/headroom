
"use strict";
const CONFIG = (typeof window !== "undefined" && window.__HEADROOM_CONFIG__ != null)
  ? window.__HEADROOM_CONFIG__
  : /*__HEADROOM_CONFIG__*/ null;
const DATA_URL = "usage.json";
const CACHE_KEY = "headroom-cache-" + ((CONFIG && CONFIG.redact) ? "r" : "f");
const SNAPSHOT_MAX_AGE = CONFIG&&Number.isFinite(CONFIG.snapshot_max_age)?CONFIG.snapshot_max_age:900;
const OBSERVATION_MAX_AGE = CONFIG&&Number.isFinite(CONFIG.observation_max_age)?CONFIG.observation_max_age:1800;
let requestNumber = 0;

/* ------------------------------------------------------------- helpers */
function esc(v){return String(v==null?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function clamp(v,lo,hi){return Math.min(hi,Math.max(lo,v));}
function tone(left){if(left==null)return"unknown";if(left<=10)return"red";if(left<=30)return"orange";if(left<=50)return"yellow";return"green";}
function age(epoch){if(!Number.isFinite(epoch))return"time unknown";const s=Math.max(0,Math.floor(Date.now()/1e3-epoch));if(s<60)return s+"s ago";if(s<3600)return Math.floor(s/60)+"m ago";if(s<86400)return Math.floor(s/3600)+"h "+Math.floor(s%3600/60)+"m ago";return Math.floor(s/86400)+"d ago";}
function untilReset(epoch){if(!Number.isFinite(epoch))return"reset not reported";const s=epoch-Math.floor(Date.now()/1e3);if(s<=0)return"reset due";if(s<86400){const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return"resets in "+(h?h+"h ":"")+m+"m";}return"resets "+new Intl.DateTimeFormat(undefined,{weekday:"short",hour:"2-digit",minute:"2-digit"}).format(new Date(1e3*epoch));}
let snapshotState="held",sourceFailed=false; /* old/failed sources demote every row */
function epochState(epoch,maxAge){const now=Date.now()/1e3;if(!Number.isFinite(epoch)||epoch>now)return"held";return now-epoch>maxAge?"stale":"current";}
function projected(a){return a&&a.__display&&typeof a.__display==="object"?a.__display:null;}
function intrinsicWindowState(a,key){
  const d=projected(a),w=d&&d.windows&&d.windows[key];
  if(!w||w.state==="held")return"held";
  if(w.state==="stale")return"stale";
  const timed=epochState(w.observed_at,OBSERVATION_MAX_AGE);
  if(timed!=="current")return timed;
  return w.state==="limited"?"limited":(w.state==="current"?"current":"held");
}
function displayState(a){
  if(sourceFailed)return"stale";
  if(snapshotState!=="current")return snapshotState;
  const d=projected(a);
  if(!d||d.state==="held")return"held";
  if(d.state==="stale")return"stale";
  const captured=epochState(a&&a.captured_at,OBSERVATION_MAX_AGE);
  if(captured!=="current")return captured;
  /* 5h is optional only for a live codex seat (OpenAI lifted it): skip an
     absent 5h so it can't force "held". 7d is always counted (mandatory), and
     a non-codex seat always counts 5h too, so a missing 5h there still holds
     (fail-closed). Grok SuperGrok meters a monthly allotment only. */
  const win=(d&&d.windows)||{};
  const keys=a.provider==="nvidia"?["5h","7d","month"]
    :(a.provider==="grok"||a.provider==="manus")?["month"]
    :["5h","7d"].filter(k=>k==="7d"||a.provider!=="codex"||win[k]!==undefined);
  const states=keys.map(k=>intrinsicWindowState(a,k));
  if(states.includes("held"))return"held";
  if(states.includes("stale"))return"stale";
  return states.includes("limited")?"limited":"current";
}
function windowState(a,key){const state=displayState(a);return state==="held"||state==="stale"?state:intrinsicWindowState(a,key);}
function windowView(a,key){
  const d=projected(a),w=d&&d.windows&&d.windows[key],state=windowState(a,key),accountState=displayState(a);
  const limited=accountState==="limited"&&state==="limited";
  /* A "limited" account can still have "current" windows with valid readings
     (e.g. 5h is fine but 7d hit the cap).  Show left_percent for any current
     window, not only when the account-level state is also current. */
  const currentWin=state==="current"&&w;
  const left=currentWin&&Number.isFinite(w.left_percent)?w.left_percent:(limited&&w&&Number.isFinite(w.last_observed_left_percent)?w.last_observed_left_percent:null);
  const projTone=currentWin&&["green","yellow","orange","red"].includes(w.tone)?w.tone:null;
  const safeTone=projTone||(left!=null?tone(left):"unknown");
  return{state:state,left:left,tone:safeTone};
}
function isCurrent(a){return displayState(a)==="current";}

function windowLabel(key, account){
  const w=account&&account.windows&&account.windows[key];
  if(key==="total"||(account&&account.provider==="manus"&&key==="5h"&&w&&w.label==="Total Credits"))
    return"Total Credits";
  if(w&&w.label&&!(account&&account.provider==="manus"&&key==="5h"))return w.label;
  if(account&&account.provider==="manus"){
    if(key==="total")return"Total Credits";
    if(key==="month")return"Monthly credits";
  }
  if(key==="5h")return"Session 5h";
  if(key==="7d")return"Weekly all";
  if(key==="month")return"Monthly allotment";
  if(key.startsWith("scoped:"))return"Weekly "+key.slice(7);
  return key;
}
function windowColumns(accounts){
  // Manus: Total Credits + Monthly only (no daily refresh column)
  if(accounts.length&&accounts.every(a=>a.provider==="manus"))
    return["total","month"];
  const keys=["5h","7d"];
  accounts.forEach(a=>Object.keys(a.windows||{}).forEach(k=>{
    if((k.startsWith("scoped:")||k==="month")&&!keys.includes(k))keys.push(k);}));
  // Keep month after standards for NVIDIA/Grok fleets
  if(keys.includes("month")){
    const rest=keys.filter(k=>k!=="month");
    return rest.concat(["month"]);
  }
  return keys;
}

/* -------------------------------------------------------------- render */
function meterMarkup(left,displayTone){
  let cells="";
  for(let i=0;i<10;i++){
    const fill=left==null?0:clamp((left-10*i)*10,0,100);
    cells+='<span class="cell" style="--fill:'+fill+'%"></span>';
  }
  return '<div class="meter tone-'+(displayTone||tone(left))+'" role="img" aria-label="'+(left==null?"unknown":Math.round(left)+"% remaining")+'">'+cells+"</div>";
}
function fmtUnits(n){
  if(n==null||!Number.isFinite(Number(n)))return null;
  const v=Number(n);
  if(Math.abs(v)>=1e6)return (v/1e6).toFixed(2)+"M";
  if(Math.abs(v)>=1e3)return (v/1e3).toFixed(1)+"k";
  return Number.isInteger(v)?String(v):v.toFixed(1);
}
function manusTotalWindow(account){
  /* Synthetic Total Credits column.
     Full tank = monthly credits allotment (pro_monthly).
     Total available can exceed Full Tank (free credits) → meter past 100%. */
  const c=account&&account.credits;
  const avail=manusTotalAvailable(account);
  const full=manusTankFull(account); // monthly allotment
  if(avail==null)return null;
  const monthlyLeft=c&&c.periodic!=null?Number(c.periodic):null;
  // % of full tank filled by TOTAL available (can be >100 when free credits push past F)
  const fillPct=full&&full>0?Math.max(0,(avail/full)*100):null;
  const used=full!=null&&monthlyLeft!=null?Math.max(0,full-monthlyLeft):null;
  const usedPct=full&&full>0&&monthlyLeft!=null
    ?clamp(((full-monthlyLeft)/full)*100,0,100):null;
  const over=fillPct!=null&&fillPct>100;
  return{
    left:fillPct, // may be >100
    tone:over?"green":(fillPct==null?"unknown":tone(Math.min(fillPct,100))),
    avail:avail,
    full:full,
    monthlyLeft:monthlyLeft,
    used:used,
    usedPct:usedPct,
    over:over
  };
}
function windowMarkup(account,key){
  // Manus: Total Credits column replaces Daily refresh
  if(account.provider==="manus"&&key==="total"){
    const tw=manusTotalWindow(account);
    if(!tw||displayState(account)==="held"||displayState(account)==="stale"){
      return '<div class="win"><span class="win-name">Total Credits</span>'+
        '<span class="win-value tone-unknown">n/a</span>'+
        meterMarkup(null,"unknown")+
        '<p class="win-note">no total available</p></div>';
    }
    const value=fmtCredits(tw.avail)||"n/a";
    const fillShow=tw.left==null?"—":(Math.round(tw.left)+"% of tank");
    const detail='<div class="win-detail">'+
      '<span>TOTAL CREDITS '+esc(fmtCredits(tw.avail))+"</span>"+
      (tw.full!=null?'<span>FULL TANK '+esc(fmtCredits(tw.full))+" monthly</span>":"")+
      (tw.over?'<span class="win-countdown">PAST FULL · free credits above tank</span>':"")+
      (tw.used!=null?'<span>MONTHLY USED '+esc(fmtCredits(tw.used))+
        (tw.usedPct!=null?" ("+Math.round(tw.usedPct)+"%)":"")+"</span>":"")+
      (tw.monthlyLeft!=null?'<span>MONTHLY LEFT '+esc(fmtCredits(tw.monthlyLeft))+"</span>":"")+
      "</div>";
    // Meter caps at 100% visually; over-full shown by value + note
    const meterLeft=tw.left==null?null:Math.min(tw.left,100);
    return '<div class="win"><span class="win-name">Total Credits</span>'+
      '<span class="win-value tone-'+tw.tone+'">'+esc(value)+"</span>"+
      meterMarkup(meterLeft,tw.tone)+
      '<p class="win-note">'+esc(fillShow)+(tw.over?" · peg past F":"")+"</p>"+detail+"</div>";
  }

  const w=(account.windows||{})[key]||null;
  const view=windowView(account,key),left=view.left;
  const unknown=left==null;
  /* A standard window absent from a live CODEX/GROK account is a lifted/N/A
     limit, not a failed read: render it neutrally, never as a fake 0% or an
     n/a error. Codex: 5h lifted. Grok: only monthly allotment exists, so 5h/7d
     are not metered. Gate on provider + not-held so limited seats still read
     cleanly, and other providers still fail closed to "n/a". */
  const absentLimit=!w&&!key.startsWith("scoped:")
    &&((account.provider==="codex"&&key==="5h")
       ||((account.provider==="grok"||account.provider==="manus")
          &&(key==="5h"||key==="7d"||key==="total")))
    &&displayState(account)!=="held";
  const usedPct=w&&Number.isFinite(w.used_percent)?w.used_percent:
    (left!=null?100-left:null);
  const value=unknown?(absentLimit?"—":"n/a"):Math.round(left)+"% left";
  const countdown=w&&(w.countdown||untilReset(w.resets_at));
  const note=unknown?(absentLimit?"no "+key+" limit":(w?"reading unavailable":(key.startsWith("scoped:")?"no scoped cap":"no reading yet"))):
    ("used "+(usedPct==null?"?":Math.round(usedPct)+"%")+(countdown?" · "+countdown:""));
  const haveU=fmtUnits(w&&w.limit_units);
  const usedU=fmtUnits(w&&w.used_units);
  const leftU=fmtUnits(w&&w.remaining_units);
  let detail="";
  if(!unknown&&(haveU||usedU||leftU||countdown)){
    detail='<div class="win-detail">'+
      (haveU?'<span>HAVE '+esc(haveU)+"</span>":"")+
      (usedU?'<span>USED '+esc(usedU)+(usedPct!=null?" ("+Math.round(usedPct)+"%)":"")+"</span>":
        (usedPct!=null?'<span>USED '+Math.round(usedPct)+"%</span>":""))+
      (leftU?'<span>LEFT '+esc(leftU)+(left!=null?" ("+Math.round(left)+"%)":"")+"</span>":
        (left!=null?'<span>LEFT '+Math.round(left)+"%</span>":""))+
      (countdown?'<span class="win-countdown">↻ '+esc(countdown)+"</span>":"")+
      "</div>";
  }
  return '<div class="win"><span class="win-name">'+esc(windowLabel(key,account))+
    '</span><span class="win-value tone-'+view.tone+'">'+esc(value)+"</span>"+
    meterMarkup(left,view.tone)+'<p class="win-note">'+esc(note)+"</p>"+detail+"</div>";
}
function fmtCredits(n){
  /* Manus-style whole credit counts with thousands separators */
  if(n==null||!Number.isFinite(Number(n)))return null;
  return Math.round(Number(n)).toLocaleString();
}
function manusCreditsStrip(account){
  /* Manus breakdown under the meters — no daily refresh (that column is gone).
       Total Credits · Free credits · Monthly credits
  */
  const c=account&&account.credits;
  if(!c||account.provider!=="manus")return"";
  const free=c.free!=null?Number(c.free):null;
  const monthlyLeft=c.periodic!=null?Number(c.periodic):null;
  const monthlyHave=c.pro_monthly!=null?Number(c.pro_monthly):null;
  let totalCredits=c.spendable!=null?Number(c.spendable):null;
  if(totalCredits==null&&(free!=null||monthlyLeft!=null))
    totalCredits=(free||0)+(monthlyLeft||0);
  const bits=[];
  if(totalCredits!=null)
    bits.push({lab:"Total Credits",val:fmtCredits(totalCredits),sub:"free + monthly left"});
  if(free!=null)
    bits.push({lab:"Free credits",val:fmtCredits(free)});
  if(monthlyLeft!=null||monthlyHave!=null)
    bits.push({lab:"Monthly credits",
      val:(fmtCredits(monthlyLeft)||"?")+" / "+(fmtCredits(monthlyHave)||"?")});
  if(!bits.length)return"";
  return '<div class="acct-credits-strip" aria-label="Manus credits">'+
    bits.map(b=>'<div><span class="c-lab">'+esc(b.lab)+'</span>'+
      '<span class="c-val">'+esc(b.val)+'</span>'+
      (b.sub?'<span class="c-sub">'+esc(b.sub)+'</span>':"")+
      "</div>").join("")+"</div>";
}
// Calm, specific state per account. Codex reads its CLI's own telemetry, so
// idle/limited are EXPECTED states — label them clearly, never as "broken".
function accountState(account){
  const state=displayState(account);
  if(state==="current"){
    return{cls:"live",label:"Live",note:"observed "+age(account.captured_at)};
  }
  if(state==="limited")return{cls:"limited",label:"At limit",note:"window full · observed "+age(account.captured_at)};
  if(state==="stale")return{cls:"held",label:"Stale",note:"last verified "+age(account.captured_at)};
  const code=account.error_code||"";
  const isCodex=account.provider==="codex";
  if(code==="codex_usage_limit"||/usage limit/i.test(account.note||""))
    return{cls:"held",label:"Held",note:account.retry_at?("resets "+untilReset(account.retry_at)):"usage limit reported"};
  if(code==="codex_reauth_required")
    return{cls:"held",label:"Sign-in expired",note:"run `codex login` on this account"};
  if(isCodex&&account.stale)
    return{cls:"idle",label:"Idle",note:"last seen "+age(account.captured_at)+" · run Codex to refresh"};
  if(isCodex&&/no .*telemetry|no rate_limits/i.test(account.note||""))
    return{cls:"idle",label:"Waiting",note:"run Codex once to start tracking"};
  return{cls:"held",label:"Held",note:account.note||account.error_code||"no verified reading"};
}
function renderFleetUsage(data){
  const fu=data.fleet_usage||{};
  const sinceEl=document.getElementById("usage-since");
  const sinceSub=document.getElementById("usage-since-sub");
  const usedEl=document.getElementById("usage-used");
  const wasteEl=document.getElementById("usage-waste");
  const resetsEl=document.getElementById("usage-resets");
  if(!sinceEl)return;
  const started=fu.tracking_started||fu.tracking_started_iso;
  if(fu.tracking_started_iso){
    const d=new Date(fu.tracking_started_iso);
    sinceEl.textContent=d.toLocaleDateString(undefined,{month:"short",day:"numeric",year:"numeric"});
  }else if(Number.isFinite(fu.tracking_started)){
    sinceEl.textContent=new Date(fu.tracking_started*1000).toLocaleDateString();
  }else sinceEl.textContent="—";
  if(sinceSub&&Number.isFinite(fu.tracking_age_seconds)){
    const days=Math.floor(fu.tracking_age_seconds/86400);
    const hours=Math.floor((fu.tracking_age_seconds%86400)/3600);
    sinceSub.textContent=days>0?(days+"d "+hours+"h of tracking"):(hours+"h of tracking");
  }
  const usedU=fmtUnits(fu.lifetime_used_units);
  if(usedEl)usedEl.textContent=usedU||"0";
  const wastePct=Number(fu.lifetime_wasted_percent_points||0);
  const wasteU=fmtUnits(fu.lifetime_wasted_units);
  if(wasteEl){
    wasteEl.textContent=(wastePct?wastePct.toFixed(1)+"% pts":"0%")+
      (wasteU?" · "+wasteU:"");
    wasteEl.className="val"+(wastePct>=50?" is-bad":wastePct>0?" is-warn":"");
  }
  if(resetsEl)resetsEl.textContent=String(fu.reset_events||0);
}
function money(usd){
  const n=Number(usd);
  if(!Number.isFinite(n))return null;
  return Number.isInteger(n)?"$"+n:"$"+n.toFixed(2);
}
function formatCost(usd, mode){
  /* mode: "mo" | "yr" | "both" (default both — monthly + yearly translation) */
  if(usd==null||!Number.isFinite(Number(usd)))return null;
  const mo=Number(usd), yr=mo*12;
  const m=money(mo), y=money(yr);
  if(mode==="mo")return m+"/mo";
  if(mode==="yr")return y+"/yr";
  return m+"/mo · "+y+"/yr";
}
function manusTotalAvailable(account){
  /* Manus total available = free + monthly remaining (not daily). */
  const c=account&&account.credits;
  if(!c)return null;
  if(c.spendable!=null&&Number.isFinite(Number(c.spendable)))return Number(c.spendable);
  const free=c.free!=null?Number(c.free):0;
  const monthly=c.periodic!=null?Number(c.periodic):0;
  if(c.free==null&&c.periodic==null)return null;
  return free+monthly;
}
function manusTankFull(account){
  /* Full tank = operator renew_amount if set, else monthly allotment (pro_monthly). */
  if(account&&account.renew_amount!=null&&Number.isFinite(Number(account.renew_amount))
     &&Number(account.renew_amount)>0)
    return Number(account.renew_amount);
  const c=account&&account.credits;
  if(!c||c.pro_monthly==null||!Number.isFinite(Number(c.pro_monthly)))return null;
  const monthlyHave=Number(c.pro_monthly);
  return monthlyHave>0?monthlyHave:null;
}
function formatRenewDate(iso){
  if(!iso||typeof iso!=="string")return null;
  const d=new Date(iso.slice(0,10)+"T12:00:00");
  if(Number.isNaN(d.getTime()))return iso.slice(0,10);
  return d.toLocaleDateString(undefined,{month:"short",day:"numeric",year:"numeric"});
}
function renewCountdown(iso){
  if(!iso||typeof iso!=="string")return null;
  const end=new Date(iso.slice(0,10)+"T00:00:00");
  if(Number.isNaN(end.getTime()))return null;
  const now=new Date();
  now.setHours(0,0,0,0);
  const days=Math.round((end-now)/864e5);
  if(days<0)return"overdue · update date";
  if(days===0)return"renews today";
  if(days===1)return"renews tomorrow";
  if(days<14)return"in "+days+" days";
  return null;
}
function accountRenewMarkup(account){
  const date=account.renews_on;
  const amount=account.renew_amount;
  if(date==null&&(amount==null||amount===""))return"";
  const bits=[];
  if(date){
    const cd=renewCountdown(date);
    bits.push("Renews <strong>"+esc(formatRenewDate(date)||date)+"</strong>"+
      (cd?' <span class="renew-soon">('+esc(cd)+")</span>":""));
  }
  if(amount!=null&&amount!==""&&Number.isFinite(Number(amount)))
    bits.push("renews at <strong>"+esc(fmtCredits(Number(amount))||String(amount))+
      "</strong> credits");
  return '<p class="acct-renew">'+bits.join(" · ")+"</p>";
}
function accountTankLeft(account){
  /* Gas-tank fill % — Full=F mark, Empty=E.
     Manus: total available / monthly Full Tank. Can be >100 when free
     credits push total past the monthly allotment (needle past F). */
  if(!account||displayState(account)==="held"||displayState(account)==="stale")
    return null;
  if(account.provider==="manus"){
    const full=manusTankFull(account);
    const avail=manusTotalAvailable(account);
    if(full==null||full<=0||avail==null)return null;
    // Do NOT clamp to 100 — past-full is intentional for free+monthly > allotment
    return Math.max(0,(avail/full)*100);
  }
  const prefer=account.provider==="grok"
    ?["month","5h","7d"]
    :(account.provider==="nvidia")
      ?["5h","7d","month"]
      :["5h","7d","month"];
  const keys=Object.keys(account.windows||{});
  const ordered=[...prefer.filter(k=>keys.includes(k)),
    ...keys.filter(k=>!prefer.includes(k)&&!k.startsWith("scoped:"))];
  let lowest=null;
  ordered.forEach(k=>{
    const view=windowView(account,k);
    if(view.state==="current"&&view.left!=null)
      lowest=lowest==null?view.left:Math.min(lowest,view.left);
  });
  return lowest==null?null:clamp(lowest,0,100);
}
function polar(cx,cy,r,deg){
  /* deg: 0=right, 90=down, 180=left, 270=up (SVG coords, y down) */
  const rad=deg*Math.PI/180;
  return{x:cx+r*Math.cos(rad), y:cy+r*Math.sin(rad)};
}
function arcPath(cx,cy,r,startDeg,endDeg){
  /* Sweep clockwise (SVG sweep=1) from startDeg → endDeg. */
  const s=polar(cx,cy,r,startDeg), e=polar(cx,cy,r,endDeg);
  let delta=((endDeg-startDeg)%360+360)%360;
  if(delta===0)delta=360;
  const large=delta>180?1:0;
  return "M "+s.x.toFixed(2)+" "+s.y.toFixed(2)+
    " A "+r+" "+r+" 0 "+large+" 1 "+e.x.toFixed(2)+" "+e.y.toFixed(2);
}
function gasGaugeMarkup(account){
  /* Circular fuel-gauge: E (empty) → F (full tank).
     Manus Full Tank = monthly allotment. Total available can exceed that
     (free credits) so the needle is allowed PAST F. */
  const left=accountTankLeft(account);
  const unknown=left==null;
  const isManus=account&&account.provider==="manus";
  const avail=isManus?manusTotalAvailable(account):null;
  const fullTank=isManus?manusTankFull(account):null;
  // fill% can be >100 for Manus past-full; other providers stay 0–100
  const fill=unknown?0:(isManus?Math.max(0,left):clamp(left,0,100));
  const over=isManus&&fill>100;
  const t=unknown?"unknown":(over?"green":tone(Math.min(fill,100)));

  // Upper semicircle E→F; over-full continues past F (down-right)
  const cx=60, cy=58, r=44, trackR=44;
  const aE=180, aF=360, aMax=400; // 40° past F — peg can sit past the F mark
  const overCap=200; // % of tank shown; 200% = needle at aMax
  const fillDraw=Math.min(fill, overCap);
  // 0%→E, 100%→F, >100% continues past F toward aMax
  let needleDeg;
  if(fillDraw<=100)needleDeg=aE+(fillDraw/100)*(aF-aE);
  else needleDeg=aF+((fillDraw-100)/(overCap-100))*(aMax-aF);
  const tip=polar(cx,cy,r-7,needleDeg);
  const hubR=4.5;

  // Color segments: red→green to F, then accent past F (over-full zone)
  const segs=[
    {from:180,to:225,color:"var(--red)"},
    {from:225,to:270,color:"var(--orange, #f59e0b)"},
    {from:270,to:315,color:"var(--yellow, #eab308)"},
    {from:315,to:360,color:"var(--green)"},
    {from:360,to:aMax,color:"var(--accent, var(--green))"},
  ];
  const colorArcs=segs.map(s=>{
    if(s.from>=needleDeg)return"";
    const from=s.from;
    const to=Math.min(s.to, needleDeg);
    if(to<=from)return"";
    return '<path class="g-arc-color" d="'+arcPath(cx,cy,trackR,from,to)+
      '" stroke="'+s.color+'"></path>';
  }).join("");

  const ticks=[180,270,360].map(d=>{
    const outer=polar(cx,cy,r+2,d), inner=polar(cx,cy,r-8,d);
    return '<line class="g-tick" x1="'+outer.x.toFixed(1)+'" y1="'+outer.y.toFixed(1)+
      '" x2="'+inner.x.toFixed(1)+'" y2="'+inner.y.toFixed(1)+'"></line>';
  }).join("");

  const ePos=polar(cx,cy,r+13,180), fPos=polar(cx,cy,r+13,360);

  let labelText;
  if(unknown)labelText='<span class="g-pct is-unknown">—</span> tank unknown';
  else if(isManus&&avail!=null)
    labelText='<span class="g-pct is-'+t+'">'+esc(fmtCredits(avail))+'</span> total available'+
      (over?' <span class="g-pct is-green">· past F</span>':
        (fullTank!=null?' · tank '+esc(fmtCredits(fullTank)):""));
  else
    labelText='<span class="g-pct is-'+t+'">'+esc(Math.round(fill)+"%")+"</span> left";
  const aria=unknown?"tank unknown"
    :(isManus&&avail!=null
      ?(fmtCredits(avail)+" total available"+
        (over?", past full tank":"")+", "+Math.round(fill)+"% of monthly tank")
      :(Math.round(fill)+"% left"));

  // Track: full E→F always; over-full stub when past F
  const trackPath=arcPath(cx,cy,trackR,aE,over?aMax:aF);

  return '<div class="acct-gauge'+(unknown?" is-unknown":"")+(over?" is-overfull":"")+
    '" role="img" aria-label="'+esc(aria)+'">'+
    '<svg class="g-dial" viewBox="0 0 120 88" aria-hidden="true">'+
      '<path class="g-arc-track" d="'+trackPath+'"></path>'+
      colorArcs+
      ticks+
      '<text class="g-end-label is-e" x="'+ePos.x.toFixed(1)+'" y="'+(ePos.y+4).toFixed(1)+
        '" text-anchor="middle">E</text>'+
      '<text class="g-end-label is-f" x="'+fPos.x.toFixed(1)+'" y="'+(fPos.y+4).toFixed(1)+
        '" text-anchor="middle">F</text>'+
      '<line class="g-needle" x1="'+cx+'" y1="'+cy+'" x2="'+tip.x.toFixed(2)+
        '" y2="'+tip.y.toFixed(2)+'"></line>'+
      '<circle class="g-hub" cx="'+cx+'" cy="'+cy+'" r="'+hubR+'"></circle>'+
    "</svg>"+
    '<div class="acct-gauge-label">'+labelText+"</div></div>";
}
function accountMarkup(account,columns,canReorder){
  canReorder=canReorder||{up:true,down:true};
  const s=accountState(account);
  const dot=s.cls==="live"?'<span class="live-dot" aria-hidden="true"></span>':"";
  // Capacity-first like original headroom; cost is secondary if set
  const plan=account.plan||account.provider;
  const cost=formatCost(account.monthly_cost_usd);
  const costBit=cost?' <span class="acct-cost">'+esc(cost)+"</span>":"";
  const u=account.usage||{};
  const wastePct=Number(u.lifetime_wasted_percent_points||0);
  const wasteU=fmtUnits(u.lifetime_wasted_units);
  const usedLife=fmtUnits(u.lifetime_used_units_seen);
  const resets=Number(u.reset_events||0);
  let strip="";
  if(wastePct>0||wasteU||usedLife||resets){
    strip='<div class="acct-usage-strip">Lifetime · wasted <strong>'+
      esc(wastePct.toFixed(1))+'%</strong> of allotment at reset'+
      (wasteU?" · <strong>"+esc(wasteU)+"</strong> units left unused":"")+
      (usedLife?" · used <strong>"+esc(usedLife)+"</strong> units (running)":"")+
      (resets?" · "+resets+" reset"+(resets===1?"":"s"):"")+
      "</div>";
  }
  // Held / re-login: click note OR Login button → Terminal/browser login flow
  const needsReconnect=s.cls==="held"||s.cls==="idle"||
    /identity|re-login|connect|sign-in|held|missing|rejected/i.test(s.note||"");
  const noteCls=needsReconnect?"state-note is-action":"state-note";
  const noteAttr=needsReconnect
    ?(' data-login="1" data-name="'+esc(account.name)+'" data-provider="'+esc(account.provider)+
      '" title="Click to open login"')
    :"";
  const noteText=needsReconnect
    ?(s.note||"identity held")+" — click to log in"
    :(s.note||"");
  const loginBtn=needsReconnect
    ?('<button type="button" class="btn-login" data-login="1" data-name="'+esc(account.name)+
      '" data-provider="'+esc(account.provider)+'">Log in</button>')
    :('<button type="button" class="btn-login secondary" data-login="1" data-name="'+esc(account.name)+
      '" data-provider="'+esc(account.provider)+'" title="Re-auth this slot">Re-auth</button>');
  const manusStrip=manusCreditsStrip(account);
  const costVal=account.monthly_cost_usd!=null&&Number.isFinite(Number(account.monthly_cost_usd))
    ?String(account.monthly_cost_usd):"";
  const renewDateVal=(account.renews_on&&String(account.renews_on).slice(0,10))||"";
  const renewAmtVal=account.renew_amount!=null&&Number.isFinite(Number(account.renew_amount))
    ?String(account.renew_amount):"";
  const inlineEdit=
    '<div class="acct-inline-edit" data-inline-edit="'+esc(account.name)+'">'+
      '<p class="ie-home" data-ie-home></p>'+
      '<div class="ie-grid">'+
        '<div><label>Cost $/month</label>'+
          '<input type="number" min="0" step="1" data-ie-cost value="'+esc(costVal)+'"></div>'+
        '<div><label>Expected email</label>'+
          '<input type="email" data-ie-email autocomplete="off" spellcheck="false" '+
          'value="'+esc(account.expected_email||"")+'" placeholder="pin identity email"></div>'+
        '<div><label>Renews on</label>'+
          '<input type="date" data-ie-renews-on value="'+esc(renewDateVal)+'"></div>'+
        '<div><label>Renews at (amount)</label>'+
          '<input type="number" min="0" step="1" data-ie-renew-amount value="'+esc(renewAmtVal)+
          '" placeholder="e.g. 4000 credits"></div>'+
      "</div>"+
      '<p class="ie-hint" data-ie-year></p>'+
      '<p class="ie-hint">Renew date + amount pin the allotment reset (Manus Full Tank uses renew amount when set).</p>'+
      '<label class="ie-check"><input type="checkbox" data-ie-reserved> Reserved (skip auto-routing)</label>'+
      '<div class="ie-actions">'+
        '<button type="button" class="primary" data-ie-save>Save</button>'+
        '<button type="button" data-ie-cancel>Cancel</button>'+
        '<button type="button" class="danger" data-ie-remove>Remove slot</button>'+
      "</div>"+
      '<p class="ie-msg" data-ie-msg></p>'+
    "</div>";
  const reorder=
    '<div class="acct-card-actions">'+
      '<div class="acct-reorder" title="Reorder (preference order)">'+
        '<button type="button" data-move="up" data-name="'+esc(account.name)+'"'+
          (canReorder.up?"":" disabled")+' aria-label="Move up">↑</button>'+
        '<button type="button" data-move="down" data-name="'+esc(account.name)+'"'+
          (canReorder.down?"":" disabled")+' aria-label="Move down">↓</button>'+
      "</div>"+
      '<button type="button" class="acct-drag" draggable="true" data-drag="'+esc(account.name)+
        '" title="Drag to reorder" aria-label="Drag to reorder">⋮⋮</button>'+
      '<button type="button" class="btn-card-edit" data-card-edit="'+esc(account.name)+
      '">Edit</button>'+
    "</div>";
  return '<article class="account is-'+s.cls+(manusStrip?" is-manus":"")+
    '" data-account="'+esc(account.name)+'" data-provider="'+esc(account.provider||"")+
    '" style="--win-cols:'+columns.length+'">'+
    '<div class="acct-identity"><span class="acct-email">'+esc(account.email||account.name)+"</span>"+
    '<div class="acct-meta"><span>'+esc(account.name)+"</span><span>"+esc(plan)+costBit+
    "</span></div>"+accountRenewMarkup(account)+gasGaugeMarkup(account)+reorder+"</div>"+
    columns.map(k=>windowMarkup(account,k)).join("")+
    '<div class="state"><span class="state-badge state-'+s.cls+'">'+dot+esc(s.label)+"</span>"+
    '<p class="'+noteCls+'"'+noteAttr+'>'+esc(noteText)+"</p>"+loginBtn+"</div>"+
    manusStrip+strip+inlineEdit+"</article>";
}
function providerSectionChrome(provider, title, sub, canMove, extraButtons){
  return '<header class="provider-head">'+
    '<h2 class="provider-name">'+esc(title)+'</h2>'+
    '<span class="provider-count">'+esc(sub)+"</span>"+
    '<div class="provider-actions">'+
      '<div class="provider-reorder" title="Reorder provider sections">'+
        '<button type="button" data-provider-move="up" data-provider="'+esc(provider)+'"'+
          (canMove.up?"":" disabled")+' aria-label="Move '+esc(title)+' up">↑</button>'+
        '<button type="button" data-provider-move="down" data-provider="'+esc(provider)+'"'+
          (canMove.down?"":" disabled")+' aria-label="Move '+esc(title)+' down">↓</button>'+
      "</div>"+
      '<button type="button" class="provider-drag" draggable="true" data-provider-drag="'+
        esc(provider)+'" title="Drag section to reorder" aria-label="Drag '+esc(title)+
        ' section">⋮⋮</button>'+
      (extraButtons||"")+
    "</div></header>";
}
function providerMarkup(provider,accounts,canMove){
  // Always render every core provider section (empty → login CTA).
  canMove=canMove||{up:true,down:true};
  const columns=windowColumns(accounts);
  const titles={claude:"Claude",codex:"Codex",grok:"Grok",manus:"Manus",nvidia:"NVIDIA"};
  const title=titles[provider]||provider;
  const n=accounts.length;
  const live=accounts.filter(isCurrent).length;
  const sub=n
    ?(n+(n===1?" subscription":" subscriptions")+" · "+live+"/"+n+" live · real-time")
    :"0 subscriptions · no accounts yet";
  const loginKind={claude:"Claude OAuth handshake",codex:"Codex OAuth handshake",
    grok:"Grok OAuth handshake",manus:"API key page",nvidia:"API key page"}[provider]||"login";
  if(!n){
    return '<section class="provider is-empty" data-provider="'+esc(provider)+'">'+
      providerSectionChrome(provider, title, sub, canMove, "")+
      '<div class="provider-empty">'+
      '<span class="hint">No '+esc(title)+' slots yet. Click login — '+esc(loginKind)+'.</span>'+
      '<button type="button" class="btn-login" data-login="1" data-provider="'+esc(provider)+
      '" data-mode="fresh">Log in to '+esc(title)+'</button>'+
      '<button type="button" class="btn-login secondary" data-manage="1" data-add-provider="'+
      esc(provider)+'">Paste key / options</button>'+
      "</div></section>";
  }
  const extras=
    '<button type="button" class="btn-login secondary" data-login="1" data-provider="'+
    esc(provider)+'" data-mode="fresh" title="Add another '+esc(title)+' account">+ Login</button>'+
    '<button type="button" class="provider-edit" data-provider-edit="'+esc(provider)+
    '" title="Edit accounts on their cards">Edit</button>';
  return '<section class="provider" data-provider="'+esc(provider)+'">'+
    providerSectionChrome(provider, title, sub, canMove, extras)+
    accounts.map((a,i)=>accountMarkup(a,columns,{
      up:i>0, down:i<accounts.length-1
    })).join("")+"</section>";
}
function validate(data){
  if(!data||!Number.isFinite(data.generated)||!Array.isArray(data.accounts))
    throw new Error("usage snapshot invalid");
  const display=data._headroom_display;
  if(!display||display.schema!=="headroom_widget@1"||!display.freshness||typeof display.freshness.state!=="string"||!Array.isArray(display.accounts)||display.accounts.length!==data.accounts.length)
    throw new Error("display projection invalid");
  data.accounts.forEach(a=>{
    if(!a||typeof a.name!=="string")throw new Error("invalid account record");
    Object.values(a.windows||{}).forEach(w=>{
      if(w&&w.used_percent!=null&&(!Number.isFinite(w.used_percent)||w.used_percent<0||w.used_percent>100))
        throw new Error("invalid usage percentage");});});
  return data;
}
function render(data,forceNoncurrent){
  const now=Date.now()/1e3,display=data._headroom_display;
  if(display.freshness.state==="held"||!Number.isFinite(data.generated)||data.generated>now) snapshotState="held";
  else if(display.freshness.state!=="current"||now-data.generated>SNAPSHOT_MAX_AGE) snapshotState="stale";
  else snapshotState="current";
  sourceFailed=forceNoncurrent===true||data.refresh_failed===true;
  const accounts=data.accounts.map((a,i)=>Object.assign({},a,{__display:display.accounts[i]}));
  const current=accounts.filter(isCurrent);
  /* Include current windows from limited accounts in summary metrics,
     not just fully-current accounts — windowView already handles the mixed case. */
  const readings=[];
  accounts.forEach(a=>["5h","7d","month"].forEach(k=>{
    const v=windowView(a,k);if(v.left!=null&&v.state==="current")readings.push({left:v.left,who:a.email||a.name});}));
  const avg=readings.length?Math.round(readings.reduce((s,r)=>s+r.left,0)/readings.length):null;
  const lowest=readings.length?readings.reduce((b,r)=>r.left<b.left?r:b):null;
  const claude=accounts.filter(a=>a.provider==="claude");
  const codex=accounts.filter(a=>a.provider==="codex");
  const grok=accounts.filter(a=>a.provider==="grok");
  const manus=accounts.filter(a=>a.provider==="manus");
  const nvidia=accounts.filter(a=>a.provider==="nvidia");
  const fleetSpend=accounts.reduce((s,a)=>s+(Number.isFinite(Number(a.monthly_cost_usd))?Number(a.monthly_cost_usd):0),0);
  set("verified",current.length+"/"+accounts.length);
  set("average",avg==null?"n/a":avg+"%");
  set("lowest",lowest?Math.round(lowest.left)+"%":"n/a");
  set("lowest-account",lowest?lowest.who:"no current readings");
  set("account-count",String(accounts.length));
  const bits=[];
  if(claude.length)bits.push("Claude "+claude.length);
  if(codex.length)bits.push("Codex "+codex.length);
  if(grok.length)bits.push("Grok "+grok.length);
  if(manus.length)bits.push("Manus "+manus.length);
  if(nvidia.length)bits.push("NVIDIA "+nvidia.length);
  if(fleetSpend>0)bits.push(formatCost(fleetSpend)+" total");
  set("account-note",bits.join(" · ")||"–");
  /* top-bar instruments */
  document.getElementById("verified-viz").innerHTML=accounts.map(a=>
    '<span class="seg '+(isCurrent(a)?"is-on":"is-off")+'" title="'+esc(a.name)+": "+(isCurrent(a)?"verified":"held")+'"></span>').join("");
  const gauge=document.getElementById("average-viz");
  gauge.className="gauge tone-"+tone(avg);
  gauge.querySelector(".gauge-fill").style.setProperty("--fill",(avg==null?0:avg)+"%");
  document.getElementById("lowest-viz").innerHTML=meterMarkup(lowest?lowest.left:null);
  document.getElementById("fleet-viz").innerHTML=accounts.map(a=>{
    const primary=(a.provider==="grok"||a.provider==="manus")?"month":"5h";
    const view=windowView(a,primary),left=view.left;
    if(view.state!=="current"||left==null)return'<span class="fbar is-unknown" title="'+esc(a.name)+': unknown"></span>';
    return'<span class="fbar tone-'+view.tone+'" style="--h:'+Math.max(4,left)+'%" title="'+esc(a.name)+": "+Math.round(left)+'% left"></span>';
  }).join("");
  set("snapshot-age","snapshot "+age(data.generated));
  set("source-line",current.length+" of "+accounts.length+" verified"
    +(fleetSpend>0?" · "+formatCost(fleetSpend)+" fleet":""));
  // Provider section order from dashboard.provider_order (Claude/Grok/Manus/…)
  const defaultProv=["claude","codex","grok","manus","nvidia"];
  let providerOrder=(CONFIG&&Array.isArray(CONFIG.provider_order)&&CONFIG.provider_order.length)
    ?CONFIG.provider_order.slice()
    :defaultProv.slice();
  // Keep known providers; append any missing
  providerOrder=providerOrder.filter(p=>defaultProv.includes(p));
  defaultProv.forEach(p=>{if(!providerOrder.includes(p))providerOrder.push(p);});
  const byProvider={claude,codex,grok,manus,nvidia};
  document.getElementById("providers").innerHTML=providerOrder.map((p,i)=>
    providerMarkup(p, byProvider[p]||accounts.filter(a=>a.provider===p), {
      up:i>0, down:i<providerOrder.length-1
    })
  ).join("");
  document.getElementById("providers").querySelectorAll("[data-manage]").forEach(btn=>{
    btn.addEventListener("click",()=>{
      const pref=btn.getAttribute("data-add-provider");
      manageOpen();
      if(pref){
        const sel=document.getElementById("add-provider");
        if(sel){sel.value=pref;syncAddFormProvider();}
      }
    });
  });
  // Provider-level Edit → open first account card inline (no sidebar)
  document.getElementById("providers").querySelectorAll("[data-provider-edit]").forEach(btn=>{
    btn.addEventListener("click",()=>{
      const prov=btn.getAttribute("data-provider-edit");
      const card=document.querySelector('.account[data-provider="'+CSS.escape(prov)+'"]');
      if(card){
        cardStartEdit(card.getAttribute("data-account"));
        card.scrollIntoView({behavior:"smooth",block:"nearest"});
      }else{
        manageOpen();
        const sel=document.getElementById("add-provider");
        if(sel&&prov){sel.value=prov;syncAddFormProvider();}
      }
    });
  });
  // Per-card Edit → inline form on the card
  document.getElementById("providers").querySelectorAll("[data-card-edit]").forEach(btn=>{
    btn.addEventListener("click",(ev)=>{
      ev.preventDefault();
      ev.stopPropagation();
      cardStartEdit(btn.getAttribute("data-card-edit"));
    });
  });
  wireInlineEditForms();
  wireAccountReorder();
  wireProviderReorder();
  // Login / reconnect / empty-provider CTAs → Terminal + browser OAuth (or key page)
  document.getElementById("providers").querySelectorAll("[data-login]").forEach(el=>{
    el.addEventListener("click",(ev)=>{
      ev.preventDefault();
      ev.stopPropagation();
      startProviderLogin({
        name:el.getAttribute("data-name")||undefined,
        provider:el.getAttribute("data-provider")||undefined,
        mode:el.getAttribute("data-mode")||"reconnect",
        el:el
      });
    });
  });
  const barCopy=document.getElementById("accounts-bar-copy");
  if(barCopy){
    const n=accounts.length;
    const spendBits=fleetSpend>0?(" · fleet "+formatCost(fleetSpend)):"";
    barCopy.innerHTML="<strong>"+n+" account"+(n===1?"":"s")+" monitored</strong>"+
      spendBits+" — add Claude, Codex, Grok, Manus, or NVIDIA, change monthly cost, or remove a slot.";
  }
  renderFleetUsage(data);
  const warnings=(data.integrity_warnings||[]).slice();
  if(sourceFailed)warnings.unshift("refresh failed — readings shown as held until collection succeeds");
  if(snapshotState!=="current")warnings.unshift("snapshot is noncurrent (maximum "+Math.floor(SNAPSHOT_MAX_AGE/60)+" minutes) — readings shown as held until a fresh collect");
  const status=document.getElementById("status");
  status.textContent=warnings.length?("⚠ "+warnings.join(" — ")):"Full meters are available capacity. Colors show what's left, not what's used.";
  status.className="statusline"+(warnings.length?" is-error":"");
}
function renderError(message){
  document.getElementById("providers").innerHTML=
    '<div class="empty">Usage feed unavailable<small>'+esc(message)+
    " — no stale value promoted to live.</small></div>";
  set("snapshot-age","no trustworthy snapshot");
  set("source-line","refresh to retry");
  const status=document.getElementById("status");
  status.textContent="Live data could not be verified.";
  status.className="statusline is-error";
}
function set(id,text){document.getElementById(id).textContent=text;}

/* ---------------------------------------------------------------- load */
async function load(manual){
  const requestId=++requestNumber;
  const button=document.getElementById("refresh");
  if(manual){button.disabled=true;button.textContent="Refreshing…";}
  try{
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),12e3);
    const response=await fetch(DATA_URL+"?t="+Date.now(),{cache:"no-store",signal:controller.signal});
    clearTimeout(timer);
    if(!response.ok)throw new Error("feed returned HTTP "+response.status);
    const data=validate(await response.json());
    if(requestId===requestNumber){
      render(data);
      // key the cache by the redaction setting so a cache written while emails
      // were visible is never reused after privacy is turned on
      try{localStorage.setItem(CACHE_KEY,JSON.stringify(data));}catch(e){}
    }
  }catch(error){
    if(requestId===requestNumber){
      let cached=null;
      try{cached=validate(JSON.parse(localStorage.getItem(CACHE_KEY)||"null"));}catch(e){}
      // Only reuse a RECENT, non-future cache. An old cache may predate a
      // privacy change or a limit change; beyond an hour (or dated in the
      // future) we refuse it rather than resurface stale data as a snapshot.
      const cacheAge=cached&&Number.isFinite(cached.generated)?(Date.now()/1e3-cached.generated):Infinity;
      if(cached&&cacheAge>=0&&cacheAge<=3600){render(cached,true);
        const status=document.getElementById("status");
        status.textContent="Feed unreachable ("+(error&&error.message||"error")+"). Showing last snapshot ("+age(cached.generated)+").";
        status.className="statusline is-error";}
      else{try{localStorage.removeItem(CACHE_KEY);}catch(e){}
        renderError(error&&error.message?error.message:"unknown feed error");}
    }
  }finally{
    if(manual&&requestId===requestNumber){button.disabled=false;button.textContent="Refresh";}
  }
}

/* =================================================== liquid-glass widget */
/* The /widget surface consumes ONLY the versioned /widget.json projection
   (headroom_widget@1) — names + providers + window states, never emails.
   FAIL-CLOSED IS SACRED: the single place a live tone can be produced is the
   `st==="current"` branch of hrWindow(); stale/held/offline readings render
   the unknown grey with the last-observed value (or n/a), never a live color. */
const HR_URL="widget.json";
let hrData=null,hrSize="popup",hrLastOffline=false,hrRequest=0;
function hrTone(left){return left==null?"unknown":left<=10?"red":left<=30?"orange":left<=50?"yellow":"green";}
function hrPct(v){return Number.isFinite(v)?clamp(v,0,100):null;}
function hrAgeText(s){if(!Number.isFinite(s))return"age unknown";s=Math.max(0,Math.floor(s));if(s<60)return s+"s ago";if(s<3600)return Math.floor(s/60)+"m ago";return Math.floor(s/3600)+"h "+Math.floor(s%3600/60)+"m ago";}
function hrClock(epoch){if(!Number.isFinite(epoch))return null;try{return new Intl.DateTimeFormat(undefined,{hour:"numeric",minute:"2-digit"}).format(new Date(1e3*epoch));}catch(e){return null;}}
/* projection -> tone/value mapping. The ONLY live-color branch requires the
   window to be state "current" AND carry a finite left_percent. */
function hrWindow(w){
  const st=w&&typeof w.state==="string"?w.state:"held";
  const live=hrPct(w&&w.left_percent);
  if(st==="current"&&live!=null)
    return{tone:hrTone(live),fill:live,value:Math.round(live)+"%",live:true};
  const last=hrPct(w&&w.last_observed_left_percent);
  if(st==="limited")
    return{tone:"red",fill:last==null?0:last,value:(last==null?0:Math.round(last))+"%",live:false};
  if(st==="stale"&&last!=null)
    return{tone:"unknown",fill:last,value:Math.round(last)+"%",live:false};
  return{tone:"unknown",fill:null,value:"n/a",live:false};
}
/* offline / noncurrent feed: every reading is shown grey at its last-observed
   value — a demoted window can never yield anything but the unknown tone. */
function hrDemoteWindow(w){
  const left=w&&Number.isFinite(w.left_percent)?w.left_percent:(w&&w.last_observed_left_percent);
  const last=hrPct(left);
  if(last!=null)return{tone:"unknown",fill:last,value:Math.round(last)+"%",live:false};
  return{tone:"unknown",fill:null,value:"n/a",live:false};
}
/* headroom_widget@1 structural validation: anything missing or mistyped is
   rejected before it can render — the OFFLINE view, never a live card.
   Strict by design: a MISSING field is not null — the projection always emits
   every contract field explicitly, so absence means a foreign/broken feed. */
function hrFiniteOrNull(v){return v===null||(typeof v==="number"&&Number.isFinite(v));}
function hrValidWindow(w){
  if(!w||typeof w!=="object"||Array.isArray(w))return false;
  if(!["current","limited","stale","held"].includes(w.state))return false;
  if(!hrFiniteOrNull(w.left_percent)||!hrFiniteOrNull(w.last_observed_left_percent)
    ||!hrFiniteOrNull(w.resets_at)||!hrFiniteOrNull(w.observed_at))return false;
  if(w.left_percent!=null&&(w.left_percent<0||w.left_percent>100))return false;
  if(w.last_observed_left_percent!=null&&(w.last_observed_left_percent<0||w.last_observed_left_percent>100))return false;
  /* value invariant: only a current window may carry a live left_percent */
  return w.state==="current"?Number.isFinite(w.left_percent):w.left_percent==null;
}
function hrValidFeed(data){
  if(!data||typeof data!=="object"||Array.isArray(data))return false;
  if(data.schema!=="headroom_widget@1")return false;
  const f=data.freshness;
  if(!f||typeof f!=="object"||Array.isArray(f))return false;
  if(!["current","stale","held"].includes(f.state))return false;
  if(typeof f.reason!=="string")return false;
  if(!Number.isFinite(f.evaluated_at))return false;
  if(f.age_seconds!==null&&!(Number.isInteger(f.age_seconds)&&f.age_seconds>=0))return false;
  if(f.state!=="held"&&f.age_seconds===null)return false;
  if(!Array.isArray(data.accounts))return false;
  if(!data.accounts.every(a=>a&&typeof a==="object"&&!Array.isArray(a)
    &&typeof a.name==="string"&&typeof a.provider==="string"
    &&["current","limited","stale","held"].includes(a.state)
    &&a.windows&&typeof a.windows==="object"&&!Array.isArray(a.windows)
    &&hrValidWindow(a.windows["7d"])
    /* 5h is optional ONLY for codex (OpenAI lifted it): a live codex seat
       reports only the weekly window. For any other provider 5h stays
       mandatory (fail-closed); a present 5h must always be valid. 7d is
       mandatory for everyone. */
    &&((a.provider==="codex"&&a.windows["5h"]===undefined)||hrValidWindow(a.windows["5h"]))))return false;
  const h=data.headline;
  if(!h||typeof h!=="object"||Array.isArray(h))return false;
  if(!Number.isInteger(h.current_accounts)||h.current_accounts<0)return false;
  if(!Number.isInteger(h.total_accounts)||h.total_accounts<0)return false;
  return h.fullest_5h_left_percent===null
    ||(Number.isFinite(h.fullest_5h_left_percent)
    &&h.fullest_5h_left_percent>=0&&h.fullest_5h_left_percent<=100);
}
function hrFreshness(data,offline){
  const f=data&&data.freshness&&typeof data.freshness==="object"?data.freshness:{};
  /* fail-closed: "current" is trusted ONLY with complete timing — a finite,
     non-negative age inside the budget. Client clock drift only ever ADDS
     age (a future-dated evaluation can never subtract age or stay current). */
  const now=Date.now()/1e3;
  const evaluatedAt=Number.isFinite(f.evaluated_at)?f.evaluated_at:null;
  let age=Number.isFinite(f.age_seconds)&&f.age_seconds>=0?f.age_seconds:null;
  if(age!=null&&evaluatedAt!=null)age+=Math.max(0,now-evaluatedAt);
  let state=f.state==="current"||f.state==="stale"?f.state:"held";
  if(offline&&state==="current")state="stale";
  if(state==="current"&&(age==null||evaluatedAt==null))state="held"; /* missing timing: hold, never trust */
  if(state==="current"&&age>SNAPSHOT_MAX_AGE)state="stale"; /* client-side fail-closed guard between polls */
  if(evaluatedAt!=null&&evaluatedAt>now+10)state="held"; /* future evaluation beyond NTP-skew tolerance: hold, never current (sub-second clock drift must not hold a live fleet; tolerance kept tight so a future-dated feed can only freeze the age briefly) */
  return{state:state,age:age};
}
function hrAccount(raw,demote){
  const a=raw&&typeof raw==="object"?raw:{};
  const name=typeof a.name==="string"?a.name:"unknown";
  const provider=typeof a.provider==="string"?a.provider:"unknown";
  let st=["current","limited","stale","held"].includes(a.state)?a.state:"held";
  if(demote&&st!=="held")st="stale";
  const w5=a.windows&&a.windows["5h"]||null,w7=a.windows&&a.windows["7d"]||null;
  /* OpenAI lifted Codex's 5h: a live codex seat reports only the weekly window,
     so an absent 5h is a lifted limit, not a failed read. Omit its row/note and
     drive the per-account battery tile, cool clock, and timing from 7d instead
     of a phantom "n/a" 5h. Provider-scoped: a non-codex seat (or a codex seat
     that still has a 5h) is unchanged, and a non-codex missing 5h never reaches
     here (hrValidFeed rejects it). */
  const has5h=provider!=="codex"||w5!=null;
  const wp=has5h?w5:w7; /* window backing the tile / clock / stale timing */
  const v5=demote?hrDemoteWindow(w5):hrWindow(w5);
  const v7=demote?hrDemoteWindow(w7):hrWindow(w7);
  const tile=has5h?v5:v7;
  /* model-scoped weekly windows (e.g. "scoped:Fable") ride along for display;
     hrWindow/hrDemoteWindow are null-safe so extras need no pre-validation */
  const extras=[];
  if(a.windows&&typeof a.windows==="object"&&!Array.isArray(a.windows))
    Object.keys(a.windows).sort().forEach(k=>{
      if(k==="5h"||k==="7d"||k.indexOf("scoped:")!==0)return;
      const w=a.windows[k];
      extras.push({label:k.slice(7),view:demote?hrDemoteWindow(w):hrWindow(w)});
    });
  const badge=st==="current"?{label:"CURRENT",tone:"green",dot:true}
    :st==="limited"?{label:"AT LIMIT",tone:"red",dot:true}
    :st==="stale"?{label:"STALE",tone:"dim",dot:false}
    :{label:"WAITING",tone:"dim",dot:false};
  const cool=st==="limited"?hrClock(wp&&wp.resets_at):null;
  let note;
  if(st==="current")note=(has5h?"5h "+untilReset(w5&&w5.resets_at)+" · ":"")+"7d "+untilReset(w7&&w7.resets_at);
  else if(st==="limited")note="window full — "+(cool?"cools until "+cool:"cooling")+" · 7d "+untilReset(w7&&w7.resets_at);
  else if(st==="stale"){const seen=wp&&Number.isFinite(wp.observed_at)?wp.observed_at:null;
    note=seen?"last verified "+age(seen):"reading held — not verified";}
  else note="no verified reading — held, never promoted to live";
  return{name:name,provider:provider,state:st,badge:badge,v5:v5,v7:v7,tile:tile,
         has5h:has5h,extras:extras,cool:cool,note:note,hasW:st!=="held"};
}
function hrView(data,offline){
  const fresh=hrFreshness(data,offline);
  const demote=offline||fresh.state!=="current";
  const rawAccounts=Array.isArray(data.accounts)?data.accounts:[];
  const accts=rawAccounts.map(a=>hrAccount(a,demote));
  /* headline: never trust the feed's numbers — derive the fleet's average
     battery per window from LIVE readings only: a current window contributes
     its left_percent, a limited window contributes an honest 0, held/stale
     windows never move an average, whatever the feed claims. */
  const total=accts.length;
  const cur=demote?0:accts.filter(a=>a.state==="current").length;
  const pool5=[],pool7=[];
  if(!demote)rawAccounts.forEach(a=>{
    if(!a||a.state==="held"||a.state==="stale")return;
    [["5h",pool5],["7d",pool7]].forEach(pair=>{
      const w=a.windows&&a.windows[pair[0]]||{};
      if(w.state==="current"){const l=hrPct(w.left_percent);if(l!=null)pair[1].push(l);}
      else if(w.state==="limited")pair[1].push(0);
    });});
  const hrAvg=pool=>pool.length?pool.reduce((s,x)=>s+x,0)/pool.length:null;
  const avg5=hrAvg(pool5),avg7=hrAvg(pool7);
  const hl=avg5!=null
    ?{value:Math.round(avg5)+"%",tone:hrTone(avg5),fill:avg5,sub:""}
    :{value:"—",tone:"dim",fill:0,
      sub:offline?"no live readings — feed unreachable"
        :fresh.state==="stale"?"no live readings — snapshot expired"
        :fresh.state==="held"?"no live readings — snapshot held"
        :"no live readings"};
  const hl7=avg7!=null
    ?{value:Math.round(avg7)+"%",tone:hrTone(avg7),fill:avg7}
    :{value:"—",tone:"dim",fill:0};
  const limited=accts.filter(a=>a.state==="limited");
  let banner=null;
  if(offline)banner={cls:"is-orange",text:"usage feed unreachable — readings held, never promoted to live"};
  else if(fresh.state==="held")banner={cls:"is-orange",text:"no trusted snapshot — readings held, never promoted to live"};
  else if(fresh.state==="stale")banner={cls:"is-orange",text:"snapshot older than "+Math.floor(SNAPSHOT_MAX_AGE/60)+"m — readings held, never promoted to live"};
  else if(limited.length===1)banner={cls:"is-red",text:limited[0].name+" hit its 5h cap"+(limited[0].cool?" — cooling until "+limited[0].cool:"")};
  else if(limited.length>1)banner={cls:"is-red",text:limited.length+" accounts hit their 5h cap — cooling until reset"};
  const liveLine=offline?"0/"+total+" live · feed unreachable"
    :fresh.state==="stale"?"0/"+total+" live · feed stale"
    :fresh.state==="held"?"0/"+total+" live · feed held"
    :limited.length?cur+"/"+total+" live · "+limited.length+" at limit"
    :cur+"/"+total+" accounts live";
  const freshText=offline?"feed unreachable"
    :fresh.state==="held"?"no trusted snapshot"
    :(fresh.age!=null?"snapshot "+hrAgeText(fresh.age):"snapshot age unknown")+(fresh.state==="stale"?" · stale":"");
  const dotc=(!offline&&fresh.state==="current")?"var(--green)"
    :(!offline&&fresh.state==="stale")?"var(--orange)":"var(--unknown)";
  return{offline:offline,fresh:fresh,accts:accts,hl:hl,hl7:hl7,banner:banner,
         liveLine:liveLine,freshText:freshText,dotc:dotc,
         schema:typeof data.schema==="string"?data.schema:"headroom_widget@1"};
}
function hrOfflineView(){
  return{offline:true,fresh:{state:"held",age:null},accts:[],
    hl:{value:"—",tone:"dim",fill:0,sub:"usage feed unavailable — nothing promoted to live"},
    hl7:{value:"—",tone:"dim",fill:0},
    banner:{cls:"is-orange",text:"usage feed unavailable — no snapshot to show, nothing promoted to live"},
    liveLine:"feed unreachable",freshText:"feed unreachable",dotc:"var(--unknown)",
    schema:"headroom_widget@1"};
}
/* ---- markup builders (names/providers escaped; values are code-built) ---- */
function hrCells(fill){
  let out="";
  for(let i=0;i<10;i++){
    const f=fill==null?0:clamp((fill-10*i)*10,0,100);
    out+='<span class="hr-cell" style="--fill:'+f+'%"></span>';
  }
  return out;
}
function hrDotMarkup(v){
  return '<span class="hr-dot'+(v.fresh.state==="current"&&!v.offline?" is-live":"")+
    '" style="--dotc:'+v.dotc+'" aria-hidden="true"></span>';
}
function hrAcctMarkup(a){
  const dot=a.badge.dot?'<span class="hr-bdot hr-tone-'+a.badge.tone+'" aria-hidden="true"></span>':"";
  const extraRows=(a.extras||[]).map(e=>
    '<div class="hr-winrow"><span class="hr-wlabel">'+esc(e.label.toUpperCase())+'</span><span class="hr-cells h7 hr-tone-'+e.view.tone+'" role="img" aria-label="'+esc(e.label)+" "+esc(e.view.value)+'">'+hrCells(e.view.fill)+'</span><span class="hr-wval hr-tone-'+e.view.tone+'">'+esc(e.view.value)+"</span></div>").join("");
  /* omit the 5H row when codex's 5h is lifted (a.has5h===false): no phantom
     "5H n/a" on a live seat. The 7D row and any scoped rows still render. */
  const fiveRow=a.has5h?'<div class="hr-winrow"><span class="hr-wlabel">5H</span><span class="hr-cells h5 hr-tone-'+a.v5.tone+'" role="img" aria-label="5h '+esc(a.v5.value)+'">'+hrCells(a.v5.fill)+'</span><span class="hr-wval hr-tone-'+a.v5.tone+'">'+esc(a.v5.value)+"</span></div>":"";
  const wins=a.hasW?'<div class="hr-wins">'+fiveRow+
    '<div class="hr-winrow"><span class="hr-wlabel">7D</span><span class="hr-cells h7 hr-tone-'+a.v7.tone+'" role="img" aria-label="7d '+esc(a.v7.value)+'">'+hrCells(a.v7.fill)+'</span><span class="hr-wval hr-tone-'+a.v7.tone+'">'+esc(a.v7.value)+"</span></div>"+extraRows+"</div>":"";
  return '<div class="hr-acct"><div class="hr-arow">'+
    '<span class="hr-aname">'+esc(a.name)+'</span>'+
    '<span class="hr-pill">'+esc(a.provider)+'</span>'+
    '<span class="hr-sp"></span>'+dot+
    '<span class="hr-badge hr-tone-'+a.badge.tone+'">'+esc(a.badge.label)+'</span>'+
    '<span class="hr-chev" aria-hidden="true">›</span></div>'+wins+
    '<p class="hr-note">'+esc(a.note)+"</p></div>";
}
function hrPopMarkup(v){
  const banner=v.banner?'<div class="hr-banner '+v.banner.cls+'">'+esc(v.banner.text)+"</div>":"";
  const accts=v.accts.length?v.accts.map(hrAcctMarkup).join("")
    :'<div class="hr-empty">no accounts in the usage feed'+(v.offline?" — feed unreachable":"")+"</div>";
  return '<div class="hr-pop hr-glass glowable">'+
    '<div class="hr-phead"><span class="hr-appname">headroom</span><span class="hr-sp"></span>'+
      hrDotMarkup(v)+'<span class="hr-fresh">'+esc(v.freshText)+"</span></div>"+
    '<div class="hr-hl"><div class="hr-hlrow"><span class="hr-hlval hr-tone-'+v.hl.tone+'">'+esc(v.hl.value)+'</span>'+
      '<span class="hr-hllabel">Avg 5h battery</span></div>'+
      '<div class="hr-hlbar" role="img" aria-label="average 5h battery"><span class="hr-hlfill hr-tone-'+v.hl.tone+'" style="--fill:'+v.hl.fill+'%"></span><span class="hr-hlticks"></span></div>'+
      '<div class="hr-hlrow is-7"><span class="hr-hlval-7 hr-tone-'+v.hl7.tone+'">'+esc(v.hl7.value)+'</span>'+
      '<span class="hr-hllabel">Avg 7d battery</span></div>'+
      '<div class="hr-hlbar is-7" role="img" aria-label="average 7d battery"><span class="hr-hlfill hr-tone-'+v.hl7.tone+'" style="--fill:'+v.hl7.fill+'%"></span><span class="hr-hlticks"></span></div>'+
      '<div class="hr-hlsub">'+(v.hl.sub||esc(v.liveLine))+"</div></div>"+banner+
    '<div class="hr-accts">'+accts+"</div>"+
    '<div class="hr-foot">'+
      '<button type="button" class="hr-btn" id="hr-refresh">↻ Refresh</button>'+
      '<a class="hr-btn" href="/" target="_blank" rel="noopener">Open Fleet Dashboard</a>'+
      '<div class="hr-schema">'+esc(v.schema)+"</div></div></div>";
}
function hrBarsMarkup(v){
  return v.accts.map(a=>{
    /* a.tile is the per-account battery source: 5h normally, 7d when codex's
       5h is lifted (so a healthy codex seat shows a filled 7d bar, not empty) */
    const last=a.tile.fill==null?6:Math.max(6,a.tile.fill);
    if(a.state==="current")return '<span class="hr-bar hr-tone-'+a.tile.tone+'" style="--h:'+last+'%" title="'+esc(a.name)+'"></span>';
    if(a.state==="limited")return '<span class="hr-bar hr-tone-red" style="--h:6%" title="'+esc(a.name)+'"></span>';
    if(a.state==="stale")return '<span class="hr-bar hr-tone-unknown is-dim" style="--h:'+last+'%" title="'+esc(a.name)+'"></span>';
    return '<span class="hr-bar hr-tone-unknown is-held" style="--h:100%" title="'+esc(a.name)+'"></span>';
  }).join("");
}
function hrSmallMarkup(v){
  return '<div class="hr-card small hr-glass glowable">'+
    '<div class="hr-chead"><span class="hr-mark">hr</span><span class="hr-brand">headroom</span><span class="hr-sp"></span>'+hrDotMarkup(v)+'</div>'+
    '<div class="hr-cmid"><div class="hr-cval hr-tone-'+v.hl.tone+'">'+esc(v.hl.value)+'</div>'+
      '<div class="hr-clabel">Avg 5h battery</div></div>'+
    '<div><div class="hr-bars" role="img" aria-label="session headroom per account">'+hrBarsMarkup(v)+'</div>'+
      '<div class="hr-liveline">'+esc(v.liveLine)+"</div></div></div>";
}
function hrMediumMarkup(v){
  const rows=v.accts.map(a=>{
    let toneCls,fill,value,dim="";
    if(a.state==="current"){toneCls=a.tile.tone;fill=a.tile.fill==null?0:a.tile.fill;value=a.tile.value;}
    else if(a.state==="limited"){toneCls="red";fill=2;value=a.tile.value;}
    else if(a.state==="stale"){toneCls="unknown";fill=a.tile.fill==null?0:a.tile.fill;value=a.tile.value;dim=" is-dim";}
    else{toneCls="unknown";fill=0;value="n/a";}
    return '<div class="hr-mrow"><span class="hr-mname">'+esc(a.name)+'</span>'+
      '<span class="hr-mbar'+dim+'"><span class="hr-mfill hr-tone-'+toneCls+'" style="--fill:'+fill+'%"></span></span>'+
      '<span class="hr-mval hr-tone-'+toneCls+'">'+esc(value)+"</span></div>";
  }).join("");
  return '<div class="hr-card medium hr-glass glowable">'+
    '<div class="hr-mleft"><div class="hr-chead"><span class="hr-mark">hr</span><span class="hr-brand">headroom</span></div>'+
    '<div class="hr-cmid"><div class="hr-cval hr-tone-'+v.hl.tone+'">'+esc(v.hl.value)+'</div>'+
      '<div class="hr-clabel">Avg 5h battery</div></div>'+
    '<div class="hr-liveline">'+esc(v.liveLine)+'</div></div>'+
    '<div class="hr-mcol">'+rows+"</div></div>";
}
function hrRender(offline){
  hrLastOffline=offline;
  const v=hrData?hrView(hrData,offline):hrOfflineView();
  const surface=document.getElementById("hr-surface");
  surface.innerHTML=hrSize==="small"?hrSmallMarkup(v)
    :hrSize==="medium"?hrMediumMarkup(v):hrPopMarkup(v);
  const button=document.getElementById("hr-refresh");
  if(button)button.addEventListener("click",()=>hrLoad(true));
}
async function hrLoad(manual){
  const requestId=++hrRequest;
  const button=document.getElementById("hr-refresh");
  if(manual&&button){button.disabled=true;button.textContent="Refreshing…";}
  try{
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),12e3);
    const response=await fetch(HR_URL+"?t="+Date.now(),{cache:"no-store",signal:controller.signal});
    clearTimeout(timer);
    if(!response.ok)throw new Error("widget feed returned HTTP "+response.status);
    const data=await response.json();
    if(!hrValidFeed(data))
      throw new Error("widget feed invalid");
    if(requestId===hrRequest){hrData=data;hrRender(false);}
  }catch(error){
    /* fail closed: whatever we still hold renders grey, nothing is promoted */
    if(requestId===hrRequest)hrRender(true);
  }
}
function hrInit(size){
  hrSize=size==="small"||size==="medium"?size:"popup";
  hrLoad(false);
  setInterval(()=>hrLoad(false),6e4);
  /* keep the snapshot-age text honest between polls */
  setInterval(()=>{if(hrData)hrRender(hrLastOffline);},15e3);
}

/* =================================================== end liquid-glass widget */
/* ---------------------------------------------- one-click provider login */
function loginToast(msg, ms){
  let el=document.getElementById("login-toast");
  if(!el){
    el=document.createElement("div");
    el.id="login-toast";
    el.className="login-toast";
    document.body.appendChild(el);
  }
  el.textContent=msg;
  el.classList.add("is-on");
  clearTimeout(loginToast._t);
  loginToast._t=setTimeout(()=>el.classList.remove("is-on"), ms||6000);
}
async function startProviderLogin(opts){
  opts=opts||{};
  const body={
    name:opts.name||undefined,
    provider:opts.provider||undefined,
    mode:opts.mode||"reconnect",
    prepare:true
  };
  const el=opts.el;
  const prev=el&&(el.tagName==="BUTTON"?el.textContent:null);
  if(el&&el.tagName==="BUTTON"){el.disabled=true;el.textContent="Opening…";}
  try{
    loginToast("Starting "+(body.provider||body.name||"provider")+" login…");
    const res=await manageApi("POST","/api/terminal",body);
    if(res.open_manage){
      manageOpen();
      const sel=document.getElementById("add-provider");
      if(sel){sel.value=res.open_manage;syncAddFormProvider();}
      if(res.open_manage==="nvidia"){
        const section=document.getElementById("insights-key-section");
        if(section)section.scrollIntoView({behavior:"smooth",block:"nearest"});
      }
    }
    let msg=res.instructions||"Login started";
    if(res.terminal_launched)msg+=" · Terminal opened";
    if(res.browser_launched)msg+=" · Browser opened";
    if(res.prepared&&res.prepared.home)msg+=" · home "+res.prepared.home;
    if(res.kind==="cli_oauth")msg+=" · sign in in the browser, then Refresh";
    if(res.kind==="api_key")msg+=" · paste the key in Accounts when ready";
    loginToast(msg, 9000);
    if(el&&el.tagName==="BUTTON"){
      el.textContent=res.ok?"Login opened":"Try again";
      setTimeout(()=>{el.disabled=false;if(prev)el.textContent=prev;}, 4000);
    }else if(el){
      el.textContent=res.ok?"Login opened — complete it, then Refresh":(res.hint||"failed");
    }
    if(!res.ok&&res.command&&navigator.clipboard){
      try{await navigator.clipboard.writeText(res.command);loginToast("Command copied — paste in Terminal");}catch(e){}
    }
  }catch(err){
    loginToast(String(err.message||err), 8000);
    if(el&&el.tagName==="BUTTON"){el.disabled=false;if(prev)el.textContent=prev;}
  }
}

/* ------------------------------------------------ use-it-or-lose-it burns */
let burnTimer=null;
function burnWire(){
  const openBtn=document.getElementById("burn-open-create");
  const checkBtn=document.getElementById("burn-check-now");
  const createBtn=document.getElementById("burn-create");
  const pushBtn=document.getElementById("burn-enable-push");
  const smtpBtn=document.getElementById("smtp-save");
  if(openBtn)openBtn.addEventListener("click",()=>{
    manageOpen();
    const section=document.getElementById("burn-goal-section");
    if(section)section.scrollIntoView({behavior:"smooth",block:"nearest"});
  });
  if(checkBtn)checkBtn.addEventListener("click",()=>burnCheck(true));
  if(createBtn)createBtn.addEventListener("click",burnCreate);
  if(pushBtn)pushBtn.addEventListener("click",burnEnablePush);
  if(smtpBtn)smtpBtn.addEventListener("click",burnSaveSmtp);
  // default deadline: end of next year promo style
  const dl=document.getElementById("burn-deadline");
  if(dl&&!dl.value){
    const d=new Date(); d.setFullYear(d.getFullYear()+1); d.setMonth(0); d.setDate(1);
    dl.value=d.toISOString().slice(0,10);
  }
  burnLoad();
  if(burnTimer)clearInterval(burnTimer);
  burnTimer=setInterval(()=>burnLoad(false),15000);
  // quiet alert check on load
  setTimeout(()=>burnCheck(false),2500);
}
async function burnLoad(){
  const list=document.getElementById("burn-list");
  if(!list)return;
  try{
    const data=await manageApi("GET","/api/burn");
    const goals=data.goals||[];
    if(!goals.length){
      list.innerHTML='<p class="burn-empty">No burn goals yet. Click <strong>+ New deadline</strong> — set email, provider, amount X, and use-by date.</p>';
      return;
    }
    list.innerHTML=goals.map(burnCardMarkup).join("");
    list.querySelectorAll("[data-burn-del]").forEach(btn=>{
      btn.addEventListener("click",async()=>{
        if(!confirm("Delete this burn goal?"))return;
        try{
          await manageApi("DELETE","/api/burn/"+encodeURIComponent(btn.getAttribute("data-burn-del")));
          burnLoad();
        }catch(err){manageMsg(String(err.message||err),false);}
      });
    });
  }catch(err){
    list.innerHTML='<p class="burn-empty">Could not load burn goals.</p>';
  }
}
function burnCardMarkup(g){
  const status=g.status||"active";
  const cls=status==="completed"?" is-done":status==="expired"?" is-expired":
    ((g.time_elapsed_percent||0)>=75&&status==="active"?" is-urgent":"");
  const pct=Number(g.progress_percent||0);
  const barCls=status==="completed"?"is-done":status==="expired"?"is-expired":
    (g.pace_ok===false?"is-behind":"");
  const provider=(g.provider||"any").toUpperCase();
  const pace=g.pace_ok===false?" · BEHIND PACE":g.pace_ok?" · on pace":"";
  return '<article class="burn-card'+cls+'">'+
    '<div class="burn-card-top"><span class="burn-label">'+esc(g.label||g.id)+
    '</span><span class="burn-countdown" data-seconds="'+(g.seconds_left==null?"":g.seconds_left)+'">'+
    esc(g.countdown||"—")+"</span></div>"+
    '<p class="burn-meta">'+esc(provider)+" · burn <strong>"+esc(String(g.target))+
    "</strong> "+esc(g.unit||"")+" before "+esc((g.deadline||"").slice(0,10))+
    "<br>Used <strong>"+esc(String(g.burned||0))+"</strong> · left to burn <strong>"+
    esc(String(g.remaining_to_burn||0))+"</strong> · "+esc(String(pct))+"%"+esc(pace)+
    (g.email?"<br>Notify: "+esc(g.email):"")+"</p>"+
    '<div class="burn-bar" role="progressbar" aria-valuenow="'+pct+'" aria-valuemin="0" aria-valuemax="100">'+
    '<span class="'+barCls+'" style="width:'+Math.min(100,pct)+'%"></span></div>'+
    '<div class="burn-actions">'+
    '<button type="button" class="danger" data-burn-del="'+esc(g.id)+'">Remove</button>'+
    "</div></article>";
}
async function burnCreate(){
  const label=(document.getElementById("burn-label").value||"").trim();
  const provider=document.getElementById("burn-provider").value;
  const unit=document.getElementById("burn-unit").value;
  const target=Number(document.getElementById("burn-target").value);
  const deadline=document.getElementById("burn-deadline").value;
  const email=(document.getElementById("burn-email").value||"").trim();
  if(!label){manageMsg("Give the goal a label",false);return;}
  if(!(target>0)){manageMsg("Target amount X must be > 0",false);return;}
  if(!deadline){manageMsg("Pick a use-by date",false);return;}
  try{
    await manageApi("POST","/api/burn",{
      label:label, provider:provider, unit:unit, target:target,
      deadline:deadline, email:email||undefined,
      notify_email:document.getElementById("burn-notify-email").checked,
      notify_browser:document.getElementById("burn-notify-browser").checked
    });
    manageMsg("Burn goal created — countdown is live",true);
    document.getElementById("burn-label").value="";
    document.getElementById("burn-target").value="";
    burnLoad();
    manageClose();
  }catch(err){manageMsg(String(err.message||err),false);}
}
async function burnSaveSmtp(){
  try{
    await manageApi("POST","/api/burn/email",{
      smtp_host:(document.getElementById("smtp-host").value||"").trim(),
      smtp_port:Number(document.getElementById("smtp-port").value||587),
      smtp_user:(document.getElementById("smtp-user").value||"").trim(),
      smtp_password:document.getElementById("smtp-pass").value||"",
      use_tls:true
    });
    document.getElementById("smtp-pass").value="";
    manageMsg("SMTP saved for burn-goal emails",true);
  }catch(err){manageMsg(String(err.message||err),false);}
}
async function burnEnablePush(){
  if(!("Notification" in window)){
    manageMsg("This browser has no Notification API",false);return;
  }
  const perm=await Notification.requestPermission();
  manageMsg(perm==="granted"?"Browser notifications enabled for this site":"Permission: "+perm,
    perm==="granted");
}
function burnShowPushes(pushes){
  if(!pushes||!pushes.length)return;
  if(!("Notification" in window)||Notification.permission!=="granted")return;
  pushes.forEach(p=>{
    try{
      new Notification(p.title||"headroom", {
        body:p.body||"",
        tag:p.tag||("headroom-burn-"+Date.now()),
        requireInteraction:true
      });
    }catch(e){}
  });
}
async function burnCheck(force){
  try{
    const path=force?"/api/burn/check?force=1":"/api/burn/check";
    const response=await fetch(path,{cache:"no-store"});
    const data=await response.json();
    if(data.goals){
      // refresh cards from check payload
      const list=document.getElementById("burn-list");
      if(list&&data.goals.length){
        list.innerHTML=data.goals.map(burnCardMarkup).join("");
        list.querySelectorAll("[data-burn-del]").forEach(btn=>{
          btn.addEventListener("click",async()=>{
            if(!confirm("Delete this burn goal?"))return;
            try{
              await manageApi("DELETE","/api/burn/"+encodeURIComponent(btn.getAttribute("data-burn-del")));
              burnLoad();
            }catch(err){manageMsg(String(err.message||err),false);}
          });
        });
      }
    }
    burnShowPushes(data.browser_pushes||[]);
    if(force&&data.email_sent&&data.email_sent.length){
      manageMsg("Sent "+data.email_sent.length+" email alert(s)",true);
    }else if(force&&!data.email_ready&&(data.goals||[]).some(g=>g.notify_email&&g.email)){
      manageMsg("Browser alerts checked. Configure SMTP to also email yourself.",true);
    }
  }catch(err){
    if(force)manageMsg(String(err.message||err),false);
  }
}

/* ------------------------------------------------------- insights (NVIDIA) */
function insightsWire(){
  const setup=document.getElementById("insights-setup");
  const refresh=document.getElementById("insights-refresh");
  const save=document.getElementById("insights-key-save");
  const clear=document.getElementById("insights-key-clear");
  const test=document.getElementById("insights-key-test");
  if(setup)setup.addEventListener("click",()=>{
    manageOpen();
    const section=document.getElementById("insights-key-section");
    if(section)section.scrollIntoView({behavior:"smooth",block:"nearest"});
  });
  if(refresh)refresh.addEventListener("click",()=>insightsLoad(true));
  if(save)save.addEventListener("click",insightsSaveKey);
  if(clear)clear.addEventListener("click",insightsClearKey);
  if(test)test.addEventListener("click",()=>insightsLoad(true));
  insightsStatus();
  insightsLoad(false);
}
async function insightsStatus(){
  try{
    const st=await manageApi("GET","/api/insights/status");
    insightsApplyStatus(st);
  }catch(err){
    const badge=document.getElementById("insights-badge");
    if(badge){badge.textContent="NVIDIA · status error";badge.className="insights-badge is-off";}
  }
}
function insightsApplyStatus(st){
  const badge=document.getElementById("insights-badge");
  const statusEl=document.getElementById("insights-key-status");
  const modelEl=document.getElementById("insights-model");
  const baseEl=document.getElementById("insights-base");
  if(modelEl&&st.model)modelEl.value=st.model;
  if(baseEl&&st.base_url)baseEl.value=st.base_url;
  if(badge){
    if(st.configured){
      badge.textContent="NVIDIA · ready · "+(st.key_preview||"key set");
      badge.className="insights-badge is-on";
    }else{
      badge.textContent="NVIDIA · not configured";
      badge.className="insights-badge is-off";
    }
  }
  if(statusEl){
    if(st.configured){
      statusEl.textContent="Key on file via "+(st.key_source||"secrets")+" · model "+(st.model||"")+" · preview "+(st.key_preview||"");
    }else{
      statusEl.textContent="No key yet. Paste a free NVIDIA key from build.nvidia.com (or set NVIDIA_API_KEY).";
    }
  }
}
async function insightsSaveKey(){
  const key=(document.getElementById("insights-key").value||"").trim();
  if(!key){manageMsg("Paste an API key first",false);return;}
  try{
    const body={
      api_key:key,
      model:(document.getElementById("insights-model").value||"").trim()||undefined,
      base_url:(document.getElementById("insights-base").value||"").trim()||undefined,
      enabled:true
    };
    const res=await manageApi("POST","/api/insights/key",body);
    document.getElementById("insights-key").value="";
    insightsApplyStatus(res.status||{});
    manageMsg("NVIDIA insights key saved locally",true);
    insightsLoad(true);
  }catch(err){manageMsg(String(err.message||err),false);}
}
async function insightsClearKey(){
  if(!confirm("Remove the stored insights API key from this machine?"))return;
  try{
    const res=await manageApi("DELETE","/api/insights/key");
    insightsApplyStatus(res.status||{});
    manageMsg("Insights key removed",true);
    const body=document.getElementById("insights-body");
    if(body){body.textContent="Insights key removed. Add a free NVIDIA key to enable automatic analysis.";body.className="insights-body is-muted";}
  }catch(err){manageMsg(String(err.message||err),false);}
}
async function insightsLoad(force){
  const body=document.getElementById("insights-body");
  const meta=document.getElementById("insights-meta");
  const btn=document.getElementById("insights-refresh");
  if(body){body.textContent=force?"Generating insights via NVIDIA…":"Loading insights…";body.className="insights-body is-muted";}
  if(btn)btn.disabled=true;
  try{
    const path=force?"/api/insights?force=1":"/api/insights";
    // force uses POST for regenerate when force=true and we want server-side force
    let data;
    if(force){
      const response=await fetch("/api/insights",{method:"POST",headers:{"accept":"application/json","content-type":"application/json"},body:"{}",cache:"no-store"});
      data=await response.json();
      if(!response.ok&&!data.text)throw new Error(data.message||data.error||("HTTP "+response.status));
    }else{
      const response=await fetch(path,{cache:"no-store"});
      data=await response.json();
      if(!response.ok&&!data.ok){
        // soft: show setup message
        if(body){
          body.textContent=data.message||"Add a free NVIDIA API key to enable automatic insights.";
          body.className="insights-body is-muted";
        }
        if(data.status)insightsApplyStatus(data.status);
        if(meta)meta.textContent="No key configured · free keys at build.nvidia.com";
        return;
      }
    }
    if(data.status)insightsApplyStatus(data.status);
    if(data.ok&&data.text){
      if(body){body.textContent=data.text;body.className="insights-body";}
      if(meta){
        const age=data.cached?("cached · "+(data.age_seconds||0)+"s old"):"fresh";
        meta.textContent=(data.model||"model")+" · "+age+" · key via "+(data.key_source||"?")+
          (data.fleet_monthly_usd!=null?(" · fleet $"+data.fleet_monthly_usd+"/mo"):"");
      }
    }else{
      if(body){body.textContent=data.message||data.error||"Insights unavailable";body.className="insights-body is-muted";}
      if(meta)meta.textContent=data.error||"error";
    }
  }catch(err){
    if(body){body.textContent=String(err.message||err);body.className="insights-body is-muted";}
  }finally{
    if(btn)btn.disabled=false;
  }
}

/* ------------------------------------------------------- history charts */
let histRange="week";
let histMetric="left";
let histData=null;
let histHidden={}; // series id -> true when toggled off
function histWire(){
  document.querySelectorAll("[data-hist-range]").forEach(btn=>{
    btn.addEventListener("click",()=>{
      histRange=btn.getAttribute("data-hist-range");
      document.querySelectorAll("[data-hist-range]").forEach(b=>b.classList.toggle("is-active",b===btn));
      histLoad();
    });
  });
  document.querySelectorAll("[data-hist-metric]").forEach(btn=>{
    btn.addEventListener("click",()=>{
      histMetric=btn.getAttribute("data-hist-metric");
      document.querySelectorAll("[data-hist-metric]").forEach(b=>b.classList.toggle("is-active",b===btn));
      histLoad();
    });
  });
  const wrap=document.getElementById("history-chart-wrap");
  if(wrap){
    wrap.addEventListener("mousemove",histHover);
    wrap.addEventListener("mouseleave",()=>{
      const tip=document.getElementById("history-tooltip");
      if(tip)tip.classList.remove("is-on");
    });
  }
}
async function histLoad(){
  try{
    const url="/api/history?range="+encodeURIComponent(histRange)+"&metric="+encodeURIComponent(histMetric)+"&t="+Date.now();
    const response=await fetch(url,{cache:"no-store"});
    if(!response.ok)throw new Error("history HTTP "+response.status);
    histData=await response.json();
    histRender();
  }catch(err){
    const empty=document.getElementById("history-empty");
    if(empty){empty.style.display="flex";empty.textContent="History unavailable — "+(err.message||err);}
  }
}
function histFmtTime(ts,range){
  const d=new Date(ts*1000);
  if(range==="day")return d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
  if(range==="week")return d.toLocaleString([], {weekday:"short",hour:"2-digit"});
  return d.toLocaleDateString([], {month:"short",day:"numeric"});
}
function histRender(){
  const svg=document.getElementById("history-svg");
  const empty=document.getElementById("history-empty");
  const legend=document.getElementById("history-legend");
  const meta=document.getElementById("history-meta");
  if(!svg||!histData)return;
  const times=histData.times||[];
  const series=(histData.series||[]).filter(s=>!histHidden[s.id]);
  const allSeries=histData.series||[];
  const W=800,H=280,padL=44,padR=16,padT=18,padB=32;
  const plotW=W-padL-padR,plotH=H-padT-padB;
  const hasPoints=allSeries.some(s=>(s.points||[]).some(p=>p!=null));
  if(!times.length||!hasPoints){
    svg.innerHTML="";
    if(empty){empty.style.display="flex";empty.textContent="No local history yet. Hit Refresh — each collect appends a point here.";}
    if(legend)legend.innerHTML="";
    return;
  }
  if(empty)empty.style.display="none";
  // y domain 0–100 for capacity %
  const yMin=0,yMax=100;
  const xAt=i=>padL+(times.length<=1?plotW/2:(i/(times.length-1))*plotW);
  const yAt=v=>padT+plotH-((v-yMin)/(yMax-yMin))*plotH;
  let parts=[];
  // grid
  for(let g=0;g<=4;g++){
    const v=g*25;
    const y=yAt(v);
    parts.push('<line x1="'+padL+'" y1="'+y+'" x2="'+(W-padR)+'" y2="'+y+
      '" stroke="currentColor" stroke-opacity="0.08" stroke-width="1"/>');
    parts.push('<text x="'+(padL-8)+'" y="'+(y+3)+'" text-anchor="end" fill="currentColor" fill-opacity="0.35" font-size="10" font-family="ui-monospace,Menlo,monospace">'+v+'%</text>');
  }
  // x labels
  const labelEvery=Math.max(1,Math.floor(times.length/6));
  times.forEach((t,i)=>{
    if(i%labelEvery!==0&&i!==times.length-1)return;
    parts.push('<text x="'+xAt(i)+'" y="'+(H-10)+'" text-anchor="middle" fill="currentColor" fill-opacity="0.35" font-size="10" font-family="ui-monospace,Menlo,monospace">'+
      esc(histFmtTime(t,histData.range))+"</text>");
  });
  // series lines + soft fills
  series.forEach(s=>{
    const pts=[];
    (s.points||[]).forEach((v,i)=>{
      if(v==null||!Number.isFinite(v))return;
      pts.push([xAt(i),yAt(Math.max(0,Math.min(100,v))),v,i]);
    });
    if(pts.length<1)return;
    // soft area under line
    if(pts.length>=2){
      let area="M "+pts[0][0]+" "+yAt(0)+" L "+pts.map(p=>p[0]+" "+p[1]).join(" L ")+
        " L "+pts[pts.length-1][0]+" "+yAt(0)+" Z";
      parts.push('<path d="'+area+'" fill="'+esc(s.color)+'" fill-opacity="0.07"/>');
    }
    let d="";
    pts.forEach((p,idx)=>{d+=(idx?" L ":"M ")+p[0]+" "+p[1];});
    parts.push('<path d="'+d+'" fill="none" stroke="'+esc(s.color)+'" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>');
    pts.forEach(p=>{
      parts.push('<circle cx="'+p[0]+'" cy="'+p[1]+'" r="2.6" fill="'+esc(s.color)+'" stroke="var(--panel)" stroke-width="1"/>');
    });
  });
  // crosshair guide (static layer; hover uses tooltip)
  svg.innerHTML=parts.join("");
  svg.style.color="var(--ink)";
  // legend
  if(legend){
    legend.innerHTML=allSeries.map(s=>{
      const off=histHidden[s.id]?" is-off":"";
      return '<span class="history-legend-item'+off+'" data-series="'+esc(s.id)+'">'+
        '<span class="history-legend-swatch" style="background:'+esc(s.color)+'"></span>'+
        esc(s.label||s.name)+"</span>";
    }).join("");
    legend.querySelectorAll("[data-series]").forEach(el=>{
      el.addEventListener("click",()=>{
        const id=el.getAttribute("data-series");
        histHidden[id]=!histHidden[id];
        histRender();
      });
    });
  }
  if(meta){
    const n=histData.sample_count||0;
    meta.textContent=n+" local sample"+(n===1?"":"s")+" · "+
      (histData.metric_label||"Remaining %")+" · "+
      histData.range+" view · stored in ~/.headroom/state/history.jsonl · "+
      (histData.retention_days||90)+"-day retention";
  }
}
function histHover(ev){
  if(!histData||!histData.times||!histData.times.length)return;
  const wrap=document.getElementById("history-chart-wrap");
  const tip=document.getElementById("history-tooltip");
  if(!wrap||!tip)return;
  const rect=wrap.getBoundingClientRect();
  const x=ev.clientX-rect.left;
  const W=rect.width, padL=44*(W/800), padR=16*(W/800);
  const plotW=W-padL-padR;
  const times=histData.times;
  let idx=0;
  if(times.length>1){
    const rel=Math.max(0,Math.min(1,(x-padL)/plotW));
    idx=Math.round(rel*(times.length-1));
  }
  const series=(histData.series||[]).filter(s=>!histHidden[s.id]);
  const rows=series.map(s=>{
    const v=(s.points||[])[idx];
    if(v==null)return null;
    return {label:s.label||s.name,color:s.color,value:v};
  }).filter(Boolean);
  if(!rows.length){tip.classList.remove("is-on");return;}
  tip.innerHTML='<div class="tt-time">'+esc(histFmtTime(times[idx],histData.range))+"</div>"+
    rows.map(r=>'<div class="tt-row"><span><span class="tt-dot" style="background:'+esc(r.color)+
      '"></span>'+esc(r.label)+'</span><strong>'+Math.round(r.value)+'%</strong></div>').join("");
  tip.classList.add("is-on");
  const tw=tip.offsetWidth||160, th=tip.offsetHeight||60;
  let left=ev.clientX-rect.left+12, top=ev.clientY-rect.top-th-8;
  if(left+tw>W-8)left=ev.clientX-rect.left-tw-12;
  if(top<4)top=ev.clientY-rect.top+12;
  tip.style.left=left+"px";
  tip.style.top=top+"px";
}

/* ------------------------------------------------------- manage accounts */
let manageState=null;
let editTarget=null;
let cardEditName=null;

function cardEl(name){
  return document.querySelector('.account[data-account="'+CSS.escape(name)+'"]');
}
function cardEditPanel(name){
  const card=cardEl(name);
  return card?card.querySelector("[data-inline-edit]"):null;
}
function cardEditMsg(name,text,ok){
  const panel=cardEditPanel(name);
  if(!panel)return;
  const el=panel.querySelector("[data-ie-msg]");
  if(!el)return;
  el.textContent=text||"";
  el.className="ie-msg"+(text?(ok?" is-ok":" is-error"):"");
}
function cardCloseAllEdits(){
  document.querySelectorAll(".account.is-editing").forEach(c=>c.classList.remove("is-editing"));
  cardEditName=null;
}
async function ensureManageState(){
  if(manageState&&Array.isArray(manageState.accounts))return manageState;
  try{
    const accounts=await manageApi("GET","/api/accounts");
    manageState={
      accounts:accounts.accounts||[],
      by_provider:accounts.by_provider||{},
      dashboard:accounts.dashboard,
      detected:accounts.detected||[],
      suggested_names:accounts.suggested_names||{},
      multi_account:accounts.multi_account||{}
    };
  }catch(e){/* demo / offline */}
  return manageState;
}
async function cardStartEdit(name){
  if(!name)return;
  // Inline on the card — never open the sidebar for edit
  if(document.getElementById("manage-panel")&&
     document.getElementById("manage-panel").classList.contains("is-open"))
    manageClose();
  await ensureManageState();
  const card=cardEl(name);
  const panel=cardEditPanel(name);
  if(!card||!panel){
    loginToast("Could not find card for "+name,5000);
    return;
  }
  cardCloseAllEdits();
  const meta=((manageState&&manageState.accounts)||[]).find(a=>a.name===name)||{};
  const costInput=panel.querySelector("[data-ie-cost]");
  const emailInput=panel.querySelector("[data-ie-email]");
  const reservedInput=panel.querySelector("[data-ie-reserved]");
  const renewsOnInput=panel.querySelector("[data-ie-renews-on]");
  const renewAmtInput=panel.querySelector("[data-ie-renew-amount]");
  const yearEl=panel.querySelector("[data-ie-year]");
  const homeEl=panel.querySelector("[data-ie-home]");
  if(costInput){
    const cost=meta.monthly_cost_usd!=null?meta.monthly_cost_usd:costInput.value;
    costInput.value=cost!=null&&cost!==""?cost:"";
  }
  if(emailInput)emailInput.value=meta.expected_email||emailInput.value||"";
  if(reservedInput)reservedInput.checked=!!meta.reserved;
  if(renewsOnInput){
    const d=meta.renews_on||renewsOnInput.value||"";
    renewsOnInput.value=d?String(d).slice(0,10):"";
  }
  if(renewAmtInput){
    renewAmtInput.value=meta.renew_amount!=null&&meta.renew_amount!==""
      ?meta.renew_amount:(renewAmtInput.value||"");
  }
  if(homeEl)homeEl.textContent=meta.home?("home · "+meta.home):"";
  if(yearEl)yearHint(costInput&&costInput.value,yearEl);
  card.classList.add("is-editing");
  cardEditName=name;
  cardEditMsg(name,"",true);
  // focus cost
  if(costInput)setTimeout(()=>costInput.focus(),30);
}
async function cardSaveEdit(name){
  const panel=cardEditPanel(name);
  if(!panel)return;
  const costRaw=(panel.querySelector("[data-ie-cost]")||{}).value;
  const email=((panel.querySelector("[data-ie-email]")||{}).value||"").trim();
  const reservedEl=panel.querySelector("[data-ie-reserved]");
  const renewsOn=((panel.querySelector("[data-ie-renews-on]")||{}).value||"").trim();
  const renewAmtRaw=((panel.querySelector("[data-ie-renew-amount]")||{}).value||"").trim();
  const body={
    expected_email:email,
    reserved:reservedEl?!!reservedEl.checked:false,
    monthly_cost_usd:costRaw===""?null:Number(costRaw),
    renews_on:renewsOn===""?null:renewsOn,
    renew_amount:renewAmtRaw===""?null:Number(renewAmtRaw)
  };
  try{
    cardEditMsg(name,"Saving…",true);
    await manageApi("PATCH","/api/accounts/"+encodeURIComponent(name),body);
    cardEditMsg(name,"Saved",true);
    manageState=null;
    await ensureManageState();
    cardCloseAllEdits();
    load(true);
    loginToast("Saved "+name,4000);
  }catch(err){
    cardEditMsg(name,String(err.message||err),false);
  }
}
async function cardRemoveAccount(name){
  if(!confirm("Remove account slot \""+name+"\" from headroom?\n(Login files stay on disk.)"))return;
  try{
    cardEditMsg(name,"Removing…",true);
    await manageApi("DELETE","/api/accounts/"+encodeURIComponent(name));
    cardCloseAllEdits();
    manageState=null;
    await ensureManageState();
    load(true);
    loginToast("Removed "+name,4000);
  }catch(err){
    cardEditMsg(name,String(err.message||err),false);
  }
}
function wireInlineEditForms(){
  document.querySelectorAll("[data-inline-edit]").forEach(panel=>{
    const name=panel.getAttribute("data-inline-edit");
    const cost=panel.querySelector("[data-ie-cost]");
    const year=panel.querySelector("[data-ie-year]");
    if(cost&&year){
      cost.addEventListener("input",()=>yearHint(cost.value,year));
      yearHint(cost.value,year);
    }
    const save=panel.querySelector("[data-ie-save]");
    const cancel=panel.querySelector("[data-ie-cancel]");
    const remove=panel.querySelector("[data-ie-remove]");
    if(save)save.addEventListener("click",()=>cardSaveEdit(name));
    if(cancel)cancel.addEventListener("click",()=>cardCloseAllEdits());
    if(remove)remove.addEventListener("click",()=>cardRemoveAccount(name));
  });
}

let reorderBusy=false;
async function moveAccountCard(name, direction){
  if(!name||reorderBusy)return;
  reorderBusy=true;
  try{
    await manageApi("POST","/api/accounts",{
      mode:"move", name:name, direction:direction, scope:"provider"
    });
    manageState=null;
    await load(true);
    loginToast(name+" moved "+direction, 2500);
  }catch(err){
    loginToast(String(err.message||err), 6000);
  }finally{
    reorderBusy=false;
  }
}
async function reorderProviderCards(provider, orderedNames){
  if(reorderBusy)return;
  reorderBusy=true;
  try{
    // Build full order: keep other providers, replace this provider's block
    await ensureManageState();
    const full=[];
    let injected=false;
    ((manageState&&manageState.accounts)||[]).forEach(a=>{
      if(a.provider===provider){
        if(!injected){
          orderedNames.forEach(n=>full.push(n));
          injected=true;
        }
      }else full.push(a.name);
    });
    if(!injected)orderedNames.forEach(n=>full.push(n));
    await manageApi("POST","/api/accounts",{mode:"reorder", order:full});
    manageState=null;
    await load(true);
    loginToast("Order saved", 2500);
  }catch(err){
    loginToast(String(err.message||err), 6000);
    await load(true);
  }finally{
    reorderBusy=false;
  }
}
function wireAccountReorder(){
  const root=document.getElementById("providers");
  if(!root)return;
  root.querySelectorAll("[data-move]").forEach(btn=>{
    btn.addEventListener("click",(ev)=>{
      ev.preventDefault();
      ev.stopPropagation();
      moveAccountCard(btn.getAttribute("data-name"), btn.getAttribute("data-move"));
    });
  });
  let dragName=null;
  root.querySelectorAll("[data-drag]").forEach(handle=>{
    const card=handle.closest(".account");
    if(!card)return;
    handle.addEventListener("dragstart",(ev)=>{
      dragName=handle.getAttribute("data-drag");
      card.classList.add("is-dragging");
      try{
        ev.dataTransfer.setData("text/plain", dragName);
        ev.dataTransfer.effectAllowed="move";
      }catch(e){}
    });
    handle.addEventListener("dragend",()=>{
      card.classList.remove("is-dragging");
      root.querySelectorAll(".account.is-drop-target").forEach(c=>c.classList.remove("is-drop-target"));
      dragName=null;
    });
  });
  root.querySelectorAll(".account[data-account]").forEach(card=>{
    card.addEventListener("dragover",(ev)=>{
      if(!dragName)return;
      const target=card.getAttribute("data-account");
      const prov=card.getAttribute("data-provider");
      const srcCard=root.querySelector('.account[data-account="'+CSS.escape(dragName)+'"]');
      if(!srcCard||srcCard.getAttribute("data-provider")!==prov)return;
      if(target===dragName)return;
      ev.preventDefault();
      try{ev.dataTransfer.dropEffect="move";}catch(e){}
      root.querySelectorAll(".account.is-drop-target").forEach(c=>c.classList.remove("is-drop-target"));
      card.classList.add("is-drop-target");
    });
    card.addEventListener("dragleave",()=>card.classList.remove("is-drop-target"));
    card.addEventListener("drop",(ev)=>{
      ev.preventDefault();
      card.classList.remove("is-drop-target");
      const target=card.getAttribute("data-account");
      const prov=card.getAttribute("data-provider");
      const src=dragName||(ev.dataTransfer&&ev.dataTransfer.getData("text/plain"));
      if(!src||src===target)return;
      const section=card.closest(".provider");
      if(!section)return;
      const names=[...section.querySelectorAll(".account[data-account]")].map(c=>c.getAttribute("data-account"));
      const from=names.indexOf(src), to=names.indexOf(target);
      if(from<0||to<0)return;
      names.splice(to,0,names.splice(from,1)[0]);
      reorderProviderCards(prov, names);
    });
  });
}

let providerReorderBusy=false;
function applyProviderOrder(order){
  if(CONFIG)CONFIG.provider_order=order.slice();
}
async function moveProviderSection(provider, direction){
  if(!provider||providerReorderBusy)return;
  providerReorderBusy=true;
  try{
    const res=await manageApi("POST","/api/providers/order",{
      mode:"move", provider:provider, direction:direction
    });
    const order=(res.provider_order||(res.dashboard&&res.dashboard.provider_order)||[]);
    if(order.length)applyProviderOrder(order);
    manageState=null;
    await load(true);
    loginToast(provider+" section moved "+direction, 2500);
  }catch(err){
    loginToast(String(err.message||err), 6000);
  }finally{
    providerReorderBusy=false;
  }
}
async function reorderProviderSections(order){
  if(providerReorderBusy)return;
  providerReorderBusy=true;
  try{
    const res=await manageApi("POST","/api/providers/order",{order:order});
    const next=(res.provider_order||(res.dashboard&&res.dashboard.provider_order)||order);
    applyProviderOrder(next);
    manageState=null;
    await load(true);
    loginToast("Provider order saved", 2500);
  }catch(err){
    loginToast(String(err.message||err), 6000);
    await load(true);
  }finally{
    providerReorderBusy=false;
  }
}
function wireProviderReorder(){
  const root=document.getElementById("providers");
  if(!root)return;
  root.querySelectorAll("[data-provider-move]").forEach(btn=>{
    btn.addEventListener("click",(ev)=>{
      ev.preventDefault();
      ev.stopPropagation();
      moveProviderSection(
        btn.getAttribute("data-provider"),
        btn.getAttribute("data-provider-move"));
    });
  });
  let dragProv=null;
  root.querySelectorAll("[data-provider-drag]").forEach(handle=>{
    const section=handle.closest(".provider");
    if(!section)return;
    handle.addEventListener("dragstart",(ev)=>{
      dragProv=handle.getAttribute("data-provider-drag");
      section.classList.add("is-dragging");
      try{
        ev.dataTransfer.setData("text/plain", "provider:"+dragProv);
        ev.dataTransfer.effectAllowed="move";
      }catch(e){}
    });
    handle.addEventListener("dragend",()=>{
      section.classList.remove("is-dragging");
      root.querySelectorAll(".provider.is-drop-target").forEach(s=>s.classList.remove("is-drop-target"));
      dragProv=null;
    });
  });
  root.querySelectorAll(".provider[data-provider]").forEach(section=>{
    section.addEventListener("dragover",(ev)=>{
      if(!dragProv)return;
      const target=section.getAttribute("data-provider");
      if(!target||target===dragProv)return;
      ev.preventDefault();
      try{ev.dataTransfer.dropEffect="move";}catch(e){}
      root.querySelectorAll(".provider.is-drop-target").forEach(s=>s.classList.remove("is-drop-target"));
      section.classList.add("is-drop-target");
    });
    section.addEventListener("dragleave",()=>section.classList.remove("is-drop-target"));
    section.addEventListener("drop",(ev)=>{
      ev.preventDefault();
      section.classList.remove("is-drop-target");
      const target=section.getAttribute("data-provider");
      let src=dragProv;
      try{
        const raw=ev.dataTransfer&&ev.dataTransfer.getData("text/plain");
        if(raw&&raw.indexOf("provider:")===0)src=raw.slice(9);
      }catch(e){}
      if(!src||!target||src===target)return;
      const order=[...root.querySelectorAll(".provider[data-provider]")].map(s=>s.getAttribute("data-provider"));
      const from=order.indexOf(src), to=order.indexOf(target);
      if(from<0||to<0)return;
      order.splice(to,0,order.splice(from,1)[0]);
      reorderProviderSections(order);
    });
  });
}

function manageOpen(){
  const panel=document.getElementById("manage-panel");
  const backdrop=document.getElementById("manage-backdrop");
  panel.hidden=false;backdrop.hidden=false;
  panel.classList.add("is-open");backdrop.classList.add("is-open");
  manageRefresh();
}
function manageClose(){
  const panel=document.getElementById("manage-panel");
  const backdrop=document.getElementById("manage-backdrop");
  panel.classList.remove("is-open");backdrop.classList.remove("is-open");
  panel.hidden=true;backdrop.hidden=true;
  editTarget=null;
  document.getElementById("edit-section").hidden=true;
}
function manageMsg(text,ok){
  const el=document.getElementById("manage-msg");
  if(!text){el.className="manage-msg";el.textContent="";return;}
  el.className="manage-msg "+(ok?"is-ok":"is-error");
  el.textContent=text;
}
function yearHint(monthly,el){
  const n=Number(monthly);
  if(!Number.isFinite(n)||n<0){el.textContent="Yearly = monthly × 12";return;}
  el.textContent=formatCost(n);
}
async function manageApi(method,path,body){
  const opts={method:method,headers:{"accept":"application/json"},cache:"no-store"};
  if(body!==undefined){
    opts.headers["content-type"]="application/json";
    opts.body=JSON.stringify(body);
  }
  const response=await fetch(path,opts);
  let data={};
  try{data=await response.json();}catch(e){data={error:"invalid response"};}
  if(!response.ok)throw new Error(data.error||("HTTP "+response.status));
  return data;
}
function renderManageList(){
  const list=document.getElementById("manage-list");
  const countEl=document.getElementById("manage-count");
  const accounts=(manageState&&manageState.accounts)||[];
  const by=manageState&&manageState.by_provider||{};
  if(countEl){
    const parts=Object.keys(by).map(p=>p+": "+by[p].length);
    countEl.textContent=accounts.length?("· "+accounts.length+" slots"+(parts.length?" ("+parts.join(", ")+")":"")):"";
  }
  if(!accounts.length){
    list.innerHTML='<p class="manage-hint">No accounts yet — adopt a login below or add another account.</p>';
    return;
  }
  list.innerHTML=accounts.map(a=>{
    const cost=formatCost(a.monthly_cost_usd)||"cost unset";
    const email=a.expected_email||"(no email pin)";
    const reserved=a.reserved?' · reserved':"";
    const multi=(by[a.provider]||[]).length>1?' · multi':"";
    return '<div class="manage-card" data-name="'+esc(a.name)+'">'+
      '<div class="manage-card-top"><span class="manage-card-name">'+esc(a.name)+
      '</span><span class="manage-card-prov">'+esc(a.provider)+esc(multi)+"</span></div>"+
      '<p class="manage-card-meta">'+esc(email)+"<br>"+esc(cost)+esc(reserved)+
      "<br><span style=\"opacity:.75\">"+esc(a.home||"")+"</span></p>"+
      '<div class="manage-card-actions">'+
      '<button type="button" data-act="edit" data-name="'+esc(a.name)+'">Edit</button>'+
      '<button type="button" class="danger" data-act="remove" data-name="'+esc(a.name)+'">Remove</button>'+
      "</div></div>";
  }).join("");
  list.querySelectorAll("button[data-act]").forEach(btn=>{
    btn.addEventListener("click",()=>{
      const name=btn.getAttribute("data-name");
      if(btn.getAttribute("data-act")==="edit")manageStartEdit(name);
      else manageRemove(name);
    });
  });
}
function renderDetected(){
  const box=document.getElementById("manage-detect");
  const found=(manageState&&manageState.detected)||[];
  const open=found.filter(d=>!d.already_connected);
  const used=found.filter(d=>d.already_connected);
  if(!open.length&&!used.length){
    box.innerHTML='<p class="manage-hint">No logins detected. For a 2nd Claude/Codex/Grok: use “New login — isolated home” below.</p>';
    return;
  }
  let html="";
  if(open.length){
    html+=open.map(d=>
      '<button type="button" data-provider="'+esc(d.provider)+'" data-home="'+esc(d.home)+
      '" data-email="'+esc(d.email||"")+'">Adopt '+esc(d.provider)+' · '+esc(d.email||d.home)+
      (d.source==="homes"?" · isolated":"")+"</button>").join("");
  }else{
    html+='<p class="manage-hint">All detected logins are already connected. Add another with a fresh isolated login.</p>';
  }
  if(used.length){
    html+='<p class="manage-hint" style="margin-top:8px">Already connected: '+
      used.map(d=>esc(d.provider)+"/"+esc(d.email||"?")).join(", ")+"</p>";
  }
  box.innerHTML=html;
  box.querySelectorAll("button[data-provider]").forEach(btn=>{
    btn.addEventListener("click",async()=>{
      const provider=btn.getAttribute("data-provider");
      const home=btn.getAttribute("data-home");
      const email=btn.getAttribute("data-email")||"";
      const suggestions=(manageState&&manageState.suggested_names)||{};
      const name=suggestions[provider]||provider;
      try{
        manageMsg("Adopting "+provider+" as "+name+"…",true);
        await manageApi("POST","/api/accounts",{
          mode:"adopt",name:name,provider:provider,home:home,
          expected_email:email||undefined
        });
        manageMsg("Connected "+name+" ("+provider+")",true);
        await manageRefresh();
        load(true);histLoad();
      }catch(err){manageMsg(String(err.message||err),false);}
    });
  });
}
function manageStartEdit(name){
  // Edit lives on the account card — close sidebar and open inline form.
  manageClose();
  cardStartEdit(name).then(()=>{
    const card=cardEl(name);
    if(card)card.scrollIntoView({behavior:"smooth",block:"nearest"});
  });
}
async function manageRemove(name){
  if(!confirm("Remove account \""+name+"\" from headroom?\n(Credentials on disk are kept.)"))return;
  try{
    await manageApi("DELETE","/api/accounts/"+encodeURIComponent(name));
    manageMsg("Removed "+name,true);
    if(editTarget===name){
      editTarget=null;
      document.getElementById("edit-section").hidden=true;
    }
    await manageRefresh();
    load(true);
  }catch(err){manageMsg(String(err.message||err),false);}
}
async function manageRefresh(){
  try{
    const meta=await manageApi("GET","/api/meta");
    const accounts=await manageApi("GET","/api/accounts");
    manageState={
      accounts:accounts.accounts||[],
      by_provider:accounts.by_provider||meta.by_provider||{},
      dashboard:accounts.dashboard||meta.dashboard,
      detected:accounts.detected||meta.detected||[],
      default_homes:meta.default_homes||{},
      cost_hints:meta.cost_hints||{},
      suggested_names:accounts.suggested_names||meta.suggested_names||{},
      multi_account:accounts.multi_account||meta.multi_account||{}
    };
    renderManageList();
    renderDetected();
    syncAddFormProvider();
  }catch(err){
    manageMsg(String(err.message||err),false);
    document.getElementById("manage-list").innerHTML=
      '<p class="manage-hint">Could not load accounts. Is this the live server (not demo)?</p>';
  }
}
let pendingFresh=null; // {name, provider, home, login_cmd, cost}
function syncAddFormProvider(){
  const provider=document.getElementById("add-provider").value;
  const homes=(manageState&&manageState.default_homes)||{};
  const suggestions=(manageState&&manageState.suggested_names)||{};
  const isKey=provider==="manus"||provider==="nvidia";
  const modeEl=document.getElementById("add-mode");
  const modeWrap=document.getElementById("add-mode-wrap");
  if(modeWrap)modeWrap.hidden=isKey;
  if(isKey&&modeEl)modeEl.value="adopt";
  const mode=modeEl?modeEl.value:"fresh";
  document.getElementById("add-key-wrap").hidden=!isKey;
  document.getElementById("add-home-wrap").hidden=isKey||mode!=="adopt";
  const keyLabel=document.querySelector('#add-key-wrap label');
  if(keyLabel)keyLabel.textContent=provider==="nvidia"?"NVIDIA API key (blank = reuse Insights key)":"Manus API key";
  document.getElementById("add-home").value=homes[provider]||"";
  const nameEl=document.getElementById("add-name");
  if(nameEl&&!nameEl.dataset.userEdited){
    nameEl.value=suggestions[provider]||provider;
    nameEl.placeholder=suggestions[provider]||(provider+"-2");
  }
  const hints=(manageState&&manageState.cost_hints&&manageState.cost_hints[provider])||{};
  const first=Object.values(hints)[0];
  if(first!=null&&!document.getElementById("add-cost").value){
    document.getElementById("add-cost").placeholder=String(first);
  }
  if(provider==="nvidia"&&!document.getElementById("add-cost").value){
    document.getElementById("add-cost").placeholder="0";
  }
  const submit=document.getElementById("add-submit");
  if(submit)submit.textContent=isKey?"Add API-key account":(mode==="fresh"?"Prepare isolated login":"Adopt path");
  document.getElementById("add-finish").hidden=true;
  document.getElementById("add-copy-cmd").hidden=true;
  document.getElementById("add-fresh-box").style.display="none";
  pendingFresh=null;
  yearHint(document.getElementById("add-cost").value,document.getElementById("add-year-hint"));
}
async function manageAdd(){
  const provider=document.getElementById("add-provider").value;
  let name=(document.getElementById("add-name").value||"").trim();
  const suggestions=(manageState&&manageState.suggested_names)||{};
  if(!name)name=suggestions[provider]||provider;
  const costRaw=document.getElementById("add-cost").value;
  const cost=costRaw===""?undefined:Number(costRaw);
  const email=(document.getElementById("add-email").value||"").trim()||undefined;
  const modeEl=document.getElementById("add-mode");
  const mode=modeEl?modeEl.value:"fresh";
  try{
    if(provider==="manus"){
      const key=(document.getElementById("add-key").value||"").trim();
      if(!key)throw new Error("Paste a Manus API key");
      manageMsg("Adding "+name+"…",true);
      await manageApi("POST","/api/accounts",{
        mode:"manus",name:name,api_key:key,email:email,monthly_cost_usd:cost
      });
      document.getElementById("add-key").value="";
      manageMsg("Added "+name+" (manus)",true);
    }else if(provider==="nvidia"){
      const key=(document.getElementById("add-key").value||"").trim();
      manageMsg("Adding "+name+"…",true);
      await manageApi("POST","/api/accounts",{
        mode:"nvidia",name:name,api_key:key||undefined,email:email,
        monthly_cost_usd:cost===undefined?0:cost,
        use_insights_key:!key
      });
      document.getElementById("add-key").value="";
      manageMsg("Added "+name+" (nvidia)",true);
    }else if(mode==="adopt"){
      const home=(document.getElementById("add-home").value||"").trim();
      manageMsg("Adopting "+name+"…",true);
      await manageApi("POST","/api/accounts",{
        mode:"adopt",name:name,provider:provider,home:home||undefined,
        expected_email:email,monthly_cost_usd:cost
      });
      manageMsg("Connected "+name+" ("+provider+")",true);
    }else{
      // Fresh multi-account isolated login (original headroom model)
      manageMsg("Preparing isolated "+provider+" home…",true);
      const res=await manageApi("POST","/api/accounts",{
        mode:"prepare",name:name,provider:provider,monthly_cost_usd:cost
      });
      const prep=res.prepared||res;
      pendingFresh={name:prep.name,provider:prep.provider,home:prep.home,
        login_cmd:prep.login_cmd,cost:cost,email:email};
      const box=document.getElementById("add-fresh-box");
      box.style.display="block";
      box.textContent=prep.instructions||prep.login_cmd;
      document.getElementById("add-finish").hidden=false;
      document.getElementById("add-copy-cmd").hidden=false;
      manageMsg("Home ready at "+prep.home+" — run the login command, then Connect slot",true);
      await manageRefresh();
      return;
    }
    document.getElementById("add-name").value="";
    document.getElementById("add-name").dataset.userEdited="";
    document.getElementById("add-cost").value="";
    await manageRefresh();
    syncAddFormProvider();
    load(true);histLoad();
  }catch(err){manageMsg(String(err.message||err),false);}
}
async function manageFinishFresh(){
  if(!pendingFresh){manageMsg("Nothing pending — prepare a fresh login first",false);return;}
  try{
    manageMsg("Connecting "+pendingFresh.name+"…",true);
    await manageApi("POST","/api/accounts",{
      mode:"finish",
      name:pendingFresh.name,
      provider:pendingFresh.provider,
      monthly_cost_usd:pendingFresh.cost,
      expected_email:pendingFresh.email
    });
    manageMsg("Connected "+pendingFresh.name+" — multi-account slot live",true);
    pendingFresh=null;
    document.getElementById("add-fresh-box").style.display="none";
    document.getElementById("add-finish").hidden=true;
    document.getElementById("add-copy-cmd").hidden=true;
    document.getElementById("add-name").value="";
    document.getElementById("add-name").dataset.userEdited="";
    await manageRefresh();
    syncAddFormProvider();
    load(true);histLoad();
  }catch(err){manageMsg(String(err.message||err),false);}
}
async function manageSaveEdit(){
  if(!editTarget)return;
  const costRaw=document.getElementById("edit-cost").value;
  const body={
    expected_email:(document.getElementById("edit-email").value||"").trim(),
    reserved:document.getElementById("edit-reserved").checked
  };
  body.monthly_cost_usd=costRaw===""?null:Number(costRaw);
  try{
    await manageApi("PATCH","/api/accounts/"+encodeURIComponent(editTarget),body);
    manageMsg("Saved "+editTarget,true);
    await manageRefresh();
    load(true);
  }catch(err){manageMsg(String(err.message||err),false);}
}
function wireManage(){
  const openBtn=document.getElementById("manage-open");
  if(!openBtn)return;
  openBtn.addEventListener("click",manageOpen);
  const barBtn=document.getElementById("accounts-bar-manage");
  if(barBtn)barBtn.addEventListener("click",manageOpen);
  const barRefresh=document.getElementById("accounts-bar-refresh");
  if(barRefresh)barRefresh.addEventListener("click",()=>{load(true);histLoad();});
  document.getElementById("manage-close").addEventListener("click",manageClose);
  document.getElementById("manage-backdrop").addEventListener("click",manageClose);
  document.getElementById("add-provider").addEventListener("change",syncAddFormProvider);
  const modeEl=document.getElementById("add-mode");
  if(modeEl)modeEl.addEventListener("change",syncAddFormProvider);
  const nameEl=document.getElementById("add-name");
  if(nameEl)nameEl.addEventListener("input",()=>{nameEl.dataset.userEdited="1";});
  document.getElementById("add-cost").addEventListener("input",()=>
    yearHint(document.getElementById("add-cost").value,document.getElementById("add-year-hint")));
  document.getElementById("edit-cost").addEventListener("input",()=>
    yearHint(document.getElementById("edit-cost").value,document.getElementById("edit-year-hint")));
  document.getElementById("add-submit").addEventListener("click",manageAdd);
  const fin=document.getElementById("add-finish");
  if(fin)fin.addEventListener("click",manageFinishFresh);
  const copyBtn=document.getElementById("add-copy-cmd");
  if(copyBtn)copyBtn.addEventListener("click",async()=>{
    if(!pendingFresh||!pendingFresh.login_cmd)return;
    try{
      await navigator.clipboard.writeText(pendingFresh.login_cmd);
      manageMsg("Login command copied",true);
    }catch(e){
      manageMsg(pendingFresh.login_cmd,true);
    }
  });
  document.getElementById("edit-save").addEventListener("click",manageSaveEdit);
  document.getElementById("edit-cancel").addEventListener("click",()=>{
    editTarget=null;document.getElementById("edit-section").hidden=true;
  });
  document.addEventListener("keydown",e=>{
    if(e.key==="Escape"){
      if(document.getElementById("manage-panel").classList.contains("is-open"))
        manageClose();
      else if(cardEditName)cardCloseAllEdits();
    }
  });
}

/* --------------------------------------------------------------- theme */
const picker=document.getElementById("theme");
function applyTheme(name){
  document.documentElement.setAttribute("data-theme",name);
  const hrRoot=document.getElementById("hr");
  if(hrRoot)hrRoot.setAttribute("data-theme",name);
  picker.value=name;
  try{localStorage.setItem("headroom-theme",name);}catch(e){}
}
picker.addEventListener("change",()=>applyTheme(picker.value));
(function init(){
  const configured=CONFIG&&CONFIG.theme;
  let stored=null;
  try{stored=localStorage.getItem("headroom-theme");}catch(e){}
  const params=new URLSearchParams(location.search);
  const widgetMode=location.pathname.replace(/\/+$/,"")==="/widget"||params.get("compact")==="1";
  document.body.classList.toggle("is-widget",widgetMode);
  applyTheme(params.get("theme")||stored||configured||"midnight");
  if(CONFIG&&CONFIG.title){set("title",CONFIG.title);document.title=CONFIG.title;}
  if(widgetMode){hrInit(params.get("size"));return;}
  document.getElementById("refresh").addEventListener("click",()=>{load(true);histLoad();});
  wireManage();
  histWire();
  insightsWire();
  burnWire();
  load(false);
  histLoad();
  setInterval(()=>load(false),6e4);
  setInterval(()=>histLoad(),6e4);
})();
