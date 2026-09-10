"use strict";
/* ======================= trạng thái ======================= */
let ST={queue:[],selected:null,running:false,progress:{}};
let PR=null;            // dự án hiện tại
let JID=null;
let TAB="sub";
let ACT=null;           // vùng đang chọn {kind,index}
let CFG={translation:{},tts:{}};
let IDEAS={items:[],detail:null,selectedId:"",search:"",provider:"browser",server:{},loaded:false,
  tab:localStorage.getItem("advn_ideas_tab")==="library"?"library":"collect",
  catalogLoaded:false,sources:[],chineseKeywords:[],sourceResults:[],
  onlineKeyword:"",sourceGroup:"zhihu",keywordIndex:-1,
  usageFilter:localStorage.getItem("advn_idea_usage_filter")||"unused"};
let MANUAL={text:"",name:"",engine:"edge",voice:"",pitch:"+0Hz",rate:"+0%",
             audio_path:"",audio_duration:0,output_path:"",status:"Sẵn sàng",error:"",
             image_status:"",image_ready:0,image_total:0,
             nhac_bai:"",nhac_db:-38,nhac_duck:true,nhac_ten:"",
             anh:"",slide_kieu:"chuyen_dong",writer_title:"",
             script_path:"",script_title:"",script_words:0,
             content_idea_id:"",content_outline:"",rewrite_brief:"",
             youtube_description:"",youtube_tags:[],calendar_path:""};
let STORY_MEDIA_MODE = localStorage.getItem("advn_story_media_mode") || "image";
let NHAC_LIST=[];
let saveTimer=null;
let configTimer=null;
let SETTINGS_TAB="api";
let _lastRev=-1;
let _manualRev=-1;
let _videoToolsRev=-1;
let _ideasRev=-1;
let _ideasWorking=false;
let _ideasLastActive="";
let _manualWorking=false;
let _cancelPending=false;
let _lastScriptPath="";
let _queuePrev={};
const V=()=>document.getElementById("video");

const COLORS=["#FFFFFF","#FFD700","#7DD3FC","#FDA4AF","#86EFAC","#C4B5FD"];
const GRID=[["top-left","top-center","top-right"],["mid-left","mid-center","mid-right"],
            ["bottom-left","bottom-center","bottom-right"]];

/* ======================= tiện ích ======================= */
function toast(msg,kind){
  const d=document.createElement("div");
  d.className="tst "+(kind||"");
  d.textContent=msg;
  document.getElementById("toast").appendChild(d);
  setTimeout(()=>d.remove(),4200);
}
function showEmpty(title,msg){
  const e=document.getElementById("empty");
  e.innerHTML="";
  const b=document.createElement("b");
  b.textContent=title;
  e.appendChild(b);
  e.appendChild(document.createTextNode(msg||""));
  e.style.display="";
  document.getElementById("vwrap").style.display="none";
}
function fmt(s){
  s=Math.max(0,s||0);
  const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=Math.floor(s%60);
  return String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(x).padStart(2,"0");
}
function parseTime(v){
  if(v==null) return null;
  const raw=String(v).trim().toLowerCase().replace(",",".");
  if(!raw) return null;
  const clean=raw.endsWith("s")?raw.slice(0,-1):raw;
  if(clean.includes(":")){
    const parts=clean.split(":").map(x=>Number(x.trim()));
    if(parts.some(x=>!Number.isFinite(x))) return null;
    return parts.reduce((acc,x)=>acc*60+x,0);
  }
  const n=Number(clean);
  return Number.isFinite(n)?n:null;
}
function dur(){ return V().duration||(PR&&PR.duration)||0; }
function trimBounds(){
  const d=Math.max(.1,dur()||0);
  const op=(PR&&PR.options)||{};
  let s=Math.max(0,Number(op.trim_start)||0);
  let e=(op.trim_end===""||op.trim_end==null)?d:Number(op.trim_end);
  if(!Number.isFinite(e)) e=d;
  s=Math.min(Math.max(0,s),Math.max(0,d-.1));
  e=Math.min(Math.max(s+.1,e),d);
  return {on:!!op.trim_enabled,start:s,end:e,duration:Math.max(.1,e-s),full:d};
}
function setTrimEnabled(on){
  if(!PR) return;
  const b=trimBounds();
  PR.options.trim_enabled=!!on;
  PR.options.trim_start=Math.round(b.start*10)/10;
  PR.options.trim_end=Math.round(b.end*10)/10;
  save(); drawTracks(true); renderPanel();
}
function setTrimPoint(k,v,quiet){
  if(!PR) return;
  const d=Math.max(.1,dur()||PR.duration||0), b=trimBounds();
  let s=b.start, e=b.end, n=parseTime(v);
  if(n==null) n=k==="end"?d:0;
  if(k==="start") s=Math.min(Math.max(0,n),Math.max(0,e-.1));
  else e=Math.min(Math.max(s+.1,n),d);
  PR.options.trim_enabled=true;
  PR.options.trim_start=Math.round(s*10)/10;
  PR.options.trim_end=Math.round(e*10)/10;
  if(!quiet){ save(); drawTracks(true); renderPanel(); }
}
function setTrimFromNow(k){ setTrimPoint(k,V().currentTime||0); }
function trimFull(){
  if(!PR) return;
  PR.options.trim_enabled=false;
  PR.options.trim_start=0;
  PR.options.trim_end=null;
  save(); drawTracks(true); renderPanel();
}
function seekTrim(k){
  const b=trimBounds();
  V().currentTime=k==="end"?Math.max(0,b.end-.15):b.start;
  tick();
}
async function api(path,body){
  const o=body?{method:"POST",headers:{"Content-Type":"application/json"},
                body:JSON.stringify(body)}:{};
  const r=await fetch(path,o);
  let j={}; try{j=await r.json()}catch(e){}
  if(j&&j.error) throw new Error(j.error);
  return j;
}
async function loadConfig(){
  try{
    const r=await api("/api/config");
    CFG.translation=r.translation||{};
    CFG.tts=r.tts||{};
    const t=CFG.tts;
    MANUAL.engine=t.engine||"edge";
    MANUAL.voice=t.voice||"";
    MANUAL.pitch=t.pitch||"+0Hz";
    MANUAL.rate=t.rate||"+0%";
  }catch(e){
    toast("Không đọc được config.yaml: "+e.message,"warn");
  }
}
async function saveTrCfg(showToast){
  clearTimeout(configTimer);
  try{
    const r=await api("/api/config",{translation:CFG.translation||{}});
    CFG.translation=r.translation||CFG.translation||{};
    if(showToast) toast("Đã lưu cấu hình dịch","ok");
    return true;
  }catch(e){
    toast("Lỗi lưu cấu hình dịch: "+e.message,"err");
    return false;
  }
}
function setTrCfg(k,v,rerender){
  CFG.translation=CFG.translation||{};
  CFG.translation[k]=v;
  clearTimeout(configTimer);
  configTimer=setTimeout(()=>saveTrCfg(false),250);
  if(rerender) renderPanel();
}
async function testTrCfg(){
  clearTimeout(configTimer);
  try{
    const r=await api("/api/test_translation",{translation:CFG.translation||{}});
    toast(r.message||"API hoạt động","ok");
  }catch(e){
    toast("Test API lỗi: "+e.message,"err");
  }
}
function save(){
  if(!PR||!JID) return;
  clearTimeout(saveTimer);
  saveTimer=setTimeout(()=>{
    api("/api/project",{id:JID,regions:PR.regions,logo:PR.logo,
        sub_style:PR.sub_style,segments:PR.segments,options:PR.options}).catch(()=>{});
  },400);
}
async function saveNow(){
  if(!PR||!JID) return;
  clearTimeout(saveTimer);
  await api("/api/project",{id:JID,regions:PR.regions,logo:PR.logo,
      sub_style:PR.sub_style,segments:PR.segments,options:PR.options});
}

/* ======================= app desktop ======================= */
const DESK=()=>!!(window.pywebview&&window.pywebview.api);
function winCmd(c){
  if(!DESK()) return;
  if(c==="min") pywebview.api.win_minimize();
  else if(c==="max") pywebview.api.win_maximize();
  else pywebview.api.win_close();
}
async function openOut(p){
  if(DESK()) await pywebview.api.open_folder(p);
  else toast(p);
}

/* ======================= modal cài đặt ======================= */
function openSettings(tab){
  SETTINGS_TAB=tab||SETTINGS_TAB||"api";
  const modal=document.getElementById("settingsModal");
  if(!modal) return;
  renderSettingsModal();
  modal.classList.add("open");
  modal.setAttribute("aria-hidden","false");
}
function closeSettings(){
  const modal=document.getElementById("settingsModal");
  if(!modal) return;
  modal.classList.remove("open");
  modal.setAttribute("aria-hidden","true");
}
function settingsBackdrop(e){ if(e.target&&e.target.id==="settingsModal") closeSettings(); }
function setSettingsTab(tab){
  SETTINGS_TAB=["api","voice","gpu"].includes(tab)?tab:"api";
  renderSettingsModal();
}
function settingsSetTr(k,v,redraw){
  CFG.translation=CFG.translation||{};
  CFG.translation[k]=v;
  clearTimeout(configTimer);
  configTimer=setTimeout(()=>saveTrCfg(false),250);
  if(redraw) renderSettingsModal();
}
function settingsSetProjectOpt(k,v,redraw){
  if(!PR||!PR.options) return toast("Hãy chọn một video trước khi đổi thiết lập dự án","warn");
  PR.options[k]=v; save();
  if(redraw) renderSettingsModal();
}
function settingsSetEngine(v){
  if(!PR) return toast("Hãy chọn một video trước khi đổi bộ giọng","warn");
  setEngine(v); renderSettingsModal();
}
function renderSettingsModal(){
  const body=document.getElementById("settingsBody"); if(!body) return;
  document.querySelectorAll("[data-settings-tab]").forEach(b=>
    b.classList.toggle("on",b.dataset.settingsTab===SETTINGS_TAB));
  const tr=CFG.translation||{}, op=(PR&&PR.options)||{};
  const disabled=PR?"":"disabled";
  if(SETTINGS_TAB==="api"){
    const provider=tr.provider||"browser";
    const providers={
      nvidia:["nvidia_api_key","nvidia_model","minimaxai/minimax-m3","NVIDIA API Key"],
      gemini:["gemini_api_key","gemini_model","gemini-3.6-flash","Google Gemini API Key"],
      inferx:["inferx_api_key","inferx_model","deepseek-v4-flash","InferX API Key"],
      tokenrouter:["tokenrouter_api_key","tokenrouter_model","moonshotai/kimi-k3-free","TokenRouter API Key"],
      tokenrouter_gemini:["tokenrouter_gemini_api_key","tokenrouter_gemini_model","google/gemini-3.6-flash","TokenRouter Gemini Key"],
      zenmux:["zenmux_api_key","zenmux_model","z-ai/glm-5.3-free","ZenMux API Key"]
    };
    const pf=providers[provider];
    body.innerHTML=`<div class="settings-group">
      ${fld("Dịch vụ dịch & AI",`<select onchange="settingsSetTr('provider',this.value,true)">
        <option value="browser" ${provider==="browser"?"selected":""}>Gemini web · profile đã đăng nhập</option>
        <option value="gemini" ${provider==="gemini"?"selected":""}>Google Gemini API</option>
        <option value="nvidia" ${provider==="nvidia"?"selected":""}>NVIDIA NIM · MiniMax M3</option>
        <option value="zenmux" ${provider==="zenmux"?"selected":""}>ZenMux · GLM-5.3 free</option>
        <option value="inferx" ${provider==="inferx"?"selected":""}>InferX · DeepSeek V4 Flash</option>
        <option value="tokenrouter_gemini" ${provider==="tokenrouter_gemini"?"selected":""}>TokenRouter · Gemini</option>
        <option value="tokenrouter" ${provider==="tokenrouter"?"selected":""}>TokenRouter · Kimi</option>
      </select>`)}
      ${pf?`<div class="grid2">
        ${fld(pf[3],`<input type="password" autocomplete="off" value="${esc(tr[pf[0]]||"")}"
          placeholder="Nhập API key…" onchange="settingsSetTr('${pf[0]}',this.value)">`)}
        ${fld("Model",`<input value="${esc(tr[pf[1]]||pf[2])}"
          onchange="settingsSetTr('${pf[1]}',this.value)">`)}</div>`:
        `<div class="ready-line"><span>✓</span><div><b>Sẵn sàng</b><small>Dùng profile Gemini đã đăng nhập, không cần API key.</small></div></div>`}
      <div class="ai-note"><span>✣</span><p><b>Tự động tối ưu văn phong lồng tiếng:</b> hệ thống giữ đúng nghĩa gốc, Việt hoá tự nhiên và tối ưu nhịp đọc.</p></div>
      <div class="rowbtns"><button class="btn" onclick="testTrCfg()">Kiểm tra kết nối API</button>
        <button class="btn" onclick="setTab('tr');closeSettings()">Mở thiết lập dịch nâng cao</button></div>
    </div>`;
  }else if(SETTINGS_TAB==="voice"){
    const origDb=Number(op.keep_original_db??-30), engine=op.engine||CFG.tts.engine||"edge";
    body.innerHTML=`${PR?"":`<div class="settings-warning">Chưa chọn video — các mục theo dự án đang tạm khoá.</div>`}
      <div class="settings-group">
      ${fld("Engine Giọng Đọc Mặc Định",`<select ${disabled} onchange="settingsSetEngine(this.value)">
        <option value="edge" ${engine==="edge"?"selected":""}>Edge TTS (Online, miễn phí)</option>
        <option value="capcut" ${engine==="capcut"?"selected":""}>CapCut TTS (Online, giọng thịnh hành)</option>
        <option value="vieneu" ${engine==="vieneu"?"selected":""}>VieNeu TTS (Offline)</option>
      </select>`)}
      <div class="settings-range"><label>Mức Giảm Âm Thanh Nền Gốc <b>${origDb} dB</b></label>
        <input ${disabled} type="range" min="-60" max="0" value="${origDb}"
          oninput="if(PR){PR.options.keep_original_db=+this.value;PR.options.keep_original_muted=false;save();this.previousElementSibling.querySelector('b').textContent=this.value+' dB'}">
        <div><span>-60 dB (gần như tắt)</span><span>-30 dB (khuyến nghị)</span><span>0 dB (giữ nguyên)</span></div></div>
      <div class="settings-checks">
        <label><input ${disabled} type="checkbox" ${Number(op.max_speed||1.6)>1?"checked":""}
          onchange="settingsSetProjectOpt('max_speed',this.checked?1.6:1,true)"> Tự động chống đè âm khi câu dịch dài</label>
        <label><input ${disabled} type="checkbox" ${Number(op.min_gap??.08)>0?"checked":""}
          onchange="settingsSetProjectOpt('min_gap',this.checked?0.08:0,true)"> Chèn khoảng nghỉ giữa các câu hội thoại</label>
      </div></div>`;
  }else{
    const crf=Number(op.crf||20);
    body.innerHTML=`${PR?"":`<div class="settings-warning">Chưa chọn video — các mục theo dự án đang tạm khoá.</div>`}
      <div class="settings-group">
      <label class="gpu-toggle"><input ${disabled} type="checkbox" ${op.use_gpu!==false?"checked":""}
        onchange="settingsSetProjectOpt('use_gpu',this.checked,true)"><span><b>Bật tăng tốc phần cứng GPU NVENC (NVIDIA CUDA)</b>
        <small>Tăng tốc render video nhiều lần so với CPU khi bộ lọc hình ảnh được bật.</small></span></label>
      <div class="settings-range"><label>Chất lượng Video CRF (Giá trị thấp = Nét hơn) <b>CRF ${crf}</b></label>
        <input ${disabled} type="range" min="14" max="30" value="${crf}"
          oninput="if(PR){PR.options.crf=+this.value;save();this.previousElementSibling.querySelector('b').textContent='CRF '+this.value}"></div>
      <div class="export-path"><span>Thư mục xuất video của dự án</span><b>${esc((PR&&PR.output_dir)||"Được tạo cạnh video nguồn")}</b></div>
      <button class="btn" ${disabled} onclick="setTab('export');closeSettings()">Mở toàn bộ tuỳ chọn xuất video</button>
      </div>`;
  }
}
async function saveSettingsModal(){
  try{
    const ok=await saveTrCfg(false);
    if(PR) await saveNow();
    if(ok){ toast("Đã lưu cấu hình","ok"); closeSettings(); }
  }catch(e){ toast("Không lưu được cấu hình: "+e.message,"err"); }
}

/* ======================= hàng đợi ======================= */
async function pickFile(){
  let paths=[];
  if(DESK()){
    const r=await pywebview.api.pick_video();
    if(!r||r.error){ if(r&&r.error) toast(r.error,"err"); return; }
    paths=Array.isArray(r)?r:[r];
  }else{
    const p=prompt("Dán ĐƯỜNG DẪN video trên máy:\n(ví dụ E:\\Video\\phim.mp4)");
    if(!p) return; paths=[p];
  }
  if(!paths.length) return;
  let last=null;
  for(const p of paths){
    try{ const r=await api("/api/queue/add",{path:p}); last=r.id; }
    catch(e){ toast(e.message,"err"); }
  }
  if(last!=null) await selectJob(last);
  refresh();
  if(paths.length>1) toast(`Đã thêm ${paths.length} video vào hàng đợi`,"ok");
}
async function addUrl(){
  const raw = document.getElementById("url").value.trim();
  if(!raw) return;
  // Split by newlines to support multiple URLs
  const lines = raw.split(/[\r\n]+/).map(s=>s.trim()).filter(Boolean);
  if(lines.length === 0) return;

  if(lines.length === 1){
    // Single URL - use original endpoint
    toast("Đã thêm vào hàng đợi tải…");
    try{
      const r = await api("/api/queue/add", {url: lines[0]});
      document.getElementById("url").value = "";
      if(r.async){
        await api("/api/queue/select", {id: r.id});
        JID=null; PR=null; _lastRev=-1;
        showEmpty("Đang tải video", "Khi tải xong, video sẽ tự hiện ở đây.");
        renderPanel();
        await refresh();
        toast("Đang tải trong nền…", "ok");
      } else {
        await selectJob(r.id); refresh(); toast("Tải xong", "ok");
      }
    } catch(e){ toast(e.message, "err"); }
  } else {
    // Multiple URLs - use batch endpoint
    toast(`Đang thêm ${lines.length} link vào hàng đợi…`);
    try{
      const r = await api("/api/queue/add_batch", {urls: lines});
      document.getElementById("url").value = "";
      const ok = (r.results||[]).filter(x=>x.ok).length;
      const fail = (r.results||[]).filter(x=>x.error).length;
      await refresh();
      if(fail) toast(`Đã thêm ${ok} link, ${fail} lỗi`, "warn");
      else toast(`Đã thêm ${ok} link vào hàng đợi tải`, "ok");
    } catch(e){ toast(e.message, "err"); }
  }
}
async function delJob(id,ev){
  ev.stopPropagation();
  await api("/api/queue/remove",{id}); if(JID===id){JID=null;PR=null;} refresh();
}
async function clearDone(){
  for(const j of ST.queue.filter(x=>x.status==="xong"))
    await api("/api/queue/remove",{id:j.id});
  refresh();
}
async function selectJob(id){
  const queued=(ST.queue||[]).find(x=>x.id===id);
  if(queued&&(!queued.path||queued.status==="đang tải")){
    JID=null; PR=null; _lastRev=-1;
    await api("/api/queue/select",{id});
    showEmpty("Đang tải video", queued.note||"Vui lòng chờ file tải xong.");
    renderPanel();
    refresh();
    return;
  }
  JID=id; await api("/api/queue/select",{id});
  PR=await api("/api/project?id="+id);
  _lastRev=-1; _ciHint=0; _trkSig="";
  const v=V();
  v.src="/api/video?id="+id;
  document.getElementById("vwrap").style.display="";
  document.getElementById("empty").style.display="none";
  v.onloadedmetadata=()=>{applyZoom();draw();document.getElementById("tdur").textContent=fmt(v.duration)};
  renderPanel(); draw(); loadVoices(false);
}
function saveProject(){ save(); toast("Đã lưu cấu hình dự án","ok"); }

/* ======================= vùng phủ (3 lớp) ======================= */
function addRegion(type){
  if(!PR) return toast("Chưa chọn video","warn");
  const w=Math.round(PR.w*0.5), h=Math.round(PR.h*0.13);
  PR.regions.push({x:Math.round((PR.w-w)/2),
                   y:type==="delogo"?Math.round(PR.h*0.05):Math.round(PR.h*0.8),
                   w,h,type,strength:20,start:null,end:null});
  ACT={kind:"rgn",i:PR.regions.length-1}; save(); draw();
}
function clearRegions(){ if(!PR)return; PR.regions=[]; ACT=null; save(); draw(); }
function addLogoBox(){
  if(!PR) return toast("Chưa chọn video","warn");
  PR.logo={x:Math.round(PR.w*0.03),y:Math.round(PR.h*0.04),
           w:Math.round(PR.w*0.15),h:Math.round(PR.h*0.09),opacity:1,path:""};
  ACT={kind:"logo"}; setTab("logo"); save(); draw();
}
async function autoDetect(){
  if(!JID) return toast("Chưa chọn video","warn");
  toast("Đang dò vùng phụ đề gốc…");
  try{
    const r=await api("/api/detect_sub",{id:JID});
    PR=await api("/api/project?id="+JID);
    ACT={kind:"rgn",i:PR.regions.length-1};
    draw(); renderPanel();
    toast(`Đã dò: ${r.region.w}×${r.region.h} tại (${r.region.x},${r.region.y})`,"ok");
  }catch(e){ toast(e.message,"err"); }
}

/* ---------- vẽ các lớp lên video ---------- */
function scale(){
  const v=V();
  if(!v.videoWidth) return 1;
  return v.clientWidth/v.videoWidth;
}
function draw(){
  const L=document.getElementById("layers");
  if(!PR||!V().videoWidth){L.innerHTML="";return;}
  const k=scale(), prev=document.getElementById("prev").checked;
  L.innerHTML="";

  (PR.regions||[]).forEach((r,i)=>{
    const isD=r.type==="delogo";
    const el=mkBox(r,k,isD?"delogo":"blur",String(i+1),
                   isD?"Xoá logo gốc":"Vùng làm mờ (lớp 2)",()=>{
      PR.regions.splice(i,1); ACT=null; save(); draw();
    });
    if(prev&&!isD){
      if(regionActiveAt(r,V().currentTime||0))
        el.style.backdropFilter=`blur(${Math.max(2,(r.strength||20)*k*0.55)}px)`;
      el.style.background="rgba(0,0,0,.04)";
    }
    el.dataset.rgn=i;
    if(ACT&&ACT.kind==="rgn"&&ACT.i===i) el.classList.add("act");
    el.onpointerdown=e=>startDrag(e,el,r,k,()=>{ACT={kind:"rgn",i};renderPanel();});
    L.appendChild(el);
  });

  if(PR.logo){
    const el=mkBox(PR.logo,k,"logo","L","Logo của bạn",()=>{PR.logo=null;save();draw();});
    if(ACT&&ACT.kind==="logo") el.classList.add("act");
    el.onpointerdown=e=>startDrag(e,el,PR.logo,k,()=>{ACT={kind:"logo"};renderPanel();});
    L.appendChild(el);
  }

  const st=PR.sub_style||{}, box=st.box;
  if(box){
    const el=mkBox(box,k,"sub","3","phụ đề Việt · kéo để di chuyển",null);
    if(ACT&&ACT.kind==="sub") el.classList.add("act");
    if(prev){
      const t=document.createElement("div");
      t.id="subtext";
      t.textContent=currentLineText()||"Phụ đề tiếng Việt sẽ hiện ở đây";
      t.style.font=`${st.bold?"700":"400"} ${Math.max(9,(st.size||30)*k)}px "${st.font||"Be Vietnam Pro"}",sans-serif`;
      t.style.color=st.color||"#fff";
      t.style.whiteSpace=st.single_line===false?"normal":"nowrap";
      t.style.overflow="visible";
      t.style.left="0";
      t.style.right="0";
      t.style.width="100%";
      t.style.maxWidth="100%";
      t.style.margin="0 auto";
      const ow=Math.max(1,(st.outline||2)*k);
      t.style.textShadow=`0 0 ${ow}px ${st.outline_color||"#000"},`+
        [[-1,-1],[1,-1],[-1,1],[1,1]].map(([a,b])=>`${a*ow}px ${b*ow}px 0 ${st.outline_color||"#000"}`).join(",");
      const al=st.align||"mid-center";
      t.style.textAlign=al.includes("left")?"left":al.includes("right")?"right":"center";
      if(al.startsWith("top")){t.style.top="0";t.style.bottom="auto";}
      else if(al.startsWith("mid")){t.style.top="50%";t.style.bottom="auto";t.style.transform="translateY(-50%)";}
      el.appendChild(t);
    }
    el.onpointerdown=e=>startDrag(e,el,box,k,()=>{ACT={kind:"sub"};renderPanel();});
    L.appendChild(el);
  }
  drawTracks();
  const a=activeRegion();
  document.getElementById("rgninfo").textContent = a
    ? `Vùng ${a.type==="delogo"?"xoá logo":a.type==="sub"?"phụ đề Việt":"làm mờ"} · ${Math.round(a.w)}×${Math.round(a.h)} @ (${Math.round(a.x)},${Math.round(a.y)})`
    : "Chưa chọn vùng nào";
}

function regionActiveAt(r,t){
  const s=(r.start===""||r.start==null)?null:Number(r.start);
  const e=(r.end===""||r.end==null)?null:Number(r.end);
  if(Number.isFinite(s)&&t<s) return false;
  if(Number.isFinite(e)&&t>e) return false;
  return true;
}
function updateRegionPreview(){
  if(!PR||!document.getElementById("prev").checked) return;
  const k=scale(), t=V().currentTime||0;
  document.querySelectorAll(".box.blur[data-rgn]").forEach(el=>{
    const r=PR.regions[+el.dataset.rgn]; if(!r) return;
    el.style.backdropFilter=regionActiveAt(r,t)
      ? `blur(${Math.max(2,(r.strength||20)*k*0.55)}px)` : "";
  });
}
function mkBox(r,k,cls,num,label,onRemove){
  const el=document.createElement("div");
  el.className="box "+cls;
  el.style.left=(r.x*k)+"px"; el.style.top=(r.y*k)+"px";
  el.style.width=(r.w*k)+"px"; el.style.height=(r.h*k)+"px";
  const tag=document.createElement("div");
  tag.className="tag"; tag.textContent=num+(label?" · "+label:"");
  el.appendChild(tag);
  if(onRemove){
    const x=document.createElement("div");
    x.className="rm"; x.textContent="×";
    x.onpointerdown=e=>{e.stopPropagation();onRemove();};
    el.appendChild(x);
  }
  ["nw","ne","sw","se","n","s","w","e"].forEach(p=>{
    const h=document.createElement("div"); h.className="h "+p; h.dataset.p=p;
    el.appendChild(h);
  });
  return el;
}
function activeRegion(){
  if(!PR||!ACT) return null;
  if(ACT.kind==="rgn") return PR.regions[ACT.i];
  if(ACT.kind==="logo") return PR.logo&&{...PR.logo,type:"logo"};
  if(ACT.kind==="sub") return PR.sub_style.box&&{...PR.sub_style.box,type:"sub"};
  return null;
}
/* ---------- KÉO & ĐỔI KÍCH CỠ (mượt) ----------
   - pointer capture: chuột đi ra ngoài khung vẫn kéo tiếp, không rớt.
   - gộp theo khung hình (rAF): chuột bắn ra ~120 sự kiện/giây, chỉ vẽ 1 lần/khung.
   - hít vào mép và tâm khung hình, cùng các vùng khác; giữ Alt để tắt hít.
   - Shift khi đổi cỡ = giữ nguyên tỉ lệ.
   - chỉ ghi lại DOM khi thả tay, lúc kéo không dựng lại gì. */
const SNAP=9;          // ngưỡng hít, tính bằng pixel màn hình
let _isDragging=false;
let _guides=null;

/* Toán học của kéo/đổi cỡ - HÀM THUẦN nên test được, không đụng DOM.
   Trả {x,y,w,h,gx,gy} với gx/gy là mốc đang hít (null nếu không hít).
   MINW/MINH: không cho vùng nhỏ quá mức bấm được. */
const MINW=24, MINH=18;
function computeDrag({o,handle,ratio,maxW,maxH,xs,ys,dx,dy,tol,noSnap,keepRatio}){
  let x=o.x,y=o.y,w=o.w,h=o.h, gx=null, gy=null;

  if(!handle){ x=o.x+dx; y=o.y+dy; }
  else{
    if(handle.includes("w")){ x=o.x+dx; w=o.w-dx; }
    if(handle.includes("e")){ w=o.w+dx; }
    if(handle.includes("n")){ y=o.y+dy; h=o.h-dy; }
    if(handle.includes("s")){ h=o.h+dy; }
    if(keepRatio&&handle.length===2){          // giữ tỉ lệ khi kéo ở góc
      h=w/ratio;
      if(handle.includes("n")) y=o.y+o.h-h;
    }
  }
  w=Math.max(MINW,w); h=Math.max(MINH,h);

  if(!noSnap){
    const trySnap=(vals,cands)=>{              // trả [lệch, mốc] gần nhất
      let best=null;
      for(const v of vals) for(const c of cands){
        const d=c-v;
        if(Math.abs(d)<tol&&(!best||Math.abs(d)<Math.abs(best[0]))) best=[d,c];
      }
      return best;
    };
    if(!handle){
      const bx=trySnap([x,x+w/2,x+w],xs), by=trySnap([y,y+h/2,y+h],ys);
      if(bx){ x+=bx[0]; gx=bx[1]; }
      if(by){ y+=by[0]; gy=by[1]; }
    }else{
      if(handle.includes("w")){const b=trySnap([x],xs); if(b){x+=b[0];w-=b[0];gx=b[1];}}
      if(handle.includes("e")){const b=trySnap([x+w],xs); if(b){w+=b[0];gx=b[1];}}
      if(handle.includes("n")){const b=trySnap([y],ys); if(b){y+=b[0];h-=b[0];gy=b[1];}}
      if(handle.includes("s")){const b=trySnap([y+h],ys); if(b){h+=b[0];gy=b[1];}}
    }
  }

  // ép nằm gọn trong khung hình
  w=Math.max(MINW,Math.min(w,maxW)); h=Math.max(MINH,Math.min(h,maxH));
  x=Math.max(0,Math.min(x,maxW-w));  y=Math.max(0,Math.min(y,maxH-h));
  return {x:Math.round(x),y:Math.round(y),w:Math.round(w),h:Math.round(h),gx,gy};
}
if(typeof module!=="undefined") module.exports={computeDrag};

function showGuides(vx,vy,k){
  if(!_guides){
    _guides=document.createElement("div");
    _guides.style.cssText="position:absolute;inset:0;pointer-events:none;z-index:5";
    document.getElementById("layers").appendChild(_guides);
  }
  _guides.innerHTML="";
  const mk=(css)=>{const d=document.createElement("div");
    d.style.cssText="position:absolute;background:#22d3ee;opacity:.85;"+css;
    _guides.appendChild(d);};
  if(vx!=null) mk(`left:${vx*k}px;top:0;bottom:0;width:1px`);
  if(vy!=null) mk(`top:${vy*k}px;left:0;right:0;height:1px`);
}
function hideGuides(){ if(_guides){_guides.remove(); _guides=null;} }

