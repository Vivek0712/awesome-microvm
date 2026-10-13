export const PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Web Function to MicroVM</title>
<style>
:root{--bg:#f7f8fa;--card:#fff;--ink:#14181f;--muted:#5b6575;--line:#dde2ea;--accent:#2563eb;--ok:#15803d;--bad:#b91c1c;--code:#0f172a;--codeink:#e2e8f0}
@media (prefers-color-scheme:dark){:root{--bg:#0b1220;--card:#121a2b;--ink:#e6ebf3;--muted:#93a1b8;--line:#24304a;--accent:#60a5fa;--ok:#4ade80;--bad:#f87171;--code:#070c16;--codeink:#cbd5e1}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}p{color:var(--muted);margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
label{display:block;font-size:13px;color:var(--muted);margin:8px 0 4px}input,textarea{width:100%;font:13px ui-monospace,Menlo,monospace;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
textarea{min-height:110px}button{margin-top:12px;background:var(--accent);color:#fff;border:0;border-radius:6px;padding:8px 16px;font-size:14px;cursor:pointer}button:disabled{opacity:.5}
pre{background:var(--code);color:var(--codeink);border-radius:8px;padding:12px;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word;max-height:420px;overflow:auto;margin:0}
.row{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--muted);margin-bottom:8px}.row b{color:var(--ink)}.ok{color:var(--ok)}.bad{color:var(--bad)}
footer{font-size:12px;color:var(--muted);text-align:center;margin-top:24px}
</style></head><body><main>
<h1>Lambda Web Function to Lambda MicroVM</h1>
<p>This page is served by a Lambda Web Function. Run sends your steps to it; it launches a MicroVM with an HTTP lease that points back at itself, streams the VM's log to you, and records the result when the VM completes the lease.</p>
<div class="card">
<label for="key">Key</label><input id="key" type="password" autocomplete="off">
<label for="steps">Steps, one shell command per line</label>
<textarea id="steps">uname -a
python3 -c "import os; print('cpus', os.cpu_count())"
for i in 1 2 3; do echo tick $i; sleep 1; done</textarea>
<button id="run">Run in a MicroVM</button>
</div>
<div class="card"><div class="row" id="meta"><span>idle</span></div><pre id="out">The VM's log appears here.</pre></div>
<footer>Developed by Vivek Raja - AWS AI Hero</footer>
</main><script>
const $=(id)=>document.getElementById(id);
try{$("key").value=localStorage.getItem("k")||""}catch(e){}
function meta(parts){$("meta").innerHTML=parts.map(([k,v,c])=>'<span>'+k+' <b class="'+(c||"")+'">'+v+'</b></span>').join("")}
$("run").onclick=async()=>{
  const key=$("key").value.trim();try{localStorage.setItem("k",key)}catch(e){}
  const steps=$("steps").value.split("\\n").map(s=>s.trim()).filter(Boolean);
  $("run").disabled=true;$("out").textContent="";const t0=performance.now();meta([["state","launching"]]);
  const add=(s)=>{$("out").textContent+=s+"\\n";$("out").scrollTop=1e9};
  try{
    const r=await fetch("/jobs?stream=1",{method:"POST",headers:{"content-type":"application/json","x-demo-key":key},body:JSON.stringify({steps})});
    if(!r.ok){const e=await r.json().catch(()=>({}));meta([["error",r.status+" "+(e.error||""),"bad"]]);return}
    const rd=r.body.getReader(),dec=new TextDecoder();let buf="",info={},seen=0;
    for(;;){const {value,done}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;
      while((i=buf.indexOf("\\n\\n"))>=0){const f=buf.slice(0,i);buf=buf.slice(i+2);
        const ev=(/^event: (.*)$/m.exec(f)||[])[1],d=JSON.parse((/^data: (.*)$/m.exec(f)||[])[1]||"null");
        if(ev==="launched"){info=d;meta([["state","running"],["microVM",d.microvmId],["RunMicrovm",d.launchMs+" ms"]])}
        else if(ev==="snapshot"&&d){for(const e of d.log_tail||[]){if(e.seq>seen){seen=e.seq;add(e.msg||"")}}}
        else if(ev==="log"&&d){if(d.seq>seen){seen=d.seq;add(d.msg||"")}}
        else if(ev==="note"){add("# "+d.message)}
        else if(ev==="result"){const ok=d.state==="succeeded";
          meta([["state",d.state,ok?"ok":"bad"],["microVM",d.microvmId||info.microvmId],["heartbeats",d.heartbeats||0],["total",((performance.now()-t0)/1000).toFixed(1)+" s"]]);
          add("\\n# result\\n"+JSON.stringify(d.outcome,null,2))}}}
  }catch(e){meta([["error",e.message,"bad"]])}finally{$("run").disabled=false}
};
</script></body></html>`;