function startDrag(e,el,r,k,onPick){
  if(e.button!==0) return;
  e.preventDefault(); e.stopPropagation();
  if(onPick) onPick();

  const handle=(e.target.dataset&&e.target.dataset.p)||null;
  const sx=e.clientX, sy=e.clientY;
  const o={x:r.x,y:r.y,w:r.w,h:r.h};
  const ratio=o.w/Math.max(1,o.h);
  const maxW=PR.w, maxH=PR.h;
  const info=document.getElementById("rgninfo");

  // các mốc để hít vào (mép + tâm khung hình + mép các vùng khác)
  const xs=[0,maxW/2,maxW], ys=[0,maxH/2,maxH];
  for(const q of (PR.regions||[])) if(q!==r){xs.push(q.x,q.x+q.w);ys.push(q.y,q.y+q.h);}
  const sb=PR.sub_style&&PR.sub_style.box;
  if(sb&&sb!==r){xs.push(sb.x,sb.x+sb.w);ys.push(sb.y,sb.y+sb.h);}

  el.style.willChange="left,top,width,height";
  el.classList.add("act");
  el.classList.add("dragging"); _isDragging=true;
  document.body.style.cursor=handle?getComputedStyle(e.target).cursor:"move";
  try{ el.setPointerCapture(e.pointerId); }catch(_){}

  let raf=null, last=null, gx=null, gy=null;

  const apply=()=>{
    raf=null;
    if(!last) return;
    const res=computeDrag({
      o, handle, ratio, maxW, maxH, xs, ys,
      dx:(last.clientX-sx)/k, dy:(last.clientY-sy)/k,
      tol:SNAP/k, noSnap:last.altKey, keepRatio:last.shiftKey,
    });
    gx=res.gx; gy=res.gy;
    r.x=res.x; r.y=res.y; r.w=res.w; r.h=res.h;
    el.style.left=(r.x*k)+"px"; el.style.top=(r.y*k)+"px";
    el.style.width=(r.w*k)+"px"; el.style.height=(r.h*k)+"px";
    if(gx!=null||gy!=null) showGuides(gx,gy,k); else hideGuides();
    info.textContent=`${Math.round(r.w)}×${Math.round(r.h)} @ (${Math.round(r.x)},${Math.round(r.y)})`
      +(gx!=null||gy!=null?"  · hít mốc":"")+"   [Alt: tắt hít · Shift: giữ tỉ lệ]";
  };

  const move=ev=>{ last=ev; if(raf===null) raf=requestAnimationFrame(apply); };
  const up=ev=>{
    window.removeEventListener("pointermove",move);
    window.removeEventListener("pointerup",up);
    window.removeEventListener("pointercancel",up);
    if(raf!==null){ cancelAnimationFrame(raf); apply(); }
    try{ el.releasePointerCapture(ev.pointerId); }catch(_){}
    el.style.willChange="";
    el.classList.remove("dragging"); _isDragging=false;
    document.body.style.cursor="";
    hideGuides();
    save(); draw(); renderPanel();
  };
  window.addEventListener("pointermove",move,{passive:true});
  window.addEventListener("pointerup",up);
  window.addEventListener("pointercancel",up);
}
function applyZoom(){
  const z=document.getElementById("zoom").value/100;
  const st=document.getElementById("stage");
  V().style.maxHeight=(st.clientHeight*z-4)+"px";
  V().style.maxWidth=(st.clientWidth*z-4)+"px";
  setTimeout(draw,30);
}

/* ======================= timeline ======================= */
function segs(){ return (PR&&PR.segments)||[]; }
/* Tìm nhị phân + nhớ vị trí lần trước: gọi mỗi khung hình nên không được quét
   tuyến tính qua hàng nghìn dòng. */
let _ciHint=0;
function curIndex(){
  const s=segs(); if(!s.length) return -1;
  const t=V().currentTime||0;
  const hit=i=>i>=0&&i<s.length&&t>=s[i].start-0.05&&t<=s[i].end+0.05;
  if(hit(_ciHint)) return _ciHint;
  if(hit(_ciHint+1)){ _ciHint++; return _ciHint; }
  let lo=0, hi=s.length-1, best=-1;
  while(lo<=hi){
    const m=(lo+hi)>>1;
    if(s[m].start-0.05<=t){ best=m; lo=m+1; } else hi=m-1;
  }
  if(hit(best)){ _ciHint=best; return best; }
  return -1;
}
function currentLineText(){
  const i=curIndex(); if(i<0) return "";
  return segs()[i].vi||segs()[i].src||"";
}
/* Dựng lại thanh thời gian là việc NẶNG (phim 3 tiếng có hàng nghìn dòng).
   Chỉ dựng khi dữ liệu thật sự đổi; còn kim thời gian thì di riêng mỗi khung
   hình. Trước đây hàm này chạy 4 lần/giây và tạo lại toàn bộ DOM -> treo app. */
let _trkSig="", _needles=[], _chips=[], _curChip=null;
const MAX_CHIPS=400;      // phim dài chỉ vẽ các dòng quanh chỗ đang xem

function trackSignature(){
  const s=segs(), d=Math.round(V().duration||(PR&&PR.duration)||0);
  const win=Math.floor((V().currentTime||0)/60);   // đổi cửa sổ mỗi phút
  const b=trimBounds();
  return `${s.length}|${(PR&&PR.regions||[]).length}|${d}|${
    s.length>MAX_CHIPS?win:0}|${b.on?1:0}|${Math.round(b.start*10)}|${Math.round(b.end*10)}`;
}
function buildTracks(){
  const d=(V().duration||PR&&PR.duration||1);
  const cut=document.getElementById("trkCut"),
        sub=document.getElementById("trkSub"), tts=document.getElementById("trkTts"),
        rgn=document.getElementById("trkRgn");
  if(cut) cut.innerHTML="";
  sub.innerHTML=""; tts.innerHTML=""; rgn.innerHTML="";
  _chips=[]; _curChip=null;

  if(cut){
    const b=trimBounds();
    const c=document.createElement("div");
    c.className="tchip cut"+(b.on?"":" off");
    c.style.left=(b.start/d*100)+"%";
    c.style.width=Math.max(.4,(b.end-b.start)/d*100)+"%";
    c.textContent=b.on?`${fmt(b.start)} → ${fmt(b.end)}`:"Toàn bộ video";
    c.title=b.on?`Giữ ${fmt(b.duration)} từ ${fmt(b.start)} đến ${fmt(b.end)}`:"Chưa bật cắt";
    ["a","b"].forEach((side,idx)=>{
      const h=document.createElement("div");
      h.className="trimH "+side;
      h.title=idx?"Kéo điểm cuối":"Kéo điểm đầu";
      h.onpointerdown=e=>startTrimDrag(e,idx?"end":"start");
      c.appendChild(h);
    });
    cut.onclick=e=>{
      if(e.target.classList.contains("trimH")) return;
      const r=cut.getBoundingClientRect();
      V().currentTime=Math.max(0,Math.min(d,(e.clientX-r.left)/r.width*d)); tick();
    };
    cut.appendChild(c);
  }

  const all=segs();
  let from=0, to=all.length;
  if(all.length>MAX_CHIPS){          // chỉ vẽ quanh vị trí đang xem
    const ci=Math.max(0,curIndex());
    from=Math.max(0,ci-MAX_CHIPS/2); to=Math.min(all.length,from+MAX_CHIPS);
  }
  const fs=document.createDocumentFragment(), ft=document.createDocumentFragment();
  for(let i=from;i<to;i++){
    const s=all[i];
    const c=document.createElement("div");
    c.className="tchip vi";
    c.style.left=(s.start/d*100)+"%";
    c.style.width=Math.max(.4,(s.end-s.start)/d*100)+"%";
    c.textContent=(s.vi||s.src||"").slice(0,22);
    c.title=(s.vi||s.src||"");
    c.dataset.i=i;
    c.onclick=()=>{V().currentTime=s.start;tick();};
    fs.appendChild(c); _chips[i]=c;
    const b=document.createElement("div");
    b.className="tbar";
    b.style.left=((s.placed!=null?s.placed:s.start)/d*100)+"%";
    ft.appendChild(b);
  }
  sub.appendChild(fs); tts.appendChild(ft);

  (PR&&PR.regions||[]).forEach((r,i)=>{
    const c=document.createElement("div");
    c.className="tchip rg";
    const rs=(r.start===""||r.start==null)?0:Math.max(0,Number(r.start)||0);
    const re=(r.end===""||r.end==null)?d:Math.min(d,Math.max(rs+.1,Number(r.end)||d));
    c.style.left=(rs/d*100)+"%"; c.style.width=Math.max(.4,(re-rs)/d*100)+"%";
    c.textContent=(r.type==="delogo"?"Xoá logo":"Vùng mờ")+" "+(i+1);
    if(i>0) c.style.opacity=.65;
    c.onclick=()=>{ACT={kind:"rgn",i};draw();renderPanel();};
    rgn.appendChild(c);
  });

  _needles=[cut,sub,tts,rgn].filter(Boolean).map(el=>{
    const n=document.createElement("div"); n.className="tneedle";
    el.appendChild(n); return n;
  });
}
function startTrimDrag(e,side){
  if(!PR) return;
  e.preventDefault(); e.stopPropagation();
  const line=document.getElementById("trkCut"), d=Math.max(.1,dur());
  const apply=ev=>{
    const r=line.getBoundingClientRect();
    const t=Math.max(0,Math.min(d,(ev.clientX-r.left)/r.width*d));
    setTrimPoint(side,t,true);
    drawTracks(true);
    const b=trimBounds();
    document.getElementById("rgninfo").textContent=
      `Đoạn giữ ${fmt(b.start)} → ${fmt(b.end)} · dài ${fmt(b.duration)}`;
  };
  const move=ev=>apply(ev);
  const up=ev=>{
    window.removeEventListener("pointermove",move);
    window.removeEventListener("pointerup",up);
    window.removeEventListener("pointercancel",up);
    apply(ev); save(); renderPanel();
  };
  window.addEventListener("pointermove",move,{passive:true});
  window.addEventListener("pointerup",up);
  window.addEventListener("pointercancel",up);
}
function drawTracks(force){
  const sig=trackSignature();
  if(force||sig!==_trkSig){ _trkSig=sig; buildTracks(); }
  moveNeedle();
}
function moveNeedle(){
  const d=(V().duration||PR&&PR.duration||1);
  const p=((V().currentTime||0)/d*100)+"%";
  for(const n of _needles) n.style.left=p;
  const ci=curIndex();
  if(_curChip!==_chips[ci]){
    if(_curChip) _curChip.classList.remove("cur");
    _curChip=_chips[ci]||null;
    if(_curChip) _curChip.classList.add("cur");
  }
}
/* tick() chạy theo mỗi khung hình video (~4 lần/giây) nên phải THẬT NHẸ:
   chỉ di kim + đổi vài dòng chữ, tuyệt đối không dựng lại DOM. */
let _lastSec=-1, _tickPend=false;
function tick(){
  if(_tickPend) return;
  _tickPend=true;
  requestAnimationFrame(()=>{
    _tickPend=false;
    const v=V(); if(!v.duration) return;
    document.querySelector("#scrub i").style.width=(v.currentTime/v.duration*100)+"%";
    drawTracks();                       // chỉ dựng lại khi chữ ký đổi
    updateRegionPreview();
    const sec=Math.floor(v.currentTime);
    if(sec!==_lastSec){                 // chữ chỉ đổi mỗi giây, không phải mỗi khung
      _lastSec=sec;
      document.getElementById("tcur").textContent=fmt(v.currentTime);
      document.getElementById("fnum").textContent=
        String(Math.round(v.currentTime*25)).padStart(5,"0");
      if(document.getElementById("prev").checked){
        const t=document.getElementById("subtext");
        const txt=currentLineText()||"Phụ đề tiếng Việt sẽ hiện ở đây";
        if(t&&t.textContent!==txt) t.textContent=txt;
      }
      renderLinesHighlight();
    }
  });
}
function seekBar(e){
  const v=V(); if(!v.duration) return;
  const r=e.currentTarget.getBoundingClientRect();
  v.currentTime=(e.clientX-r.left)/r.width*v.duration; tick();
}
function jump(d){ const v=V(); v.currentTime=Math.max(0,v.currentTime+d); tick(); }
function togglePlay(){
  const v=V();
  const overlay=document.getElementById("playOverlay");
  if(v.paused){
    v.play();
    document.getElementById("play").textContent="❚❚";
    if(overlay){overlay.classList.add("playing"); overlay.querySelector("span").textContent="❚❚";}
  } else {
    v.pause();
    document.getElementById("play").textContent="▶";
    if(overlay){overlay.classList.remove("playing"); overlay.querySelector("span").textContent="▶";}
  }
}
function stepLine(dir){
  const s=segs(); if(!s.length) return;
  const t=V().currentTime;
  let target=null;
  if(dir>0){ for(const x of s) if(x.start>t+0.05){target=x.start;break;} }
  else{ for(let i=s.length-1;i>=0;i--) if(s[i].start<t-0.25){target=s[i].start;break;} }
  if(target!=null){ V().currentTime=target; tick(); }
}

/* ======================= bảng bên phải ======================= */
function setTab(t){
  TAB=t;
  if(t==="tts") loadVoices(false);
  if(t==="manual"){ loadManualVoices(false); loadNhacNen(false); }
  document.querySelectorAll("#tabs .tab-item, #tabs .btn").forEach(b=>
    b.classList.toggle("on",b.dataset.t===t));
  renderPanel();
}
function fld(label,html){return `<div class="fld"><label>${label}</label>${html}</div>`;}
function rng(label,val,unit,min,max,step,fn){
  return `<div class="rng"><div class="top"><span>${label}</span><b>${val}${unit||""}</b></div>
    <input type="range" min="${min}" max="${max}" step="${step||1}" value="${val}"
      oninput="${fn}"></div>`;
}
function renderManualPanel(P){
  const eng=MANUAL.engine||"edge", vs=VOICES[eng]||[];
  const voiceOpts=vs.length
    ? vs.map(v=>`<option value="${esc(v.id)}" ${MANUAL.voice===v.id?"selected":""}>${v.status==="ok"?"✓ ":v.status==="failed"?"✕ ":""}${esc(v.name)}</option>`).join("")
    : `<option value="${esc(MANUAL.voice||"")}">${MANUAL.voice?esc(MANUAL.voice):"(đang nạp danh sách giọng…)"}</option>`;
  const audioPath=MANUAL.audio_path||"";
  const outputPath=MANUAL.output_path||"";
  const busy=!!(ST.manual&&ST.manual.working);
  const selectedVideo=PR&&PR.video ? PR.video : "";
  P.innerHTML=`<div class="sect">TẠO ÂM THANH TỪ VĂN BẢN</div>
    ${fld("Nội dung truyện / văn bản",`<textarea id="manualText"
      placeholder="Dán hoặc viết nội dung cần đọc tại đây…"
      oninput="MANUAL.text=this.value;updateManualCharCount()">${esc(MANUAL.text||"")}</textarea>`)}
    <div class="rowbtns" style="align-items:center;margin-top:8px">
      <button class="btn" ${busy?"disabled":""} onclick="pickManualText()">
        T&#7843;i file v&#259;n b&#7843;n</button>
      <span id="manualCharCount" style="color:var(--muted);font-size:11px;white-space:nowrap">
        ${(MANUAL.text||"").length.toLocaleString("vi-VN")} k&#253; t&#7921;</span>
    </div>
    <div class="hint">H&#7895; tr&#7907; file <b>TXT, MD</b> (UTF-8/UTF-16), t&#7889;i &#273;a
      200.000 k&#253; t&#7921; m&#7895;i l&#7847;n t&#7841;o audio.</div>
    <div class="grid2">
      ${fld("Tên file",`<input value="${esc(MANUAL.name||"")}" placeholder="ví dụ: Chuyện ngắn số 1"
        oninput="MANUAL.name=this.value">`)}
      ${fld("Bộ giọng",`<select onchange="setManualEngine(this.value)">
        <option value="edge" ${eng==="edge"?"selected":""}>edge-tts</option>
        <option value="vieneu" ${eng==="vieneu"?"selected":""}>VieNeu-TTS (offline)</option>
        <option value="capcut" ${eng==="capcut"?"selected":""}>CapCut TTS</option>
      </select>`)}
    </div>
    ${fld(`Giọng đọc &nbsp;<span style="color:var(--accent)">${vs.length} giọng</span>`,
      `<select onchange="MANUAL.voice=this.value">${voiceOpts}</select>`)}
    <div style="height:7px"></div>
    ${eng==="edge"?rng("Cao độ giọng",parseInt(MANUAL.pitch||"0")," Hz",-200,200,10,
      "MANUAL.pitch=(this.value>=0?'+':'')+this.value+'Hz';this.previousElementSibling.querySelector('b').textContent=this.value+' Hz'"):""}
    ${rng("Tốc độ đọc",parseInt(MANUAL.rate||"0"),"%",-30,50,5,
      "MANUAL.rate=(this.value>0?'+':'')+this.value+'%';this.previousElementSibling.querySelector('b').textContent=this.value+'%'")}
    ${nutNgheThu("story")}
    <div class="hint">Nghe thử đọc một câu bằng đúng giọng/tốc độ đang chọn (có văn bản
      thì đọc chính đoạn đầu của bạn), khỏi phải tạo cả file mới biết giọng có hợp không.</div>
    <button class="btn pri" style="width:100%;text-align:center;margin-bottom:12px" ${busy?"disabled":""}
      onclick="createManualAudio()">▶ Tạo file MP3</button>
    <div class="hint" id="manualStatus"><b>${esc(MANUAL.status||"Sẵn sàng")}</b>
      ${MANUAL.error?`<br><span style="color:var(--red)">${esc(MANUAL.error)}</span>`:""}
      ${audioPath?`<br>${MANUAL.audio_duration?fmt(MANUAL.audio_duration)+" · ":""}${esc(audioPath)}`:""}</div>
    ${audioPath?`<audio controls preload="metadata" style="width:100%;margin-bottom:12px"
      src="/api/manual/audio?v=${_manualRev}"></audio>`:""}
    <div class="rowbtns">
      <button class="btn" onclick="pickManualAudio()">Chọn audio có sẵn</button>
      <button class="btn" ${audioPath?"":"disabled"} onclick="openManualFolder('audio')">Mở thư mục audio</button>
    </div>

    <div class="sect" style="margin-top:17px">NHẠC NỀN</div>
    <div class="hint">Nhạc chỉ lấy loại <b>CC0 / Public Domain</b> nên dùng thương mại thoải mái.
      Nguồn từng bài ghi trong <b>assets/nhac_nen/nguon.json</b>; muốn dùng nhạc riêng thì
      cứ chép file vào thư mục đó.</div>
    ${fld("Bài nhạc",`<select onchange="MANUAL.nhac_bai=this.value">
      <option value="">— tự chọn ngẫu nhiên trong kho —</option>
      ${NHAC_LIST.map(x=>`<option value="${esc(x.ten)}" ${MANUAL.nhac_bai===x.ten?"selected":""}
        >${esc(x.ten)}${x.giay_phep?" · "+esc(x.giay_phep):""}</option>`).join("")}
    </select>`)}
    <div class="rowbtns" style="margin-bottom:8px">
      <button class="btn" ${busy?"disabled":""} onclick="taiNhacNen()">Tải thêm nhạc CC0</button>
      <button class="btn" onclick="loadNhacNen(true)">Làm mới danh sách</button>
      <span style="color:var(--muted);font-size:11px;align-self:center">${NHAC_LIST.length} bài trong máy</span>
    </div>
    ${rng("Mức nhạc nền",MANUAL.nhac_db," dB",-50,-20,1,
      "MANUAL.nhac_db=+this.value;this.previousElementSibling.querySelector('b').textContent=this.value+' dB'")}
    <div class="hint">-40 chỉ đủ lấp khoảng lặng, -35 nghe rõ hơn chút. Trên -30 là bắt đầu tranh với giọng đọc.</div>
    <label style="display:flex;gap:7px;align-items:center;margin:8px 0">
      <input type="checkbox" ${MANUAL.nhac_duck?"checked":""}
        onchange="MANUAL.nhac_duck=this.checked">
      <span>Nhạc tự nhỏ lại khi có lời (ducking)</span></label>
    <button class="btn pri" style="width:100%;text-align:center;margin-bottom:10px"
      ${(!audioPath||busy)?"disabled":""} onclick="tronNhacNen()">▶ Trộn nhạc nền vào giọng đọc</button>
    ${MANUAL.nhac_ten?`<div class="hint">Đang dùng nhạc: <b>${esc(MANUAL.nhac_ten)}</b></div>`:""}

    <div class="sect" style="margin-top:17px">DỰNG VIDEO TỪ ẢNH</div>
    <div class="hint">Dùng khi không có sẵn video. Ảnh được chia đều theo độ dài giọng đọc nên
      video ra luôn vừa khít. Ảnh lệch tỉ lệ khung hình sẽ đặt giữa, hai bên lấp bằng chính
      ảnh đó làm nền mờ.</div>
    ${fld("Ảnh hoặc thư mục ảnh (mỗi dòng một mục)",`<textarea id="manualAnh" style="min-height:60px"
      placeholder="Ví dụ: E:\\anh\\truyen1"
      oninput="MANUAL.anh=this.value">${esc(MANUAL.anh||"")}</textarea>`)}
    <div class="rowbtns" style="margin-bottom:8px">
      <button class="btn" ${busy?"disabled":""} onclick="pickAnh()">Chọn thư mục ảnh</button>
      ${fld("Kiểu hình",`<select onchange="MANUAL.slide_kieu=this.value">
        <option value="chuyen_dong" ${MANUAL.slide_kieu==="chuyen_dong"?"selected":""}>Ảnh trôi và phóng chậm</option>
        <option value="tinh" ${MANUAL.slide_kieu==="tinh"?"selected":""}>Ảnh đứng yên (dựng nhanh)</option>
      </select>`)}
    </div>
    <button class="btn pri" style="width:100%;text-align:center;margin-bottom:12px"
      ${(!audioPath||busy)?"disabled":""} onclick="dungVideoTuAnh()">▶ Dựng video từ ảnh</button>

    <div class="sect" style="margin-top:17px">GHÉP AUDIO VÀO VIDEO</div>
    <div class="hint">Video đang chọn: <b>${selectedVideo?esc(selectedVideo):"chưa chọn"}</b><br>
      Khi ghép, ứng dụng vẫn áp dụng phần cắt video, vùng mờ, logo, phụ đề và mức âm nền gốc
      trong tab <b>Xuất file</b>. Audio ngắn hơn sẽ được chèn lặng ở cuối; audio dài hơn video sẽ bị cắt.</div>
    <button class="btn pri" style="width:100%;text-align:center;margin-bottom:10px"
      ${(!JID||!audioPath||busy)?"disabled":""} onclick="muxManualAudio()">▶ Ghép audio vào video đang chọn</button>
    ${outputPath?`<button class="btn" style="width:100%;text-align:center"
      onclick="openManualFolder('output')">Mở video đã ghép</button>`:""}`;
}
function renderPanel(){
  const P=document.getElementById("panel");
  if(TAB==="manual"){ renderManualPanel(P); return; }
  if(!PR){ P.innerHTML=`<div class="hint">Chọn một video để bắt đầu chỉnh.</div>`; return; }
  const st=PR.sub_style||{}, op=PR.options||{};

  if(TAB==="sub"){
    const box=st.box||{x:0,y:0,w:PR.w,h:60};
    P.innerHTML=`<div class="sect">PHỤ ĐỀ TIẾNG VIỆT (LỚP 3)</div>
    <div class="grid2">
      ${fld("Font chữ",`<select onchange="setSt('font',this.value)">
        ${["Be Vietnam Pro","Arial","Segoe UI","Roboto","Tahoma","Times New Roman"]
          .map(f=>`<option ${st.font===f?"selected":""}>${f}</option>`).join("")}</select>`)}
      ${fld("Cỡ chữ",`<input type="number" min="8" max="200" value="${st.size||30}"
        onchange="setSt('size',+this.value)">`)}
    </div>
    <div class="grid2">
      ${fld("Màu chữ",`<div class="sw">${COLORS.map(c=>
        `<i class="${(st.color||"").toUpperCase()===c?"on":""}" style="background:${c}"
           onclick="setSt('color','${c}')"></i>`).join("")}</div>`)}
      ${fld("Viền / bóng",`<select onchange="setOutline(this.value)">
        <option value="2,1" ${st.outline==2?"selected":""}>Viền đen 2px + bóng</option>
        <option value="3,0" ${st.outline==3?"selected":""}>Viền đen 3px dày</option>
        <option value="1,0" ${st.outline==1?"selected":""}>Viền mảnh 1px</option>
        <option value="0,0" ${st.outline==0?"selected":""}>Không viền</option>
      </select>`)}
    </div>
    <div class="sect">VỊ TRÍ TRÊN KHUNG HÌNH</div>
    <div class="g9">${GRID.flat().map(g=>
      `<button class="${st.align===g?"on":""}" onclick="setSt('align','${g}')"></button>`).join("")}</div>
    ${rng("Vị trí ngang",Math.round(box.x+box.w/2)," px",0,PR.w,1,"setBoxCenter(+this.value,null)")}
    ${rng("Vị trí dọc",Math.round(box.y+box.h)," px",0,PR.h,1,"setBoxBottom(+this.value)")}
    <button class="btn" style="width:100%;text-align:center;margin-bottom:12px"
      onclick="resetSubBox()">Đưa về đáy giữa (mặc định)</button>
    <label class="chk2"><input type="checkbox" ${op.hardsub?"checked":""}
      onchange="setOpt('hardsub',this.checked)"> Ghi cứng phụ đề vào video</label>
    <label class="chk2"><input type="checkbox" ${op.export_srt?"checked":""}
      onchange="setOpt('export_srt',this.checked)"> Xuất kèm file .srt</label>
    <div id="lines"></div>`;
    renderLines();
  }

  else if(TAB==="blur"){
    const a=ACT&&ACT.kind==="rgn"?PR.regions[ACT.i]:null;
    P.innerHTML=`<div class="sect">LỚP 2 · CHE SUB GỐC / XOÁ LOGO</div>
    <div class="hint">Sub tiếng Trung <b>cháy cứng</b> trong hình (lớp 1) không xoá được,
      nên ta <b>phủ mờ</b> lên. Bấm <b>Tự dò sub cứng</b> để máy tự khoanh vùng,
      hoặc kéo–thả khung đỏ ngay trên video.</div>
    <div class="rowbtns">
      <button class="btn on" onclick="autoDetect()">✦ Tự dò</button>
      <button class="btn" onclick="addRegion('blur')">+ Vùng mờ</button>
      <button class="btn" onclick="addRegion('delogo')">+ Xoá logo</button>
    </div>
    ${(PR.regions||[]).length?"":`<div class="hint">Chưa có vùng nào.</div>`}
    ${(PR.regions||[]).map((r,i)=>`
      <div class="lrow ${ACT&&ACT.kind==='rgn'&&ACT.i===i?'cur':''}"
           onclick="ACT={kind:'rgn',i:${i}};draw();renderPanel()">
        <div class="lmeta"><span class="lid">${i+1}</span>
          ${r.type==="delogo"?"Xoá logo":"Làm mờ"} · ${r.w}×${r.h} @ (${r.x},${r.y})
          <span class="lplay" onclick="event.stopPropagation();PR.regions.splice(${i},1);ACT=null;save();draw();renderPanel()">xoá</span>
        </div>
        ${r.type==="delogo"?"":rng("Độ mờ",r.strength||20,"",4,60,1,
          `PR.regions[${i}].strength=+this.value;save();draw()`)}
        <div class="grid2" style="margin-top:7px">
          ${fld("Tu giay",`<input type="number" min="0" step="0.1" value="${r.start??""}"
            placeholder="0" onchange="setRgnAt(${i},'start',this.value)">`)}
          ${fld("Den giay",`<input type="number" min="0" step="0.1" value="${r.end??""}"
            placeholder="toan video" onchange="setRgnAt(${i},'end',this.value)">`)}
        </div>
        <div class="rowbtns" style="margin-top:7px">
          <button class="btn" onclick="event.stopPropagation();setRgnAt(${i},'start',V().currentTime)">Lay bat dau</button>
          <button class="btn" onclick="event.stopPropagation();setRgnAt(${i},'end',V().currentTime)">Lay ket thuc</button>
        </div>
      </div>`).join("")}
    ${a?`<div class="sect" style="margin-top:8px">TOẠ ĐỘ CHÍNH XÁC</div>
    <div class="grid2">
      ${fld("X",`<input type="number" value="${a.x}" onchange="setRgn('x',+this.value)">`)}
      ${fld("Y",`<input type="number" value="${a.y}" onchange="setRgn('y',+this.value)">`)}
      ${fld("Rộng",`<input type="number" value="${a.w}" onchange="setRgn('w',+this.value)">`)}
      ${fld("Cao",`<input type="number" value="${a.h}" onchange="setRgn('h',+this.value)">`)}
    </div>`:""}`;
  }

  else if(TAB==="logo"){
    const l=PR.logo;
    P.innerHTML=`<div class="sect">CHÈN LOGO CỦA BẠN</div>
    ${l?`${fld("Ảnh logo (.png nền trong)",`<input id="logopath" value="${esc(l.path||"")}"
        placeholder="chưa chọn ảnh" onchange="PR.logo.path=this.value;save()">`)}
      <button class="btn" style="width:100%;text-align:center;margin:7px 0 4px"
        onclick="pickLogo()">📂 Chọn ảnh logo…</button>
      <div style="height:11px"></div>
      ${rng("Độ mờ",Math.round((l.opacity||1)*100),"%",5,100,1,
        "PR.logo.opacity=+this.value/100;save();draw()")}
      <div class="grid2">
        ${fld("X",`<input type="number" value="${l.x}" onchange="PR.logo.x=+this.value;save();draw()">`)}
        ${fld("Y",`<input type="number" value="${l.y}" onchange="PR.logo.y=+this.value;save();draw()">`)}
        ${fld("Rộng",`<input type="number" value="${l.w}" onchange="PR.logo.w=+this.value;save();draw()">`)}
        ${fld("Cao",`<input type="number" value="${l.h}" onchange="PR.logo.h=+this.value;save();draw()">`)}
      </div>
      <button class="btn danger" style="width:100%" onclick="PR.logo=null;save();draw();renderPanel()">Bỏ logo</button>`
    :`<div class="hint">Chưa chèn logo. Bấm nút dưới rồi kéo khung tím tới vị trí mong muốn.</div>
      <button class="btn" style="width:100%;text-align:center" onclick="addLogoBox()">▤ Chèn logo</button>`}`;
  }

  else if(TAB==="cut"){
    const b=trimBounds();
    P.innerHTML=`<div class="sect">CẮT VIDEO THEO ĐOẠN GIỮ</div>
    <label class="chk2"><input type="checkbox" ${b.on?"checked":""}
      onchange="setTrimEnabled(this.checked)"> Bật cắt theo đoạn đang chọn</label>
    <div class="grid2">
      ${fld("Giữ từ",`<input value="${fmt(b.start)}"
        placeholder="00:00:30" onchange="setTrimPoint('start',this.value)">`)}
      ${fld("Giữ đến",`<input value="${fmt(b.end)}"
        placeholder="hết video" onchange="setTrimPoint('end',this.value)">`)}
    </div>
    <div class="rowbtns">
      <button class="btn" onclick="setTrimFromNow('start')">Lấy đầu</button>
      <button class="btn" onclick="setTrimFromNow('end')">Lấy cuối</button>
    </div>
    <div class="rowbtns">
      <button class="btn" onclick="seekTrim('start')">Tới đầu</button>
      <button class="btn" onclick="seekTrim('end')">Tới cuối</button>
    </div>
    <div class="hint">Đoạn xuất: <b>${fmt(b.duration)}</b> / video gốc ${fmt(b.full)}.
      Có thể kéo hai tay nắm ở track <b>ĐOẠN GIỮ</b> bên dưới video.</div>
    <button class="btn" style="width:100%;text-align:center;margin-bottom:9px"
      onclick="trimFull()">Dùng toàn bộ video</button>
    <button class="btn pri" style="width:100%;text-align:center"
      onclick="runAll()">▶ Chạy đoạn đã chọn</button>`;
  }

  else if(TAB==="asr"){
    P.innerHTML=`<div class="sect">NHẬN DẠNG PHỤ ĐỀ GỐC</div>
    <div class="hint">Cấu hình engine nằm trong <b>config.yaml</b> (mục <b>asr</b>).
      Mặc định <b>paraformer</b> cho tiếng Trung, tự dự phòng sang faster-whisper.
      Có sẵn <b>kiểm tra độ phủ</b> và <b>tự vá lỗ hổng</b> chống mất đoạn.</div>
    <button class="btn pri" style="width:100%;text-align:center"
      onclick="run(['asr'])">▶ Chạy nhận dạng</button>
    <div style="height:9px"></div>
    <div class="hint">Số dòng hiện có: <b>${segs().length}</b></div>`;
  }

  else if(TAB==="tr"){
    const tr=CFG.translation||{};
    const provider=tr.provider||"browser";
    const providerFields = provider==="zenmux" ? `
      <div class="grid2">
        ${fld("ZenMux API key",`<input type="password" autocomplete="off"
          value="${esc(tr.zenmux_api_key||"")}" placeholder="sk-mg-v1-..."
          onchange="setTrCfg('zenmux_api_key',this.value)">`)}
        ${fld("Model",`<input value="${esc(tr.zenmux_model||"z-ai/glm-5.3-free")}"
          onchange="setTrCfg('zenmux_model',this.value)">`)}
      </div>
      ${fld("Base URL",`<input value="${esc(tr.zenmux_base_url||"https://zenmux.ai/api/v1")}"
        onchange="setTrCfg('zenmux_base_url',this.value)">`)}
      ${fld("Timeout mỗi request",`<input type="number" min="60" step="30"
        value="${Number(tr.zenmux_timeout||420)}"
        onchange="setTrCfg('zenmux_timeout',Math.max(60,+this.value||420))">`)}
      <div class="hint">ZenMux dùng chuẩn OpenAI-compatible. Model mặc định <b>z-ai/glm-5.3-free</b>; khóa chỉ lưu cục bộ trong config.yaml.</div>`
    : provider==="nvidia" ? `
      <div class="grid2">
        ${fld("NVIDIA API key",`<input type="password" autocomplete="off"
          value="${esc(tr.nvidia_api_key||"")}" placeholder="nvapi-..."
          onchange="setTrCfg('nvidia_api_key',this.value)">`)}
        ${fld("Model",`<input value="${esc(tr.nvidia_model||"minimaxai/minimax-m3")}"
          onchange="setTrCfg('nvidia_model',this.value)">`)}
      </div>
      ${fld("Base URL",`<input value="${esc(tr.nvidia_base_url||"https://integrate.api.nvidia.com/v1")}"
        onchange="setTrCfg('nvidia_base_url',this.value)">`)}
      ${fld("Timeout mỗi request",`<input type="number" min="60" step="30"
        value="${Number(tr.nvidia_timeout||420)}"
        onchange="setTrCfg('nvidia_timeout',Math.max(60,+this.value||420))">`)}
      <div class="hint">Key <b>nvapi-...</b> tạo FREE tại build.nvidia.com (không cần thẻ),
        MỘT key dùng cho mọi model trong catalog. Mặc định mới là
        <b>minimaxai/minimax-m3</b>; muốn thử model khác chỉ cần đổi ô Model.</div>`
    : provider==="tokenrouter" ? `
      <div class="grid2">
        ${fld("TokenRouter API key",`<input type="password" autocomplete="off"
          value="${esc(tr.tokenrouter_api_key||"")}" placeholder="tr_..."
          onchange="setTrCfg('tokenrouter_api_key',this.value)">`)}
        ${fld("Model",`<input value="${esc(tr.tokenrouter_model||"moonshotai/kimi-k3-free")}"
          onchange="setTrCfg('tokenrouter_model',this.value)">`)}
      </div>
      ${fld("Base URL",`<input value="${esc(tr.tokenrouter_base_url||"https://api.tokenrouter.com/v1")}"
        onchange="setTrCfg('tokenrouter_base_url',this.value)">`)}
      ${fld("Timeout mỗi request",`<input type="number" min="60" step="30"
        value="${Number(tr.tokenrouter_timeout||420)}"
        onchange="setTrCfg('tokenrouter_timeout',Math.max(60,+this.value||420))">`)}
      <div class="hint">TokenRouter dùng chuẩn OpenAI-compatible /v1/chat/completions. Mặc định là <b>moonshotai/kimi-k3-free</b>.</div>`
    : provider==="inferx" ? `
      <div class="grid2">
        ${fld("InferX API key",`<input type="password" autocomplete="off"
          value="${esc(tr.inferx_api_key||"")}" placeholder="ix_..."
          onchange="setTrCfg('inferx_api_key',this.value)">`)}
        ${fld("InferX model",`<input value="${esc(tr.inferx_model||"deepseek-v4-flash")}"
          onchange="setTrCfg('inferx_model',this.value)">`)}
      </div>
      ${fld("InferX Base URL",`<input value="${esc(tr.inferx_base_url||"https://model.inferx.net/endpoints/v1")}"
        onchange="setTrCfg('inferx_base_url',this.value)">`)}
      ${fld("Timeout mỗi request",`<input type="number" min="60" step="30"
        value="${Number(tr.inferx_timeout||420)}"
        onchange="setTrCfg('inferx_timeout',Math.max(60,+this.value||420))">`)}
      <div class="hint">InferX dùng endpoint OpenAI-compatible /chat/completions, model mặc định <b>deepseek-v4-flash</b>.</div>`
    : provider==="tokenrouter_gemini" ? `
      <div class="grid2">
        ${fld("TokenRouter Gemini key",`<input type="password" autocomplete="off"
          value="${esc(tr.tokenrouter_gemini_api_key||"")}" placeholder="sk-..."
          onchange="setTrCfg('tokenrouter_gemini_api_key',this.value)">`)}
        ${fld("Gemini model",`<input value="${esc(tr.tokenrouter_gemini_model||"google/gemini-3.6-flash")}"
          onchange="setTrCfg('tokenrouter_gemini_model',this.value)">`)}
      </div>
      ${fld("Gemini Base URL",`<input value="${esc(tr.tokenrouter_gemini_base_url||"https://api.tokenrouter.com/v1beta/models")}"
        onchange="setTrCfg('tokenrouter_gemini_base_url',this.value)">`)}
      ${fld("Timeout mỗi request",`<input type="number" min="60" step="30"
        value="${Number(tr.tokenrouter_gemini_timeout||420)}"
        onchange="setTrCfg('tokenrouter_gemini_timeout',Math.max(60,+this.value||420))">`)}
      <div class="hint">Dùng endpoint native Gemini của TokenRouter: /v1beta/models/google/gemini-3.6-flash:generateContent.</div>`
    : provider==="gemini" ? `
      <div class="grid2">
        ${fld("Gemini API key",`<input type="password" autocomplete="off"
          value="${esc(tr.gemini_api_key||"")}" placeholder="AIza..."
          onchange="setTrCfg('gemini_api_key',this.value)">`)}
        ${fld("Gemini model",`<input value="${esc(tr.gemini_model||"gemini-3.6-flash")}"
          onchange="setTrCfg('gemini_model',this.value)">`)}
      </div>`
    : `<div class="hint">Dùng Gemini qua trình duyệt Edge/Chrome đã đăng nhập, không cần API key.</div>`;

    P.innerHTML=`<div class="sect">DỊCH SANG TIẾNG VIỆT</div>
    <div class="grid2">
      ${fld("Chế độ dịch",`<select onchange="setTrCfg('provider',this.value,true)">
        <option value="browser" ${provider==="browser"?"selected":""}>browser - Gemini qua trình duyệt</option>
        <option value="gemini" ${provider==="gemini"?"selected":""}>gemini - API key</option>
        <option value="nvidia" ${provider==="nvidia"?"selected":""}>nvidia - MiniMax M3 free (NIM)</option>
        <option value="zenmux" ${provider==="zenmux"?"selected":""}>zenmux - GLM-5.3 free</option>
        <option value="inferx" ${provider==="inferx"?"selected":""}>inferx - DeepSeek V4 Flash</option>
        <option value="tokenrouter_gemini" ${provider==="tokenrouter_gemini"?"selected":""}>tokenrouter - Gemini 3.6 Flash</option>
        <option value="tokenrouter" ${provider==="tokenrouter"?"selected":""}>tokenrouter - Kimi K3 free</option>
      </select>`)}
      ${fld("Số dòng mỗi lượt",`<input type="number" min="1" step="1"
        value="${Number(tr.chunk_size||80)}"
        onchange="setTrCfg('chunk_size',Math.max(1,+this.value||80))">`)}
      ${fld("Nhịp ký tự / giây",`<input type="number" min="0" step="1"
        value="${Number(tr.chars_per_sec??14)}"
        onchange="setTrCfg('chars_per_sec',Math.max(0,+this.value||0))">`)}
    </div>
    ${providerFields}
    <div class="grid2">
      ${fld("Ép tên nam chính",`<input value="${esc(tr.male_lead_name||"")}"
        placeholder="để trống = không ép"
        onchange="setTrCfg('male_lead_name',this.value)">`)}
      ${fld("Ép tên nữ chính",`<input value="${esc(tr.female_lead_name||"")}"
        placeholder="để trống = không ép"
        onchange="setTrCfg('female_lead_name',this.value)">`)}
    </div>
    <div class="rowbtns">
      <button class="btn" onclick="testTrCfg()">Test API</button>
      <button class="btn" onclick="saveTrCfg(true)">Lưu cấu hình dịch</button>
    </div>
    <button class="btn pri" style="width:100%;text-align:center"
      onclick="run(['translate'])">▶ Chạy dịch</button>
    <div style="height:9px"></div>
    <div class="hint">Đã dịch: <b>${segs().filter(s=>s.vi&&s.vi.trim()).length}</b>
      / ${segs().length} dòng</div>`;
  }

  else if(TAB==="tts"){
    const eng=op.engine||"edge";
    const vs=VOICES[eng]||[];
    const cur=op.narrator_voice||"";
    const opts=vs.length
      ? vs.map(v=>`<option value="${esc(v.id)}" ${cur===v.id?"selected":""}>${esc(v.name)}</option>`).join("")
      : `<option value="">(chưa lấy được danh sách giọng)</option>`;
    P.innerHTML=`<div class="sect">GIỌNG ĐỌC TIẾNG VIỆT</div>
    <div class="grid2">
      ${fld("Bộ giọng (engine)",`<select onchange="setEngine(this.value)">
        <option value="edge" ${eng==="edge"?"selected":""}>edge-tts (nhanh, cần mạng)</option>
        <option value="vieneu" ${eng==="vieneu"?"selected":""}>VieNeu-TTS (offline, tự nhiên hơn)</option>
        <option value="capcut" ${eng==="capcut"?"selected":""}>CapCut TTS (online, nhiều giọng)</option>
      </select>`)}
      ${fld("Kiểu giọng",`<select onchange="setOpt('voice_mode',this.value)">
        <option value="narrator" ${op.voice_mode==="narrator"?"selected":""}>1 giọng kể</option>
        <option value="alternate" ${op.voice_mode==="alternate"?"selected":""}>Nam/nữ luân phiên</option>
        <option value="per-speaker" ${op.voice_mode==="per-speaker"?"selected":""}>Mỗi nhân vật 1 giọng</option>
      </select>`)}
    </div>
    ${fld(`Giọng chính &nbsp;<span style="color:var(--accent)">${vs.length} giọng</span>`,
      `<select onchange="setOpt('narrator_voice',this.value)">${opts}</select>`)}
    <div style="height:8px"></div>
    <div class="rowbtns">
      <button class="btn" onclick="loadVoices(true)">⟳ Nạp lại danh sách giọng</button>
      ${eng==="vieneu"?`<button class="btn on" onclick="prefetch()">⬇ Tải model về máy</button>`:""}
    </div>
    ${nutNgheThu("dub")}
    ${eng==="vieneu"&&!vs.length?`<div class="hint">Chưa thấy giọng nào. Model
      VieNeu-TTS chưa tải xong — bấm <b>Tải model về máy</b> (vài GB, chỉ một
      lần), hoặc chạy <b>tai_model.bat</b>. Trong lúc chờ vẫn dùng được
      <b>edge-tts</b>.</div>`:""}
    ${eng==="capcut"?`<div class="hint">CapCut TTS dùng API online, nhiều giọng hơn nhưng có thể bị queue/rate-limit.
      Nếu mạng hoặc CapCut lỗi, file <b>tts_loi.txt</b> sẽ ghi rõ dòng hỏng.</div>`:""}
    ${rng("Cao độ giọng (pitch)",parseInt(op.narrator_pitch||"0")," Hz",-200,200,10,
      "setOpt('narrator_pitch',(this.value>=0?'+':'')+this.value+'Hz')")}
    <div class="hint">Tăng <b>pitch</b> = giọng cao hơn (giọng trẻ, nữ). Giảm = giọng trầm hơn (giọng già, nam).
      Chỉ có hiệu lực với engine <b>edge-tts</b>.</div>
    ${rng("Tốc độ nói nền",parseInt(op.base_rate||"+0%"),"%",-30,50,5,
      "setOpt('base_rate',(this.value>0?'+':'')+this.value+'%')")}
    ${rng("Trần tăng tốc chống đè",(op.max_speed||1.6).toFixed(2),"×",1,2.5,.05,
      "setOpt('max_speed',+this.value)")}
    ${rng("Khoảng nghỉ giữa câu",(op.min_gap||.08).toFixed(2)," s",0,.6,.01,
      "setOpt('min_gap',+this.value)")}
    ${rng("Được đọc quá cuối câu",(op.max_overhang_seconds??.75).toFixed(2)," s",0,2,.05,
      "setOpt('max_overhang_seconds',+this.value)")}
    <div class="hint">Ở chế độ strict, mỗi câu luôn bắt đầu đúng timestamp gốc. Câu Việt dài
      được tăng tốc vừa đủ và chỉ được đọc quá mốc kết thúc tối đa mức trên; đặt <b>0 giây</b>
      nếu muốn bám chặt hình, nhưng câu quá dài sẽ phải cắt nhiều hơn.</div>
    <button class="btn pri" style="width:100%;text-align:center"
      onclick="run(['tts'])">▶ Dựng giọng đọc</button>`;
  }

  else if(TAB==="export"){
    const legacyVol=op.keep_original_volume==null?null:+op.keep_original_volume;
    const origDb=op.keep_original_db!=null ? +op.keep_original_db
      : legacyVol!=null&&legacyVol>0 ? Math.round(20*Math.log10(legacyVol/10)) : -30;
    const origMuted=!!op.keep_original_muted ||
      (op.keep_original_db==null && (legacyVol==null || legacyVol<=0));
    P.innerHTML=`<div class="sect">XUẤT FILE</div>
    <label class="chk2"><input type="checkbox" ${op.use_gpu?"checked":""}
      onchange="setOpt('use_gpu',this.checked)"> Dùng GPU (NVENC) cho nhanh</label>
    <label class="chk2"><input type="checkbox" ${op.hardsub?"checked":""}
      onchange="setOpt('hardsub',this.checked)"> Ghi cứng phụ đề Việt</label>
    <label class="chk2"><input type="checkbox" ${op.render_chunked?"checked":""}
      onchange="setOpt('render_chunked',this.checked)"> Chia render rồi tự ghép lại</label>
    ${fld("Mỗi phần render",`<input type="number" min="10" step="10"
      value="${op.render_chunk_minutes||120}"
      onchange="setOpt('render_chunk_minutes',Math.max(10,+this.value||120))"> phút`)}
    <label class="chk2"><input type="checkbox" ${op.export_srt?"checked":""}
      onchange="setOpt('export_srt',this.checked)"> Xuất kèm .srt</label>
    <div style="height:8px"></div>
    <label class="chk2"><input type="checkbox" ${origMuted?"checked":""}
      onchange="setOriginalMuted(this.checked)"> Tắt hẳn âm thanh gốc</label>
    ${rng("Âm lượng nền gốc",Math.max(-60,Math.min(0,Math.round(origDb)))," dB",-60,0,1,
      "setOriginalDb(+this.value)")}
    <div class="hint"><b>-60 dB</b> = gần như im, <b>-40 dB</b> = rất nhỏ,
      <b>-30 dB</b> = nền nhỏ (khuyến nghị), <b>-20 dB</b> vẫn có thể nghe khá rõ,
      <b>0 dB</b> = giữ nguyên âm lượng gốc.</div>
    <div style="height:4px"></div>
    ${rng("Chất lượng (thấp = nét hơn)",op.crf||20,"",14,30,1,"setOpt('crf',+this.value)")}
    <div class="hint">Không có vùng phủ nào và tắt ghi cứng phụ đề →
      video được <b>copy nguyên luồng hình</b>, xuất gần như tức thì.</div>
    <button class="btn" style="width:100%;text-align:center;margin-bottom:9px"
      onclick="cleanupTemp()">Dọn file tạm (_tmp)</button>
    <button class="btn pri" style="width:100%;text-align:center"
      onclick="run(['render'])">▶ Xuất video</button>`;
  }
}
async function pickLogo(){
  if(!DESK()) return toast("Dán đường dẫn ảnh vào ô phía trên","warn");
  const p=await pywebview.api.pick_image();
  if(!p||p.error) return;
  PR.logo.path=p; save(); renderPanel(); draw();
  toast("Đã chọn logo","ok");
}
const VOICES={edge:[],vieneu:[],capcut:[]};
const VOICE_RECS={analysis:null,items:[],cast:[],coverage:0,loading:false,engine:""};
let VOICE_LIBRARY_OPEN=false;
async function loadVoices(force){
  const eng=(PR&&PR.options&&PR.options.engine)||"edge";
  if(!force && VOICES[eng] && VOICES[eng].length) return;
  try{
    const r=await api("/api/voices?engine="+encodeURIComponent(eng));
    VOICES[eng]=r.voices||[];
    if(force) toast(`Có ${VOICES[eng].length} giọng cho ${eng}`, VOICES[eng].length?"ok":"warn");
  }catch(e){ if(force) toast(e.message,"err"); }
  if(TAB==="tts") renderPanel();
}
function setEngine(v){
  PR.options.engine=v;
  const vs=VOICES[v]||[];
  if(vs.length) PR.options.narrator_voice=vs[0].id;   // giọng cũ có thể không thuộc engine mới
  save(); renderPanel(); loadVoices(false);
}
async function loadManualVoices(force){
  const eng=MANUAL.engine||"edge";
  if(!force && VOICES[eng] && VOICES[eng].length){
    if(!VOICES[eng].some(v=>v.id===MANUAL.voice)) MANUAL.voice=VOICES[eng][0].id;
    return;
  }
  try{
    const r=await api("/api/voices?engine="+encodeURIComponent(eng));
    VOICES[eng]=r.voices||[];
    if(VOICES[eng].length && !VOICES[eng].some(v=>v.id===MANUAL.voice))
      MANUAL.voice=VOICES[eng][0].id;
    if(force) toast(`Có ${VOICES[eng].length} giọng cho ${eng}`,
                    VOICES[eng].length?"ok":"warn");
  }catch(e){ if(force) toast(e.message,"err"); }
  if(MODE==="story") renderStory();
}
function setManualEngine(v){
  MANUAL.engine=v;
  const vs=VOICES[v]||[];
  MANUAL.voice=vs.length?vs[0].id:"";
  rerenderMode(); loadManualVoices(false);
}
function voiceRecommendationHtml(eng,vs,busy){
  const a=VOICE_RECS.analysis, items=VOICE_RECS.engine===eng?VOICE_RECS.items:[];
  const summary=a?`<div class="hint" style="margin-top:7px"><b>Phân tích:</b>
    ${esc(a.genre_label||"đời thường")} · ${(a.word_count||0).toLocaleString("vi-VN")} từ
    · khoảng ${a.estimated_minutes||0} phút · ${a.dialogue_ratio||0}% đoạn đối thoại.</div>`:"";
  const cards=items.length?`<div style="display:grid;gap:6px;margin-top:7px">
    ${items.map((r,i)=>`<div style="border:1px solid var(--line);border-radius:7px;padding:7px">
      <div style="display:flex;gap:6px;align-items:center"><b style="flex:1">${i+1}. ${esc(r.name)}</b>
        <button class="btn sm" onclick="chooseRecommendedVoice(${i},false)">Chọn</button>
        <button class="btn sm" ${busy?"disabled":""} onclick="chooseRecommendedVoice(${i},true)">▶ Nghe</button></div>
      <div class="hint" style="margin-top:3px">${esc((r.reasons||[]).join(" · "))}</div>
    </div>`).join("")}</div>`:"";
  const cast=VOICE_RECS.engine===eng?VOICE_RECS.cast:[];
  const castHtml=cast.length?`<div style="margin-top:8px;border-top:1px solid var(--line);padding-top:7px">
    <div class="hint"><b>Dàn nhân vật tự chọn:</b> ${cast.length} giọng riêng · nhận diện chắc
      ${Number(VOICE_RECS.coverage||0).toFixed(1)}% lượt thoại.</div>
    <div style="display:grid;gap:5px;margin-top:6px">${cast.map(c=>`
      <div style="border:1px solid var(--line);border-radius:7px;padding:6px 7px">
        <div style="display:flex;align-items:center;gap:5px"><b style="flex:1">${esc(c.character||"")}</b>
          <span class="hint">${c.dialogue_lines||0} lượt</span>
          <button class="btn sm" ${busy?"disabled":""} onclick="previewCastVoice(${VOICE_RECS.cast.indexOf(c)})">▶ Nghe</button></div>
        <span style="color:var(--accent)">${esc(c.voice_name||c.voice_id||"")}</span>
        ${c.description?`<div class="hint" style="margin-top:2px">${esc(c.description)}</div>`:""}
      </div>`).join("")}</div></div>`:"";
  const library=VOICE_LIBRARY_OPEN?`<div style="max-height:210px;overflow:auto;display:grid;gap:4px;margin-top:7px">
    ${vs.map((v,i)=>`<button class="btn sm ${MANUAL.voice===v.id?"pri":""}"
      style="text-align:left;${v.status==="failed"?"opacity:.62":""}" ${busy?"disabled":""}
      title="${esc(v.status_error||"")}" onclick="previewVoiceAt(${i})">▶ ${v.status==="ok"?"✓ ":v.status==="failed"?"✕ ":""}${esc(v.name)}</button>`).join("")}
    ${vs.length?"":`<div class="hint">Đang nạp catalog giọng…</div>`}</div>`:"";
  return `<label style="display:flex;gap:7px;align-items:center;margin-top:7px">
      <input type="checkbox" ${STORY.auto_voice?"checked":""}
        onchange="STORY.auto_voice=this.checked;localStorage.setItem('advn_auto_voice',this.checked?'1':'0')">
      <span>Tự phân tích truyện và chọn giọng kể phù hợp</span></label>
    <label style="display:flex;gap:7px;align-items:center;margin-top:7px">
      <input type="checkbox" ${STORY.multi_voice?"checked":""}
        onchange="STORY.multi_voice=this.checked;localStorage.setItem('advn_multi_voice',this.checked?'1':'0');renderStoryPanel()">
      <span><b>Đa giọng:</b> mỗi nhân vật một giọng, lời dẫn giữ giọng kể</span></label>
    <div class="rowbtns" style="margin-top:7px">
      <button class="btn" ${busy||VOICE_RECS.loading?"disabled":""} onclick="analyseStoryVoice()">
        ${VOICE_RECS.loading?"Đang phân tích…":"✨ Phân tích & đề xuất giọng"}</button>
      <button class="btn" onclick="VOICE_LIBRARY_OPEN=!VOICE_LIBRARY_OPEN;renderStoryPanel()">
        🎧 ${VOICE_LIBRARY_OPEN?"Đóng":"Mở"} thư viện ${vs.length} giọng</button>
    </div>${summary}${cards}${castHtml}${library}`;
}
async function analyseStoryVoice(){
  const ta=document.getElementById("manualText");
  if(ta) MANUAL.text=ta.value;
  if(!String(MANUAL.text||"").trim()) return toast("Hãy nạp nội dung truyện trước","warn");
  VOICE_RECS.loading=true; renderStoryPanel();
  try{
    const r=await api("/api/story/voice_recommendations",{
      text:MANUAL.text,engine:MANUAL.engine||"capcut",voice:MANUAL.voice||"",
      txt_path:MANUAL.script_path||"",max_character_voices:8});
    VOICE_RECS.analysis=r.analysis||null;
    VOICE_RECS.items=r.recommendations||[];
    VOICE_RECS.cast=r.cast||[];
    VOICE_RECS.coverage=+r.assignment_coverage||0;
    VOICE_RECS.engine=r.engine||MANUAL.engine;
    toast(`Đã chọn giọng kể và ${VOICE_RECS.cast.length} giọng nhân vật`,"ok");
  }catch(e){ toast(e.message||String(e),"err"); }
  finally{ VOICE_RECS.loading=false; renderStoryPanel(); }
}
async function chooseRecommendedVoice(i,play){
  const r=VOICE_RECS.items[i]; if(!r) return;
  MANUAL.engine=r.engine||VOICE_RECS.engine||MANUAL.engine;
  await loadManualVoices(false);
  MANUAL.voice=r.id;
  renderStoryPanel();
  if(play) ngheThuGiong("story");
}
function previewVoiceAt(i){
  const vs=VOICES[MANUAL.engine||"edge"]||[], v=vs[i]; if(!v) return;
  MANUAL.voice=v.id; renderStoryPanel(); ngheThuGiong("story");
}
async function previewCastVoice(i){
  const c=VOICE_RECS.cast[i]; if(!c||!c.voice_id) return;
  const old=MANUAL.voice;
  MANUAL.voice=c.voice_id;
  try{ await ngheThuGiong("story"); }
  finally{ MANUAL.voice=old; }
}

/* ---------- nghe thử giọng ----------
   Đối tượng Audio giữ ở biến JS, KHÔNG đặt trong panel: panel bị vẽ lại mỗi
   khi trạng thái đổi (1,2 giây một lần), thẻ <audio> nằm trong đó sẽ bị xoá
   giữa lúc đang phát. */
let _NT_AUDIO=null, _NT_URL="", _NT_BUSY=false;
function ngheThuTrangThai(msg,mau){
  document.querySelectorAll(".nthu").forEach(el=>{
    el.textContent=msg||"";
    el.style.color=mau||"var(--muted)";
  });
}
function ngheThuDung(){
  if(_NT_AUDIO){ try{_NT_AUDIO.pause();}catch(e){} }
  if(_NT_URL){ URL.revokeObjectURL(_NT_URL); _NT_URL=""; }
  _NT_AUDIO=null;
}
async function ngheThuGiong(nguon){
  if(_NT_BUSY) return;
  const dub=(nguon==="dub");
  const op=(PR&&PR.options)||{};
  const o=dub
    ? {engine:op.engine||"edge", voice:op.narrator_voice||"",
       pitch:op.narrator_pitch||"+0Hz", rate:op.base_rate||"+0%", text:""}
    : {engine:MANUAL.engine||"edge", voice:MANUAL.voice||"",
       pitch:MANUAL.pitch||"+0Hz", rate:MANUAL.rate||"+0%",
       text:String(MANUAL.text||"").slice(0,400)};
  if(!o.voice) return toast("Chưa chọn giọng đọc","warn");
  ngheThuDung();
  _NT_BUSY=true;
  ngheThuTrangThai("Đang đọc thử… (vài giây)");
  try{
    const r=await fetch("/api/nghe_thu?"+new URLSearchParams(o).toString());
    if(!r.ok){
      let msg="Không tạo được bản nghe thử";
      try{ msg=(await r.json()).error||msg; }catch(e){}
      throw new Error(msg);
    }
    _NT_URL=URL.createObjectURL(await r.blob());
    _NT_AUDIO=new Audio(_NT_URL);
    _NT_AUDIO.onended=()=>ngheThuTrangThai(
      o.text?"Xong — đó là giọng đọc truyện của bạn":"Xong — bấm lại để nghe lần nữa");
    await _NT_AUDIO.play();
    ngheThuTrangThai("Đang phát…","var(--accent)");
  }catch(e){
    toast(e.message||String(e),"err");
    ngheThuTrangThai("");
  }finally{ _NT_BUSY=false; }
}
function nutNgheThu(nguon){
  return `<div class="rowbtns" style="align-items:center;margin:6px 0 4px">
    <button class="btn" onclick="ngheThuGiong('${nguon}')">🔊 Nghe giọng đang chọn</button>
    <button class="btn sm" style="flex:0 0 auto" title="Dừng phát"
      onclick="ngheThuDung();ngheThuTrangThai('')">■</button></div>
  <div class="nthu" style="font-size:11px;color:var(--muted);margin:0 0 8px"></div>`;
}
function updateManualCharCount(){
  const n=(MANUAL.text||"").length;
  const out=document.getElementById("manualCharCount");
  if(out){
    out.textContent=n.toLocaleString("vi-VN")+" k\u00fd t\u1ef1";
    out.style.color=n>200000?"var(--red)":"var(--muted)";
  }
}
function applyManualTextFile(file){
  const text=String((file&&file.text)||"").replace(/^\uFEFF/,"");
  if(!text.trim()) return toast("File v\u0103n b\u1ea3n \u0111ang tr\u1ed1ng","warn");
  if(text.length>200000)
    return toast("V\u0103n b\u1ea3n v\u01b0\u1ee3t qu\u00e1 200.000 k\u00fd t\u1ef1; h\u00e3y chia th\u00e0nh nhi\u1ec1u file","warn");
  MANUAL.text=text;
  if(!String(MANUAL.name||"").trim() && file.name) MANUAL.name=file.name;
  MANUAL.status=`\u0110\u00e3 n\u1ea1p ${file.filename||"file v\u0103n b\u1ea3n"} \u00b7 ${text.length.toLocaleString("vi-VN")} k\u00fd t\u1ef1`;
  MANUAL.error="";
  rerenderMode();
  toast("\u0110\u00e3 n\u1ea1p n\u1ed9i dung t\u1eeb file","ok");
}
async function pickManualText(){
  if(DESK()){
    try{
      const r=await pywebview.api.pick_text();
      if(!r) return;
      if(r.error) return toast(r.error,"err");
      applyManualTextFile(r);
    }catch(e){ toast(e.message||String(e),"err"); }
    return;
  }

  const input=document.createElement("input");
  input.type="file";
  input.accept=".txt,.md,text/plain,text/markdown";
  input.onchange=async()=>{
    const f=input.files&&input.files[0];
    if(!f) return;
    if(f.size>10*1024*1024) return toast("File v\u0103n b\u1ea3n qu\u00e1 l\u1edbn (t\u1ed1i \u0111a 10 MB)","warn");
    try{
      const text=await f.text();
      applyManualTextFile({text,name:f.name.replace(/\.[^.]+$/,""),filename:f.name});
    }catch(e){ toast("Kh\u00f4ng \u0111\u1ecdc \u0111\u01b0\u1ee3c file v\u0103n b\u1ea3n","err"); }
  };
  input.click();
}
async function createManualAudio(){
  const ta=document.getElementById("manualText");
  if(ta) MANUAL.text=ta.value;
  if(!MANUAL.text.trim()) return toast("Hãy nhập văn bản cần đọc","warn");
  try{
    await api("/api/manual/tts",{
      text:MANUAL.text,name:MANUAL.name,engine:MANUAL.engine,
      voice:MANUAL.voice,pitch:MANUAL.pitch,rate:MANUAL.rate,
      txt_path:MANUAL.script_path||"",
      voice_auto:STORY.auto_voice,multi_voice:STORY.multi_voice,
      max_character_voices:8
    });
    ST.manual={...(ST.manual||{}),working:true};
    MANUAL.status="Đang tổng hợp giọng…"; MANUAL.error="";
    rerenderMode(); toast("Đã bắt đầu tạo file MP3","ok");
  }catch(e){ toast(e.message,"err"); }
}
async function pickManualAudio(){
  let path="";
  if(DESK()){
    const r=await pywebview.api.pick_audio();
    if(r&&r.error) return toast(r.error,"err");
    path=Array.isArray(r)?(r[0]||""):(r||"");
  }else{
    path=prompt("Dán đường dẫn file MP3/WAV/M4A:")||"";
  }
  if(!path) return;
  try{
    const r=await api("/api/manual/use_audio",{path});
    MANUAL.audio_path=r.path||path;
    MANUAL.audio_duration=+r.duration||0;
    MANUAL.output_path="";
    MANUAL.status="Đã chọn audio có sẵn";
    rerenderMode(); toast("Đã chọn file âm thanh","ok");
  }catch(e){ toast(e.message,"err"); }
}
async function muxManualAudio(){
  if(!JID||!PR) return toast("Hãy chọn video cần ghép","warn");
  if(!MANUAL.audio_path) return toast("Hãy tạo hoặc chọn audio trước","warn");
  try{
    await saveNow();
    await api("/api/manual/mux",{id:JID,audio_path:MANUAL.audio_path});
    ST.manual={...(ST.manual||{}),working:true};
    MANUAL.status="Đang xuất video…"; MANUAL.error="";
    rerenderMode(); toast("Đã bắt đầu ghép audio vào video","ok");
  }catch(e){ toast(e.message,"err"); }
}
async function loadNhacNen(thongBao){
  try{
    const r=await fetch("/api/nhac_nen").then(x=>x.json());
    if(r.error) throw new Error(r.error);
    NHAC_LIST=r.bai||[];
    rerenderMode();
    if(thongBao) toast(`Kho nhạc có ${NHAC_LIST.length} bài`,"ok");
  }catch(e){ if(thongBao) toast(e.message||String(e),"err"); }
}
async function taiNhacNen(){
  try{
    await api("/api/nhac_nen/tai",{so_bai:(NHAC_LIST.length||0)+3});
    toast("Đang tìm và tải nhạc CC0… xem thanh tiến độ dưới cùng","ok");
    setTimeout(()=>loadNhacNen(true),9000);
  }catch(e){ toast(e.message,"err"); }
}
async function tronNhacNen(){
  if(!MANUAL.audio_path) return toast("Hãy tạo giọng đọc trước","warn");
  try{
    await api("/api/manual/nhac_nen",{
      audio_path:MANUAL.audio_path,bai:MANUAL.nhac_bai,
      muc_db:MANUAL.nhac_db,duck:MANUAL.nhac_duck
    });
    ST.manual={...(ST.manual||{}),working:true};
    MANUAL.status="Đang trộn nhạc nền…"; MANUAL.error="";
    rerenderMode(); toast("Đã bắt đầu trộn nhạc nền","ok");
  }catch(e){ toast(e.message,"err"); }
}
async function pickAnh(){
  let path="";
  if(DESK()){
    try{
      const r=await pywebview.api.pick_folder();
      if(r&&r.error) return toast(r.error,"err");
      path=Array.isArray(r)?(r[0]||""):(r||"");
    }catch(e){ return toast(e.message||String(e),"err"); }
  }else{
    path=prompt("Dán đường dẫn thư mục chứa ảnh:")||"";
  }
  if(!path) return;
  MANUAL.anh=(MANUAL.anh?MANUAL.anh.replace(/\s+$/,"")+"\n":"")+path;
  renderPanel();
}
async function dungVideoTuAnh(){
  const ta=document.getElementById("manualAnh");
  if(ta) MANUAL.anh=ta.value;
  const list=String(MANUAL.anh||"").split(/[\r\n]+/).map(s=>s.trim()).filter(Boolean);
  if(!list.length) return toast("Hãy chọn ảnh hoặc thư mục ảnh","warn");
  if(!MANUAL.audio_path) return toast("Hãy tạo giọng đọc trước","warn");
  try{
    await api("/api/manual/slideshow",{
      audio_path:MANUAL.audio_path,anh:list,
      kieu:MANUAL.slide_kieu,name:MANUAL.name
    });
    ST.manual={...(ST.manual||{}),working:true};
    MANUAL.status="Đang dựng video từ ảnh…"; MANUAL.error="";
    rerenderMode(); toast("Đã bắt đầu dựng video từ ảnh","ok");
  }catch(e){ toast(e.message,"err"); }
}
async function openManualFolder(kind){
  const path=kind==="output"?MANUAL.output_path:MANUAL.audio_path;
  if(!path) return;
  await openOut(path);
}
async function prefetch(){
  try{ await api("/api/prefetch",{}); toast("Đang tải model về máy… xem thanh tiến độ dưới cùng"); }
  catch(e){ toast(e.message,"err"); }
}
async function cleanupTemp(){
  try{
    const r=await api("/api/cleanup_temp",{});
    const gb=((r.bytes||0)/1073741824).toFixed(2);
    const free=((r.free||0)/1073741824).toFixed(2);
    toast(`Đã dọn ${r.files||0} file tạm, giải phóng ${gb} GB. Còn trống ${free} GB.`,"ok");
  }catch(e){ toast(e.message,"err"); }
}
function setSt(k,v){ PR.sub_style[k]=v; save(); draw(); renderPanel(); }
function setOutline(v){
  const [o,s]=v.split(",").map(Number);
  PR.sub_style.outline=o; PR.sub_style.shadow=s; save(); draw(); renderPanel();
}
function setOpt(k,v){ PR.options[k]=v; save(); renderPanel(); }
function setOriginalDb(v){
  if(!PR) return;
  PR.options.keep_original_db=Math.max(-60,Math.min(0,+v||-60));
  PR.options.keep_original_muted=false;
  PR.options.keep_original_volume=null;
  save(); renderPanel();
}
function setOriginalMuted(on){
  if(!PR) return;
  PR.options.keep_original_muted=!!on;
  PR.options.keep_original_volume=null;
  if(!on && PR.options.keep_original_db==null) PR.options.keep_original_db=-30;
  save(); renderPanel();
}
function setRgn(k,v){ if(ACT&&ACT.kind==="rgn"){PR.regions[ACT.i][k]=v;save();draw();renderPanel();} }
function setRgnAt(i,k,v){
  if(!PR||!PR.regions||!PR.regions[i]) return;
  if(v===""||v===null||Number.isNaN(Number(v))) delete PR.regions[i][k];
  else PR.regions[i][k]=Math.max(0,Math.round(Number(v)*10)/10);
  save(); draw(); renderPanel();
}
function setBoxCenter(cx){
  const b=PR.sub_style.box; b.x=Math.max(0,Math.min(PR.w-b.w,Math.round(cx-b.w/2)));
  save(); draw(); renderPanel();
}
function setBoxBottom(y){
  const b=PR.sub_style.box; b.y=Math.max(0,Math.min(PR.h-b.h,Math.round(y-b.h)));
  save(); draw(); renderPanel();
}
function resetSubBox(){
  PR.sub_style.box={x:Math.round(PR.w*0.08),y:Math.round(PR.h*0.44),
                    w:Math.round(PR.w*0.84),h:Math.round(PR.h*0.12)};
  PR.sub_style.align="mid-center"; save(); draw(); renderPanel();
}

/* ---------- sửa từng dòng ---------- */
function renderLines(){
  const el=document.getElementById("lines"); if(!el) return;
  const s=segs();
  if(!s.length){ el.innerHTML=`<div class="hint">Chưa có phụ đề.
    Sang thẻ <b>Nhận dạng</b> để bốc sub từ video.</div>`; return; }
  const ci=curIndex(), from=Math.max(0,ci-2);
  el.innerHTML=`<div class="lhd"><div class="sect" style="margin:0">SỬA TỪNG DÒNG</div>
    <span class="stat">${s.length} dòng</span></div>`+
    s.slice(from,from+14).map((x,k)=>{const i=from+k;return `
    <div class="lrow ${i===ci?"cur":""}" data-i="${i}">
      <div class="lmeta"><span class="lid">${String(i+1).padStart(4,"0")}</span>
        ${fmtms(x.start)} — ${fmtms(x.end)}
        <span class="lplay" onclick="V_seek(${x.start})">▶ nghe</span></div>
      ${x.src?`<div class="lsrc" title="${esc(x.src)}">${esc(x.src)}</div>`:""}
      <input value="${esc(x.vi||"")}" placeholder="bản dịch tiếng Việt…"
        onchange="PR.segments[${i}].vi=this.value;save();draw()">
    </div>`}).join("");
}
function renderLinesHighlight(){
  const ci=curIndex();
  document.querySelectorAll("#lines .lrow").forEach(r=>
    r.classList.toggle("cur",+r.dataset.i===ci));
}
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/'/g,"&#39;").replace(/`/g,"&#96;");}
function fmtms(s){
  s=s||0; const m=Math.floor(s/60), x=(s%60).toFixed(3);
  return `${String(Math.floor(m/60)).padStart(2,"0")}:${String(m%60).padStart(2,"0")}:${x.padStart(6,"0")}`;
}
function V_seek(t){ V().currentTime=t; tick(); }

/* ======================= chạy pipeline ======================= */
async function run(steps){
  if(!JID) return toast("Chưa chọn video","warn");
  try{ await saveNow(); }catch(e){ toast("Lỗi lưu project: "+e.message,"err"); return; }
  if(!(await saveTrCfg(false))) return;
  try{ await api("/api/run",{id:JID,steps}); toast("Đã bắt đầu: "+steps.join(" → ")); }
  catch(e){ toast(e.message,"err"); }
}
function runAll(){
  if(MODE==="story") return storyRunAll();
  run(["asr","translate","tts","render"]);
}
async function cancelRun(){
  if(_cancelPending) return;
  _cancelPending=true;
  if(ST.manual&&ST.manual.working){
    MANUAL.status="Đang dừng tác vụ…";
    MANUAL.error="";
    ST.manual={...ST.manual,status:MANUAL.status};
    rerenderMode();
  }
  try{
    const r=await api("/api/cancel",{});
    toast(r&&r.active!==false?"Đang dừng an toàn…":"Hiện không có tác vụ đang chạy","warn");
  }catch(e){
    _cancelPending=false;
    toast(e.message||String(e),"err");
  }
}
function cancelStoryRun(){ return cancelRun(); }

/* ======================= đồng bộ trạng thái ======================= */
async function refresh(){
  try{ ST=await api("/api/state"); }catch(e){ return; }
  const manualState=ST.manual||{};
  const manualFinished=_manualWorking&&!manualState.working;
  _manualWorking=!!manualState.working;
  if(!manualState.working&&!ST.running) _cancelPending=false;
  if(manualState.rev!=null && manualState.rev!==_manualRev){
    _manualRev=manualState.rev;
    MANUAL.audio_path=manualState.audio_path||MANUAL.audio_path||"";
    MANUAL.audio_duration=+manualState.audio_duration||0;
    MANUAL.output_path=manualState.output_path||"";
    MANUAL.calendar_path=manualState.calendar_path||MANUAL.calendar_path||"";
    MANUAL.status=manualState.status||"Sẵn sàng";
    MANUAL.error=manualState.error||"";
    const oldScriptTitle=String(MANUAL.script_title||"");
    const nextScriptTitle=String(manualState.script_title||"");
    const uiStoryTitle=String(MANUAL.writer_title||"").trim();
    const stateMatchesUi=!uiStoryTitle||!nextScriptTitle||
      storyTitleKey(uiStoryTitle)===storyTitleKey(nextScriptTitle);
    MANUAL.image_status=stateMatchesUi?
      (manualState.image_generation_status||""):"";
    if(stateMatchesUi&&(manualState.image_pack_path||
                       Number(manualState.image_scene_count||0)>0)){
      MANUAL.image_ready=Number(manualState.image_ready_count||0);
      MANUAL.image_total=Number(manualState.image_scene_count||0);
    }else{
      // Backend xoa gói ảnh khi bắt đầu tiêu đề mới. Không giữ lại bộ đếm
      // của truyện cũ, nếu không nút chính có thể hiểu nhầm là đang resume.
      MANUAL.image_ready=0;
      MANUAL.image_total=0;
    }
    if(Array.isArray(manualState.source_results))
      STORY.source_results=manualState.source_results;
    if(Array.isArray(manualState.source_videos))
      STORY.source_videos=manualState.source_videos;
    if(Array.isArray(manualState.source_clips))
      STORY.source_clips=manualState.source_clips;
    if(Array.isArray(manualState.source_links))
      STORY.source_links=manualState.source_links.join("\n");
    STORY.source_keyword=String(manualState.source_keyword||STORY.source_keyword||"");
    STORY.source_status=String(manualState.source_status||"");
    STORY.cut_status=String(manualState.cut_status||"");
    STORY.cut_done=Number(manualState.cut_done||0);
    STORY.cut_total=Number(manualState.cut_total||0);
    // STATE luôn có các khóa này (kể cả khi giá trị rỗng), vì vậy không được
    // dùng fallback sang script cũ khi backend vừa bắt đầu một tiêu đề mới.
    MANUAL.script_path=String(manualState.script_path||"");
    MANUAL.script_words=+manualState.script_words||0;
    const titleStillFollowsScript=!MANUAL.writer_title||
      storyTitleKey(MANUAL.writer_title)===storyTitleKey(oldScriptTitle);
    MANUAL.script_title=nextScriptTitle;
    if(manualState.content_idea_id!==undefined)
      MANUAL.content_idea_id=String(manualState.content_idea_id||"");
    if(manualState.youtube_description!==undefined)
      MANUAL.youtube_description=String(manualState.youtube_description||"");
    if(Array.isArray(manualState.youtube_tags))
      MANUAL.youtube_tags=manualState.youtube_tags;
    if(manualState.content_outline!==undefined)
      MANUAL.content_outline=String(manualState.content_outline||"");
    if(manualState.rewrite_brief!==undefined)
      MANUAL.rewrite_brief=String(manualState.rewrite_brief||"");
    // Không được kéo tiêu đề cũ từ tác vụ nền đè lên tiêu đề mới người dùng
    // vừa nhập. Chỉ đồng bộ khi ô tiêu đề vẫn đang bám theo script hiện tại.
    if(nextScriptTitle&&titleStillFollowsScript)
      MANUAL.writer_title=nextScriptTitle;
    if(Array.isArray(manualState.voice_recommendations)&&manualState.voice_recommendations.length){
      VOICE_RECS.items=manualState.voice_recommendations;
      VOICE_RECS.analysis=manualState.voice_analysis||null;
      VOICE_RECS.engine=manualState.voice_recommendations[0].engine||MANUAL.engine;
    }
    if(Array.isArray(manualState.voice_cast)){
      VOICE_RECS.cast=manualState.voice_cast;
      VOICE_RECS.coverage=+manualState.voice_assignment_coverage||0;
      if(manualState.voice_cast.length) VOICE_RECS.engine=MANUAL.engine;
    }
    if(manualState.recommended_voice){
      MANUAL.voice=manualState.recommended_voice;
      if(VOICE_RECS.engine) MANUAL.engine=VOICE_RECS.engine;
    }
    if(manualState.nhac_nen) MANUAL.nhac_ten=manualState.nhac_nen;
    if(MANUAL.script_path && MANUAL.script_path!==_lastScriptPath){
      _lastScriptPath=MANUAL.script_path;
      const generated=await api("/api/story/generated_script").catch(()=>null);
      const scriptBelongsToUi=!MANUAL.writer_title||!nextScriptTitle||
        storyTitleKey(MANUAL.writer_title)===storyTitleKey(nextScriptTitle);
      if(generated&&generated.text&&scriptBelongsToUi){
        MANUAL.text=generated.text;
        MANUAL.script_words=+generated.words||MANUAL.script_words;
        MANUAL.writer_title=generated.title||MANUAL.writer_title;
        if(!MANUAL.name) MANUAL.name=generated.title||"";
        toast(`Đã tự nạp kịch bản ${MANUAL.script_words.toLocaleString("vi-VN")} từ`,"ok");
      }
    }
    const imagePack=String(manualState.image_pack_path||"");
    const imageReady=Number(manualState.image_ready_count||0);
    const promptWaiting=!!manualState.image_prompt_ready;
    if(imagePack&&(imagePack!==STORY.pack||imageReady!==_lastImageReadyCount||
                    (promptWaiting&&imagePack!==_lastPromptPack))){
      const shouldOpen=promptWaiting&&imagePack!==_lastPromptPack;
      const pack=await api("/api/story/image_pack",{
        manifest_path:imagePack,include_prompt:shouldOpen}).catch(()=>null);
      if(pack){
        const stateTitle=String(manualState.script_title||"");
        const packTitle=String(pack.title||"");
        const uiTitle=String(MANUAL.writer_title||"").trim();
        const titleMatchesUi=!uiTitle||!stateTitle||
          storyTitleKey(uiTitle)===storyTitleKey(stateTitle);
        const belongsToState=titleMatchesUi&&(!stateTitle||
          (!!packTitle&&storyTitleKey(packTitle)===storyTitleKey(stateTitle)));
        if(belongsToState){
          STORY.pack=pack.manifest_path||imagePack;
          STORY.packTitle=packTitle||MANUAL.writer_title||"";
          localStorage.setItem("advn_story_image_pack",STORY.pack);
          if(Array.isArray(pack.images)) STORY.imgs=pack.images;
          _lastImageReadyCount=imageReady;
          if(shouldOpen){
            _lastPromptPack=imagePack;
            await showStoryPrompt(pack,true);
          }
        }else if(uiTitle&&stateTitle&&
                  storyTitleKey(uiTitle)!==storyTitleKey(stateTitle)){
          // Người dùng đã nhập tiêu đề mới trong khi project cũ còn dang dở.
          // Không cho prompt/gói ảnh của project cũ lọt vào UI mới.
          if(STORY.pack===imagePack||
             storyTitleKey(STORY.packTitle)===storyTitleKey(stateTitle)){
            STORY.pack=""; STORY.packTitle=""; STORY.imgs=[]; STORY.sel=-1;
            localStorage.removeItem("advn_story_image_pack");
            renderStoryImgs(); renderStoryStage();
          }
          _lastImageReadyCount=imageReady;
          _lastPromptPack=imagePack;
        }
      }
    }
    rerenderMode();
  }
  if(manualFinished){
    const stopped=/dừng/i.test(String(manualState.status||""));
    toast(manualState.error||manualState.status||"Đã xử lý xong",
          manualState.error?"err":stopped?"warn":"ok");
  }
  const videoToolsState=ST.video_tools||{};
  if(videoToolsState.rev!=null&&videoToolsState.rev!==_videoToolsRev){
    _videoToolsRev=videoToolsState.rev;
    VIDEO_TOOLS.server=videoToolsState;
    if(Array.isArray(videoToolsState.download_files))
      VIDEO_TOOLS.download_files=videoToolsState.download_files;
    if(Array.isArray(videoToolsState.search_results))
      VIDEO_TOOLS.search_results=videoToolsState.search_results;
    if(videoToolsState.search_keyword)
      VIDEO_TOOLS.search_keyword=videoToolsState.search_keyword;
    if(videoToolsState.search_provider)
      VIDEO_TOOLS.search_provider=videoToolsState.search_provider;
    if(Array.isArray(videoToolsState.cut_sources))
      VIDEO_TOOLS.cut_inputs=videoToolsState.cut_sources;
    if(Array.isArray(videoToolsState.cut_files))
      VIDEO_TOOLS.cut_files=videoToolsState.cut_files;
    if(videoToolsState.download_output_dir){
      VIDEO_TOOLS.download_output=videoToolsState.download_output_dir;
      localStorage.setItem("advn_vt_download_output",VIDEO_TOOLS.download_output);
    }
    if(videoToolsState.cut_output_dir){
      VIDEO_TOOLS.cut_output=videoToolsState.cut_output_dir;
      localStorage.setItem("advn_vt_cut_output",VIDEO_TOOLS.cut_output);
    }
    if(MODE==="tools") renderVideoTools();
  }
  const ideaState=ST.content_pipeline||{};
  const ideaFinished=_ideasWorking&&!ideaState.working;
  if(ideaState.working&&ideaState.active) _ideasLastActive=ideaState.active;
  _ideasWorking=!!ideaState.working;
  if(ideaState.rev!=null&&ideaState.rev!==_ideasRev){
    _ideasRev=ideaState.rev;
    IDEAS.server=ideaState;
    if(Array.isArray(ideaState.search_results)){
      const selected=new Map(IDEAS.sourceResults.map(x=>[x.url,x.selected!==false]));
      IDEAS.sourceResults=ideaState.search_results.map(x=>({...x,
        selected:selected.has(x.url)?selected.get(x.url):x.selected!==false}));
    }
    if(ideaState.search_keyword) IDEAS.onlineKeyword=ideaState.search_keyword;
    if(MODE==="ideas"){renderIdeaStatus();renderIdeaSourceResults();}
  }
  // Khi API đang chờ phản hồi, backend có thể chưa phát sinh rev mới. Vẫn vẽ
  // lại để đồng hồ "đã chờ" chạy, giúp phân biệt đang làm với bị treo.
  else if(MODE==="ideas"&&ideaState.working) renderIdeaStatus();
  if(ideaFinished){
    await ideasLoad(true);
    if(_ideasLastActive==="source_download"&&Number(ideaState.download_success)>0)
      setIdeasTab("library");
    toast(ideaState.error||ideaState.status||"Đã xử lý kho ý tưởng",
          ideaState.error?"err":"ok");
    _ideasLastActive="";
  }
  const readyDownloads=[];
  const failedDownloads=[];
  for(const j of (ST.queue||[])){
    const old=_queuePrev[j.id];
    if(old&&old.status==="đang tải"&&j.status!=="đang tải"){
      if(j.status==="lỗi") failedDownloads.push(j);
      else if(j.path) readyDownloads.push(j);
    }
  }
  document.getElementById("cuda").innerHTML = ST.nvenc?"<i></i> CUDA sẵn sàng":"<i></i> chạy CPU";
  const topGpu=document.getElementById("topGpu");
  if(topGpu) topGpu.innerHTML=`<span>▧</span> GPU ${ST.nvenc?"NVENC":"CPU"} <i></i>`;
  document.getElementById("qcount").textContent=ST.queue.length+" mục";
  // Download badge
  const dlActive = (ST.queue||[]).filter(x=>x.status==="đang tải").length;
  const dlWaiting = (ST.queue||[]).filter(x=>x.status==="chờ tải").length;
  const dlBadge = document.getElementById("dlcount");
  if(dlBadge){
    if(dlActive + dlWaiting > 0){
      dlBadge.textContent = `⇩ ${dlActive}${dlWaiting ? " +" + dlWaiting : ""}`;
      dlBadge.style.display = "";
    } else {
      dlBadge.style.display = "none";
    }
  }
  const q=document.getElementById("queue");
  q.innerHTML=ST.queue.map(j=>{
    const cls=j.status==="xong"?"done":j.status==="lỗi"?"err":
              (j.status==="dang chay"||j.status==="đang tải")?"run":"";
    const jpct=Number(j.progress||0);
    const pct=j.status==="xong"?100:
      (j.status==="đang tải"?Math.max(5,jpct):
       (j.id===ST.selected&&ST.running?(ST.progress.pct||0):jpct));
    const note=esc(j.note||j.status);
    return `<div class="qitem ${j.id===ST.selected?"sel":""}" onclick="selectJob(${j.id})">
      <div class="qx" onclick="delJob(${j.id},event)">×</div>
      <div class="qtop"><span class="qdot ${cls}"></span>
        <span class="qname" title="${esc(j.name)}">${esc(j.name)}</span></div>
      <div class="qbar"><i style="width:${pct}%"></i></div>
      <div class="qfoot">${j.output?`<span class="lplay" style="margin-right:auto"
         onclick="event.stopPropagation();openOut('${esc(j.output).replace(/\\/g,"\\\\")}')"
         >📂 mở thư mục</span>`:""}<span class="qstatus" title="${note}">${note}</span></div></div>`;
  }).join("")||`<div class="hint" style="margin:8px 4px">Hàng đợi trống.</div>`;

  const p=ST.progress||{};
  document.getElementById("pstep").textContent= ST.busy ? ST.busy :
    (ST.running?`Bước ${p.sub||1}/${p.total||6} · ${p.step||""}`:(p.step||"Sẵn sàng"));
  document.getElementById("pdetail").textContent=p.detail||"";
  document.getElementById("ppct").textContent=Math.round(p.pct||0)+"%";
  document.querySelector("#pbar i").style.width=(p.pct||0)+"%";
  document.getElementById("runbtn").disabled=!!ST.running;
  document.getElementById("gpu").textContent=ST.nvenc?"NVENC":"CPU";

  /* Chỉ tải lại dự án khi máy chủ báo có thay đổi THẬT (số hiệu phiên bản đổi).
     Trước đây kéo cả dự án về mỗi 1.2 giây - phim dài thì vài MB JSON mỗi nhịp,
     đó là một trong các lý do cửa sổ treo. */
  if(JID && ST.rev!=null && ST.rev!==_lastRev){
    _lastRev=ST.rev;
    const np=await api("/api/project?id="+JID).catch(()=>null);
    if(np&&np.w){
      const box=PR&&PR.sub_style&&PR.sub_style.box;
      PR=np;
      if(box) PR.sub_style.box=box;      // giữ hộp người dùng đang kéo
      _ciHint=0; drawTracks(true); renderPanel(); draw();
    }
  }
  (ST.log||[]).slice(-1).forEach(l=>{
    if(l.kind==="err"&&l.msg!==window._lastErr){window._lastErr=l.msg;toast(l.msg,"err");}
  });
  for(const j of readyDownloads){
    toast("Tải xong: "+j.name,"ok");
    if(j.id===ST.selected) await selectJob(j.id);
  }
  for(const j of failedDownloads){
    toast(j.note||"Tải video lỗi","err");
  }
  _queuePrev=Object.fromEntries((ST.queue||[]).map(j=>[j.id,{
    status:j.status,path:j.path,note:j.note
  }]));
}

/* ======================= khởi động ======================= */
V().addEventListener("timeupdate",tick);
V().addEventListener("seeked",tick);
/* Click vào vùng video (không phải box) = play/pause */
document.getElementById("vwrap").addEventListener("click",function(e){
  if(_isDragging) return;
  if(e.target.closest(".box")) return;
  if(e.target.id==="playOverlay"||e.target.closest("#playOverlay")) return;
  togglePlay();
});
window.addEventListener("resize",()=>{applyZoom();});
document.addEventListener("keydown",e=>{
  if(e.key==="Escape"&&document.getElementById("settingsModal")?.classList.contains("open")){
    closeSettings(); return;
  }
  if(e.target.tagName==="INPUT"||e.target.tagName==="SELECT"||
     e.target.tagName==="TEXTAREA"||e.target.isContentEditable) return;
  if(e.code==="Space"){e.preventDefault();togglePlay();}
  if(e.key==="ArrowLeft")jump(-5);
  if(e.key==="ArrowRight")jump(5);
  if(e.key==="Delete"&&ACT&&ACT.kind==="rgn"){PR.regions.splice(ACT.i,1);ACT=null;save();draw();renderPanel();}
});
/* Chạy trong trình duyệt (không phải app desktop) thì ẩn nút cửa sổ */
function initDesktop(){
  if(!DESK()){
    const w=document.getElementById("wbtns"); if(w) w.style.display="none";
    document.getElementById("titlebar").style.display="none";
  }
}
window.addEventListener("pywebviewready",initDesktop);
setTimeout(initDesktop,900);

/* ======================= CHẾ ĐỘ 2: VIDEO KỂ CHUYỆN ======================= */
let MODE=localStorage.getItem("advn_mode")||"dub";
if(!["dub","story","tools","ideas"].includes(MODE)) MODE="dub";
const VIDEO_TOOLS={
  tab:localStorage.getItem("advn_video_tools_tab")==="cut"?"cut":"download",
  links:"",
  search_keyword:"", search_provider:"all", search_count:10,
  search_results:[], search_selected:[],
  quality:localStorage.getItem("advn_vt_quality")||"480",
  download_output:localStorage.getItem("advn_vt_download_output")||"",
  download_files:[],
  cut_inputs:[],
  cut_output:localStorage.getItem("advn_vt_cut_output")||"",
  min_minutes:Math.max(.1,Number(localStorage.getItem("advn_vt_min_minutes"))||5),
  max_minutes:Math.max(.1,Number(localStorage.getItem("advn_vt_max_minutes"))||10),
  cut_files:[],
  server:{}
};
const DEFAULT_STORY_CTA="Bạn đang nghe chuyện tại gốc mít kể chuyện . Nếu thấy câu chuyện này ý nghĩa, cô chú, anh chị nhớ đăng ký kênh, bật chuông và để lại một lời bình luận để tiếp tục đồng hành cùng Gốc Mít nghen. Mọi nội dung đều hư cấu xin mọi người không làm theo bất cứ dưới hình thức nào hoặc tung tin đồn , chúng tôi không chịu trách nhiệm .";
const STORY_TITLE_PROMPT=`Bạn đặt tiêu đề video cho kênh audio Việt Nam "Gốc Mít Kể Chuyện", chủ đề chuyện gia đình, tuổi già và chuyện tâm linh ông bà kể, khán giả từ 45 tuổi trở lên.

NỘI DUNG TRUYỆN: [DÁN TÓM TẮT]

Hãy tạo 8 tiêu đề theo đúng công thức 3 phần:
[MÓC CẢM XÚC] : [MỆNH ĐỀ VIẾT HOA TOÀN BỘ] | [ĐUÔI TỪ KHÓA]

Móc cảm xúc chọn trong: Nghe Mà Thấm / Nghe THẤM Tận Xương / Nghe Là Khóc / Nghe Mà Nghẹn Lòng / Nghe Mà Rơi Nước Mắt / Nghe Sướng Lỗ Tai / Nghe Mà Sốc / Truyện Ngắn Tuổi Xế Chiều Cực Hay. Riêng chuyện tâm linh có thể dùng: Nghe Mà Lạnh Gáy / Chuyện Ông Bà Kể / Nghe Mà Rùng Mình / Chuyện Làng Quê Kỳ Bí

Mệnh đề viết hoa nêu đúng tình huống sốc nhất, 8 đến 14 từ, có số liệu nếu phù hợp.
Đuôi từ khóa chọn trong: Kể Chuyện Đêm Khuya / Đọc Truyện Đêm Khuya / Kể Chuyện Tuổi Già / Kể Chuyện Làng Quê / Kể Chuyện Tâm Linh

Tổng tiêu đề không quá 100 ký tự; không đưa tên kênh, số tập hoặc kết thúc. Trả về 8 dòng, không giải thích.`;
const STORY={
  imgs:[],                 // đường dẫn ảnh / thư mục, đúng thứ tự cảnh
  pack:localStorage.getItem("advn_story_image_pack")||"",
  packTitle:"",            // tiêu đề sở hữu gói ảnh; chặn mượn ảnh truyện trước
  sel:-1,                  // ảnh đang xem trước
  step:Math.max(0,Math.min(7,Number(localStorage.getItem("advn_story_step"))||0)),
  aspect:localStorage.getItem("advn_aspect")||"16:9",
  fps:30, kieu:"chuyen_dong",
  nhac_enabled:true, sub_enabled:true,
  auto_images:localStorage.getItem("advn_auto_images")!=="0",
  scene_count:Math.max(6,Math.min(14,Number(localStorage.getItem("advn_story_scene_count"))||8)),
  auto_voice:localStorage.getItem("advn_auto_voice")!=="0",
  multi_voice:localStorage.getItem("advn_multi_voice")!=="0",
  cta_enabled:localStorage.getItem("advn_story_cta_enabled")!=="0",
  cta_text:DEFAULT_STORY_CTA,
  cta_positions:localStorage.getItem("advn_story_cta_positions")||"12,55",
  cta_speed:Math.max(1,Math.min(2,Number(localStorage.getItem("advn_story_cta_speed"))||2)),
  logo_enabled:localStorage.getItem("advn_story_logo_enabled")==="1",
  logo_path:localStorage.getItem("advn_story_logo_path")||"",
  logo_position:localStorage.getItem("advn_story_logo_position")||"top-right",
  logo_width:Math.max(4,Math.min(40,Number(localStorage.getItem("advn_story_logo_width"))||12)),
  logo_opacity:Math.max(5,Math.min(100,Number(localStorage.getItem("advn_story_logo_opacity"))||82)),
  source_keyword:"", source_count:10, source_provider:"all", source_results:[], source_selected:[],
  source_links:"", source_videos:[], source_status:"", source_sel:0,
  source_clips:[], cut_status:"", cut_done:0, cut_total:0,
  source_effect:"tinh", source_cover:"none",
  source_clip_min_minutes:Math.max(.1,Number(localStorage.getItem("advn_source_clip_min"))||5),
  source_clip_max_minutes:Math.max(.1,Number(localStorage.getItem("advn_source_clip_max"))||10),
  source_random:localStorage.getItem("advn_source_random")!=="0",
  source_random_seed:Number(localStorage.getItem("advn_source_seed"))||Date.now(),
  source_zoom:Math.max(40,Math.min(220,Number(localStorage.getItem("advn_source_zoom"))||100)),
  source_x:Math.max(0,Math.min(100,Number(localStorage.getItem("advn_source_x"))||50)),
  source_y:Math.max(0,Math.min(100,Number(localStorage.getItem("advn_source_y"))||50)),
  source_crop_left:0, source_crop_right:0, source_crop_top:0, source_crop_bottom:0,
  character_enabled:localStorage.getItem("advn_story_character_enabled")==="1",
  character_scale:Math.max(.55,Math.min(1.8,Number(localStorage.getItem("advn_story_character_scale"))||1)),
  character_opacity:Math.max(25,Math.min(100,Number(localStorage.getItem("advn_story_character_opacity"))||92)),
  sub:{size:48,color:"#FFFFFF",outline:2,bold:true,
       align:"bottom-center",margin_v:90},
};
function rerenderMode(){
  if(MODE==="story") renderStory();
  else if(MODE==="tools") renderVideoTools();
  else if(MODE==="ideas") renderIdeas();
  else renderPanel();
}
function setMode(m){
  MODE=["dub","story","tools","ideas"].includes(m)?m:"dub";
  localStorage.setItem("advn_mode",MODE);
  document.body.dataset.mode=MODE;
  document.getElementById("storyMain").style.display=MODE==="story"?"grid":"none";
  document.getElementById("videoToolsMain").style.display=MODE==="tools"?"block":"none";
  document.getElementById("ideasMain").style.display=MODE==="ideas"?"block":"none";
  document.getElementById("mDub").classList.toggle("on",MODE==="dub");
  document.getElementById("mStory").classList.toggle("on",MODE==="story");
  document.getElementById("mVideoTools").classList.toggle("on",MODE==="tools");
  document.getElementById("mIdeas").classList.toggle("on",MODE==="ideas");
  if(MODE==="story"){ loadManualVoices(false); loadNhacNen(false); renderStory(); }
  else if(MODE==="tools") renderVideoTools();
  else if(MODE==="ideas"){
    const provider=document.getElementById("ideaProvider");
    if(provider) provider.value=IDEAS.provider||"browser";
    ideasLoadCatalog();
    ideasLoad(false);
  }
  else renderPanel();
}

/* ======================= kho ý tưởng & kế hoạch ======================= */
function setIdeasTab(tab){
  IDEAS.tab=tab==="library"?"library":"collect";
  localStorage.setItem("advn_ideas_tab",IDEAS.tab);
  const collect=document.getElementById("ideaCollectView"),library=document.getElementById("ideaLibraryView");
  if(collect) collect.style.display=IDEAS.tab==="collect"?"grid":"none";
  if(library) library.style.display=IDEAS.tab==="library"?"flex":"none";
  document.getElementById("ideaTabCollect")?.classList.toggle("on",IDEAS.tab==="collect");
  document.getElementById("ideaTabLibrary")?.classList.toggle("on",IDEAS.tab==="library");
  if(IDEAS.tab==="collect") renderIdeaSourceResults();
  else {renderIdeaList();renderIdeaDetail();}
}
function ideaSourceKeys(group){
  const chineseReal=["zhihu_yanxuan","douban_groups"];
  const chineseLiterary=["douban_read","hongxiu"];
  const chineseSerial=["fanqie","qidian","hongxiu","qimao","zongheng"];
  const chinese=[...new Set([...chineseReal,...chineseLiterary,...chineseSerial])];
  const groups={
    zhihu:["zhihu_yanxuan"],
    chinese_real:chineseReal,
    chinese_literary:chineseLiterary,
    chinese_serial:chineseSerial,
    chinese_folklore:["660i_story"],
    spiritual:["zhihu_yanxuan","douban_groups","660i_story"],
    vietnam:["vnexpress_tamsu","dantri_tinhyeu","webtretho_honnhan","phunuonline"],
    reddit:["reddit_aita"],
    chinese,
    all:[...chinese,"vnexpress_tamsu","dantri_tinhyeu","webtretho_honnhan","phunuonline","reddit_aita"]
  };
  return groups[group]||groups.zhihu;
}
async function ideasLoadCatalog(){
  if(IDEAS.catalogLoaded) return;
  try{
    const data=await api("/api/content/catalog");
    IDEAS.sources=Array.isArray(data.sources)?data.sources:[];
    IDEAS.chineseKeywords=Array.isArray(data.chinese_keywords)?data.chinese_keywords:[];
    IDEAS.catalogLoaded=true;
    const select=document.getElementById("ideaKeywordPreset");
    if(select){
      select.innerHTML=`<option value="">— Chọn chủ đề hoặc tự gõ bên dưới —</option>`+
        IDEAS.chineseKeywords.map((x,i)=>`<option value="${i}">${esc(x.keyword)} — ${esc(x.meaning)}</option>`).join("");
      if(IDEAS.keywordIndex>=0) select.value=String(IDEAS.keywordIndex);
    }
    renderIdeaKeywordInfo();
  }catch(e){toast("Không tải được danh mục nguồn: "+e.message,"err");}
}
function ideasChooseKeyword(value){
  const index=value===""?-1:Number(value);
  IDEAS.keywordIndex=Number.isFinite(index)?index:-1;
  const item=IDEAS.chineseKeywords[IDEAS.keywordIndex];
  if(item){
    IDEAS.onlineKeyword=item.keyword||"";
    const input=document.getElementById("ideaOnlineKeyword");if(input)input.value=IDEAS.onlineKeyword;
    if(item.source_group){
      IDEAS.sourceGroup=item.source_group;
      const group=document.getElementById("ideaSourceGroup");if(group)group.value=IDEAS.sourceGroup;
    }
  }
  renderIdeaKeywordInfo();
}
function renderIdeaKeywordInfo(){
  const box=document.getElementById("ideaKeywordInfo");if(!box)return;
  const x=IDEAS.chineseKeywords[IDEAS.keywordIndex];
  box.innerHTML=x?`<b>${esc(x.meaning)}</b> · Nhóm: ${esc(x.topic)}<br>${esc(x.note||"")}`:
    "Chọn một từ khóa để xem nghĩa và nhóm chủ đề.";
}
async function ideasSearchSources(){
  const keyword=String(document.getElementById("ideaOnlineKeyword")?.value||IDEAS.onlineKeyword||"").trim();
  if(!keyword) return toast("Hãy chọn hoặc nhập từ khóa tìm chuyện","warn");
  IDEAS.onlineKeyword=keyword;
  IDEAS.sourceGroup=document.getElementById("ideaSourceGroup")?.value||IDEAS.sourceGroup||"zhihu";
  const preset=IDEAS.chineseKeywords[IDEAS.keywordIndex]||{};
  const limit=Math.max(1,Math.min(50,Number(document.getElementById("ideaSearchLimit")?.value)||20));
  IDEAS.sourceResults=[];renderIdeaSourceResults();
  try{
    await api("/api/content/search",{keyword,limit,source_keys:ideaSourceKeys(IDEAS.sourceGroup),
      topic:preset.topic||"",meaning:preset.meaning||""});
    IDEAS.server={working:true,active:"source_search",status:`Đang tìm “${keyword}”…`,progress:0};
    _ideasLastActive="source_search";renderIdeaStatus();
  }catch(e){toast(e.message,"err");}
}
function ideasToggleSource(index,selected){
  const item=IDEAS.sourceResults[index];
  if(item&&!item.used_before&&!item.already_downloaded)
    item.selected=selected;
  renderIdeaSourceResults();
}
function ideasToggleAllSources(selected){
  IDEAS.sourceResults.forEach(x=>x.selected=selected&&!x.used_before&&!x.already_downloaded);
  renderIdeaSourceResults();
}
async function ideasOpenSource(encoded){
  const url=decodeURIComponent(String(encoded||""));if(!url)return;
  if(DESK()&&pywebview.api.open_url) await pywebview.api.open_url(url);
  else window.open(url,"_blank","noopener");
}
function renderIdeaSourceResults(){
  const box=document.getElementById("ideaSourceResults"),summary=document.getElementById("ideaResultSummary");
  if(!box)return;
  const rows=IDEAS.sourceResults||[], chosen=rows.filter(x=>x.selected===true).length;
  const usedCount=rows.filter(x=>x.used_before).length;
  const knownReads=rows.filter(x=>Number(x.read_count)>0).length;
  const errors=new Map((IDEAS.server?.download_errors||[]).map(x=>[x.url,x.error]));
  if(summary) summary.textContent=rows.length?`${rows.length} kết quả · ${knownReads} có lượt đọc · ${usedCount} đã dùng · ${chosen} đang chọn`:
    (IDEAS.server?.active==="source_search"&&IDEAS.server?.working?"Đang tìm trên các nguồn đã chọn…":"Chọn từ khóa và bấm Tìm nguồn nội dung.");
  box.innerHTML=rows.map((x,i)=>{
    const failed=errors.get(x.url)||x.fetch_error,done=x.download_status==="Đã tải";
    const blocked=x.used_before||x.already_downloaded;
    const reads=Number(x.read_count)||0,engagement=Number(x.engagement_count)||0;
    return `<article class="idea-source-row ${blocked?"blocked":""}" onclick="ideasToggleSource(${i},!${x.selected===true})">
      <input type="checkbox" ${x.selected===true?"checked":""} ${blocked?"disabled":""}
        onclick="event.stopPropagation()" onchange="ideasToggleSource(${i},this.checked)">
      <div><h4>${esc(x.title||"Bài chưa có tiêu đề")}</h4><p>${esc(x.excerpt||"Chưa có mô tả; hệ thống sẽ kiểm tra toàn văn khi tải.")}</p>
        <div class="idea-source-meta"><span>#${Number(x.popularity_rank)||i+1} ưu tiên</span>
          ${Number(x.relevance_score)>0?`<span class="relevance">Khớp chủ đề ${Number(x.relevance_score)}/100</span>`:""}<span>${esc(x.source_name||x.channel||"web")}</span>
          ${x.language?`<span>${esc(String(x.language).toUpperCase())}</span>`:""}
          ${x.narrative_style?`<span>${esc(x.narrative_style)}</span>`:""}
          <span class="${reads?"reads":"unknown"}">${reads?`◉ ${reads.toLocaleString("vi-VN")} lượt đọc`:
            engagement?`★ ${engagement.toLocaleString("vi-VN")} tương tác`:"Lượt đọc không công khai"}</span>
          ${x.used_before?`<span class="fail">Đã dùng · khóa lặp</span>`:x.already_downloaded?`<span class="done">Đã có trong kho</span>`:""}
          ${done?`<span class="done">Đã tải</span>`:""}${failed&&!x.used_before?`<span class="fail" title="${esc(failed)}">Chưa đọc được</span>`:""}</div></div>
      <div class="idea-source-row-actions">
        ${x.used_before?`<button class="btn sm" disabled>Đã dùng</button>`:
          x.already_downloaded?`<button class="btn sm" onclick="event.stopPropagation();ideasOpenExisting('${esc(x.existing_id||"")}')">Mở trong kho</button>`:
          `<button class="btn pri sm" onclick="event.stopPropagation();ideasDownloadOne(${i})">⇩ ${x.content_ready===false?"Thử tải với Cookie":"Tải chuyện này"}</button>`}
        <button class="btn sm idea-source-open" onclick="event.stopPropagation();ideasOpenSource('${encodeURIComponent(x.url||"")}')" title="Xem bài gốc">↗</button>
      </div>
    </article>`;
  }).join("")||`<div class="idea-source-empty"><div><b>Chưa có kết quả tìm kiếm</b>Chọn một từ khóa Trung ở cột trái hoặc tự nhập từ khóa.</div></div>`;
}
function ideaDownloadCookie(){return String(document.getElementById("ideaZhihuCookie")?.value||"").trim();}
async function ideasDownloadSelected(){
  const items=IDEAS.sourceResults.filter(x=>x.selected===true&&!x.used_before&&!x.already_downloaded);
  if(!items.length) return toast("Hãy chọn ít nhất một bài cần tải","warn");
  try{
    const r=await api("/api/content/download",{items,cookie:ideaDownloadCookie(),concurrency:3});
    IDEAS.server={working:true,active:"source_download",status:`Đang tải ${r.total} bài vào kho…`,
      total:r.total,done:0,progress:0,provider:r.provider,provider_model:r.provider_model,
      provider_configured:r.provider_configured,started_at:Date.now()/1000};
    _ideasLastActive="source_download";renderIdeaStatus();
  }catch(e){toast(e.message,"err");}
}
async function ideasDownloadOne(index){
  const item=IDEAS.sourceResults[index];
  if(!item||item.used_before||item.already_downloaded)return;
  item.selected=true;renderIdeaSourceResults();
  try{
    const r=await api("/api/content/download",{items:[item],cookie:ideaDownloadCookie(),concurrency:1});
    IDEAS.server={working:true,active:"source_download",status:"Đang đưa chuyện đã chọn vào kho…",
      total:r.total,done:0,progress:0,provider:r.provider,provider_model:r.provider_model,
      provider_configured:r.provider_configured,started_at:Date.now()/1000};
    _ideasLastActive="source_download";renderIdeaStatus();
  }catch(e){toast(e.message,"err");}
}
async function ideasOpenExisting(id){
  if(!id)return;setIdeasTab("library");await ideasSelect(id);
}
async function ideasDownloadUrls(){
  const urls=String(document.getElementById("ideaSourceUrls")?.value||"").split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
  if(!urls.length) return toast("Hãy dán ít nhất một link bài viết","warn");
  try{
    const r=await api("/api/content/download",{urls,cookie:ideaDownloadCookie(),concurrency:3});
    IDEAS.server={working:true,active:"source_download",status:`Đang tải ${r.total} link vào kho…`,
      total:r.total,done:0,progress:0,provider:r.provider,provider_model:r.provider_model,
      provider_configured:r.provider_configured,started_at:Date.now()/1000};
    _ideasLastActive="source_download";renderIdeaStatus();
  }catch(e){toast(e.message,"err");}
}
function ideaArray(value){
  if(Array.isArray(value)) return value.map(x=>String(x||"").trim()).filter(Boolean);
  return String(value||"").split(/[;,\n]+/).map(x=>x.trim()).filter(Boolean);
}
function ideaLines(value){
  return String(value||"").split(/\r?\n+/).map(x=>x.trim()).filter(Boolean);
}
function ideaWasUsed(item){
  if(!item)return false;
  return !!item.used_at||Number(item.usage_count)>0||["đã dùng","used","rendered","published"].includes(String(item.status||"").toLocaleLowerCase("vi"));
}
function ideaSelectedIds(){ return IDEAS.items.filter(x=>x.selected&&!ideaWasUsed(x)).map(x=>x.id); }
function ideaStoryTargetId(){
  const selected=ideaSelectedIds(),detailId=String(IDEAS.detail?.id||"");
  // Một ô được tick là chỉ định rõ ràng nhất của người dùng, kể cả khung chi
  // tiết chưa kịp đổi theo. Với nhiều ô, mục đang mở phải nằm trong số đã tick.
  if(selected.length===1) return selected[0];
  if(selected.length>1) return selected.includes(detailId)?detailId:"";
  return detailId;
}
function ideaStatusClass(item){
  const score=Number(item.priority_score)||0;
  return score>=7.2?"good":"";
}
async function ideasLoad(keepDetail){
  try{
    const data=await api("/api/content/list?limit=10000");
    IDEAS.items=Array.isArray(data.items)?data.items:[];
    IDEAS.loaded=true;
    if(!keepDetail&&!IDEAS.selectedId&&IDEAS.items.length)
      IDEAS.selectedId=IDEAS.items[0].id;
    if(IDEAS.selectedId&&!IDEAS.items.some(x=>x.id===IDEAS.selectedId)){
      IDEAS.selectedId=IDEAS.items[0]?.id||""; IDEAS.detail=null;
    }
    const visible=ideasFiltered();
    if(IDEAS.selectedId&&!visible.some(x=>x.id===IDEAS.selectedId)){
      IDEAS.selectedId=visible[0]?.id||"";IDEAS.detail=null;
    }
    if(IDEAS.selectedId&&(!IDEAS.detail||IDEAS.detail.id!==IDEAS.selectedId))
      await ideasSelect(IDEAS.selectedId,false);
    renderIdeas();
  }catch(e){ toast("Không đọc được kho ý tưởng: "+e.message,"err"); }
}
function ideasFiltered(){
  const q=String(IDEAS.search||"").toLocaleLowerCase("vi").trim();
  const usage=IDEAS.usageFilter||"unused";
  const usageRows=IDEAS.items.filter(x=>usage==="all"||(usage==="used"?ideaWasUsed(x):!ideaWasUsed(x)));
  if(!q) return usageRows;
  const tokens=q.split(/\s+/).filter(Boolean);
  return usageRows.filter(x=>{
    const haystack=[x.title_original,x.title_localized,x.source,
    x.primary_genre,x.emotion,x.series,(x.themes||[]).join(" ")]
      .join(" ").toLocaleLowerCase("vi");
    // Từ khóa dài như "婆媳矛盾 故事" vẫn tìm ra bài có phần chủ đề chính,
    // thay vì khiến người dùng tưởng kho trống vì thiếu đúng một từ phụ.
    return tokens.some(token=>haystack.includes(token));
  });
}
function renderIdeaStatus(){
  const box=document.getElementById("ideaStatus"); if(!box) return;
  const s=IDEAS.server||{};
  if(!s.working&&!s.status&&!s.error){box.innerHTML="";return;}
  const pct=Math.max(0,Math.min(100,Number(s.progress)||0));
  const providerNames={nvidia:"NVIDIA",zenmux:"ZenMux",gemini:"Gemini API",
    tokenrouter:"TokenRouter",heuristic:"Quy tắc local",offline:"Offline",
    browser:"Gemini Web",perplexity_browser:"Claude · Perplexity Web"};
  const providerKey=String(s.provider||"").toLowerCase();
  const providerName=providerNames[providerKey]||String(s.provider||"").toUpperCase();
  const providerText=providerName?providerName+(s.provider_model?` · ${esc(s.provider_model)}`:""):"";
  const stageNames={prepare:"Chuẩn bị",download:"Tải toàn văn",downloaded:"Đã tải nguồn",
    ai_sending:"Gửi yêu cầu AI",ai_waiting:"Chờ AI phản hồi",ai_parsing:"Kiểm tra phản hồi AI",
    ai_done:"AI hoàn tất",ai_warning:"AI chuyển offline",ai_error:"AI lỗi",
    offline:"Phân tích offline",saved:"Lưu SQLite",done:"Hoàn tất",error:"Có lỗi"};
  const stage=stageNames[String(s.current_stage||"")]||String(s.current_stage||"").replace(/^ai_/,"AI · ");
  const elapsed=s.working&&Number(s.started_at)>0?
    Math.max(0,Math.floor(Date.now()/1000-Number(s.started_at))):0;
  const elapsedText=elapsed>=60?`${Math.floor(elapsed/60)}p ${elapsed%60}s`:`${elapsed}s`;
  const activity=(Array.isArray(s.activity)?s.activity:[]).slice(-10);
  const logRows=activity.map(row=>{
    const time=Number(row.t)>0?new Date(Number(row.t)*1000).toLocaleTimeString("vi-VN",{hour12:false}):"";
    return `<div class="idea-log-row ${esc(row.kind||"info")}"><time>${esc(time)}</time><span>${esc(row.message||"")}</span></div>`;
  }).join("");
  const planOutput=s.export_xlsx?`<button class="btn sm" onclick="openFolder('${esc(s.output_dir||s.export_xlsx).replace(/\\/g,"\\\\")}')">Mở thư mục kế hoạch</button>`:"";
  const calendarOutput=s.calendar_path?`<button class="btn sm" onclick="openOut('${esc(s.calendar_path).replace(/\\/g,"\\\\")}')">Mở lịch nội dung Excel</button>`:"";
  const warning=String(s.warning||"");
  box.innerHTML=`<section class="idea-status-card ${s.error?"err":warning?"warn":""}">
    <div class="idea-status-main"><b>${s.working?"Đang xử lý":"Trạng thái"}</b>
      ${providerText?`<span class="idea-ai-chip ${s.provider_configured?"ready":"offline"}">Trình phân tích: ${providerText}</span>`:""}
      ${stage?`<span class="idea-stage-chip">${esc(stage)}</span>`:""}
      ${s.working?`<span class="idea-elapsed">Đã chạy ${elapsedText}</span>`:""}
      <strong>${Math.round(pct)}%</strong></div>
    <div class="idea-status-message">${esc(s.error||s.status||"Sẵn sàng")}${s.total?` · ${Number(s.done)||0}/${s.total}`:""}</div>
    ${s.current_item?`<div class="idea-current-item">Đang làm: ${esc(s.current_item)}</div>`:""}
    ${s.working?`<div class="idea-status-bar"><i style="width:${pct}%"></i></div>`:""}
    ${s.error?`<div class="idea-status-alert error">${esc(s.error)}</div>`:""}
    ${warning&&!s.error?`<div class="idea-status-alert warning"><span>${esc(warning)}</span><button class="btn sm" onclick="openSettings('api')">Mở Cài đặt API</button></div>`:""}
    ${(Number(s.ai_success)||Number(s.ai_failed))?`<div class="idea-ai-counts"><span>AI thành công: ${Number(s.ai_success)||0}</span><span>Offline/lỗi AI: ${Number(s.ai_failed)||0}</span></div>`:""}
    ${activity.length?`<details class="idea-live-log" ${s.working||s.error||warning?"open":""}><summary>Nhật ký tiến trình (${activity.length} dòng gần nhất)</summary><div>${logRows}</div></details>`:""}
    ${(calendarOutput||planOutput)?`<div class="idea-status-actions">${calendarOutput}${planOutput}</div>`:""}
  </section>`;
}
function renderIdeaList(){
  const list=document.getElementById("ideaList"), count=document.getElementById("ideaCount");
  if(!list) return;
  const rows=ideasFiltered();
  if(count) count.textContent=`${rows.length}/${IDEAS.items.length} mục · ${ideaSelectedIds().length} chọn`;
  document.getElementById("ideaClearSearch")?.classList.toggle("show",!!String(IDEAS.search||"").trim());
  list.innerHTML=rows.map(item=>{const used=ideaWasUsed(item);return `<article class="idea-card ${item.id===IDEAS.selectedId?"on":""} ${used?"used":""}" onclick="ideasSelect('${esc(item.id)}')">
    <input type="checkbox" ${item.selected?"checked":""} ${used?"disabled":""} onclick="event.stopPropagation()" onchange="ideasToggle('${esc(item.id)}',this.checked)">
    <div><h4>${esc(item.title_localized||item.title_original||"Chưa có tiêu đề")}</h4>
      <small>${esc(item.source||"manual")} · ${Number(item.word_count||0).toLocaleString("vi-VN")} từ</small>
      <div class="idea-card-meta">${used?`<span class="idea-pill used">Đã dùng ${item.used_at?`· ${esc(String(item.used_at).slice(0,10))}`:""}</span>`:""}<span class="idea-pill ${ideaStatusClass(item)}">${esc(item.recommendation||item.status||"Chưa phân tích")}</span>
      <span class="idea-pill idea-score">Hook ${item.hook_score||"–"} · Twist ${item.plot_twist_score||"–"}</span>
      ${item.primary_genre?`<span class="idea-pill">${esc(item.primary_genre)}</span>`:""}</div></div></article>`}).join("")||
      `<div class="idea-empty"><div><b>${IDEAS.items.length?"Không có mục trong bộ lọc này":"Kho chưa có nội dung"}</b>${IDEAS.items.length?
        (IDEAS.usageFilter==="used"?"Chưa có chuyện nào được ghi vào lịch sử sử dụng.":`Đổi bộ lọc hoặc bấm × để xem lại kho.`):"Sang tab Tìm & tải nội dung để lấy chuyện thật, hoặc nhập JSON/SQLite."}</div></div>`;
}
function ideaField(label,control,full){return `<div class="idea-field ${full?"full":""}"><label>${label}</label>${control}</div>`;}
function renderIdeaDetail(){
  const box=document.getElementById("ideaDetail"); if(!box) return;
  const x=IDEAS.detail;
  if(!x){box.innerHTML=`<div class="idea-empty"><div><b>Chọn một ý tưởng</b>Xem, chỉnh và duyệt kế hoạch trước khi sản xuất.</div></div>`;return;}
  const titles=(x.titles||[]).join("\n");
  const titleChoices=(x.titles||[]).map((title,index)=>
    `<option value="${index}">${index+1}. ${esc(title)}</option>`).join("");
  const descriptions=(x.descriptions||[]).join("\n---\n");
  const encodedId=encodeURIComponent(String(x.id||""));
  box.innerHTML=`<div class="idea-detail-head"><div><h3>${esc(x.title_localized||x.title_original)}</h3>
      <p>${esc(x.source||"manual")}${x.source_url?` · ${esc(x.source_url)}`:""}</p></div>
    <div class="idea-detail-actions"><button class="btn" onclick="ideasSave()">Lưu chỉnh sửa</button>
      <button class="btn" ${ideaWasUsed(x)?"disabled":""} onclick="ideasReloadSelected([decodeURIComponent('${encodedId}')])">↻ Tải lại</button>
      <button class="btn danger" ${ideaWasUsed(x)?"disabled":""} onclick="ideasDeleteSelected([decodeURIComponent('${encodedId}')])">🗑 Xóa</button>
      <button class="btn pri" ${ideaWasUsed(x)?"disabled":""} onclick="ideasUseStory()">${ideaWasUsed(x)?"✓ Đã dùng · khóa lặp":"▶ DÙNG ĐÚNG TRUYỆN NÀY → AI STORY"}</button></div></div>
    <div class="idea-use-target"><span>AI Story sẽ nhận:</span><b>${esc(x.title_localized||x.title_original)}</b><small>${esc(x.source||"manual")}</small></div>
    <div class="idea-metrics">
      <div class="idea-metric"><span>Hook</span><b class="accent">${x.hook_score||"–"}/10</b></div>
      <div class="idea-metric"><span>Plot Twist</span><b class="accent">${x.plot_twist_score||"–"}/10</b></div>
      <div class="idea-metric"><span>Ưu tiên</span><b>${x.priority_score||"–"}</b></div>
      <div class="idea-metric"><span>Thời lượng</span><b>${x.estimated_duration_minutes||"–"} phút</b></div>
      <div class="idea-metric"><span>Khuyến nghị</span><b>${esc(x.recommendation||"Chưa có")}</b></div>
    </div>
    <div class="idea-form">
      ${ideaField("Tiêu đề Việt / tên làm việc",`<input id="ideaTitleLocalized" value="${esc(x.title_localized||"")}">`)}
      ${ideaField("Tiêu đề YouTube sẽ đưa sang Perplexity",`<select id="ideaTitleChoice">${titleChoices||`<option value="0">Dùng tên làm việc</option>`}</select>`,true)}
      ${ideaField("Series",`<input id="ideaSeries" value="${esc(x.series||"")}">`)}
      ${ideaField("Thể loại chính",`<input id="ideaGenre" value="${esc(x.primary_genre||"")}">`)}
      ${ideaField("Cảm xúc chủ đạo",`<input id="ideaEmotion" value="${esc(x.emotion||"")}">`)}
      ${ideaField("Chủ đề (ngăn bằng dấu phẩy)",`<input id="ideaThemes" value="${esc((x.themes||[]).join(", "))}">`,true)}
      ${ideaField("Từ khóa",`<textarea id="ideaKeywords">${esc((x.keywords||[]).join(", "))}</textarea>`)}
      ${ideaField("Tags",`<textarea id="ideaTags">${esc((x.tags||[]).join(", "))}</textarea>`)}
      ${ideaField("8 tiêu đề AI đúng prompt — mỗi dòng một tiêu đề",`<textarea id="ideaTitles" class="tall">${esc(titles)}</textarea>`,true)}
      ${ideaField("3 mô tả — phân cách bằng dòng ---",`<textarea id="ideaDescriptions" class="tall">${esc(descriptions)}</textarea>`,true)}
      ${ideaField("Hook chính rút từ nguồn",`<textarea id="ideaMainHook">${esc(x.main_hook||"")}</textarea>`,true)}
      ${ideaField("Các cảnh kịch tính đáng khai thác — mỗi dòng một cảnh",`<textarea id="ideaTensionScenes" class="tall">${esc((x.high_tension_scenes||[]).join("\n"))}</textarea>`,true)}
      ${ideaField("Các plot twist đáng khai thác — mỗi dòng một cú lật",`<textarea id="ideaPlotTwists">${esc((x.plot_twists||[]).join("\n"))}</textarea>`,true)}
      ${ideaField("Hồ sơ sáng tác Việt gửi sang Perplexity — KHÔNG chứa toàn văn nguồn",`<textarea id="ideaRewriteBrief" class="tall">${esc(x.rewrite_brief||"")}</textarea>`,true)}
      ${ideaField("Outline 3 phần",`<textarea id="ideaOutline" class="tall">${esc(x.outline||"")}</textarea>`,true)}
      ${ideaField("Nhân vật chính",`<input id="ideaCharacters" value="${esc((x.main_characters||[]).join(", "))}">`)}
      ${ideaField("Thời điểm đăng",`<input id="ideaPublish" value="${esc(x.best_publish_time||"")}">`)}
      ${ideaField("Ghi chú biên tập",`<textarea id="ideaNotes">${esc(x.notes||"")}</textarea>`,true)}
      <div class="idea-field full"><label>Trích đoạn nguồn để tham khảo — không đọc/sao chép nguyên văn</label><div class="idea-source-preview">${esc(x.content||x.raw_content||x.content_preview||"")}</div></div>
    </div>`;
}
function renderIdeas(){
  renderIdeaStatus();
  const badge=document.getElementById("ideaLibraryBadge");if(badge)badge.textContent=String(IDEAS.items.length||0);
  const online=document.getElementById("ideaOnlineKeyword");if(online&&document.activeElement!==online)online.value=IDEAS.onlineKeyword||"";
  const group=document.getElementById("ideaSourceGroup");if(group)group.value=IDEAS.sourceGroup||"zhihu";
  const usage=document.getElementById("ideaUsageFilter");if(usage)usage.value=IDEAS.usageFilter||"unused";
  setIdeasTab(IDEAS.tab);
}
async function ideasSelect(id,renderNow=true){
  IDEAS.selectedId=id;
  if(renderNow) renderIdeaList();
  try{
    const r=await api("/api/content/item?id="+encodeURIComponent(id));
    IDEAS.detail=r.item||null; renderIdeaDetail();return IDEAS.detail;
  }catch(e){toast(e.message,"err");return null;}
}
async function ideasToggle(id,selected){
  const item=IDEAS.items.find(x=>x.id===id);if(item&&ideaWasUsed(item))return;
  if(item) item.selected=selected;
  renderIdeaList();
  try{
    await api("/api/content/select",{ids:[id],selected});
    // Checkbox trước đây chỉ phục vụ chọn hàng loạt nên khung chi tiết vẫn trỏ
    // vào truyện cũ. Khi tick, đồng thời mở đúng truyện để mọi thao tác một mục
    // (đặc biệt Dùng trong AI Story) có cùng một nguồn chuẩn.
    if(selected&&IDEAS.selectedId!==id) await ideasSelect(id);
  }
  catch(e){if(item)item.selected=!selected;renderIdeaList();toast(e.message,"err");}
}
async function ideasSetVisibleSelection(selected){
  const rows=selected?ideasFiltered():IDEAS.items.filter(x=>x.selected);
  const ids=rows.filter(x=>!ideaWasUsed(x)).map(x=>x.id);
  if(!ids.length)return toast(selected?"Không có mục chưa dùng trong bộ lọc này":"Hàng đợi đang trống","warn");
  const idSet=new Set(ids);
  IDEAS.items.forEach(x=>{if(idSet.has(x.id))x.selected=!!selected;});
  renderIdeaList();
  try{await api("/api/content/select",{ids,selected});}
  catch(e){await ideasLoad(true);toast(e.message,"err");}
}
async function ideasReloadSelected(inputIds){
  const ids=(Array.isArray(inputIds)&&inputIds.length?inputIds:ideaSelectedIds())
    .filter(id=>{const item=IDEAS.items.find(x=>x.id===id);return item&&!ideaWasUsed(item);});
  if(!ids.length)return toast("Hãy tick ít nhất một mục chưa dùng cần tải lại","warn");
  if(!confirm(`Tải lại nội dung nguồn và thay kết quả phân tích hiện tại của ${ids.length} mục?`))return;
  const provider=document.getElementById("ideaProvider")?.value||IDEAS.provider||"browser";
  IDEAS.provider=provider;
  try{
    const r=await api("/api/content/reload",{ids,provider,use_ai:provider!=="heuristic",
      cookie:ideaDownloadCookie(),concurrency:3});
    IDEAS.server={working:true,active:"content_reload",status:`Đang tải lại và phân tích ${r.total} mục…`,
      total:r.total,done:0,progress:0,protected_count:r.protected||0};
    _ideasLastActive="content_reload";renderIdeaStatus();
    if(provider!=="heuristic"&&!r.provider_configured)
      toast("Chưa mở được phiên Gemini/Perplexity đã đăng nhập; hàng đợi sẽ dùng quy tắc local.","warn");
  }catch(e){toast(e.message,"err");}
}
async function ideasDeleteSelected(inputIds){
  const ids=(Array.isArray(inputIds)&&inputIds.length?inputIds:ideaSelectedIds())
    .filter(id=>{const item=IDEAS.items.find(x=>x.id===id);return item&&!ideaWasUsed(item);});
  if(!ids.length)return toast("Hãy tick ít nhất một mục chưa dùng cần xóa","warn");
  if(!confirm(`Xóa ${ids.length} mục khỏi Kho đã tải & phân tích? Hành động này không xóa lịch sử truyện đã dùng.`))return;
  try{
    const r=await api("/api/content/delete",{ids});
    const deleted=new Set(r.deleted||[]);
    if(deleted.has(IDEAS.selectedId)){IDEAS.selectedId="";IDEAS.detail=null;}
    await ideasLoad(false);
    toast(r.status||`Đã xóa ${deleted.size} mục`,deleted.size?"ok":"warn");
  }catch(e){toast(e.message,"err");}
}
function ideasSearch(value){IDEAS.search=value;renderIdeaList();}
function ideasSetUsageFilter(value){
  IDEAS.usageFilter=["unused","used","all"].includes(value)?value:"unused";
  localStorage.setItem("advn_idea_usage_filter",IDEAS.usageFilter);renderIdeaList();
}
function ideasClearSearch(){
  IDEAS.search="";const input=document.getElementById("ideaSearch");if(input)input.value="";renderIdeaList();
}
async function ideasImport(){
  let path="";
  try{
    if(DESK()&&pywebview.api.pick_story_data) path=await pywebview.api.pick_story_data();
    else path=prompt("Đường dẫn file JSON/SQLite:","")||"";
    if(path&&path.error) throw new Error(path.error);
    if(!path) return;
    await api("/api/content/import",{path});
    IDEAS.server={working:true,status:"Đang nhập kho truyện…",progress:0};renderIdeaStatus();
  }catch(e){toast(e.message,"err");}
}
async function ideasSample(){
  try{await api("/api/content/sample",{});IDEAS.server={working:true,status:"Đang tạo dữ liệu mẫu…"};renderIdeaStatus();}
  catch(e){toast(e.message,"err");}
}
async function ideasAnalyze(all){
  const ids=all?[]:ideaSelectedIds();
  if(!all&&!ids.length) return toast("Hãy tick ít nhất một ý tưởng","warn");
  const provider=document.getElementById("ideaProvider")?.value||IDEAS.provider||"browser";
  IDEAS.provider=provider;
  try{
    const r=await api("/api/content/analyze",{ids,all,provider,use_ai:provider!=="heuristic",concurrency:3});
    IDEAS.server={working:true,active:"analyze",status:`Đang phân tích ${r.total} ý tưởng bằng ${provider}…`,
      total:r.total,done:0,progress:0,provider:r.provider,provider_model:r.provider_model,
      provider_configured:r.provider_configured,started_at:Date.now()/1000};renderIdeaStatus();
    if(provider!=="heuristic"&&!r.provider_configured)
      toast("Chưa mở được phiên Gemini/Perplexity đã đăng nhập; lượt này dùng quy tắc local.","warn");
  }catch(e){toast(e.message,"err");}
}
async function ideasSave(silent){
  const x=IDEAS.detail;if(!x)return;
  const descriptions=String(document.getElementById("ideaDescriptions")?.value||"").split(/\n\s*---\s*\n/).map(v=>v.trim()).filter(Boolean);
  const values={
    title_localized:document.getElementById("ideaTitleLocalized")?.value||"",
    series:document.getElementById("ideaSeries")?.value||"",
    primary_genre:document.getElementById("ideaGenre")?.value||"",
    emotion:document.getElementById("ideaEmotion")?.value||"",
    themes:ideaArray(document.getElementById("ideaThemes")?.value),
    keywords:ideaArray(document.getElementById("ideaKeywords")?.value),
    tags:ideaArray(document.getElementById("ideaTags")?.value),
    titles:String(document.getElementById("ideaTitles")?.value||"").split("\n").map(v=>v.trim()).filter(Boolean).slice(0,8),
    descriptions:descriptions.slice(0,3),outline:document.getElementById("ideaOutline")?.value||"",
    main_hook:document.getElementById("ideaMainHook")?.value||"",
    high_tension_scenes:ideaLines(document.getElementById("ideaTensionScenes")?.value),
    plot_twists:ideaLines(document.getElementById("ideaPlotTwists")?.value),
    rewrite_brief:document.getElementById("ideaRewriteBrief")?.value||"",
    main_characters:ideaArray(document.getElementById("ideaCharacters")?.value),
    best_publish_time:document.getElementById("ideaPublish")?.value||"",
    notes:document.getElementById("ideaNotes")?.value||"",
  };
  try{const r=await api("/api/content/update",{id:x.id,values});IDEAS.detail=r.item;await ideasLoad(true);if(!silent)toast("Đã lưu chỉnh sửa","ok");return IDEAS.detail;}
  catch(e){toast(e.message,"err");return null;}
}
async function ideasExport(all){
  const ids=all?[]:ideaSelectedIds();if(!all&&!ids.length)return toast("Hãy chọn ý tưởng cần xuất","warn");
  try{const r=await api("/api/content/export",{ids,all});IDEAS.server={working:true,status:`Đang tạo Excel cho ${r.total} ý tưởng…`,total:r.total,progress:0};renderIdeaStatus();}
  catch(e){toast(e.message,"err");}
}
async function ideasUseStory(){
  try{
    const selectedIds=ideaSelectedIds();
    const targetId=ideaStoryTargetId();
    if(!targetId){
      if(selectedIds.length>1)
        throw new Error("Bạn đang tick nhiều truyện. Hãy bấm mở đúng một truyện trong số đã tick rồi mới đưa sang AI Story.");
      throw new Error("Hãy chọn một truyện cần đưa sang AI Story.");
    }
    if(!IDEAS.detail||IDEAS.detail.id!==targetId){
      const loaded=await ideasSelect(targetId,false);
      if(!loaded||loaded.id!==targetId) throw new Error("Không nạp được đúng truyện đã tick.");
    }
    const targetTitle=String(IDEAS.detail.title_localized||IDEAS.detail.title_original||"").trim();
    const titleIndex=Math.max(0,Number(document.getElementById("ideaTitleChoice")?.value)||0);
    const saved=await ideasSave(true);
    if(!saved||saved.id!==targetId) throw new Error("Không lưu được đúng truyện đã chọn; đã dừng để tránh viết nhầm.");
    let current=saved;
    toast(`Đang chuyển đúng truyện: ${targetTitle}`,"ok");
    const provider=document.getElementById("ideaProvider")?.value||IDEAS.provider||"browser";
    const analysisProvider=String(current.analysis_provider||"").toLowerCase();
    const needsAi=String(current.language||"").toLowerCase()==="zh"&&
      (!current.title_localized||!current.rewrite_brief||
       /heuristic|offline|fallback/.test(analysisProvider));
    if(needsAi){
      if(provider==="heuristic") throw new Error("Truyện Trung cần Gemini Web hoặc Claude trên Perplexity để rút hook/plot twist và Việt hóa tiêu đề.");
      const started=await api("/api/content/analyze",{ids:[current.id],provider,use_ai:true,concurrency:1});
      IDEAS.server={working:true,active:"analyze",status:"Đang rút hook, plot twist và tạo 8 tiêu đề Việt…",total:1,done:0,progress:0};
      renderIdeaStatus();
      if(!started.provider_configured)
        toast("Không mở được phiên trình duyệt đã đăng nhập; lượt này sẽ dùng quy tắc local.","warn");
      const deadline=Date.now()+12*60*1000;
      while(Date.now()<deadline){
        await new Promise(resolve=>setTimeout(resolve,900));
        const state=await api("/api/state");
        const task=state.content_pipeline||{};
        IDEAS.server=task;renderIdeaStatus();
        if(!task.working){
          if(task.error) throw new Error(task.error);
          break;
        }
      }
      const fresh=await api("/api/content/item?id="+encodeURIComponent(current.id));
      current=fresh.item||current;IDEAS.detail=current;
      if(current.id!==targetId) throw new Error("Dữ liệu phân tích trả về sai truyện; đã dừng an toàn.");
      const freshProvider=String(current.analysis_provider||"").toLowerCase();
      if(!current.title_localized||!current.rewrite_brief||/heuristic|offline|fallback/.test(freshProvider))
        throw new Error(current.analysis_error||"Trình duyệt chưa tạo được tiêu đề Việt và hồ sơ sáng tác. Kiểm tra đăng nhập Gemini/Perplexity rồi thử lại.");
    }
    if(current.id!==targetId) throw new Error("Lựa chọn đã thay đổi trong lúc xử lý; đã dừng để tránh viết nhầm.");
    const r=await api("/api/content/use_story",{id:targetId,title_index:titleIndex,description_index:0});
    const story=r.story||{};MANUAL.writer_title=story.title||current.title_localized||current.title_original;
    if(story.id!==targetId) throw new Error("Máy chủ trả về sai truyện; đã dừng an toàn.");
    MANUAL.name=MANUAL.writer_title;MANUAL.youtube_description=story.description||"";
    MANUAL.youtube_tags=story.tags||[];MANUAL.content_outline=story.outline||"";
    MANUAL.rewrite_brief=story.rewrite_brief||"";
    MANUAL.content_idea_id=story.id||current.id;
    MANUAL.status="Đã nhận hồ sơ; đang mở Perplexity để viết mới toàn bộ.";
    setMode("story");renderStory();
    toast("Đã chuyển tiêu đề + hook/plot twist; bắt đầu quy trình Perplexity 4 bước","ok");
    await storyGenerateAndRun();
  }catch(e){toast(e.message,"err");}
}
function setVideoToolsTab(tab){
  VIDEO_TOOLS.tab=tab==="cut"?"cut":"download";
  localStorage.setItem("advn_video_tools_tab",VIDEO_TOOLS.tab);
  renderVideoTools();
}
function videoToolName(path){
  const value=String(path||"");
  return value.split(/[\\/]/).pop()||value;
}
function videoToolFileRows(paths,removable){
  const list=Array.isArray(paths)?paths:[];
  if(!list.length) return `<div class="vt-empty">Chưa có file nào trong danh sách.</div>`;
  return `<div class="vt-files">${list.map((path,index)=>`<div class="vt-file" title="${esc(path)}">
    <span class="vt-file-no">${index+1}</span><span class="vt-file-info">
      <b>${esc(videoToolName(path))}</b><small>${esc(path)}</small></span>
    ${removable?`<button class="btn sm danger" onclick="videoToolRemoveCutInput(${index})" title="Bỏ khỏi danh sách">×</button>`:""}
  </div>`).join("")}</div>`;
}
function videoToolStatus(kind){
  const state=VIDEO_TOOLS.server||{};
  const isDownload=kind==="download";
  const active=state.active===kind&&state.working;
  const status=String(isDownload?state.download_status||"":state.cut_status||"");
  const pct=Math.max(0,Math.min(100,Number(isDownload?state.download_pct:state.cut_pct)||0));
  const done=Number(isDownload?state.download_done:state.cut_done)||0;
  const total=Number(isDownload?state.download_total:state.cut_total)||0;
  if(!active&&!status&&!state.error) return "";
  return `<div class="vt-status">
    <div class="vt-status-head"><b>${active?"Đang xử lý":"Trạng thái"}${total?` · ${done}/${total}`:""}</b>
      <strong>${Math.round(pct)}%</strong></div>
    <div class="vt-progress"><i style="width:${pct}%"></i></div>
    <div>${esc(status||(state.error?"Có lỗi xảy ra":"Đang chuẩn bị…"))}</div>
    ${state.error?`<div class="vt-error">${esc(state.error)}</div>`:""}
  </div>`;
}
function renderVideoTools(){
  const body=document.getElementById("videoToolsBody"); if(!body) return;
  document.getElementById("vtTabDownload")?.classList.toggle("on",VIDEO_TOOLS.tab==="download");
  document.getElementById("vtTabCut")?.classList.toggle("on",VIDEO_TOOLS.tab==="cut");
  const state=VIDEO_TOOLS.server||{};
  const working=!!state.working;
  const appBusy=!!ST.running&&!working;
  if(VIDEO_TOOLS.tab==="download"){
    const files=VIDEO_TOOLS.download_files||[];
    const selected=new Set(VIDEO_TOOLS.search_selected||[]);
    const searchRows=(VIDEO_TOOLS.search_results||[]).map((row,index)=>{
      const url=String(row.url||"");
      const meta=`${row.duration?fmt(row.duration):"?"} · ${row.channel||row.provider||"Video nền"}`;
      return `<label class="story-source-row"><input type="checkbox" ${selected.has(url)?"checked":""}
        onchange="videoToolToggleSearchResult(decodeURIComponent('${encodeURIComponent(url)}'),this.checked)">
        <span><b>${index+1}. ${esc(row.title||url)}</b><small>${esc(meta)}</small></span></label>`;
    }).join("");
    body.innerHTML=`<div class="vt-grid">
      <section class="vt-card"><h3>⇩ Tải video nền cho phần làm Audio</h3>
        <p class="vt-desc">Kho tải riêng cho video nền. Link ở đây không được coi là tư liệu tham khảo viết truyện.</p>
        <div class="vt-options"><div class="vt-field"><label>Nguồn tìm kiếm</label>
          <select id="vtSearchProvider" ${working?"disabled":""}>
            <option value="all" ${VIDEO_TOOLS.search_provider==="all"?"selected":""}>Bilibili + YouTube</option>
            <option value="bilibili" ${VIDEO_TOOLS.search_provider==="bilibili"?"selected":""}>Chỉ Bilibili</option>
            <option value="youtube" ${VIDEO_TOOLS.search_provider==="youtube"?"selected":""}>Chỉ YouTube</option>
          </select></div><div class="vt-field"><label>Số video muốn tìm</label>
          <input id="vtSearchCount" type="number" min="1" max="50" value="${VIDEO_TOOLS.search_count}" ${working?"disabled":""}></div></div>
        <div class="vt-field"><label>Từ khóa video nền (Việt / Trung / Anh)</label><div class="vt-folder-row">
          <input id="vtSearchKeyword" value="${esc(VIDEO_TOOLS.search_keyword)}" ${working?"disabled":""}
            placeholder="婆媳矛盾 / cảnh làng quê / rural rainy walk">
          <button class="btn" ${working||appBusy?"disabled":""} onclick="videoToolSearch()">⌕ Tìm video</button>
        </div></div>
        ${searchRows?`<div class="story-source-results">${searchRows}</div>`:""}
        ${state.search_status?`<div class="vt-search-status">${esc(state.search_status)}</div>`:""}
        <div class="vt-separator"><span>HOẶC DÁN LINK TRỰC TIẾP</span></div>
        <div class="vt-field"><label>Link Bilibili / YouTube / Douyin — mỗi dòng một link</label>
          <textarea id="vtDownloadLinks" ${working?"disabled":""}
            placeholder="https://www.bilibili.com/video/BV...\nhttps://www.youtube.com/watch?v=...">${esc(VIDEO_TOOLS.links)}</textarea></div>
        <div class="vt-field"><label>Thư mục lưu video</label><div class="vt-folder-row">
          <input id="vtDownloadOutput" value="${esc(VIDEO_TOOLS.download_output)}" ${working?"disabled":""}
            placeholder="Để trống: tự tạo trong downloads/audio_background">
          <button class="btn" ${working?"disabled":""} onclick="videoToolPickOutput('download')">📁 Chọn thư mục</button>
        </div></div>
        <div class="vt-options"><div class="vt-field"><label>Chất lượng video nền</label>
          <select id="vtDownloadQuality" ${working?"disabled":""}>
            <option value="360" ${VIDEO_TOOLS.quality==="360"?"selected":""}>Tối đa 360p · nhẹ</option>
            <option value="480" ${VIDEO_TOOLS.quality==="480"?"selected":""}>Tối đa 480p · khuyên dùng</option>
            <option value="720" ${VIDEO_TOOLS.quality==="720"?"selected":""}>Tối đa 720p</option>
            <option value="best" ${VIDEO_TOOLS.quality==="best"?"selected":""}>Nét tốt nhất có thể</option>
          </select></div><div class="vt-field"><label>Xử lý đồng thời</label>
          <input value="Tối đa 3 video" disabled></div></div>
        <div class="vt-actions">
          ${working&&state.active==="download"?`<button class="btn danger" onclick="cancelRun()">■ Dừng tải</button>`:""}
          <button class="btn pri" ${working||appBusy?"disabled":""} onclick="videoToolRunDownload()">⇩ TẢI VIDEO NỀN</button>
        </div>${appBusy?`<div class="vt-note">Một tác vụ khác của AutoDubVN đang chạy. Hãy chờ tác vụ đó hoàn tất.</div>`:""}
        ${videoToolStatus("download")}
      </section>
      <aside class="vt-card"><div class="vt-summary"><span>Video đã tải</span><b>${files.length} file</b></div>
        ${videoToolFileRows(files,false)}
        <div class="vt-actions">
          <button class="btn" ${!files.length?"disabled":""} onclick="videoToolOpenFolder('download')">📂 Mở thư mục</button>
          <button class="btn" ${!files.length?"disabled":""} onclick="videoToolUseDownloadsForCut()">✂ Đưa sang cắt</button>
          <button class="btn" ${!files.length?"disabled":""} onclick="videoToolImportToStory('download')">🎬 Dùng cho Video kể chuyện</button>
        </div><div class="vt-note">Tải xong vẫn nằm trong kho riêng. Chỉ khi bấm “Dùng cho Video kể chuyện”, các file mới được nhập vào phần Audio.</div>
      </aside></div>`;
  }else{
    const inputs=VIDEO_TOOLS.cut_inputs||[], files=VIDEO_TOOLS.cut_files||[];
    body.innerHTML=`<div class="vt-grid">
      <section class="vt-card"><h3>✂ Cắt nhiều video cùng lúc</h3>
        <p class="vt-desc">Chọn nhiều file trong cùng hộp thoại, hoặc thêm cả thư mục. Mỗi video được cắt nhanh thành các đoạn nhỏ.</p>
        <div class="vt-actions" style="margin-top:0">
          <button class="btn pri" ${working?"disabled":""} onclick="videoToolPickCutVideos()">▣ Chọn nhiều video cùng lúc</button>
          <button class="btn" ${working?"disabled":""} onclick="videoToolPickCutFolder()">📁 Thêm thư mục video</button>
          <button class="btn danger" ${working||!inputs.length?"disabled":""} onclick="videoToolClearCutInputs()">Xoá danh sách</button>
        </div>
        <div class="vt-summary" style="margin-top:13px"><span>Danh sách nguồn đã chọn</span><b>${inputs.length} mục</b></div>
        ${videoToolFileRows(inputs,true)}
        <div class="vt-field" style="margin-top:13px"><label>Thư mục lưu các đoạn đã cắt</label><div class="vt-folder-row">
          <input id="vtCutOutput" value="${esc(VIDEO_TOOLS.cut_output)}" ${working?"disabled":""}
            placeholder="Để trống: tự tạo trong downloads/video_segments">
          <button class="btn" ${working?"disabled":""} onclick="videoToolPickOutput('cut')">📁 Chọn thư mục</button>
        </div></div>
        <div class="vt-options">
          <div class="vt-field"><label>Đoạn ngắn nhất (phút)</label><input id="vtCutMin" type="number" min="0.1" max="180" step="0.5" value="${VIDEO_TOOLS.min_minutes}" ${working?"disabled":""}></div>
          <div class="vt-field"><label>Đoạn dài nhất (phút)</label><input id="vtCutMax" type="number" min="0.1" max="180" step="0.5" value="${VIDEO_TOOLS.max_minutes}" ${working?"disabled":""}></div>
        </div><div class="vt-actions">
          ${working&&state.active==="cut"?`<button class="btn danger" onclick="cancelRun()">■ Dừng cắt</button>`:""}
          <button class="btn pri" ${working||appBusy||!inputs.length?"disabled":""} onclick="videoToolRunCut()">✂ CẮT TOÀN BỘ VIDEO ĐÃ CHỌN</button>
        </div>${appBusy?`<div class="vt-note">Một tác vụ khác của AutoDubVN đang chạy. Hãy chờ tác vụ đó hoàn tất.</div>`:""}
        ${videoToolStatus("cut")}
      </section>
      <aside class="vt-card"><div class="vt-summary"><span>Kết quả đã cắt</span><b>${files.length} đoạn</b></div>
        ${videoToolFileRows(files,false)}
        <div class="vt-actions"><button class="btn" ${!files.length?"disabled":""} onclick="videoToolOpenFolder('cut')">📂 Mở thư mục</button>
          <button class="btn pri" ${!files.length?"disabled":""} onclick="videoToolImportToStory('cut')">🎲 Dùng làm kho random cho Audio</button></div>
        <div class="vt-note">Sau khi nhập, phần Video kể chuyện có thể random các đoạn này cho tới khi đủ thời lượng audio.</div>
      </aside></div>`;
  }
}
function videoToolToggleSearchResult(url,on){
  const selected=new Set(VIDEO_TOOLS.search_selected||[]);
  if(on) selected.add(url); else selected.delete(url);
  VIDEO_TOOLS.search_selected=[...selected];
}
async function videoToolSearch(){
  const keyword=(document.getElementById("vtSearchKeyword")?.value||VIDEO_TOOLS.search_keyword||"").trim();
  if(!keyword) return toast("Hãy nhập từ khóa tìm video nền","warn");
  const provider=document.getElementById("vtSearchProvider")?.value||"all";
  const limit=Math.max(1,Math.min(50,Number(document.getElementById("vtSearchCount")?.value)||10));
  VIDEO_TOOLS.search_keyword=keyword; VIDEO_TOOLS.search_provider=provider; VIDEO_TOOLS.search_count=limit;
  VIDEO_TOOLS.server={...(VIDEO_TOOLS.server||{}),search_status:"Đang tìm video nền…",error:""};
  renderVideoTools();
  try{
    const r=await api("/api/tools/search_videos",{keyword,provider,limit});
    VIDEO_TOOLS.search_results=r.results||[]; VIDEO_TOOLS.search_selected=[];
    VIDEO_TOOLS.server={...(VIDEO_TOOLS.server||{}),search_status:`Tìm thấy ${VIDEO_TOOLS.search_results.length} video`,error:""};
    renderVideoTools(); toast(`Tìm thấy ${VIDEO_TOOLS.search_results.length} video nền`,"ok");
  }catch(e){
    VIDEO_TOOLS.server={...(VIDEO_TOOLS.server||{}),search_status:"Tìm video thất bại",error:e.message||String(e)};
    renderVideoTools(); toast(e.message||String(e),"err");
  }
}
async function videoToolPickOutput(kind){
  let folder="";
  if(DESK()){
    try{ folder=await pywebview.api.pick_folder(); }
    catch(e){ return toast(e.message||String(e),"err"); }
  }else folder=prompt("Dán đường dẫn thư mục lưu:")||"";
  if(!folder) return;
  if(kind==="cut"){
    VIDEO_TOOLS.cut_output=String(folder); localStorage.setItem("advn_vt_cut_output",VIDEO_TOOLS.cut_output);
  }else{
    VIDEO_TOOLS.download_output=String(folder); localStorage.setItem("advn_vt_download_output",VIDEO_TOOLS.download_output);
  }
  renderVideoTools();
}
async function videoToolPickCutVideos(){
  let files=[];
  if(DESK()){
    try{ files=await pywebview.api.pick_video(); }
    catch(e){ return toast(e.message||String(e),"err"); }
    if(files&&files.error) return toast(files.error,"err");
  }else{
    const raw=prompt("Dán đường dẫn video, mỗi dòng một file:")||"";
    files=raw.split(/[\r\n]+/).map(x=>x.trim()).filter(Boolean);
  }
  if(!Array.isArray(files)) files=files?[files]:[];
  VIDEO_TOOLS.cut_inputs=[...new Set([...(VIDEO_TOOLS.cut_inputs||[]),...files.map(String)])];
  if(files.length) toast(`Đã thêm ${files.length} video vào danh sách cắt`,"ok");
  renderVideoTools();
}
async function videoToolPickCutFolder(){
  let folder="";
  if(DESK()){
    try{ folder=await pywebview.api.pick_folder(); }
    catch(e){ return toast(e.message||String(e),"err"); }
  }else folder=prompt("Dán đường dẫn thư mục video:")||"";
  if(!folder) return;
  VIDEO_TOOLS.cut_inputs=[...new Set([...(VIDEO_TOOLS.cut_inputs||[]),String(folder)])];
  renderVideoTools();
}
function videoToolRemoveCutInput(index){ VIDEO_TOOLS.cut_inputs.splice(index,1); renderVideoTools(); }
function videoToolClearCutInputs(){ VIDEO_TOOLS.cut_inputs=[]; renderVideoTools(); }
async function videoToolRunDownload(){
  const pasted=document.getElementById("vtDownloadLinks")?.value||VIDEO_TOOLS.links||"";
  const links=[...new Set([...(VIDEO_TOOLS.search_selected||[]),
    ...String(pasted).split(/[\r\n]+/).map(x=>x.trim()).filter(Boolean)])].join("\n");
  const output=document.getElementById("vtDownloadOutput")?.value.trim()||"";
  const quality=document.getElementById("vtDownloadQuality")?.value||VIDEO_TOOLS.quality;
  if(!String(links).trim()) return toast("Hãy chọn kết quả tìm kiếm hoặc dán ít nhất một link video","warn");
  VIDEO_TOOLS.links=String(pasted); VIDEO_TOOLS.download_output=output; VIDEO_TOOLS.quality=quality;
  localStorage.setItem("advn_vt_download_output",output); localStorage.setItem("advn_vt_quality",quality);
  try{
    const r=await api("/api/tools/download_videos",{links,output_dir:output,quality});
    VIDEO_TOOLS.download_output=r.output_dir||output;
    VIDEO_TOOLS.server={...(VIDEO_TOOLS.server||{}),working:true,active:"download",error:"",
      download_status:`Đang tải ${r.total||1} video…`,download_pct:0};
    renderVideoTools(); toast("Đã bắt đầu tải video nền cho Audio","ok");
  }catch(e){ toast(e.message||String(e),"err"); }
}
async function videoToolRunCut(){
  const inputs=VIDEO_TOOLS.cut_inputs||[];
  if(!inputs.length) return toast("Hãy chọn một hoặc nhiều video cần cắt","warn");
  const output=document.getElementById("vtCutOutput")?.value.trim()||"";
  const min=Math.max(.1,Number(document.getElementById("vtCutMin")?.value)||5);
  const max=Math.max(min,Number(document.getElementById("vtCutMax")?.value)||10);
  VIDEO_TOOLS.cut_output=output; VIDEO_TOOLS.min_minutes=min; VIDEO_TOOLS.max_minutes=max;
  localStorage.setItem("advn_vt_cut_output",output);
  localStorage.setItem("advn_vt_min_minutes",String(min)); localStorage.setItem("advn_vt_max_minutes",String(max));
  try{
    const r=await api("/api/tools/cut_videos",{paths:inputs,output_dir:output,min_seconds:min*60,max_seconds:max*60});
    VIDEO_TOOLS.cut_output=r.output_dir||output;
    VIDEO_TOOLS.server={...(VIDEO_TOOLS.server||{}),working:true,active:"cut",error:"",
      cut_status:`Đang cắt ${r.total||inputs.length} video…`,cut_pct:0};
    renderVideoTools(); toast(`Đã bắt đầu cắt ${r.total||inputs.length} video`,"ok");
  }catch(e){ toast(e.message||String(e),"err"); }
}
function videoToolUseDownloadsForCut(){
  VIDEO_TOOLS.cut_inputs=[...new Set([...(VIDEO_TOOLS.cut_inputs||[]),...(VIDEO_TOOLS.download_files||[])])];
  setVideoToolsTab("cut"); toast(`Đã đưa ${VIDEO_TOOLS.download_files.length} video sang danh sách cắt`,"ok");
}
async function videoToolOpenFolder(kind){
  const path=kind==="cut"?VIDEO_TOOLS.cut_output:VIDEO_TOOLS.download_output;
  if(path) await openOut(path); else toast("Chưa có thư mục kết quả","warn");
}
function videoToolImportToStory(kind){
  const files=kind==="cut"?(VIDEO_TOOLS.cut_files||[]):(VIDEO_TOOLS.download_files||[]);
  if(!files.length) return toast("Chưa có file để nhập","warn");
  STORY.source_videos=[...new Set([...(STORY.source_videos||[]),...files])];
  STORY.source_sel=0; STORY_MEDIA_MODE="video"; STORY.step=6;
  localStorage.setItem("advn_story_media_mode","video"); localStorage.setItem("advn_story_step","6");
  setMode("story"); renderStoryMedia(); storyUpdateVideoInfo(); renderStory();
  toast(`Đã nhập ${files.length} video vào kho nền Audio`,"ok");
}
function storyDims(){
  if(STORY.aspect==="9:16") return {w:1080,h:1920};
  if(STORY.aspect==="1:1") return {w:1080,h:1080};
  return {w:1920,h:1080};
}
const _IMG_EXT=/\.(jpe?g|png|webp|bmp|gif|tiff?)$/i;
function storyThumb(p,i){
  const sel=i===STORY.sel?" sel":"";
  const mv=`<div class="smv">
      <b onclick="event.stopPropagation();storyMove(${i},-1)" title="Lên">↑</b>
      <b onclick="event.stopPropagation();storyMove(${i},1)" title="Xuống">↓</b></div>`;
  const del=`<div class="sdel" title="Bỏ khỏi danh sách"
      onclick="event.stopPropagation();storyDelImg(${i})">✕</div>`;
  if(_IMG_EXT.test(p))
    return `<div class="simg${sel}" onclick="storySel(${i})" title="${esc(p)}">
      <img loading="lazy" src="/api/local_image?path=${encodeURIComponent(p)}">
      <div class="sno">${i+1}</div>${del}${mv}</div>`;
  return `<div class="simg${sel}" onclick="storySel(${i})" title="${esc(p)}">
    <div class="sfolder">📁</div><div class="sno">${i+1}</div>
    <div class="sfname">${esc(p.split(/[\\/]/).pop()||p)}</div>${del}${mv}</div>`;
}
function renderStoryImgs(){
  const el=document.getElementById("simgs"); if(!el) return;
  const countEl = document.getElementById("simgcount");
  if(countEl) countEl.textContent = STORY_MEDIA_MODE==="image" ? STORY.imgs.length+" ảnh" : (STORY.source_videos ? STORY.source_videos.length : 0)+" video";
  el.innerHTML=STORY.imgs.map((p,i)=>storyThumb(p,i)).join("")
    ||`<div class="hint" style="grid-column:1/-1">Chưa có ảnh nào.<br>
       Bấm <b>+ Thêm ảnh</b> (chọn được nhiều ảnh một lúc) hoặc
       <b>+ Thư mục ảnh</b>. Ảnh được chia đều theo độ dài giọng đọc.</div>`;
}
function renderStoryStage(){
  const st=document.getElementById("sstage"); if(!st) return;
  st.classList.toggle("doc",STORY.aspect==="9:16");
  st.classList.toggle("square",STORY.aspect==="1:1");
  const firstImg=STORY.imgs.find(p=>_IMG_EXT.test(p));
  const sel=(STORY.sel>=0&&STORY.imgs[STORY.sel]&&_IMG_EXT.test(STORY.imgs[STORY.sel]))
            ?STORY.imgs[STORY.sel]:firstImg;
  const sourceVideos=STORY.source_videos||[];
  const sourceIndex=Math.max(0,Math.min(sourceVideos.length-1,Number(STORY.source_sel)||0));
  const sourceVideo=sourceVideos[sourceIndex]||"";
  let inner="";
  if(MANUAL.output_path){
    inner=`<video controls preload="metadata" src="/api/manual/output?v=${_manualRev}"></video>`;
  }else if(STORY_MEDIA_MODE==="video"&&sourceVideo){
    const z=(Number(STORY.source_zoom)||100)/100;
    const fit=z<1?"contain":"cover";
    const clip=`inset(${STORY.source_crop_top||0}% ${STORY.source_crop_right||0}% ${STORY.source_crop_bottom||0}% ${STORY.source_crop_left||0}%)`;
    inner=`<video id="storySourcePreview" controls muted preload="metadata"
      src="/api/local_video?path=${encodeURIComponent(sourceVideo)}"
      style="object-fit:${fit};object-position:${STORY.source_x}% ${STORY.source_y}%;transform:scale(${z});clip-path:${clip}"></video>
      <div class="story-transform-help">Kéo hình để đổi tâm · lăn chuột để zoom</div>`;
  }else if(sel){
    inner=`<img src="/api/local_image?path=${encodeURIComponent(sel)}">`;
  }else{
    inner=`<div class="sempty"><b>Khung xem trước ${STORY.aspect}</b>
      ${STORY_MEDIA_MODE==="video"?"Thêm video ở cột trái để crop, kéo và zoom":"Thêm ảnh ở cột trái để xem bố cục"}</div>`;
  }
  if(!MANUAL.output_path&&STORY.logo_enabled&&STORY.logo_path){
    const margin="1.8%", pos=STORY.logo_position;
    const corner=pos==="top-left"?`left:${margin};top:${margin}`:
      pos==="bottom-left"?`left:${margin};bottom:${margin}`:
      pos==="bottom-right"?`right:${margin};bottom:${margin}`:
      `right:${margin};top:${margin}`;
    inner+=`<img class="story-logo-preview"
      src="/api/local_image?path=${encodeURIComponent(STORY.logo_path)}"
      style="${corner};width:${STORY.logo_width}%;opacity:${STORY.logo_opacity/100}">`;
  }
  if(!MANUAL.output_path && STORY.character_enabled){
    const charW = Math.max(8, Math.min(32, Math.round(18 * (STORY.character_scale || 1))));
    const charOp = (STORY.character_opacity || 92) / 100;
    inner+=`<div class="story-character-preview" aria-label="Nhân vật chỉ dẫn" style="width:${charW}%;opacity:${charOp}">
      <img src="/api/local_image?path=assets%2Fnhan_vat_mit.png" alt="Nhân vật quả mít"></div>`;
  }
  if(!MANUAL.output_path && STORY.sub_enabled){
    const firstLine=(MANUAL.text||"Phụ đề mẫu sẽ hiển thị như thế này")
      .trim().split(/[\r\n]+/)[0].slice(0,90)||"Phụ đề mẫu";
    const s=STORY.sub, d=storyDims();
    const pos=s.align==="top-center"?"top:5.5%":
              s.align==="mid-center"?"top:50%;transform:translateY(-50%)":
              `bottom:${Math.round(s.margin_v/d.h*100)}%`;
    const previewHeight=STORY.aspect==="16:9"
      ?(document.getElementById("sstage").clientWidth*9/16||300)
      :(document.getElementById("sstage").clientHeight||520);
    const px=Math.max(11,Math.round(s.size/d.h*previewHeight));
    inner+=`<div class="ssub" style="${pos};color:${s.color};
      font-size:${px}px;${s.bold?"":"font-weight:400;"}">${esc(firstLine)}</div>`;
  }
  st.innerHTML=inner;
  bindStoryVideoStage();
  const previewVideo=st.querySelector("video");
  if(previewVideo){
    const sync=()=>{
      const el=document.getElementById("storyTime");
      if(el) el.textContent=`${fmt(previewVideo.currentTime)} / ${fmt(previewVideo.duration||0)}`;
    };
    previewVideo.addEventListener("loadedmetadata",sync);
    previewVideo.addEventListener("timeupdate",sync);
  }else{
    const time=document.getElementById("storyTime"); if(time) time.textContent="00:00 / 00:00";
  }
  const aspectBadge=document.getElementById("storyAspectBadge");
  if(aspectBadge) aspectBadge.textContent=STORY.aspect;
  const sceneBadge=document.getElementById("storySceneBadge");
  if(sceneBadge){
    const at=STORY.imgs.length?Math.max(1,STORY.sel+1):0;
    sceneBadge.textContent=`Cảnh ${at}/${STORY.imgs.length}`;
  }
  const previewTitle=document.getElementById("storyPreviewTitle");
  if(previewTitle) previewTitle.textContent=MANUAL.writer_title||MANUAL.name||"Video kể chuyện AI";
  const info=document.getElementById("sinfo");
  if(info){
    const n=(MANUAL.text||"").length;
    const estMin=n?(n/18/60):0;   // giọng Việt đọc ~18-20 ký tự/giây
    const mediaText=STORY_MEDIA_MODE==="video"
      ?`${(STORY.source_videos||[]).length} video nguồn`:`${STORY.imgs.length} mục ảnh`;
    info.innerHTML=`Khổ <b>${STORY.aspect}</b> · ${mediaText}
      ${STORY.pack?` · <span title="${esc(STORY.pack)}">📦 đã lưu gói ảnh</span>`:""}
      · ${n.toLocaleString("vi-VN")} ký tự${n?` · giọng đọc ước ~<b>${estMin.toFixed(1)} phút</b>`:""}
      ${MANUAL.audio_duration?` · audio hiện có <b>${fmt(MANUAL.audio_duration)}</b>`:""}
      ${MANUAL.output_path?` · <span class="lplay" onclick="openManualFolder('output')">📂 mở thư mục video</span>`:""}
      ${MANUAL.calendar_path?` · <span class="lplay" onclick="openOut('${esc(MANUAL.calendar_path).replace(/\\/g,"\\\\")}')">📊 mở Excel lịch nội dung</span>`:""}`;
  }
}
function renderStoryPanel(){
  const P=document.getElementById("spanel"); if(!P) return;
  const eng=MANUAL.engine||"edge", vs=VOICES[eng]||[];
  const voiceOpts=vs.length
    ? vs.map(v=>`<option value="${esc(v.id)}" ${MANUAL.voice===v.id?"selected":""}>${v.status==="ok"?"✓ ":v.status==="failed"?"✕ ":""}${esc(v.name)}</option>`).join("")
    : `<option value="${esc(MANUAL.voice||"")}">${MANUAL.voice?esc(MANUAL.voice):"(đang nạp giọng…)"}</option>`;
  const busy=!!(ST.manual&&ST.manual.working);
  const resumeImages=!busy&&storyCanResumeImages();
  const s=STORY.sub;
  const stepNames=["✨ AI Viết", "📝 Nội Dung", "🎙 Giọng Đọc", "🎵 Nhạc Nền", "𝐓 Phụ Đề", "🖼 Khung Hình", "🎬 Nguồn Video", "⚙ Logo & Nhân Vật"];
  const sourceFiles=(STORY.source_videos||[]).length;
  P.innerHTML=`<div class="story-wizard-tabs">
    ${stepNames.map((name,i)=>`<button class="${STORY.step===i?"on":""}"
      onclick="setStoryStep(${i})">${name}</button>`).join("")}
    </div>
  <div class="sstep"><div class="shd"><span class="sn">1</span>✨ Tự động viết truyện từ tiêu đề</div>
    ${fld("Tiêu đề / Ý tưởng của video",`<input value="${esc(MANUAL.writer_title||"")}"
      placeholder="Ví dụ: Con dâu bỏ đi 10 năm, ngày về bất ngờ…"
      oninput="storyWriterTitleInput(this.value)">`)}
    ${MANUAL.content_idea_id?`<div class="story-plan-imported"><b>◆ Kế hoạch từ Kho ý tưởng</b>
      <span>${esc((MANUAL.youtube_tags||[]).join(", ")||"Chưa có tag")}</span>
      ${MANUAL.rewrite_brief?`<small>Đã nhận hồ sơ hook/plot twist tiếng Việt; Perplexity sẽ viết mới hoàn toàn.</small>`:
        MANUAL.content_outline?`<small>${esc(MANUAL.content_outline)}</small>`:""}</div>`:""}
    ${busy?`<button class="btn danger story-stop-action" style="width:100%;text-align:center;height:38px"
      ${_cancelPending?"disabled":""} onclick="cancelStoryRun()">
      ■ ${_cancelPending?"ĐANG DỪNG…":"DỪNG TÁC VỤ"}</button>`:
      `<button id="storyPrimaryAction" class="btn pri" style="width:100%;text-align:center;height:38px"
      onclick="storyPrimaryAction()">${resumeImages?
        `↻ TIẾP TỤC ${MANUAL.image_ready}/${MANUAL.image_total} ẢNH → RA VIDEO`:
        `✨ TẠO TRUYỆN → TẠO ẢNH → RA VIDEO`}</button>`}
    <label style="display:flex;gap:7px;align-items:flex-start;margin-top:10px">
      <input type="checkbox" ${STORY.auto_images?"checked":""}
        onchange="STORY.auto_images=this.checked;localStorage.setItem('advn_auto_images',this.checked?'1':'0')">
      <span>Tự tạo ảnh bằng Gemini Pro<br><small style="color:var(--muted)">
        Không cần API key: app tự gửi từng cảnh, chờ và tải ảnh về.</small></span></label>
    ${fld("Số ảnh (ít ảnh sẽ xong nhanh hơn)",`<select onchange="STORY.scene_count=+this.value;localStorage.setItem('advn_story_scene_count',this.value)">
      <option value="6" ${STORY.scene_count===6?"selected":""}>6 ảnh · nhanh nhất</option>
      <option value="8" ${STORY.scene_count===8?"selected":""}>8 ảnh · cân bằng (khuyên dùng)</option>
      <option value="10" ${STORY.scene_count===10?"selected":""}>10 ảnh · chi tiết</option>
      <option value="14" ${STORY.scene_count===14?"selected":""}>14 ảnh · chậm nhất</option>
    </select>`)}
    <div class="story-cta-box">
      <label class="sonoff"><input type="checkbox" ${STORY.cta_enabled?"checked":""}
        onchange="STORY.cta_enabled=this.checked;storySaveCta();renderStoryPanel()">
        Chèn lời nhắc kênh (CTA)</label>
      ${STORY.cta_enabled?`
        ${fld("Câu chèn cố định của kênh",`<textarea readonly style="min-height:110px;opacity:.9"
          title="CTA này được giữ cố định theo yêu cầu của bạn">${esc(STORY.cta_text)}</textarea>`)}
        <div class="grid2">
          ${fld("Vị trí theo % truyện",`<input value="${esc(STORY.cta_positions)}"
            placeholder="12,55" oninput="STORY.cta_positions=this.value;storySaveCta()">`)}
          ${fld("Tốc độ riêng câu chèn",`<select onchange="STORY.cta_speed=+this.value;storySaveCta()">
            <option value="1" ${STORY.cta_speed===1?"selected":""}>1x</option>
            <option value="1.25" ${STORY.cta_speed===1.25?"selected":""}>1.25x</option>
            <option value="1.5" ${STORY.cta_speed===1.5?"selected":""}>1.5x</option>
            <option value="2" ${STORY.cta_speed===2?"selected":""}>2x (khuyên dùng)</option>
          </select>`)}
        </div>
        <div class="hint">Mặc định chèn ở 12% và 55% nội dung. Phải có ít nhất 2 vị trí;
          nếu nhập thiếu, app tự bổ sung. Chỉ các câu này được tăng tốc.</div>`:""}
    </div>
    <button class="btn sm" style="margin-top:8px" onclick="storyUseTitlePrompt()">✎ Nạp prompt tạo 8 tiêu đề</button>
    <div class="hint">Công cụ <b>Tạo kịch bản</b> sẽ viết từng chương, xuất đúng
      <b>KICH_BAN_DOC.txt</b>, rút prompt theo 6 chương, chuẩn bị ảnh rồi mới tạo
      giọng, nhạc, phụ đề và video. Không cần thêm ảnh trước khi bấm.</div>
    ${MANUAL.image_status?`<div class="hint"><b>Ảnh:</b> ${esc(MANUAL.image_status)}</div>`:""}
    ${MANUAL.script_path?`<div class="hint" title="${esc(MANUAL.script_path)}">
      ✓ Đã nạp ${MANUAL.script_words.toLocaleString("vi-VN")} từ ·
      ${esc(MANUAL.script_path.split(/[\\/]/).pop())}</div>`:""}
  </div>
  <div class="sstep"><div class="shd"><span class="sn">2</span>📝 Nội dung & Kịch bản truyện</div>
    ${fld("",`<textarea id="manualText" style="min-height:140px"
      placeholder="Dán nội dung truyện cần đọc, hoặc bấm 'Tải file' bên dưới…"
      oninput="MANUAL.text=this.value;storySubLive()">${esc(MANUAL.text||"")}</textarea>`)}
    <div class="rowbtns" style="align-items:center">
      <button class="btn" ${busy?"disabled":""} onclick="pickManualText()">📂 Tải file văn bản</button>
      ${fld("",`<input value="${esc(MANUAL.name||"")}" placeholder="Tên video xuất ra…"
        oninput="MANUAL.name=this.value">`)}
    </div>
  </div>
  <div class="sstep"><div class="shd"><span class="sn">3</span>🎙 Giọng đọc & Phân vai</div>
    <div class="grid2">
      ${fld("Bộ giọng",`<select onchange="setManualEngine(this.value)">
        <option value="edge" ${eng==="edge"?"selected":""}>edge-tts (Online nhanh)</option>
        <option value="vieneu" ${eng==="vieneu"?"selected":""}>VieNeu (Offline)</option>
        <option value="capcut" ${eng==="capcut"?"selected":""}>CapCut TTS</option>
      </select>`)}
      ${fld("Giọng",`<select onchange="MANUAL.voice=this.value">${voiceOpts}</select>`)}
    </div>
    ${rng("Tốc độ đọc",parseInt(MANUAL.rate||"0"),"%",-30,50,5,
      "MANUAL.rate=(this.value>0?'+':'')+this.value+'%';this.previousElementSibling.querySelector('b').textContent=this.value+'%'")}
    ${nutNgheThu("story")}
    <div class="hint">Giọng đang chọn dùng cho <b>lời kể</b>. Khi bật đa giọng, công cụ tự
      phát hiện tuổi/giới tính/vai trò, gán giọng riêng cho từng nhân vật.</div>
    ${voiceRecommendationHtml(eng,vs,busy)}
    <div class="rowbtns">
      <button class="btn" ${busy?"disabled":""} onclick="createManualAudio()">🎵 Tạo riêng audio</button>
      <button class="btn" onclick="pickManualAudio()">📂 Dùng audio có sẵn</button>
    </div>
    ${MANUAL.audio_path?`<audio controls preload="metadata" style="width:100%;margin-top:8px"
      src="/api/manual/audio?v=${_manualRev}"></audio>`:""}
  </div>
  <div class="sstep"><div class="shd"><span class="sn">4</span>🎵 Nhạc nền lồng tiếng
    <label class="sonoff"><input type="checkbox" ${STORY.nhac_enabled?"checked":""}
      onchange="STORY.nhac_enabled=this.checked;renderStoryPanel()"> Bật</label></div>
    ${STORY.nhac_enabled?`
      ${fld("Bài nhạc nền (CC0)",`<select onchange="MANUAL.nhac_bai=this.value">
        <option value="">— Ngẫu nhiên trong kho (${NHAC_LIST.length} bài) —</option>
        ${NHAC_LIST.map(x=>`<option value="${esc(x.ten)}" ${MANUAL.nhac_bai===x.ten?"selected":""}>${esc(x.ten)}</option>`).join("")}
      </select>`)}
      ${rng("Âm lượng nhạc",MANUAL.nhac_db," dB",-50,-20,1,
        "MANUAL.nhac_db=+this.value;this.previousElementSibling.querySelector('b').textContent=this.value+' dB'")}
      <label style="display:flex;gap:7px;align-items:center;margin-top:6px">
        <input type="checkbox" ${MANUAL.nhac_duck?"checked":""}
          onchange="MANUAL.nhac_duck=this.checked">
        <span>Tự động giảm nhạc khi có giọng nói (Ducking)</span></label>
      <div class="rowbtns" style="margin-top:8px">
        <button class="btn sm" ${busy?"disabled":""} onclick="taiNhacNen()">⇩ Tải thêm nhạc</button>
        <button class="btn sm" onclick="loadNhacNen(true)">↻ Làm mới</button>
      </div>`:`<div class="hint">Video sẽ chỉ phát giọng đọc, không có nhạc nền.</div>`}
  </div>
  <div class="sstep"><div class="shd"><span class="sn">5</span>𝐓 Cấu hình phụ đề cứng
    <label class="sonoff"><input type="checkbox" ${STORY.sub_enabled?"checked":""}
      onchange="STORY.sub_enabled=this.checked;renderStoryPanel();renderStoryStage()"> Bật</label></div>
    ${STORY.sub_enabled?`
      <div class="grid2">
        ${fld("Cỡ chữ",`<input type="number" min="20" max="120" value="${s.size}"
          onchange="STORY.sub.size=Math.max(20,+this.value||48);renderStoryStage()">`)}
        ${fld("Vị trí hiển thị",`<select onchange="STORY.sub.align=this.value;renderStoryStage()">
          <option value="bottom-center" ${s.align==="bottom-center"?"selected":""}>Dưới đáy</option>
          <option value="mid-center" ${s.align==="mid-center"?"selected":""}>Giữa khung hình</option>
          <option value="top-center" ${s.align==="top-center"?"selected":""}>Trên đỉnh</option>
        </select>`)}
      </div>
      <div class="grid2">
        ${fld("Màu sắc chữ",`<input type="color" value="${s.color}" style="height:34px;padding:2px"
          onchange="STORY.sub.color=this.value;renderStoryStage()">`)}
        ${fld("Độ dày viền",`<input type="number" min="0" max="6" value="${s.outline}"
          onchange="STORY.sub.outline=Math.max(0,+this.value||0)">`)}
      </div>
      <div class="hint">Phụ đề tự động khớp mốc thời gian với giọng đọc, ngắt dòng thông minh tối đa 2 dòng.</div>`
      :`<div class="hint">Video sẽ không in phụ đề lên hình.</div>`}
  </div>
  <div class="sstep"><div class="shd"><span class="sn">6</span>🖼 Tỷ lệ khung hình & Hiệu ứng ảnh</div>
    <label class="mini-label">Tỷ lệ video xuất ra</label>
    <div class="aspect-grid">
      <button class="aspect-card ${STORY.aspect==="16:9"?"on":""}" onclick="storySetAspect('16:9')"><b>16:9 Ngang</b><span>YouTube / TV</span></button>
      <button class="aspect-card ${STORY.aspect==="9:16"?"on":""}" onclick="storySetAspect('9:16')"><b>9:16 Dọc</b><span>TikTok / Reels / Shorts</span></button>
      <button class="aspect-card ${STORY.aspect==="1:1"?"on":""}" onclick="storySetAspect('1:1')"><b>1:1 Vuông</b><span>Facebook / Feed</span></button>
    </div>
    <div class="grid2 single-setting" style="margin-top:12px">
      ${fld("Hiệu ứng chuyển động hình ảnh",`<select onchange="STORY.kieu=this.value">
        <option value="chuyen_dong" ${STORY.kieu==="chuyen_dong"?"selected":""}>Ảnh trôi + phóng chậm (Ken Burns sống động)</option>
        <option value="tinh" ${STORY.kieu==="tinh"?"selected":""}>Ảnh đứng yên (Xuất nhanh nhất)</option>
      </select>`)}
    </div>
    <div class="hint" style="margin-top:12px">Bạn có thể chọn ảnh minh họa ở thanh danh sách cảnh dưới cùng hoặc để AI tự tạo.</div>
  </div>
  <div class="sstep"><div class="shd"><span class="sn">7</span>🎬 Video nền cho Audio</div>
    <div class="story-source-box story-video-source-box" style="margin-top:0">
      <div class="shd story-subhead">Kho video nền đang dùng
        <span class="story-library-badge ${sourceFiles?'has-files':''}">${sourceFiles?
          `${sourceFiles} nguồn` : "Kho đang trống"}</span></div>
      <div class="story-video-intro">Tải và cắt nằm trong <b>Công cụ Video</b>. Tại đây bạn chọn kho đã có,
        sau đó thiết lập cách video được ghép vào audio.</div>
      <div class="story-video-tool-grid">
        <button class="story-video-tool-card primary" onclick="setMode('tools');setVideoToolsTab('download')">
          <span class="story-video-tool-icon">⇩</span><span><b>Tải video nền</b><small>Bilibili · YouTube · link trực tiếp</small></span>
        </button>
        <button class="story-video-tool-card" onclick="setMode('tools');setVideoToolsTab('cut')">
          <span class="story-video-tool-icon">✂</span><span><b>Cắt video</b><small>Chia hàng loạt thành đoạn nhỏ</small></span>
        </button>
        <button class="story-video-pick" onclick="storySetMediaMode('video');storyAddVideos()">
          <span>▣</span><span><b>Chọn video có sẵn</b><small>Nhập nhiều file hoặc cả thư mục vào kho đang dùng</small></span><i>›</i>
        </button>
      </div>
      <div class="grid2">
        ${fld("Hiệu ứng ghép video",`<select onchange="STORY.source_effect=this.value">
          <option value="tinh" ${STORY.source_effect==="tinh"?"selected":""}>Giữ khung ổn định</option>
          <option value="zoom_in" ${STORY.source_effect==="zoom_in"?"selected":""}>Zoom vào nhẹ</option>
          <option value="zoom_out" ${STORY.source_effect==="zoom_out"?"selected":""}>Zoom ra nhẹ</option>
        </select>`)}
        ${fld("Cách lấy video",`<label style="display:flex;gap:7px;align-items:center;height:34px">
          <input type="checkbox" ${STORY.source_random?"checked":""}
          onchange="STORY.source_random=this.checked;storySaveSourceTransform()"> Random, tránh lặp liền nhau</label>`)}
      </div>
      <div class="grid2">
        ${fld("Đoạn random ngắn nhất (phút)",`<input type="number" min="0.1" max="60" step="0.5"
          value="${STORY.source_clip_min_minutes}"
          onchange="STORY.source_clip_min_minutes=Math.max(.1,+this.value||5);storySaveSourceTransform()">`)}
        ${fld("Đoạn random dài nhất (phút)",`<input type="number" min="0.1" max="60" step="0.5"
          value="${STORY.source_clip_max_minutes}"
          onchange="STORY.source_clip_max_minutes=Math.max(STORY.source_clip_min_minutes,+this.value||10);storySaveSourceTransform()">`)}
      </div>
      <div class="rowbtns" style="margin:7px 0 10px">
        <button class="btn sm" onclick="storyRandomizeSource()">⤨ Trộn một lượt mới</button>
        <button class="btn sm" onclick="storyResetSourceTransform()">↺ Đặt lại crop / zoom</button>
      </div>
      <div class="story-source-transform">
        <div class="shd story-subhead">Crop / zoom video nguồn
          <span class="hint">Kéo trực tiếp video trong khung xem trước</span></div>
        ${rng("Phóng / thu video",STORY.source_zoom,"%",40,220,5,
          "STORY.source_zoom=+this.value;this.previousElementSibling.querySelector('b').textContent=this.value+'%';storySaveSourceTransform();renderStoryStage()")}
        <div class="grid2">
          ${fld("Tâm ngang (%)",`<input type="number" min="0" max="100" value="${Math.round(STORY.source_x)}"
            onchange="STORY.source_x=Math.max(0,Math.min(100,+this.value||0));storySaveSourceTransform();renderStoryStage()">`)}
          ${fld("Tâm dọc (%)",`<input type="number" min="0" max="100" value="${Math.round(STORY.source_y)}"
            onchange="STORY.source_y=Math.max(0,Math.min(100,+this.value||0));storySaveSourceTransform();renderStoryStage()">`)}
        </div>
        <div class="grid2 story-crop-grid">
          ${fld("Cắt trái / phải (%)",`<div class="crop-pair"><input type="number" min="0" max="45" value="${STORY.source_crop_left}"
            onchange="STORY.source_crop_left=Math.max(0,Math.min(45,+this.value||0));renderStoryStage()"><input type="number" min="0" max="45" value="${STORY.source_crop_right}"
            onchange="STORY.source_crop_right=Math.max(0,Math.min(45,+this.value||0));renderStoryStage()"></div>`)}
          ${fld("Cắt trên / dưới (%)",`<div class="crop-pair"><input type="number" min="0" max="45" value="${STORY.source_crop_top}"
            onchange="STORY.source_crop_top=Math.max(0,Math.min(45,+this.value||0));renderStoryStage()"><input type="number" min="0" max="45" value="${STORY.source_crop_bottom}"
            onchange="STORY.source_crop_bottom=Math.max(0,Math.min(45,+this.value||0));renderStoryStage()"></div>`)}
        </div>
      </div>
      ${fld("Xử lý phụ đề gốc trên video",`<select onchange="STORY.source_cover=this.value">
        <option value="none" ${STORY.source_cover==="none"?"selected":""}>Giữ nguyên video</option>
        <option value="blur_bottom" ${STORY.source_cover==="blur_bottom"?"selected":""}>Làm mờ dải phụ đề dưới</option>
        <option value="che_bottom" ${STORY.source_cover==="che_bottom"?"selected":""}>Che đen dải phụ đề dưới</option>
      </select>`)}
      <div class="hint">💡 Khi bật Random, công cụ sẽ trộn các video/clip đã nhập và pick tiếp cho tới khi đủ thời lượng audio.
        Muốn tạo clip sẵn ${STORY.source_clip_min_minutes}–${STORY.source_clip_max_minutes} phút, hãy dùng tab <b>Cắt video hàng loạt</b> ở Công cụ Video.</div>
    </div>
  </div>
  <div class="sstep"><div class="shd"><span class="sn">8</span>⚙ Logo thương hiệu & Nhân vật chỉ dẫn</div>
    <div class="story-logo-box" style="margin-top:0">
      <div class="shd story-subhead">Logo thương hiệu
        <label class="sonoff"><input type="checkbox" ${STORY.logo_enabled?"checked":""}
          onchange="STORY.logo_enabled=this.checked;storySaveLogo();renderStoryPanel();renderStoryStage()">
          Bật</label></div>
      <div class="rowbtns">
        <button class="btn" onclick="storyPickLogo()">▣ Chọn ảnh Logo</button>
        ${STORY.logo_path?`<button class="btn danger" onclick="storyClearLogo()">✕ Bỏ logo</button>`:""}
      </div>
      ${STORY.logo_path?`<div class="hint story-logo-path" title="${esc(STORY.logo_path)}">
        ✓ ${esc(STORY.logo_path.split(/[\\/]/).pop()||STORY.logo_path)}</div>`:
        `<div class="hint">Khuyên dùng ảnh định dạng PNG nền trong suốt để đẹp nhất.</div>`}
      ${STORY.logo_enabled?`<div class="grid2">
        ${fld("Vị trí hiển thị",`<select onchange="STORY.logo_position=this.value;storySaveLogo();renderStoryStage()">
          <option value="top-right" ${STORY.logo_position==="top-right"?"selected":""}>Góc trên - Phải</option>
          <option value="top-left" ${STORY.logo_position==="top-left"?"selected":""}>Góc trên - Trái</option>
          <option value="bottom-right" ${STORY.logo_position==="bottom-right"?"selected":""}>Góc dưới - Phải</option>
          <option value="bottom-left" ${STORY.logo_position==="bottom-left"?"selected":""}>Góc dưới - Trái</option>
        </select>`)}
        ${fld("Độ rõ nét (%)",`<input type="number" min="5" max="100" value="${STORY.logo_opacity}"
          onchange="STORY.logo_opacity=Math.max(5,Math.min(100,+this.value||82));storySaveLogo();renderStoryStage()">`)}
      </div>
      ${rng("Kích thước logo",STORY.logo_width,"%",4,40,1,
        "STORY.logo_width=+this.value;this.previousElementSibling.querySelector('b').textContent=this.value+'%';storySaveLogo();renderStoryStage()")}`:""}
    </div>
    <div class="story-character-box">
      <div class="shd story-subhead">Nhân vật quả mít chỉ dẫn (Góc phải dưới)
        <label class="sonoff"><input type="checkbox" ${STORY.character_enabled?"checked":""}
          onchange="STORY.character_enabled=this.checked;storySaveCharacter();renderStoryPanel();renderStoryStage()"> Bật</label>
      </div>
      ${STORY.character_enabled?`<div class="grid2">
        ${fld("Kích thước (%)",`<input type="number" min="55" max="180" value="${Math.round(STORY.character_scale*100)}"
          onchange="STORY.character_scale=Math.max(.55,Math.min(1.8,(+this.value||100)/100));storySaveCharacter();renderStoryStage()">`)}
        ${fld("Độ rõ nét (%)",`<input type="number" min="25" max="100" value="${STORY.character_opacity}"
          onchange="STORY.character_opacity=Math.max(25,Math.min(100,+this.value||92));storySaveCharacter();renderStoryStage()">`)}
      </div>
      <div class="hint" style="margin-top:8px">Nhân vật quả mít xinh xắn sẽ xuất hiện ở góc dưới bên phải, nhấp nhô chỉ dẫn vào nội dung.</div>`
      :`<div class="hint">Tắt nhân vật chỉ dẫn, video sẽ giữ nguyên không chèn nhân vật.</div>`}
    </div>
  </div>
  ${busy?`<button class="btn danger story-run-all story-stop-action"
    ${_cancelPending?"disabled":""} onclick="cancelStoryRun()">
    ■ ${_cancelPending?"ĐANG DỪNG AN TOÀN…":"DỪNG TÁC VỤ ĐANG CHẠY"}</button>`:
    (MANUAL.audio_path&&(STORY.source_videos||[]).length
      ?`<button class="btn pri story-run-all" onclick="storyRenderReadyAudio()">
        ▶ RANDOM VIDEO + XUẤT MP4 — GIỮ AUDIO HIỆN CÓ</button>`
      :`<button class="btn pri story-run-all" onclick="storyRunAll()">
        ▶ CHẠY TẤT CẢ — XUẤT VIDEO HOÀN CHỈNH</button>`)}
  <div class="hint" style="margin-top:8px"><b>${esc(MANUAL.status||"Sẵn sàng")}</b>
    ${MANUAL.error?`<br><span style="color:var(--red)">${esc(MANUAL.error)}</span>`:""}</div>
  ${MANUAL.output_path?`<button class="btn" style="width:100%;text-align:center;margin-top:6px"
    onclick="openManualFolder('output')">📂 Mở thư mục video kết quả</button>`:""}
  <div class="hint story-last-hint" style="margin-top:10px">Muốn ghép audio vào một video có sẵn thay vì dựng
    từ ảnh: chuyển sang chế độ <b>Lồng tiếng</b>, chọn video rồi quay lại đây
    <span class="lplay" onclick="muxManualAudio()">▶ ghép vào video đang chọn</span>.</div>`;
  const steps=Array.from(P.querySelectorAll(".sstep"));
  steps.forEach((el,i)=>{
    el.dataset.step=String(i);
    el.classList.toggle("active",i===STORY.step);
    if(i<steps.length-1){
      const next=document.createElement("button");
      next.className="btn story-next";
      next.textContent=`Tiếp tục: ${stepNames[i+1]} →`;
      next.onclick=()=>setStoryStep(i+1);
      el.appendChild(next);
    }
  });
}
function setStoryStep(i){
  STORY.step=Math.max(0,Math.min(7,Number(i)||0));
  localStorage.setItem("advn_story_step",String(STORY.step));
  renderStoryPanel();
}
function storySetAspect(aspect){
  STORY.aspect=["16:9","9:16","1:1"].includes(aspect)?aspect:"16:9";
  localStorage.setItem("advn_aspect",STORY.aspect);
  renderStoryPanel(); renderStoryStage();
}
function storySaveCta(){
  localStorage.setItem("advn_story_cta_enabled",STORY.cta_enabled?"1":"0");
  localStorage.setItem("advn_story_cta_text",STORY.cta_text||"");
  localStorage.setItem("advn_story_cta_positions",STORY.cta_positions||"12,55");
  localStorage.setItem("advn_story_cta_speed",String(STORY.cta_speed||2));
}
function storySaveLogo(){
  localStorage.setItem("advn_story_logo_enabled",STORY.logo_enabled?"1":"0");
  localStorage.setItem("advn_story_logo_path",STORY.logo_path||"");
  localStorage.setItem("advn_story_logo_position",STORY.logo_position||"top-right");
  localStorage.setItem("advn_story_logo_width",String(STORY.logo_width||12));
  localStorage.setItem("advn_story_logo_opacity",String(STORY.logo_opacity||82));
}
function storySaveCharacter(){
  localStorage.setItem("advn_story_character_enabled",STORY.character_enabled?"1":"0");
  localStorage.setItem("advn_story_character_scale",String(STORY.character_scale||1));
  localStorage.setItem("advn_story_character_opacity",String(STORY.character_opacity||92));
}
async function storyPickLogo(){
  let path="";
  if(DESK()){
    try{
      const r=await pywebview.api.pick_image();
      if(r&&r.error) return toast(r.error,"err");
      path=Array.isArray(r)?(r[0]||""):(r||"");
    }catch(e){ return toast(e.message||String(e),"err"); }
  }else path=prompt("Dán đường dẫn file logo:")||"";
  if(!String(path).trim()) return;
  STORY.logo_path=String(path).trim(); STORY.logo_enabled=true;
  MANUAL.output_path=""; storySaveLogo(); renderStoryPanel(); renderStoryStage();
  toast("Đã chọn logo cho video kể chuyện","ok");
}
function storyClearLogo(){
  STORY.logo_path=""; STORY.logo_enabled=false; MANUAL.output_path="";
  storySaveLogo(); renderStoryPanel(); renderStoryStage();
}
function storySelectRelative(delta){
  if(!STORY.imgs.length) return;
  const start=STORY.sel>=0?STORY.sel:(delta>0?-1:0);
  STORY.sel=(start+delta+STORY.imgs.length)%STORY.imgs.length;
  MANUAL.output_path=""; renderStoryImgs(); renderStoryStage();
}
function storyTogglePreview(){
  const v=document.querySelector("#sstage video");
  if(v){ if(v.paused)v.play();else v.pause(); return; }
  storySelectRelative(1);
}
function storySubLive(){
  /* Cập nhật NHẸ khi đang gõ: chỉ đổi chữ phụ đề mẫu + số ký tự,
     không dựng lại <img> để ảnh xem trước khỏi nháy theo từng phím. */
  const el=document.querySelector("#sstage .ssub");
  if(el) el.textContent=(MANUAL.text||"").trim().split(/[\r\n]+/)[0].slice(0,90)
                        ||"Phụ đề mẫu";
  const n=(MANUAL.text||"").length;
  const info=document.getElementById("sinfo");
  if(info&&n) info.innerHTML=`Khổ <b>${STORY.aspect}</b> · ${STORY.imgs.length} mục ảnh
    · ${n.toLocaleString("vi-VN")} ký tự · giọng đọc ước ~<b>${(n/18/60).toFixed(1)} phút</b>`;
}
function renderStory(){
  const isVideo=STORY_MEDIA_MODE==="video";
  document.getElementById("storyModeImg")?.classList.toggle("on",!isVideo);
  document.getElementById("storyModeVid")?.classList.toggle("on",isVideo);
  if(document.getElementById("storyImageActions")) document.getElementById("storyImageActions").style.display=isVideo?"none":"";
  if(document.getElementById("storyVideoActions")) document.getElementById("storyVideoActions").style.display=isVideo?"":"none";
  if(document.getElementById("simgs")) document.getElementById("simgs").style.display=isVideo?"none":"";
  if(document.getElementById("svideos")) document.getElementById("svideos").style.display=isVideo?"":"none";
  renderStoryMedia(); renderStoryStage(); renderStoryPanel();
}
function storySel(i){ STORY.sel=i; MANUAL.output_path=""; renderStoryImgs(); renderStoryStage(); }
function storyDelImg(i){ STORY.imgs.splice(i,1); if(STORY.sel>=STORY.imgs.length)STORY.sel=-1; renderStory(); storyPersistPack(); }
async function storyClearImgs(){
  if(!STORY.imgs.length) return toast("Danh sách ảnh đã trống","warn");
  STORY.imgs=[]; STORY.sel=-1; MANUAL.output_path=""; renderStory();
  const saved=await storyPersistPack(true);
  if(saved!==false) toast("Đã xoá hết ảnh khỏi danh sách","ok");
}
function storyMove(i,d){
  const j=i+d; if(j<0||j>=STORY.imgs.length) return;
  [STORY.imgs[i],STORY.imgs[j]]=[STORY.imgs[j],STORY.imgs[i]];
  if(STORY.sel===i)STORY.sel=j; else if(STORY.sel===j)STORY.sel=i;
  renderStoryImgs(); renderStoryStage(); storyPersistPack();
}

function storySetMediaMode(mode){
  STORY_MEDIA_MODE = mode === "video" ? "video" : "image";
  localStorage.setItem("advn_story_media_mode", STORY_MEDIA_MODE);
  document.getElementById("storyModeImg").classList.toggle("on", STORY_MEDIA_MODE==="image");
  document.getElementById("storyModeVid").classList.toggle("on", STORY_MEDIA_MODE==="video");
  document.getElementById("storyImageActions").style.display = STORY_MEDIA_MODE==="image" ? "" : "none";
  document.getElementById("storyVideoActions").style.display = STORY_MEDIA_MODE==="video" ? "" : "none";
  document.getElementById("simgs").style.display = STORY_MEDIA_MODE==="image" ? "" : "none";
  document.getElementById("svideos").style.display = STORY_MEDIA_MODE==="video" ? "" : "none";
  renderStoryMedia(); renderStoryStage();
}

function storySaveSourceTransform(){
  localStorage.setItem("advn_source_clip_min",String(STORY.source_clip_min_minutes));
  localStorage.setItem("advn_source_clip_max",String(STORY.source_clip_max_minutes));
  localStorage.setItem("advn_source_random",STORY.source_random?"1":"0");
  localStorage.setItem("advn_source_seed",String(STORY.source_random_seed||0));
  localStorage.setItem("advn_source_zoom",String(STORY.source_zoom));
  localStorage.setItem("advn_source_x",String(STORY.source_x));
  localStorage.setItem("advn_source_y",String(STORY.source_y));
}
function storyResetSourceTransform(){
  STORY.source_zoom=100; STORY.source_x=50; STORY.source_y=50;
  STORY.source_crop_left=0; STORY.source_crop_right=0;
  STORY.source_crop_top=0; STORY.source_crop_bottom=0;
  storySaveSourceTransform(); renderStoryPanel(); renderStoryStage();
}
function storyRandomizeSource(){
  STORY.source_random_seed=Date.now(); storySaveSourceTransform();
  toast("Đã tạo lượt trộn video mới","ok");
}
function bindStoryVideoStage(){
  const v=document.getElementById("storySourcePreview");
  if(!v) return;
  let dragging=false,lastX=0,lastY=0;
  v.addEventListener("pointerdown",e=>{dragging=true;lastX=e.clientX;lastY=e.clientY;v.setPointerCapture?.(e.pointerId);});
  v.addEventListener("pointermove",e=>{
    if(!dragging) return;
    const rect=v.getBoundingClientRect();
    STORY.source_x=Math.max(0,Math.min(100,STORY.source_x+(e.clientX-lastX)/Math.max(1,rect.width)*100));
    STORY.source_y=Math.max(0,Math.min(100,STORY.source_y+(e.clientY-lastY)/Math.max(1,rect.height)*100));
    lastX=e.clientX;lastY=e.clientY;
    v.style.objectPosition=`${STORY.source_x}% ${STORY.source_y}%`;
  });
  const end=()=>{if(dragging){dragging=false;storySaveSourceTransform();renderStoryPanel();}};
  v.addEventListener("pointerup",end);v.addEventListener("pointercancel",end);
  v.addEventListener("wheel",e=>{
    e.preventDefault(); STORY.source_zoom=Math.max(40,Math.min(220,STORY.source_zoom+(e.deltaY<0?5:-5)));
    storySaveSourceTransform(); renderStoryPanel(); renderStoryStage();
  },{passive:false});
}

function renderStoryMedia(){
  if(STORY_MEDIA_MODE==="video") renderStoryVideos();
  else renderStoryImgs();
}

async function storyAddVideos(){
  let files;
  if(window.pywebview && window.pywebview.api && window.pywebview.api.pick_video){
    files = await window.pywebview.api.pick_video();
  } else {
    // fallback: prompt for path
    const p = prompt("Đường dẫn video:");
    files = p ? [p] : [];
  }
  if(!files || !files.length) return;
  for(const f of files){
    if(!STORY.source_videos.includes(f)) STORY.source_videos.push(f);
  }
  renderStoryMedia();
  storyUpdateVideoInfo();
}

async function storyAddVideoFolder(){
  let folder;
  if(window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder){
    folder = await window.pywebview.api.pick_folder();
  } else {
    folder = prompt("Đường dẫn thư mục:");
  }
  if(!folder) return;
  // Add folder path - backend will scan for video files
  if(!STORY.source_videos.includes(folder)) STORY.source_videos.push(folder);
  renderStoryMedia();
  storyUpdateVideoInfo();
}

function storyClearVideos(){
  STORY.source_videos = [];
  STORY._video_info = null;
  renderStoryMedia();
}

function storyDeleteVideo(i){
  STORY.source_videos.splice(i, 1);
  STORY._video_info = null;
  renderStoryMedia();
  storyUpdateVideoInfo();
}

function storyMoveVideo(i, d){
  const j = i + d;
  if(j < 0 || j >= STORY.source_videos.length) return;
  const t = STORY.source_videos[i];
  STORY.source_videos[i] = STORY.source_videos[j];
  STORY.source_videos[j] = t;
  renderStoryMedia();
}

async function storyUpdateVideoInfo(){
  if(!STORY.source_videos.length){ STORY._video_info = null; return; }
  try{
    const info = await api("/api/story/video_info", {paths: STORY.source_videos});
    if(Array.isArray(info.paths)&&info.paths.length) STORY.source_videos=info.paths;
    STORY._video_info = info;
    renderStoryMedia();
  } catch(e){ console.warn("video_info error:", e); }
}

function renderStoryVideos(){
  const el = document.getElementById("svideos");
  if(!el) return;
  const vids = STORY.source_videos || [];
  const info = STORY._video_info || {};
  const videoDetails = info.videos || [];

  // Count
  const countEl = document.getElementById("simgcount");
  if(countEl) countEl.textContent = STORY_MEDIA_MODE==="video" ? `${vids.length} video` : `${STORY.imgs.length} ảnh`;

  if(!vids.length){
    el.innerHTML = `<div class="story-empty-hint">Bấm <b>+ Thêm video</b> để chọn video nguồn.<br>
      Video sẽ tự được cắt khớp thời lượng audio.</div>`;
    return;
  }

  let totalDur = 0;
  const rows = vids.map((v, i) => {
    const name = v.split(/[\\/]/).pop() || v;
    const detail = videoDetails.find(d => d.path === v);
    const dur = detail ? detail.duration : 0;
    totalDur += dur;
    const durText = dur ? fmt(dur) : "?";
    const res = detail && detail.width ? `${detail.width}×${detail.height}` : "";
    return `<div class="story-video-item${i === STORY.source_sel ? ' selected' : ''}" onclick="storySelectVideo(${i})">
      <div class="story-video-info">
        <b>${i+1}. ${esc(name)}</b>
        <small>${durText}${res ? " · " + res : ""}</small>
      </div>
      <div class="story-video-actions">
        <button class="btn sm" onclick="storyMoveVideo(${i},-1)" title="Lên">▲</button>
        <button class="btn sm" onclick="storyMoveVideo(${i},1)" title="Xuống">▼</button>
        <button class="btn sm danger" onclick="storyDeleteVideo(${i})" title="Xóa">✕</button>
      </div>
    </div>`;
  }).join("");

  // Trim info
  const audioDur = info.audio_duration || MANUAL.audio_duration || 0;
  let trimHtml = "";
  if(audioDur > 0 && totalDur > 0){
    const diff = totalDur - audioDur;
    if(diff > 1){
      trimHtml = `<div class="story-trim-info trim-excess">⚡ Tổng video: ${fmt(totalDur)} — Audio: ${fmt(audioDur)} → Tự cắt ${fmt(diff)} thừa</div>`;
    } else if(diff < -1){
      trimHtml = `<div class="story-trim-info trim-short">⚠ Tổng video: ${fmt(totalDur)} ngắn hơn audio ${fmt(audioDur)} — video sẽ được lặp lại</div>`;
    } else {
      trimHtml = `<div class="story-trim-info trim-ok">✓ Tổng video (${fmt(totalDur)}) khớp audio (${fmt(audioDur)})</div>`;
    }
  }

  el.innerHTML = rows + trimHtml;
}
function storySelectVideo(i){
  STORY.source_sel=Math.max(0,Math.min((STORY.source_videos||[]).length-1,Number(i)||0));
  MANUAL.output_path=""; renderStoryVideos(); renderStoryStage();
}
function storyTitleKey(value){
  return String(value||"").normalize("NFKC").toLocaleLowerCase("vi-VN")
    .replace(/[^\p{L}\p{N}]+/gu," ").trim();
}
function storyPackMatchesTitle(title){
  const wanted=storyTitleKey(title);
  const owner=storyTitleKey(STORY.packTitle);
  return !!wanted&&!!owner&&wanted===owner;
}
function storyCanResumeImages(){
  return STORY.auto_images&&!!STORY.pack&&storyPackMatchesTitle(MANUAL.writer_title)&&
    MANUAL.image_total>0&&MANUAL.image_ready<MANUAL.image_total;
}
function storyWriterTitleInput(value){
  MANUAL.writer_title=value;
  const nextTitle=String(value||"").trim();
  const oldPackTitle=String(STORY.packTitle||"").trim();
  if(nextTitle&&oldPackTitle&&
     storyTitleKey(nextTitle)!==storyTitleKey(oldPackTitle)){
    // Đổi tiêu đề nghĩa là mở một phiên truyện mới. Tách toàn bộ dữ liệu
    // phục hồi của truyện cũ khỏi UI; file trên đĩa vẫn được giữ nguyên.
    closeStoryPrompt(); _storyPromptData={text:"",url:""};
    STORY.pack=""; STORY.packTitle=""; STORY.imgs=[]; STORY.sel=-1;
    MANUAL.image_ready=0; MANUAL.image_total=0; MANUAL.image_status="";
    MANUAL.script_path=""; MANUAL.script_title=oldPackTitle;
    MANUAL.script_words=0; MANUAL.output_path="";
    localStorage.removeItem("advn_story_image_pack");
    _lastPromptPack=""; _lastImageReadyCount=-1;
    renderStoryImgs(); renderStoryStage();
  }
  const previewTitle=document.getElementById("storyPreviewTitle");
  if(previewTitle) previewTitle.textContent=value||MANUAL.name||"Video kể chuyện AI";
  const button=document.getElementById("storyPrimaryAction");
  if(!button) return;
  const resume=storyCanResumeImages();
  button.textContent=resume
    ?`↻ TIẾP TỤC ${MANUAL.image_ready}/${MANUAL.image_total} ẢNH → RA VIDEO`
    :`✨ TẠO TRUYỆN → TẠO ẢNH → RA VIDEO`;
}
function storyPrimaryAction(){
  return storyCanResumeImages()?storyResumeImages():storyGenerateAndRun();
}
async function storyPersistPack(replaceEmpty=false){
  if(!STORY.pack) return true;
  try{
    const r=await api("/api/story/image_pack",{
      manifest_path:STORY.pack, images:STORY.imgs,
      replace_images:replaceEmpty||STORY.imgs.length>0, include_prompt:false});
    if(r&&r.manifest_path){
      STORY.pack=r.manifest_path;
      STORY.packTitle=String(r.title||STORY.packTitle||"");
      localStorage.setItem("advn_story_image_pack",STORY.pack);
      if(Array.isArray(r.images)) STORY.imgs=r.images;
      renderStoryImgs(); renderStoryStage();
    }
    return true;
  }catch(e){ toast("Không lưu được thứ tự gói ảnh: "+e.message,"warn"); return false; }
}
async function storyRestoreImagePack(){
  try{
    let r=null;
    if(STORY.pack){
      try{ r=await api("/api/story/image_pack",{manifest_path:STORY.pack,include_prompt:false}); }
      catch(_e){ STORY.pack=""; localStorage.removeItem("advn_story_image_pack"); }
    }
    if(!r) r=await api("/api/story/image_pack/latest");
    if(r&&r.manifest_path){
      STORY.pack=r.manifest_path;
      STORY.packTitle=String(r.title||"");
      localStorage.setItem("advn_story_image_pack",STORY.pack);
      if(!STORY.imgs.length&&Array.isArray(r.images)) STORY.imgs=r.images;
      MANUAL.image_ready=Number(r.ready_count||0);
      MANUAL.image_total=Number(r.scene_count||0);
    }
  }catch(_e){}
}
async function _copyStoryPrompt(text){
  if(!text) return false;
  if(DESK()&&pywebview.api.copy_text){
    try{ if(await pywebview.api.copy_text(text)) return true; }catch(_e){}
  }
  try{ await navigator.clipboard.writeText(text); return true; }
  catch(_e){
    try{
      const ta=document.createElement("textarea"); ta.value=text;
      ta.style.position="fixed"; ta.style.opacity="0"; document.body.appendChild(ta);
      ta.select(); const ok=document.execCommand("copy"); ta.remove(); return ok;
    }catch(_e2){ return false; }
  }
}
let _storyPromptData={text:"",url:""};
let _lastPromptPack="",_lastImageReadyCount=-1;
function _storyPromptHtmlText(text){
  return String(text||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function closeStoryPrompt(){ document.getElementById("storyPromptModal")?.remove(); }
async function copyStoryPromptFromModal(){
  const ok=await _copyStoryPrompt(_storyPromptData.text);
  toast(ok?"Đã sao chép prompt":"Không sao chép được; hãy chọn nội dung trong ô và Ctrl+C",ok?"ok":"warn");
}
async function openStoryPromptProvider(){
  const {url,text}=_storyPromptData;
  if(!url) return;
  if(DESK()&&pywebview.api.open_url_and_paste){
    const r=await pywebview.api.open_url_and_paste(url,text);
    if(r&&r.paste_scheduled){
      toast("Đang mở Gemini — app sẽ tự dán prompt vào ô chat","ok");
      return true;
    }
  }
  const copied=await _copyStoryPrompt(text);
  if(DESK()&&pywebview.api.open_url) await pywebview.api.open_url(url);
  else window.open(url,"_blank","noopener");
  toast(copied?"Đã mở Gemini; nếu chưa thấy prompt hãy nhấn Ctrl+V":"Đã mở Gemini; prompt vẫn hiện trong app","warn");
  return false;
}
async function showStoryPrompt(r,openProvider=false){
  const text=String((r&&r.prompt_text)||"");
  if(!text) return toast("Gói ảnh chưa có nội dung prompt","warn");
  _storyPromptData={text,url:String((r&&r.provider_url)||"")};
  closeStoryPrompt();
  const shell=document.createElement("div");
  shell.id="storyPromptModal"; shell.className="modal-shell open";
  shell.innerHTML=`<div class="settings-dialog story-prompt-dialog">
    <div class="settings-head"><span class="settings-symbol">✦</span><div>
      <h2>Prompt hình ảnh đã sẵn sàng</h2>
      <p>Prompt được rút từ bản thiết kế và lưu cùng gói ảnh.</p></div>
      <button class="modal-close" onclick="closeStoryPrompt()">×</button></div>
    <div class="story-prompt-body">
      <textarea readonly spellcheck="false">${_storyPromptHtmlText(text)}</textarea>
      <div class="hint">Đây là màn dự phòng khi lượt tự động bị lỗi. App sẽ mở
        Gemini bằng tài khoản đang đăng nhập và dán prompt để bạn xử lý tiếp.
        Nếu trình duyệt chặn, chỉ cần nhấn <b>Ctrl+V</b>.</div>
    </div><div class="settings-foot">
      <button class="btn" onclick="copyStoryPromptFromModal()">Sao chép prompt</button>
      <button class="btn pri" onclick="openStoryPromptProvider()">Mở Gemini &amp; tự dán</button>
    </div></div>`;
  document.body.appendChild(shell);
  const copied=await _copyStoryPrompt(text);
  if(openProvider){ await openStoryPromptProvider(); }
  else toast(copied?"Prompt đã hiện và đã sao chép":"Prompt đã hiện; bạn có thể chọn và Ctrl+C",copied?"ok":"warn");
}
async function storyCreateImagePack(){
  const title=String(MANUAL.writer_title||MANUAL.name||"Truyện").trim()||"Truyện";
  // Nút Prompt ảnh luôn thuộc tiêu đề đang nhập. Gói khôi phục từ
  // localStorage của truyện khác phải bị tách khỏi phiên hiện tại trước.
  if(STORY.pack&&!storyPackMatchesTitle(title)){
    STORY.pack=""; STORY.packTitle=""; STORY.imgs=[]; STORY.sel=-1;
    MANUAL.image_ready=0; MANUAL.image_total=0;
    localStorage.removeItem("advn_story_image_pack");
    _lastPromptPack=""; _lastImageReadyCount=-1;
    renderStoryImgs(); renderStoryStage();
  }
  const currentPack=storyPackMatchesTitle(title)?STORY.pack:"";
  const scriptMatches=!!MANUAL.script_path&&
    storyTitleKey(MANUAL.script_title)===storyTitleKey(title);
  if(!currentPack&&!scriptMatches)
    return toast("Hãy tạo truyện trước; prompt ảnh cần cốt truyện và hồ sơ nhân vật","warn");
  try{
    const body=currentPack
      ?{manifest_path:currentPack,expected_title:title,include_prompt:true}
      :{title, name:title, txt_path:MANUAL.script_path||"", aspect:STORY.aspect,
        scene_count:STORY.scene_count, images:STORY.pack?[]:STORY.imgs, include_prompt:true};
    const r=await api("/api/story/image_pack",body);
    STORY.pack=r.manifest_path||"";
    STORY.packTitle=String(r.title||title);
    if(STORY.pack) localStorage.setItem("advn_story_image_pack",STORY.pack);
    if(Array.isArray(r.images)&&r.images.length) STORY.imgs=r.images;
    renderStory();
    await showStoryPrompt(r,true);
  }catch(e){ toast(e.message||String(e),"err"); }
}
async function storyAddImgs(){
  if(DESK()){
    try{
      const r=await pywebview.api.pick_images();
      if(r&&r.error) return toast(r.error,"err");
      const list=Array.isArray(r)?r:(r?[r]:[]);
      if(list.length){ STORY.imgs.push(...list); renderStory(); await storyPersistPack(); toast(`Đã thêm ${list.length} ảnh`,"ok"); }
    }catch(e){ toast(e.message||String(e),"err"); }
    return;
  }
  const p=prompt("Dán đường dẫn ảnh (mỗi lần một ảnh):")||"";
  if(p.trim()){ STORY.imgs.push(p.trim()); renderStory(); await storyPersistPack(); }
}
async function storyAddFolder(){
  let path="";
  if(DESK()){
    try{
      const r=await pywebview.api.pick_folder();
      if(r&&r.error) return toast(r.error,"err");
      path=Array.isArray(r)?(r[0]||""):(r||"");
    }catch(e){ return toast(e.message||String(e),"err"); }
  }else path=prompt("Dán đường dẫn thư mục ảnh:")||"";
  if(path.trim()){ STORY.imgs.push(path.trim()); renderStory(); await storyPersistPack(); toast("Đã thêm thư mục ảnh","ok"); }
}
function storyUseTitlePrompt(){
  const summary=String(MANUAL.writer_title||"").trim();
  MANUAL.writer_title=STORY_TITLE_PROMPT.replace("[DÁN TÓM TẮT]",summary||"[DÁN TÓM TẮT]");
  STORY.step=0; localStorage.setItem("advn_story_step","0"); renderStory();
  toast("Đã nạp prompt tạo 8 tiêu đề; hãy thay phần tóm tắt rồi chạy AI viết","ok");
}
function storyRequestPayload(includeText){
  const ta=document.getElementById("manualText");
  if(ta) MANUAL.text=ta.value;
  const d=storyDims();
  const payload={
    name:MANUAL.name,
    txt_path:MANUAL.script_path||"",
    engine:MANUAL.engine, voice:MANUAL.voice, pitch:MANUAL.pitch, rate:MANUAL.rate,
    anh:STORY.imgs, image_pack:STORY.pack, aspect:STORY.aspect,
    video_sources:STORY.source_videos||[],
    source_effect:STORY.source_effect||"tinh",
    source_clip_min_seconds:Math.max(2,Number(STORY.source_clip_min_minutes||5)*60),
    source_clip_max_seconds:Math.max(2,Number(STORY.source_clip_max_minutes||10)*60),
    source_random:!!STORY.source_random,
    source_random_seed:Number(STORY.source_random_seed)||0,
    source_transform:{zoom:Number(STORY.source_zoom)||100,x:Number(STORY.source_x)||50,
      y:Number(STORY.source_y)||50,crop_left:Number(STORY.source_crop_left)||0,
      crop_right:Number(STORY.source_crop_right)||0,crop_top:Number(STORY.source_crop_top)||0,
      crop_bottom:Number(STORY.source_crop_bottom)||0},
    source_cover:STORY.source_cover||"none",
    character:{enabled:!!STORY.character_enabled,scale:STORY.character_scale,opacity:STORY.character_opacity/100},
    scene_count:STORY.scene_count, auto_images:STORY.auto_images,
    w:d.w, h:d.h, fps:STORY.fps, kieu:STORY.kieu,
    nhac:{enabled:STORY.nhac_enabled, bai:MANUAL.nhac_bai,
          muc_db:MANUAL.nhac_db, duck:MANUAL.nhac_duck},
    cta:{enabled:STORY.cta_enabled, text:STORY.cta_text,
         positions:String(STORY.cta_positions||"12,55").split(/[,;\s]+/)
           .map(Number).filter(Number.isFinite), speed:STORY.cta_speed},
    logo:{enabled:STORY.logo_enabled, path:STORY.logo_path,
          position:STORY.logo_position, width_pct:STORY.logo_width,
          opacity:STORY.logo_opacity/100},
    sub:{enabled:STORY.sub_enabled, style:{
      size:STORY.sub.size, color:STORY.sub.color, outline:STORY.sub.outline,
      bold:STORY.sub.bold, align:STORY.sub.align, margin_v:STORY.sub.margin_v}},
    voice_auto:STORY.auto_voice,
    multi_voice:STORY.multi_voice,
    max_character_voices:8,
    regions: STORY.regions || (PR && PR.regions) || [],
    blur_bottom_ratio: STORY.source_cover === "blur_bottom" ? 0.22 : 0,
    content_idea_id:MANUAL.content_idea_id||"",
    youtube_description:MANUAL.youtube_description||"",
    youtube_tags:MANUAL.youtube_tags||[],
    content_outline:MANUAL.content_outline||"",
    rewrite_brief:MANUAL.rewrite_brief||"",
  };
  if(includeText) payload.text=MANUAL.text;
  return payload;
}
async function storyGenerateAndRun(){
  const title=String(MANUAL.writer_title||"").trim();
  if(!title) return toast("Hãy nhập tiêu đề truyện","warn");
  try{
    const payload=storyRequestPayload(false);
    payload.name=title;
    if(storyTitleKey(MANUAL.script_title)!==storyTitleKey(title))
      payload.txt_path="";
    const stalePack=STORY.auto_images&&STORY.pack&&
      storyTitleKey(STORY.packTitle)!==storyTitleKey(title);
    if(stalePack){
      // Ảnh được tự khôi phục từ localStorage thuộc truyện trước. Không gửi
      // chúng sang backend cho tiêu đề mới.
      payload.anh=[];
      payload.image_pack="";
    }else if(!STORY.imgs.length) payload.image_pack="";
    payload.story_title=title;
    await api("/api/story/generate_and_run",payload);
    if(stalePack){
      STORY.imgs=[]; STORY.pack=""; STORY.packTitle=title; STORY.sel=-1;
      localStorage.removeItem("advn_story_image_pack");
      _lastImageReadyCount=-1; _lastPromptPack="";
    }
    MANUAL.name=title;
    MANUAL.output_path=""; MANUAL.status="Đang tạo kịch bản từ tiêu đề…"; MANUAL.error="";
    MANUAL.image_status="";
    MANUAL.script_path=""; MANUAL.script_title=title; MANUAL.script_words=0;
    ST.manual={...(ST.manual||{}),working:true};
    renderStory();
    toast("Đã bắt đầu: viết truyện → prompt → ảnh → giọng → video","ok");
  }catch(e){ toast(e.message,"err"); }
}
async function storyResumeImages(){
  const title=String(MANUAL.writer_title||"").trim();
  if(!STORY.pack||!storyPackMatchesTitle(title)) return storyGenerateAndRun();
  try{
    const payload=storyRequestPayload(false);
    payload.anh=[];
    payload.image_pack=STORY.pack;
    payload.story_title=title;
    payload.txt_path=MANUAL.script_path||"";
    await api("/api/story/resume_images",payload);
    MANUAL.output_path=""; MANUAL.error="";
    MANUAL.status=`Đang tiếp tục từ ${MANUAL.image_ready}/${MANUAL.image_total} ảnh…`;
    ST.manual={...(ST.manual||{}),working:true};
    renderStory(); toast("Đang tạo tiếp các cảnh còn thiếu; ảnh cũ được giữ nguyên","ok");
  }catch(e){ toast(e.message||String(e),"err"); }
}
async function storyRunAll(){
  storyRequestPayload(true);
  if(!String(MANUAL.text||"").trim()) return toast("Hãy nhập nội dung truyện (bước 1)","warn");
  if(!STORY.imgs.length&&!((STORY.source_videos||[]).length))
    return toast("Hãy thêm ảnh hoặc tải video nguồn ở cột trái","warn");
  try{
    await api("/api/manual/run_all",storyRequestPayload(true));
    MANUAL.output_path=""; MANUAL.status="Bắt đầu làm video kể chuyện…"; MANUAL.error="";
    ST.manual={...(ST.manual||{}),working:true};
    renderStory(); toast("Đã bắt đầu: giọng đọc → nhạc nền → phụ đề → video","ok");
  }catch(e){ toast(e.message,"err"); }
}

async function storyRenderReadyAudio(){
  if(!MANUAL.audio_path) return toast("Chưa có audio để dựng video","warn");
  if(!((STORY.source_videos||[]).length))
    return toast("Hãy chọn thư mục hoặc video nguồn trước","warn");
  try{
    const payload=storyRequestPayload(false);
    payload.audio_path=MANUAL.audio_path;
    await api("/api/manual/slideshow",payload);
    MANUAL.output_path=""; MANUAL.status="Đang random video cho đủ thời lượng audio…";
    MANUAL.error=""; ST.manual={...(ST.manual||{}),working:true};
    renderStory();
    toast("Đã vào thẳng bước random video; không tạo lại giọng và nhạc","ok");
  }catch(e){ toast(e.message,"err"); }
}

/* ======================= khởi động ======================= */
async function init(){
  await loadConfig();
  await storyRestoreImagePack();
  setMode(MODE);
  refresh();
  setInterval(refresh,1200);
}
init();
