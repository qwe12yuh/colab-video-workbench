"use strict";

const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const statusText = {pending:"待生成",review:"待审片",approved:"已确认",running:"生成中"};
const labels = {generate:"生成视频",qa:"对白转写",approve:"确认镜头",cut:"导出成片",release:"释放 GPU",voice:"合成音色"};
const statuses = {queued:"排队",running:"执行中",complete:"完成",failed:"失败",cancelled:"已取消",interrupted:"已中断"};
const stages = {queued:"准备",auth:"验证授权",allocate:"抢占显卡",verify_gpu:"核验显存",render:"准备渲染",
                release:"释放 GPU",voice:"合成音色",qa:"转写对白",approve:"检查素材",cut:"剪辑并验证",done:"完成"};
const generateSteps = [["auth","授权"],["allocate","抢卡"],["verify_gpu","验显存"],["render","渲染"],["release","释放"]];
const voiceSteps = [["auth","授权"],["allocate","抢小卡"],["verify_gpu","验显存"],["voice","合成"],["release","释放"]];
const voicePresets = ["二十多岁的女性，声音清澈温柔，语速偏慢","三十多岁的男性，嗓音低沉稳重，略带沙哑",
  "十几岁的少年，声音明亮有朝气","七十岁左右的老人，声音苍老缓慢、和蔼","字正腔圆的播音腔，干净有力","慵懒随性的成熟女声，尾音上扬"];
let voiceCatalog = null, voiceActor = "";
const fallbackGpus = [{id:"G4",label:"G4 · RTX PRO 6000 96GB",min_mib:90000},
  {id:"H100",label:"H100 · 80GB",min_mib:79000},{id:"A100",label:"A100 · 只要 80GB",min_mib:79000}];
const gpuDefault = {gpus:["G4"],rounds:3,wait:45,high_mem:true};
let project, projects = [], jobs = [], currentShot = "", focusedJob = "", editorTab = "shot";
let savedManifest, dirty = false, working = false, exportPreview = null, previewEnd = null;
let gatewayConfig = {active:"",profiles:[]}, quickThumb = "", agentText = "";
let syncing, ownerState = "", usageAt = 0, usageRequest, jobFingerprints = new Map(), changedProjects = new Set();
const cancelling = new Set();
let gpuOptions = fallbackGpus, gpuPlan = loadGpuPlan(), noticeTimer = 0;
let directorPlan = null, aiBusy = "", lastRewrite = null, voiceRefresh = false;
let colabState = "", assistantMessages = [], assistantBusy = false, nextActions = [];
let agentTranscript = [], agentSession = "";
const traceLabels = {workbench_status:"任务状态",read_job:"任务日志",read_project:"项目分镜",colab_usage:"Colab 余额",doctor:"环境自检"};
function pendingText(pending) {
  const args = pending.args || {}, parts = [];
  if (args.job_id) {
    const row = jobs.find(item => item.id === args.job_id);
    parts.push(row ? labels[row.action] + " · " + projectTitle(row.project) : "任务 " + String(args.job_id).slice(0,8));
  }
  if (Array.isArray(args.gpus) && args.gpus.length) parts.push("显卡 " + args.gpus.map(gpuName).join(" → "));
  if (Number.isInteger(args.rounds)) parts.push(args.rounds + " 轮");
  if (pending.name === "retry_job") parts.push("生成或合成会消耗 Colab 算力");
  return parts.join(" · ");
}
const rewritePresets = [["cinematic","更有电影感"],["emotion","强化情绪"],["camera","补全运镜"],
  ["concise","精简要点"],["continuity","衔接前后"],["custom","自定义…"]];

function loadGpuPlan() {
  try {
    const value = JSON.parse(localStorage.getItem("gpuPlan") || "null");
    if (value && Array.isArray(value.gpus) && value.gpus.length) return {...gpuDefault,...value};
  } catch (_) {}
  return {...gpuDefault};
}
const gpuName = id => ({G4:"G4 96G",H100:"H100 80G",A100:"A100 80G",L4:"L4 24G",T4:"T4 16G"})[id] || id;
const gpuSummary = () => gpuPlan.gpus.map(gpuName).join(" → ") + " · " + gpuPlan.rounds + " 轮";
function renderGpuSummary() {
  $("gpu-summary").textContent = gpuSummary();
  $("quick-gpu").textContent = gpuSummary();
}
function gpuWarning(shots) {
  if (gpuPlan.gpus.includes("G4")) return "";
  const long = (shots || []).filter(row => Number(row.seconds) > 7);
  return long.length ? "只选了 80GB 卡：" + long.map(row => row.id.toUpperCase()).join("、")
    + " 超过 7 秒，显存可能不足。建议把 G4 加入候选或缩短镜头。" : "";
}
async function request(path, body) {
  const init = body === undefined ? {} : {method:"POST",
    headers:{"Content-Type":"application/json","X-Workbench":"1"},body:JSON.stringify(body)};
  const response = await fetch(path, init);
  const value = await response.json();
  if (!response.ok) throw Error(value.error || ("请求失败 " + response.status));
  return value;
}
function notice(message, error=false) {
  clearTimeout(noticeTimer);
  $("notice").textContent = message;
  $("notice").hidden = !message;
  $("notice").classList.toggle("error",error);
  if (message && !error) noticeTimer = setTimeout(() => {$("notice").hidden = true;},4200);
}
function handle(operation, feedback=notice) {
  return async (...args) => {
    try { await operation(...args); }
    catch (error) { feedback(error.message || String(error),true); }
  };
}
function status(element, text, className="") {
  if (element.textContent !== text) element.textContent = text;
  element.className = "badge " + className;
}
function button(text, action, value="", cls="quiet", extra="") {
  return '<button type="button" class="' + cls + '" data-action="' + action + '" data-value="' + esc(value) + '" '
    + extra + '>' + esc(text) + '</button>';
}
function field(label, key, value, type="text", extra="") {
  return '<label class="field">' + esc(label) + '<input data-field="' + key + '" type="' + type
    + '" value="' + esc(value) + '" ' + extra + '></label>';
}
function area(label, key, value) {
  return '<label class="field">' + esc(label) + '<textarea data-field="' + key + '" rows="2">' + esc(value) + '</textarea></label>';
}
function replaceMarkup(el, html) {
  if (el.dataset.view === html) return;
  el.innerHTML = html;
  el.dataset.view = html;
}
const shot = () => project?.manifest.shots.find(row => row.id === currentShot);
const takeFor = id => project?.videos.find(row => row.id === id);
const isActive = row => ["queued","running"].includes(row.status);
const activeTask = () => jobs.find(isActive);
const unresolved = () => jobs.find(row => row.may_own_runtime && !isActive(row));
const busy = () => Boolean(working || activeTask());
const currentBusy = () => Boolean(activeTask() && activeTask().project === project?.id);
const castLabel = (id, actor) => actor?.label || id;
const takeState = id => activeTask()?.action === "generate" && activeTask().project === project?.id
  && activeTask().shots.includes(id) ? "running" : takeFor(id)?.state || "pending";
function generationChanged(row) {
  if (!dirty || !savedManifest) return false;
  const old = savedManifest.shots.find(item => item.id === row.id);
  if (!old || old.source !== row.source) return true;
  const inputs = (manifest,item) => [
    manifest.aspect, manifest.style, manifest.locations[item.location], item.cast.map(id => {
      const actor = manifest.characters[id];
      return [id,actor?.description,actor?.image,actor?.voice];
    }), item.action, item.seconds, item.seed, item.input_mode, item.dialogue
  ];
  return JSON.stringify(inputs(project.manifest,row)) !== JSON.stringify(inputs(savedManifest,old));
}
function trimChanged(row) {
  const old = savedManifest?.shots.find(item => item.id === row.id);
  return Number(old?.trim_start || 0) !== Number(row.trim_start || 0);
}
const pendingIds = () => project?.manifest.shots.filter(row => !row.source && (takeState(row.id) === "pending" || generationChanged(row)))
  .map(row => row.id) || [];
const assetURL = path => "/media/" + project.id + "/asset/" + encodeURIComponent(String(path).split("/").pop());
const fmt = number => new Intl.NumberFormat("zh-CN", {maximumFractionDigits:2}).format(number);

function markDirty() {
  dirty = true;
  updateStage();
  updateActions();
  updateDelivery();
}
function projectTitle(id) {
  return projects.find(row => row.id === id)?.title || (id ? id : "");
}
function remember() {
  localStorage.setItem("currentProject", project.id);
  if (currentShot) localStorage.setItem("shot:" + project.id, currentShot);
}
function renderNav() {
  replaceMarkup($("projects"), projects.map(row => button(row.title,"project",row.id,row.id === project?.id ? "on" : "",
    row.id === project?.id ? 'aria-current="page"' : "")).join(""));
}
function shotThumb(row) {
  const actor = project.manifest.characters[row.cast?.[0]];
  if (row.source) return '<span class="shot-thumb">MP4</span>';
  return actor?.image ? '<span class="shot-thumb"><img alt="" loading="lazy" src="' + esc(assetURL(actor.image)) + '"></span>'
    : '<span class="shot-thumb">' + esc(row.id.toUpperCase()) + '</span>';
}
function renderTimeline() {
  const shots = project.manifest.shots;
  replaceMarkup($("shots"), shots.map((row,index) => {
    const state = takeState(row.id), selected = row.id === currentShot && !exportPreview;
    return '<button type="button" class="shot-chip' + (selected ? ' on' : '') + '" data-action="shot" data-value="'
      + esc(row.id) + '" aria-pressed="' + selected + '">' + shotThumb(row) + '<strong><em>'
      + String(index+1).padStart(2,"0") + '</em>' + esc(row.title) + '</strong><span class="shot-meta"><small>'
      + esc(row.seconds) + ' 秒</small><span class="badge ' + state + '">' + statusText[state] + '</span></span></button>';
  }).join(""));
  replaceMarkup($("ruler"), shots.map(row => '<span class="' + takeState(row.id)
    + (row.id === currentShot && !exportPreview ? " on" : "") + '" data-action="shot" data-value="' + esc(row.id)
    + '" data-grow="' + esc(row.seconds) + '" title="' + esc(row.id.toUpperCase() + " · " + row.seconds + " 秒") + '"></span>').join(""));
  for (const item of $("ruler").children) item.style.flexGrow = item.dataset.grow;
  const total = shots.reduce((sum,row) => sum + Number(row.seconds),0);
  $("timeline-total").textContent = shots.length + " 镜 · " + fmt(total) + " 秒";
}
function updateStage() {
  if (!project || !shot()) return;
  const current = shot(), take = takeFor(current.id), state = takeState(current.id);
  const clip = exportPreview || take;
  const stage = $("stage");
  const url = clip?.url || "";
  const icon = state === "running" ? "◐" : "▷";
  const note = exportPreview ? (exportPreview.current && !dirty ? "当前成片" : "历史成片")
    : dirty && url ? "已保存的素材" : statusText[state];
  status($("preview-status"),note,exportPreview ? (exportPreview.current && !dirty ? "approved" : "") : state);
  $("preview-title").textContent = exportPreview ? "成片预览" : current.title + " · 预览";
  $("show-shot").hidden = !exportPreview;
  $("play-trim").hidden = Boolean(!take?.url || exportPreview);
  if (url !== (stage.dataset.src || "")) previewEnd = null;
  stage.dataset.src = url;
  if (url) {
    replaceMarkup(stage,'<video controls preload="metadata" aria-label="' + (exportPreview ? "成片预览" : "镜头预览")
      + '" src="' + esc(url) + '"></video>');
  } else {
    const title = state === "running" ? "后台正在生成这个镜头" : "这个镜头还没有视频";
    const text = state === "running" ? "右侧任务区显示抢卡和渲染进度，回传后可直接播放。"
      : current.source ? "导入的素材无法读取，请在「剪辑与字幕」重新导入。"
      : "写好画面要求后点「抢卡并生成」，完成后在这里审片。";
    replaceMarkup(stage,'<div class="stage-empty"><span class="stage-symbol' + (state === "running" ? " spin" : "")
      + '" aria-hidden="true">' + icon
      + '</span><b>' + title + '</b><p>' + text + '</p></div>');
  }
  const trim = Number(current.trim_start || 0);
  const trimNote = !exportPreview && take?.url ? "原片预览 · 成片取段 " + fmt(trim) + "–" + fmt(trim + current.seconds) + " 秒"
    + (take.asr && !dirty ? " · 转写：" + (take.asr.heard || "未识别到对白") : "") : "";
  $("preview-note").textContent = trimNote;
  $("preview-note").hidden = !trimNote;
}
function voiceTakes(id) {
  const row = jobs.find(item => item.action === "voice" && item.project === project.id && item.voice?.actor === id
    && item.status === "complete" && item.voice_candidates?.length);
  return row ? row.voice_candidates : [];
}
function voiceBox(id,actor) {
  const current = actor.voice ? '<div class="voice-take"><audio controls preload="none" src="' + esc(assetURL(actor.voice))
    + '"></audio>' + button("移除","voice-remove",id) + '</div>'
    : '<small>还没有音色：H3 会按画面自动配一个声音。想固定音色，上传一段 2–15 秒录音，或用 AI 合成。</small>';
  const takes = voiceTakes(id).filter(take => take.path !== actor.voice);
  return '<div class="voice-box"><div class="section-head"><h3>参考音色</h3>' + (actor.voice ? '<span class="badge ok">已设置</span>' : '')
    + '</div>' + current + (takes.length ? '<small>最近合成的候选：</small>' + takes.map(take => '<div class="voice-take"><audio controls preload="none" src="'
      + esc(assetURL(take.path)) + '"></audio>' + button("用这个 · " + fmt(take.seconds) + " 秒","voice-use",take.path,"quiet",'data-actor="' + esc(id) + '"') + '</div>').join("") : '')
    + '<div class="group"><label class="quiet-file">上传录音<input type="file" data-asset="voice" accept="audio/wav,audio/mpeg"></label>'
    + button("✦ AI 合成音色","voice-open",id,"quiet ai") + '</div></div>';
}
function aiRow(current) {
  const disabled = aiBusy || !gatewayConfig.active ? " disabled" : "";
  const undo = lastRewrite?.shot === current.id ? button("撤销改写","ai-undo","","quiet",aiBusy ? "disabled" : "") : "";
  const label = aiBusy ? "改写中…" : gatewayConfig.active ? "AI 改写" : "AI 改写（先连接模型网关）";
  return '<div class="ai-row"><span class="label"><svg aria-hidden="true"><use href="#i-spark"/></svg>' + label + '</span>'
    + rewritePresets.map(([id,text]) => button(text,"ai-rewrite",id,"quiet",disabled)).join("") + undo + '</div>';
}
function shotContext(row) {
  const manifest = project.manifest, index = manifest.shots.indexOf(row);
  const neighbor = (item,label) => item ? label + "：" + item.action.slice(0,160) : "";
  return {action:row.action,seconds:row.seconds,location:manifest.locations[row.location],style:manifest.style,
    cast:row.cast.map(id => castLabel(id,manifest.characters[id]) + "：" + (manifest.characters[id]?.description || "")),
    neighbors:[neighbor(manifest.shots[index-1],"上一镜"),neighbor(manifest.shots[index+1],"下一镜")].filter(Boolean).join("\n")};
}
async function rewriteShot(preset, given) {
  const row = shot();
  if (!row.action.trim()) throw Error("先写一点画面描述，再让 AI 改写。");
  let instruction = given;
  if (!instruction && preset === "custom") {
    instruction = window.prompt("想怎么改？例如：改成黄昏逆光，人物更紧张","");
    if (!instruction?.trim()) return;
  }
  aiBusy = row.id;presentEditor(true);
  try {
    const result = await request("/api/director/rewrite",{...shotContext(row),
      ...(instruction ? {instruction:instruction.trim()} : {preset})});
    if (shot() !== row) return;
    lastRewrite = {shot:row.id,action:row.action};
    row.action = result.action;
    notice("已用 " + result.model + " 改写，可撤销。");
    markDirty();
  } finally {aiBusy = "";presentEditor(true);}
}
function renderEditor() {
  const current = shot();
  if (!current) return;
  const manifest = project.manifest;
  const imported = Boolean(current.source), quick = current.input_mode === "reference_prompt";
  const tabs = [["shot","镜头"],["actors","角色素材"],["cut","剪辑与字幕"]].map(([id,label]) =>
    button(label,"tab",id,editorTab === id ? "on" : "",'aria-pressed="' + (editorTab === id) + '"')).join("");
  const characters = Object.entries(manifest.characters);
  let body;
  if (editorTab === "actors") {
    body = '<div class="section-head"><h3>项目角色</h3>' + button("＋ 导入角色","actor-add") + '</div>'
      + characters.map(([id,actor]) =>
        '<article class="actor-row" data-actor="' + esc(id) + '"><div class="group"><img class="actor-thumb" alt="" src="'
        + esc(assetURL(actor.image)) + '"><div><strong>' + esc(castLabel(id,actor)) + '</strong><small>　@'
        + esc(id) + (actor.voice ? " · 已有音色" : "") + '</small></div></div>'
        + '<label class="field">外观说明<input data-description value="' + esc(actor.description) + '"></label>'
        + '<label>替换参考图<input type="file" data-asset="image" accept="image/png,image/jpeg"></label>'
        + voiceBox(id,actor) + '</article>').join("");
  } else if (editorTab === "cut") {
    const captions = (current.subtitles || []).map((line,index) =>
      '<div class="dialogue cue" data-cue="' + index + '">'
      + '<input type="number" min="0" step="0.01" aria-label="字幕开始秒" data-cue-field="start" value="' + esc(line.start) + '">'
      + '<input type="number" min="0" step="0.01" aria-label="字幕结束秒" data-cue-field="end" value="' + esc(line.end) + '">'
      + '<input aria-label="字幕文字" data-cue-field="text" value="' + esc(line.text) + '">'
      + button("×","caption-remove",index,"icon",'aria-label="移除字幕"') + '</div>').join("");
    const index = manifest.shots.indexOf(current);
    body = '<div class="fields">'
      + field("裁掉开头（秒）","trim_start",current.trim_start || 0,"number",'min="0" max="60" step="0.01"')
      + field("成片时长（秒）","seconds",current.seconds,"number",'min="3" max="12"')
      + '</div><div class="section-head"><h3>成片字幕</h3>'
      + '<label class="muted"><input type="checkbox" data-field="native_captions"' + (current.native_captions ? " checked" : "")
      + '> 素材已有画内字幕</label></div><small>字幕时间从剪辑后的镜头开头算。</small>' + captions
      + '<div class="group">' + button("＋ 字幕","caption-add") + button("从对白复制","caption-copy") + '</div>'
      + '<label class="field">导入本地视频<input type="file" data-asset="source" accept="video/mp4"></label>'
      + '<div class="group">' + button("上移","move","-1","quiet",index === 0 ? "disabled" : "")
      + button("下移","move","1","quiet",index === manifest.shots.length-1 ? "disabled" : "")
      + button("移出时间线","remove-shot","", "quiet", manifest.shots.length < 2 ? "disabled" : "") + '</div>';
  } else {
    const refs = characters.map(([id, actor]) =>
      '<label class="ref"><input type="checkbox" data-cast="' + esc(id) + '" '
      + (current.cast.includes(id) ? "checked" : "") + '><img alt="" src="' + esc(assetURL(actor.image)) + '"><span>'
      + esc(castLabel(id,actor)) + (actor.voice ? " · 音色" : "") + '</span></label>').join("");
    const dialogue = (current.dialogue || []).map((line, index) => {
      const options = [...new Set([line.speaker,...current.cast])].map(id =>
        '<option value="' + esc(id) + '"' + (line.speaker === id ? " selected" : "") + '>'
        + esc(castLabel(id,manifest.characters[id])) + (current.cast.includes(id) ? "" : "（未选）") + '</option>').join("");
      return '<div class="dialogue" data-line="' + index + '"><select aria-label="说话角色" data-line-field="speaker">'
        + options + '</select><input type="number" min="0" step="0.1" aria-label="开始秒" data-line-field="start" value="'
        + esc(line.start) + '"><input type="number" min="0" step="0.1" aria-label="结束秒" data-line-field="end" value="'
        + esc(line.end) + '"><input aria-label="台词" data-line-field="text" value="' + esc(line.text) + '">'
        + button("×","line-remove",index,"icon",'aria-label="移除台词"') + '</div>';
    }).join("");
    const options = '<div class="fields">' + field("镜头名","title",current.title)
      + field("时长（秒）","seconds",current.seconds,"number",'min="3" max="12"')
      + '<label class="field">全片画幅<select data-field="aspect"><option value="portrait"'
      + (manifest.aspect === "portrait" ? " selected" : "") + '>9:16 竖屏</option><option value="landscape"'
      + (manifest.aspect === "landscape" ? " selected" : "") + '>16:9 横屏</option></select></label></div>';
    body = imported
      ? options + '<div class="group"><small>已导入本地素材。</small>'
        + button("调裁剪和字幕","tab","cut") + button("切换为角色参考","switch-mode") + '</div>'
      : '<div class="refs" aria-label="参考角色">' + refs + button("＋ 导入角色","actor-add") + '</div>'
        + area(quick ? "中文要求" : "画面要求 · 动作、表演与运镜","action",current.action) + aiRow(current) + options
        + (quick ? '<small>对白和声音可以直接写在中文要求里。</small>' + '<div>' + button("使用分镜对白","storyboard-mode") + '</div>'
          : '<details class="details"><summary>对白 · ' + (current.dialogue || []).length + ' 句</summary>'
          + '<small>对白结尾留出约 1 秒。</small>' + dialogue + button("＋ 台词","line-add") + '</details>'
          + '<details class="details"><summary>场景和风格</summary>'
          + area("场景描述","location",manifest.locations[current.location]) + area("全片风格","style",manifest.style) + '</details>');
  }
  $("editor").innerHTML = '<div class="editor-head"><div class="tabs" role="group" aria-label="镜头设置标签">' + tabs
    + '</div><span class="badge">' + (imported ? "本地素材" : quick ? "参考图 + 中文要求" : "MiniMax H3 · 分镜")
    + '</span></div><div class="editor-body">' + body + '</div>';
  updateActions();
}
function updateActions() {
  const current = shot();
  if (!current) return;
  const take = takeFor(current.id);
  const blocked = busy();
  const generateBlocked = blocked || Boolean(unresolved());
  const canGenerate = !current.source && (!take?.url || generationChanged(current));
  const saveButton = dirty ? button("保存修改","save","","quiet",working || currentBusy() ? "disabled" : "") : "";
  const qa = take?.url ? button("核对对白","qa","","quiet",blocked ? "disabled" : "") : "";
  let next;
  if (takeState(current.id) === "running") next = '<span class="badge running">后台生成中</span>';
  else if (canGenerate) next = button("抢卡并生成","generate","","primary",generateBlocked ? "disabled" : "");
  else if (take?.state === "review" || dirty && take?.url && (trimChanged(current) || current.source))
    next = button("确认镜头","approve","","primary",blocked ? "disabled" : "");
  else next = '<span class="badge approved">已确认</span>';
  const regenerate = take?.url && !current.source ? button("准备新版本","seed","","quiet",currentBusy() ? "disabled" : "") : "";
  const warning = canGenerate ? gpuWarning([current]) : "";
  replaceMarkup($("editor-actions"),'<span class="' + (dirty ? "dirty-dot" : warning ? "hint warn" : "muted") + '">'
    + esc(dirty ? "修改尚未保存" : warning || ("显卡：" + gpuSummary())) + '</span><div class="group">'
    + saveButton + qa + regenerate + next + '</div>');
  const batchCount = pendingIds().length;
  $("batch").hidden = batchCount < 2;
  $("batch").textContent = "抢卡生成 " + batchCount + " 镜";
  $("batch").disabled = generateBlocked;
  $("quick-render").disabled = generateBlocked;
  $("add-shot").disabled = working || currentBusy();
  if (!$("quick-hint").classList.contains("job-error")) $("quick-hint").textContent = unresolved() ? "请先处理任务区尚未释放的 GPU。"
    : busy() ? "后台任务运行中，可以先保存草稿。"
    : "按「" + gpuSummary() + "」抢卡，显存不达标自动释放重抢。完成后回传并释放 GPU。";
}
function renderExport(row,index) {
  const current = row.current && !dirty;
  return '<div class="export-card"><div class="section-head"><strong>' + esc(row.seconds) + ' 秒 · ' + esc(row.width) + '×'
    + esc(row.height) + '</strong><span class="badge ' + (current ? 'approved' : '') + '">' + (current ? "当前成片" : "历史成片")
    + '</span></div><div class="group">' + button("预览","show-export",index)
    + '<a href="' + esc(row.url) + '" download="film.mp4">保存 MP4</a>'
    + (row.has_subtitles ? '<a href="' + esc(row.subtitles) + '" download="timeline.srt">字幕</a>' : '') + '</div></div>';
}
function updateDelivery() {
  if (!project) return;
  const counts = project.manifest.shots.map(row => takeState(row.id));
  const approved = counts.filter(state => state === "approved").length;
  const current = project.exports.some(row => row.current) && !dirty;
  $("approved-count").textContent = approved + " / " + counts.length + " 镜确认";
  replaceMarkup($("export-progress"),counts.map(state => '<span class="' + state + '"></span>').join(""));
  $("export-progress").setAttribute("aria-label",approved + " 镜已确认");
  $("export-hint").textContent = dirty ? "保存修改后更新导出状态。"
    : current ? "当前时间线已导出。" : approved === counts.length ? "全部镜头已确认，可以导出。"
    : counts.includes("review") ? "先确认待审片的镜头。" : "生成并确认全部镜头后导出。";
  $("export").disabled = busy() || dirty || approved !== counts.length || current;
  replaceMarkup($("exports"),project.exports.slice(0,4).map(renderExport).join(""));
}
function renderProject() {
  renderNav();
  renderAssistant();
  $("workspace").hidden = !project;
  $("welcome").hidden = Boolean(project);
  if (!project) return;
  $("title").textContent = project.manifest.title;
  $("mode").textContent = (project.manifest.aspect === "landscape" ? "16:9 横屏" : "9:16 竖屏")
    + " · " + project.manifest.shots.length + " 镜";
  renderTimeline();
  updateStage();
  renderEditor();
  updateDelivery();
}
async function loadProject(id) {
  project = await request("/api/project/" + id);
  savedManifest = structuredClone(project.manifest);
  currentShot = localStorage.getItem("shot:" + id) || "";
  if (!shot()) currentShot = project.manifest.shots[0].id;
  dirty = false;
  exportPreview = null;
  editorTab = "shot";
  remember();
  renderProject();
}
async function save() {
  if (!dirty) return;
  project.manifest.target_seconds = project.manifest.shots.reduce((sum,row) => sum + Number(row.seconds),0);
  project = await request("/api/project/" + project.id,{revision:project.revision,manifest:project.manifest});
  savedManifest = structuredClone(project.manifest);
  dirty = false;
  changedProjects.delete(project.id);
  renderProject();
  notice("项目已保存。");
}
async function locked(operation) {
  if (working) return;
  working = true;
  $("editor").inert = true;
  updateActions();
  updateDelivery();
  try { return await operation(); }
  finally {
    working = false;
    $("editor").inert = false;
    updateActions();
    updateDelivery();
  }
}
async function submit(action, ids=[], target) {
  const body = {action,project:project?.id,shots:ids,target};
  if (action === "generate") body.gpu = gpuPlan;
  const row = await request("/api/run",body);
  focusedJob = row.id;
  notice("已提交" + labels[action] + "任务。");
  await syncJobs();
}
async function enqueue(action, ids=[], target) {
  if (busy()) throw Error("后台已有任务。");
  if (action === "generate" && unresolved()) throw Error("请先处理未确认释放的 GPU。");
  return locked(async () => {
    if (action !== "release") await save();
    if (action === "generate") {
      ids = ids.filter(id => takeFor(id)?.state === "pending" && !takeFor(id)?.source);
      if (!ids.length) throw Error("所选镜头已生成。可先准备新版本，再生成。");
    }
    if (["qa","approve"].includes(action) && !ids.every(id => takeFor(id)?.url))
      throw Error("修改后的镜头需要先生成，再审片。");
    await submit(action,ids,target);
  });
}

function taskStage(row, log="") {
  if (row.cancel_requested || cancelling.has(row.id)) return row.step === "release" ? "正在释放 GPU" : "正在取消";
  if (row.step === "release" && ["generate","voice"].includes(row.action) && !row.gpu) return "释放不合格显卡";
  if (row.step === "voice" && log) {
    for (const line of log.trim().split("\n").reverse()) {
      if (line.includes("[voice] fetched")) return "回传音色候选";
      const take = line.match(/\[voice\] take (\d+\/\d+)/);
      if (take) return "合成候选 " + take[1];
      if (line.includes("[voice] loading")) return "下载并加载 TTS 模型";
      if (line.includes("[voice] installing")) return "首次安装 Qwen3-TTS";
      if (line.includes("调用 TTS 接口")) return "调用 TTS 接口";
    }
    return "上传请求";
  }
  if (row.step !== "render" || !log) return stages[row.step] || row.step;
  for (const line of log.trim().split("\n").reverse()) {
    if (line.includes("Fetched and verified")) return "已回传，核验文件";
    if (line.includes("ready for review")) return "等待回传";
    if (line.includes(" · decoding")) return "解码画面和声音";
    if (line.includes("sampling six steps")) return "生成画面和声音";
    if (line.includes("Loading the Ref2VA transformer")) return "加载 H3 主模型";
    if (line.includes("encoding references")) return "编码参考素材";
    if (line.includes("encoding prompt")) return "编码提示词";
    const progress = line.match(/Fetching \d+ files:.*? (\d+\/\d+) \[/);
    if (progress) return "下载模型文件 " + progress[1];
  }
  return "安装环境并准备模型";
}
function renderJobDetail(detail) {
  const pane = $("job-detail"), key = detail?.id || "";
  if (pane.dataset.key !== key) {
    pane.dataset.key = key;
    pane.innerHTML = key ? '<div class="job-run"><div class="job-line job-error" hidden></div>'
      + '<details><summary>运行日志</summary><pre tabindex="0"></pre></details></div>' : "";
  }
  if (!detail) return;
  const error = pane.querySelector(".job-error"), log = pane.querySelector("pre");
  error.hidden = !detail.error;
  error.textContent = detail.error || "";
  const text = detail.log || "等待日志…";
  if (log.textContent !== text) {
    const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 20;
    log.textContent = text;
    if (atEnd) log.scrollTop = log.scrollHeight;
  }
}
function elapsed(row) {
  const seconds = Math.max(0,Math.round((isActive(row) ? Date.now()/1000 : row.updated) - row.created));
  return seconds < 60 ? seconds + " 秒" : seconds < 3600 ? Math.floor(seconds/60) + " 分 " + seconds%60 + " 秒"
    : Math.floor(seconds/3600) + " 时 " + Math.floor(seconds%3600/60) + " 分";
}
function when(row) {
  const date = new Date(row.created*1000), today = new Date().toDateString() === date.toDateString();
  return date.toLocaleString("zh-CN",today ? {hour:"2-digit",minute:"2-digit"}
    : {month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"});
}
function stepper(row) {
  const steps = row.action === "generate" ? generateSteps : row.action === "voice" && row.session ? voiceSteps : null;
  if (!steps) return "";
  const current = row.step === "queued" ? -1 : steps.findIndex(([id]) => id === row.step);
  // Releasing a wrong card during allocation is part of the grab loop, not the final release.
  const index = row.step === "release" && !row.gpu ? 1 : current;
  return '<ol class="stepper">' + steps.map(([,label],i) =>
    '<li class="' + (i < index ? "done" : i === index ? "now" : "") + '">' + label + '</li>').join("") + '</ol>';
}
function gpuLine(row) {
  if (row.action !== "generate" && !(row.action === "voice" && row.session)) return "";
  const plan = row.gpu_plan?.gpus?.map(gpuName).join(" → ") || (row.action === "voice" ? "L4 24G → T4 16G" : "G4 96G");
  const text = row.gpu ? row.gpu.name + " · " + Math.round(row.gpu.mib/1024) + " GiB" + (row.gpu_attempts > 1 ? " · 第 " + row.gpu_attempts + " 次抢到" : "")
    : (row.gpu_note || "候选：" + plan).replace(/^\[抢卡\] /,"") + (row.gpu_attempts ? " · 已尝试 " + row.gpu_attempts + " 次" : "");
  return '<div class="job-gpu"><svg aria-hidden="true"><use href="#i-chip"/></svg><span>' + esc(text) + '</span></div>';
}
function renderJobs(detail) {
  const active = activeTask(), debt = unresolved();
  status($("count"),active ? "运行中" : debt ? "待释放" : "空闲",active ? "running" : debt ? "review" : "");
  let html;
  if (active) {
    const pendingCancel = active.cancel_requested || cancelling.has(active.id);
    html = '<div class="job-run"><div class="section-head"><strong>' + esc(labels[active.action])
      + '</strong><span class="badge running">' + esc(statuses[active.status]) + ' · ' + elapsed(active) + '</span></div><p class="job-line">'
      + esc([projectTitle(active.project),active.shots?.map(id => id.toUpperCase()).join("、")].filter(Boolean).join(" · "))
      + (active.action === "voice" ? " · " + esc(castLabel(active.voice?.actor,project?.manifest.characters[active.voice?.actor])) : "")
      + '</p>' + stepper(active) + '<p class="job-line"><b>' + esc(taskStage(active,detail?.id === active.id ? detail.log : ""))
      + '</b></p>' + gpuLine(active) + '<div class="group">'
      + button("查看日志","job",active.id)
      + (active.step !== "release" ? button(pendingCancel ? "取消已请求" : "取消任务","cancel",active.id,
        "quiet",pendingCancel ? "disabled" : "") : "") + '</div></div>';
  } else if (debt) {
    html = '<div class="job-run"><strong>上次申请的 GPU 未确认释放</strong><p class="job-line">'
      + esc(projectTitle(debt.project)) + '</p>' + button("释放 GPU","release",debt.id) + '</div>';
  } else {
    html = '<p class="job-line">当前没有正在执行的任务。生成时会按「' + esc(gpuSummary()) + '」抢卡。</p>';
  }
  replaceMarkup($("active-job"),html);
  const history = jobs.filter(row => row.id !== active?.id);
  $("task-history").hidden = !history.length;
  $("history-count").textContent = "(" + history.length + ")";
  replaceMarkup($("jobs"), history.slice(0,8).map(row =>
    '<button class="job-card' + (row.id === focusedJob ? " selected" : "")
    + '" data-action="job" data-value="' + esc(row.id) + '"><strong>' + esc(labels[row.action])
    + (row.gpu ? " · " + esc(gpuName(row.gpu.gpu)) : "") + '</strong><span class="badge '
    + ({complete:"ok",failed:"fail",interrupted:"fail",cancelled:""})[row.status] + '">' + esc(statuses[row.status])
    + '</span><small>' + esc([projectTitle(row.project),when(row),elapsed(row)].filter(Boolean).join(" · ")) + '</small></button>').join(""));
  if (detail !== undefined) renderJobDetail(detail);
}
async function updateJobs() {
  const rows = await request("/api/jobs");
  for (const row of rows) {
    const stamp = String(row.updated) + "|" + row.status;
    const seen = jobFingerprints.has(row.id);
    if (jobFingerprints.get(row.id) !== stamp && !isActive(row) && row.project) changedProjects.add(row.project);
    if (seen && jobFingerprints.get(row.id) !== stamp && row.action === "voice" && row.project === project?.id) {
      if (row.status === "complete") {editorTab = "actors";notice("音色合成完成：在「角色素材」里试听候选，点「用这个」后保存。");}
      else if (!isActive(row)) notice("音色合成" + statuses[row.status] + "：" + (row.error || "请查看任务日志。"),true);
      voiceRefresh = true;
    }
    jobFingerprints.set(row.id,stamp);
    if (!isActive(row)) cancelling.delete(row.id);
  }
  jobs = rows;
  const active = activeTask();
  const key = active?.action === "generate" ? active.id + ":" + active.step : "";
  if (key !== ownerState || active?.action === "generate" && Date.now()-usageAt > 30000) {
    ownerState = key;void refreshUsage();
  }
  if (!dirty && !working && project && changedProjects.has(project.id)) {
    const id = project.id, revision = project.revision;
    const data = await request("/api/project/" + id);
    if (!dirty && !working && id === project?.id && revision === project.revision) {
      project = data;
      changedProjects.delete(id);
      if (!shot()) currentShot = project.manifest.shots[0].id;
      if (exportPreview) exportPreview = project.exports.find(row => row.url === exportPreview.url) || null;
      if (data.revision !== revision) {savedManifest = structuredClone(data.manifest);renderProject();}
    }
  }
  if (project) {renderTimeline();updateStage();updateActions();updateDelivery();}
  if (voiceRefresh && project) {voiceRefresh = false;presentEditor(true);}
  renderAssistant();
  const selected = (focusedJob && jobs.find(row => row.id === focusedJob)) || active;
  renderJobs(selected ? await request("/api/job/" + selected.id) : null);
}
function syncJobs() {
  if (!syncing) syncing = updateJobs().finally(() => {syncing = null;});
  return syncing;
}
function refreshUsage() {
  if (usageRequest) return usageRequest;
  usageAt = Date.now();
  $("refresh").disabled = true;
  usageRequest = request("/api/usage").then(data => {
    $("balance").textContent = data.balance.toLocaleString("zh-CN",{minimumFractionDigits:2,maximumFractionDigits:2});
    $("rate").textContent = data.rate ? data.rate.toFixed(2) : "0";
    $("runway").textContent = data.rate ? fmt(data.balance/data.rate) + " 时" : "空闲";
    status($("cloud-status"),data.assignments ? data.assignments + " 台运行中" : "已连接",data.assignments ? "running" : "ok");
    colabState = "ok";renderAssistant();
    $("setup-info").textContent = "账户余额 " + data.balance.toFixed(2) + " 单元。"
      + "使用工作台专用授权。";
  }).catch(error => {
    $("balance").textContent = $("rate").textContent = $("runway").textContent = "—";
    status($("cloud-status"),"未连通","fail");
    colabState = "fail";renderAssistant();
    $("setup-info").textContent = error.message;
  }).finally(() => {$("refresh").disabled = false;usageRequest = null;});
  return usageRequest;
}
function addShot() {
  const before = shot();
  if (!before || working || currentBusy()) return;
  const latest = Math.max(0,...project.manifest.shots.map(row => Number(row.id.replace(/^s/,"")) || 0));
  currentShot = "s" + String(latest+1).padStart(2,"0");
  project.manifest.shots.push({id:currentShot,title:"新镜头",seconds:6,location:before.location,
    cast:[...before.cast],action:"",dialogue:[],seed:crypto.getRandomValues(new Uint32Array(1))[0]});
  exportPreview = null;editorTab = "shot";dirty = true;
  renderProject();
  $("editor").querySelector('[data-field="action"]')?.focus();
}
function presentEditor(preserve=false) {
  const expanded = preserve && $("editor").querySelector("details")?.open;
  renderEditor();
  if (expanded && $("editor").querySelector("details")) $("editor").querySelector("details").open = true;
}
function selectShot(id) {
  currentShot = id;exportPreview = null;previewEnd = null;
  remember();renderTimeline();updateStage();presentEditor();
}
async function playTrim() {
  const media = $("stage").querySelector("video");
  if (!media || media.readyState < 1) throw Error("请等预览载入后再播放。");
  const current = shot(), from = Number(current.trim_start || 0);
  if (from + current.seconds > media.duration + 0.05) throw Error("设置的取段超过了原片长度。");
  previewEnd = from + current.seconds;
  media.currentTime = from;
  await media.play();
}
$("stage").addEventListener("timeupdate", event => {
  if (previewEnd !== null && event.target.currentTime >= previewEnd) {
    previewEnd = null;
    event.target.pause();
  }
},true);

document.body.addEventListener("click",handle(async event => {
  const closer = event.target.closest("[data-close]");
  if (closer) {$(closer.dataset.close).close();return;}
  const item = event.target.closest("[data-action]");
  if (!item) return;
  const {action,value} = item.dataset;
  if (action === "job") {
    focusedJob = value;
    await syncJobs();
    if ($("job-detail").dataset.key !== value) await syncJobs();
    const details = $("job-detail").querySelector("details");
    if (details) details.open = true;
    return;
  }
  if (action === "cancel") {
    if (cancelling.has(value)) return;
    cancelling.add(value);renderJobs();
    try {await request("/api/cancel",{job:value});notice("已请求取消，后台会处理运行时。");}
    catch (error) {cancelling.delete(value);throw error;}
    finally {await syncJobs();}
    return;
  }
  if (action === "release") {await enqueue("release",[],value);return;}
  if (working) return;
  if (action === "project") {
    if (value === project?.id) return;
    if (dirty && !window.confirm("当前项目有未保存的修改。切换项目？")) return;
    await locked(() => loadProject(value));return;
  }
  if (action === "shot") {selectShot(value);return;}
  if (action === "show-export") {
    exportPreview = project.exports[Number(value)];previewEnd = null;
    renderTimeline();updateStage();return;
  }
  if (action === "tab") {editorTab = value;renderEditor();return;}
  if (action === "actor-add") {$("actor-dialog").showModal();return;}
  if (action === "save") {await locked(save);return;}
  if (action === "voice-open") {await showVoice(value);return;}
  if (action === "voice-use") {
    if (currentBusy()) throw Error("这个项目正在执行任务，等任务结束后再修改。");
    project.manifest.characters[item.dataset.actor].voice = value;
    markDirty();presentEditor();
    notice("已设为参考音色。保存修改后生效；用到该角色对白的镜头需要重新生成。");return;
  }
  if (action === "ai-rewrite" || action === "ai-undo") {
    if (currentBusy()) throw Error("这个项目正在执行任务，等任务结束后再修改。");
    if (aiBusy) return;
    if (action === "ai-rewrite") {await rewriteShot(value);return;}
    if (lastRewrite?.shot === currentShot) {shot().action = lastRewrite.action;lastRewrite = null;markDirty();presentEditor(true);}
    return;
  }
  if (["generate","qa","approve"].includes(action)) {await enqueue(action,[currentShot]);return;}
  if (currentBusy()) throw Error("这个项目正在执行任务，等任务结束后再修改。");
  const row = shot(), manifest = project.manifest;
  if (action === "seed") {
    row.seed = crypto.getRandomValues(new Uint32Array(1))[0];markDirty();
  } else if (action === "switch-mode") {
    delete row.source;delete row.native_captions;
    row.seed = crypto.getRandomValues(new Uint32Array(1))[0];markDirty();presentEditor();
  } else if (action === "storyboard-mode") {
    delete row.input_mode;markDirty();presentEditor();
  } else if (action === "move" || action === "remove-shot") {
    const items = manifest.shots, index = items.indexOf(row);
    if (action === "move") {
      const next = index + Number(value);
      if (next < 0 || next >= items.length) return;
      [items[index],items[next]] = [items[next],items[index]];
    } else {
      if (items.length < 2 || !window.confirm("从时间线移除此镜头？素材会保留。")) return;
      items.splice(index,1);
      currentShot = items[Math.min(index,items.length-1)].id;
    }
    markDirty();renderProject();
  } else if (action === "voice-remove") {
    delete manifest.characters[value].voice;markDirty();presentEditor();
  } else if (action.startsWith("caption-")) {
    row.subtitles ||= [];
    if (action === "caption-copy") row.subtitles = (row.dialogue || []).map(({start,end,text}) => ({start,end,text}));
    else if (action === "caption-remove") row.subtitles.splice(Number(value),1);
    else {
      const start = row.subtitles.at(-1)?.end || 0;
      if (start >= row.seconds) throw Error("镜头的字幕时间已用完。");
      row.subtitles.push({start,end:Math.min(start+1.5,row.seconds),text:""});
    }
    markDirty();presentEditor();
  } else if (action === "line-add" || action === "line-remove") {
    row.dialogue ||= [];
    if (action === "line-remove") row.dialogue.splice(Number(value),1);
    else {
      const start = row.dialogue.at(-1)?.end || 0;
      if (!row.cast.length || start >= row.seconds) throw Error("先选择说话角色，并留出对白时间。");
      row.dialogue.push({speaker:row.cast[0],start,end:Math.min(start+1.5,row.seconds),text:""});
    }
    markDirty();presentEditor(true);
  }
}));

$("editor").addEventListener("input",handle(async event => {
  const target = event.target, row = shot(), {field,cast,lineField,cueField} = target.dataset;
  if (field) {
    if (field === "location") project.manifest.locations[row.location] = target.value;
    else if (["aspect","style"].includes(field)) project.manifest[field] = target.value;
    else if (field === "seconds" || field === "trim_start") row[field] = Number(target.value);
    else if (field === "native_captions") row[field] = target.checked;
    else row[field] = target.value;
  } else if (cast) {
    if (target.checked && row.cast.length >= 3) {target.checked = false;throw Error("一个镜头最多选择三个角色。");}
    row.cast = target.checked ? [...row.cast,cast] : row.cast.filter(id => id !== cast);
    markDirty();presentEditor(true);return;
  } else if (lineField || cueField) {
    const name = lineField || cueField;
    const holder = lineField ? target.closest("[data-line]") : target.closest("[data-cue]");
    const items = lineField ? row.dialogue : row.subtitles;
    items[Number(holder.dataset.line ?? holder.dataset.cue)][name] =
      ["start","end"].includes(name) ? Number(target.value) : target.value;
  } else if (target.hasAttribute("data-description")) {
    project.manifest.characters[target.closest("[data-actor]").dataset.actor].description = target.value;
  } else return;
  markDirty();
}));

async function uploadData(file) {
  if (!file || !file.size || file.size > 17500000) throw Error("请选择不超过 17.5 MB 的素材。");
  let extension = "." + file.name.split(".").at(-1).toLowerCase();
  if (extension === ".jpeg") extension = ".jpg";
  const base64 = await new Promise((resolve,reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",")[1]);
    reader.onerror = () => reject(Error("读取素材失败。"));
    reader.readAsDataURL(file);
  });
  return {extension,base64};
}
async function uploadFile(id,file) {
  return (await request("/api/asset/" + id,await uploadData(file))).path;
}
$("editor").addEventListener("change",handle(async event => {
  const input = event.target, file = input.files?.[0], kind = input.dataset.asset;
  if (!file || !kind) return;
  if (currentBusy()) throw Error("请在任务结束后替换素材。");
  const id = project.id, row = shot(), actor = input.closest("[data-actor]")?.dataset.actor;
  await locked(async () => {
    const path = await uploadFile(id,file);
    if (kind === "source") row.source = path;
    else project.manifest.characters[actor][kind] = path;
    markDirty();renderProject();notice("素材已导入。保存修改后应用。");
  });
}));

function resetQuickImage() {
  if (quickThumb) URL.revokeObjectURL(quickThumb);
  quickThumb = "";
  $("quick-thumb").removeAttribute("src");
  $("quick-thumb").hidden = true;
}
function quickMessage(text,error=false) {
  $("quick-hint").textContent = text;
  $("quick-hint").classList.toggle("job-error",error);
}
function showQuick() {
  $("quick-hint").classList.remove("job-error");
  if (project) updateActions();
  else quickMessage("按「" + gpuSummary() + "」抢卡，显存不达标自动释放重抢。完成后回传并释放 GPU。");
  renderGpuSummary();
  $("quick-dialog").showModal();
  $("polish").hidden = !gatewayConfig.active;
}
function syncQuickFields() {
  const form = $("quick-form").elements;
  $("seconds-out").textContent = form.seconds.value + " 秒";
  $("prompt-count").textContent = form.prompt.value.length + " / 1800";
}
$("quick-form").addEventListener("input",syncQuickFields);
$("quick-form").addEventListener("reset",() => setTimeout(syncQuickFields));
for (const type of ["dragenter","dragover"]) $("quick-file-label").addEventListener(type,() => $("quick-file-label").classList.add("drag"));
for (const type of ["dragleave","drop"]) $("quick-file-label").addEventListener(type,() => $("quick-file-label").classList.remove("drag"));
$("quick-gpu-edit").onclick = () => showGpu();
$("quick").onclick = $("welcome-new").onclick = showQuick;
$("welcome-blank").onclick = () => $("new-dialog").showModal();
$("quick-form").elements.image.onchange = event => {
  resetQuickImage();
  const file = event.target.files[0];
  if (!file) return;
  quickThumb = URL.createObjectURL(file);
  $("quick-thumb").src = quickThumb;
  $("quick-thumb").hidden = false;
};
$("quick-form").onsubmit = handle(async event => {
  event.preventDefault();
  if (working) return;
  const createAndRender = event.submitter?.value !== "draft";
  if (createAndRender && (busy() || unresolved())) throw Error("等当前任务结束或释放 GPU，也可以先保存草稿。");
  if (dirty && !window.confirm("当前项目有未保存的修改。继续创建？")) return;
  const elements = event.target.elements;
  if (!elements.prompt.value.trim()) throw Error("请输入中文要求。");
  quickMessage("正在上传参考图…");
  $("quick-form").inert = true;
  try {
    await locked(async () => {
      const image = await uploadData(elements.image.files[0]);
      const result = await request("/api/quick",{...image,prompt:elements.prompt.value.trim(),
        aspect:elements.aspect.value,seconds:Number(elements.seconds.value)});
      projects = [...projects,{id:result.id,title:result.manifest.title}];
      await loadProject(result.id);
      event.target.reset();resetQuickImage();$("quick-dialog").close();
      if (createAndRender) await submit("generate",[currentShot]);
      else notice("参考图和中文要求已保存为草稿。");
    });
  } finally {
    $("quick-form").inert = false;
  }
},(message) => {quickMessage(message,true);if (!$("quick-dialog").open) notice(message,true);});
$("actor-form").onsubmit = handle(async event => {
  event.preventDefault();
  if (currentBusy()) throw Error("请在任务结束后导入角色。");
  const elements = event.target.elements, id = project.id;
  const label = elements.label.value.trim(), description = elements.description.value.trim();
  if (!label) throw Error("请输入角色名。");
  const row = shot(), voice = elements.voice.files[0];
  $("actor-form").inert = true;
  try {
    await locked(async () => {
      const actor = {label,description:description || (label + "，保持参考图中的人物外观。"),
        image:await uploadFile(id,elements.image.files[0])};
      if (voice) actor.voice = await uploadFile(id,voice);
      const key = "actor-" + crypto.randomUUID().slice(0,8);
      project.manifest.characters[key] = actor;
      if (!row.source && row.cast.length < 3) row.cast.push(key);
      event.target.reset();$("actor-dialog").close();
      editorTab = "actors";markDirty();renderProject();
      notice("角色已导入。请保存修改。");
    });
  } finally {
    $("actor-form").inert = false;
  }
},(message) => {$("actor-status").textContent = message;});
function gatewayMessage(message,error=false) {
  $("gateway-status").textContent = message;
  $("gateway-status").classList.toggle("job-error",error);
}
function gatewayData() {
  return {
    id:$("gateway-profile").value, kind:$("gateway-kind").value, name:$("gateway-name").value,
    base_url:$("gateway-url").value.trim(), key:$("gateway-key").value,
    clear_key:$("gateway-clear-key").checked, model:$("gateway-model").value
  };
}
function gatewayFields() {
  const api = $("gateway-kind").value === "api";
  $("gateway-api-fields").hidden = !api;
  $("gateway-login").hidden = api;
  $("gateway-check").textContent = api ? "检查并读取模型" : "检查本机登录";
  $("gateway-model").placeholder = api ? "选择返回的模型，或手动填写" : "留空使用 Codex 默认模型";
  $("gateway-model-list").replaceChildren();
}
function loadGatewayProfile() {
  const row = gatewayConfig.profiles.find(profile => profile.id === $("gateway-profile").value);
  $("gateway-kind").value = row?.kind || "codex";
  $("gateway-name").value = row?.name || "";
  $("gateway-url").value = row?.base_url || "";
  $("gateway-key").value = "";
  $("gateway-key").placeholder = row?.has_key ? "已保存，留空保留" : "服务不需要密钥时可留空";
  $("gateway-clear-key").checked = false;
  $("gateway-model").value = row?.model || "";
  gatewayFields();
  gatewayMessage(row?.id === gatewayConfig.active ? "这里是当前启用的连接。" : "");
}
function loadGatewayProfiles() {
  $("gateway-profile").replaceChildren(new Option("＋ 新建连接",""),
    ...gatewayConfig.profiles.map(item => new Option(item.name,item.id)));
  $("gateway-profile").value = gatewayConfig.active || "";
  loadGatewayProfile();
}
$("gateway").onclick = handle(async () => {
  gatewayConfig = await request("/api/gateway");
  loadGatewayProfiles();
  $("gateway-dialog").showModal();
});
$("gateway-profile").onchange = loadGatewayProfile;
$("gateway-kind").onchange = gatewayFields;
$("gateway-check").onclick = handle(async () => {
  const data = gatewayData();
  $("gateway-check").disabled = true;
  gatewayMessage("正在检查…");
  try {
    const result = await request("/api/gateway/models",data);
    if (data.kind === $("gateway-kind").value && data.base_url === $("gateway-url").value.trim()) {
      $("gateway-model-list").replaceChildren(...result.models.map(model => new Option(model.label,model.id)));
      if (!$("gateway-model").value && result.models.length === 1) $("gateway-model").value = result.models[0].id;
      gatewayMessage(result.status);
    }
  } finally {$("gateway-check").disabled = false;}
},gatewayMessage);
$("gateway-login").onclick = handle(async () => {
  if (window.pywebview?.api?.open_codex_login) {
    await window.pywebview.api.open_codex_login();
    gatewayMessage("按官方终端提示登录，完成后回来检查本机登录。");
  } else gatewayMessage("在运行工作台的终端环境执行 codex login，再回来检查本机登录。");
},gatewayMessage);
$("gateway-form").onsubmit = handle(async event => {
  event.preventDefault();
  $("gateway-save").disabled = true;
  try {
    gatewayConfig = await request("/api/gateway/save",gatewayData());
    loadGatewayProfiles();
    gatewayMessage("已保存并启用。可用于快速生成润色、AI 分镜和镜头改写。");
    if (project) presentEditor(true);
    renderAssistant();
  } finally {$("gateway-save").disabled = false;}
},gatewayMessage);
$("polish").onclick = handle(async () => {
  const field = $("quick-form").elements.prompt, text = field.value;
  if (!text.trim()) throw Error("先输入中文要求。");
  $("polish").disabled = true;
  try {
    const result = await request("/api/gateway/polish",{prompt:text});
    if (field.value === text && $("quick-dialog").open) {
      field.value = result.prompt;quickMessage("已用 " + result.model + " 润色，可以继续编辑。");
    }
  } finally {$("polish").disabled = false;}
},quickMessage);

async function copyText(text) {
  try {
    if (window.pywebview?.api?.copy_agent_text) return Boolean(await window.pywebview.api.copy_agent_text(text));
  } catch (_) {}
  try {await navigator.clipboard.writeText(text);return true;} catch (_) {return false;}
}
function makeAgentInstruction(config) {
  const path = "http://127.0.0.1:" + location.port;
  return [
    "请连接并操作这台电脑上的 Colab 影像工作台。你需要在同一台电脑上运行终端或 MCP。",
    "先连接并读取项目，不要在这一步启动付费生成。",
    "Windows PowerShell：Invoke-RestMethod " + path + "/api/projects",
    "Bash：curl -fsS " + path + "/api/projects",
    "再读取 GET " + path + "/api/project/{id}。"+
      (project ? "请优先读取我当前打开的项目 " + project.id + "。" : ""),
    "如果客户端能配置本机 stdio MCP，请按下面配置接入，使用 list_projects、read_project、save_storyboard、submit_task 和 read_job。",
    "若不能在当前会话注册 MCP，可以直接调用上述本机 HTTP API。写请求携带 Origin: " + path
      + "、X-Workbench: 1、Content-Type: application/json。保存分镜之前先读取当前 revision。",
    "只有在我明确要求用 Colab 生成时才提交 generate。等我确认审片后再 approve。先告诉我连接是否成功。",
    JSON.stringify(config,null,2)
  ].join("\n\n");
}
$("agent").onclick = handle(async () => {
  agentText = makeAgentInstruction(await request("/api/agent-config"));
  if (await copyText(agentText)) {notice("已复制。把这段指令直接发给同机 Agent。");return;}
  $("agent-config").value = agentText;$("agent-dialog").showModal();$("agent-config").select();
});
$("copy-agent").onclick = handle(async () => {
  $("agent-config").select();
  $("copy-agent").textContent = await copyText(agentText) ? "已复制" : "请手动复制所选文本";
});
let gpuDraft = null;
function renderGpuDialog() {
  const order = [...gpuDraft.gpus,...gpuOptions.map(row => row.id).filter(id => !gpuDraft.gpus.includes(id))];
  $("gpu-list").replaceChildren(...order.map(id => {
    const option = gpuOptions.find(row => row.id === id), rank = gpuDraft.gpus.indexOf(id), on = rank >= 0;
    const row = document.createElement("div");
    row.className = "gpu-row" + (on ? " on" : "");
    row.innerHTML = '<input type="checkbox" aria-label="使用 ' + esc(option.label) + '"' + (on ? " checked" : "") + ' data-gpu="' + esc(id)
      + '"><span class="rank">' + (on ? rank+1 : "–") + '</span><div><strong>' + esc(option.label) + '</strong><small>显存核验 ≥ '
      + Math.floor(option.min_mib/1024) + ' GiB' + (id === "A100" ? " · 分到 40GB 自动释放重抢" : "") + '</small></div><div class="group">'
      + '<button type="button" class="icon" data-gpu-move="-1" data-gpu="' + esc(id) + '" aria-label="提前"' + (rank > 0 ? "" : " disabled") + '>↑</button>'
      + '<button type="button" class="icon" data-gpu-move="1" data-gpu="' + esc(id) + '" aria-label="推后"'
      + (on && rank < gpuDraft.gpus.length-1 ? "" : " disabled") + '>↓</button></div>';
    return row;
  }));
  $("gpu-rounds").value = gpuDraft.rounds;
  $("gpu-wait").value = gpuDraft.wait;
  $("gpu-highmem").checked = gpuDraft.high_mem;
  const before = gpuPlan;
  gpuPlan = gpuDraft;
  const warning = gpuWarning(project?.manifest.shots);
  gpuPlan = before;
  $("gpu-warning").textContent = warning;
  $("gpu-warning").hidden = !warning;
}
function showGpu() {
  gpuDraft = structuredClone(gpuPlan);
  renderGpuDialog();
  $("gpu-dialog").showModal();
}
$("gpu-list").addEventListener("click",event => {
  const control = event.target.closest("[data-gpu-move]");
  if (!control) return;
  const list = gpuDraft.gpus, index = list.indexOf(control.dataset.gpu), next = index + Number(control.dataset.gpuMove);
  if (index < 0 || next < 0 || next >= list.length) return;
  [list[index],list[next]] = [list[next],list[index]];
  renderGpuDialog();
});
$("gpu-list").addEventListener("change",event => {
  const id = event.target.dataset.gpu;
  if (!id) return;
  if (event.target.checked) gpuDraft.gpus.push(id);
  else if (gpuDraft.gpus.length > 1) gpuDraft.gpus = gpuDraft.gpus.filter(item => item !== id);
  renderGpuDialog();
});
$("gpu-form").onsubmit = handle(async event => {
  event.preventDefault();
  const rounds = Number($("gpu-rounds").value), wait = Number($("gpu-wait").value);
  if (!Number.isInteger(rounds) || rounds < 1 || rounds > 30) throw Error("抢卡轮数须在 1–30 之间。");
  if (!Number.isInteger(wait) || wait < 10 || wait > 600) throw Error("每轮间隔须在 10–600 秒之间。");
  gpuPlan = {gpus:[...gpuDraft.gpus],rounds,wait,high_mem:$("gpu-highmem").checked};
  try {localStorage.setItem("gpuPlan",JSON.stringify(gpuPlan));} catch (_) {}
  $("gpu-dialog").close();
  renderGpuSummary();
  if (project) {updateActions();renderJobs();}
  notice("显卡策略已保存：" + gpuSummary());
});
$("gpu-open").onclick = $("gpu-chip").onclick = showGpu;

function voiceStatus(text,error=false) {
  $("voice-status").textContent = text;
  $("voice-status").classList.toggle("job-error",error);
}
function voiceModel() {
  const id = $("voice-form").querySelector('input[name="model"]:checked')?.value;
  return voiceCatalog.models.find(row => row.id === id);
}
function renderVoiceFields() {
  const model = voiceModel(), form = $("voice-form").elements;
  for (const kind of ["design","custom","clone","api"]) $("voice-" + kind).hidden = model.kind !== kind;
  const chars = form.text.value.trim().length, seconds = chars / 4.5;
  $("voice-length").textContent = chars + " 字 · 约 " + fmt(Math.round(seconds)) + " 秒" + (seconds > 12 ? "（超过 12 秒会截断）" : seconds < 3 ? "（太短）" : "");
  const actor = project.manifest.characters[voiceActor], busyNow = busy() || unresolved();
  const notes = [];
  if (model.engine === "colab") notes.push("会按 <b>" + esc(voicePlan().map(gpuName).join(" → ")) + "</b> 抢一张小卡（显存需求：" + esc(model.vram) + "）"
    + "。首次在新机器上约 5–8 分钟（安装 TTS、下载约 4 GB 权重），完成后自动释放，不占用渲染用的大卡。");
  else notes.push("直接调用你的 TTS 服务，不申请 Colab 显卡，费用按该服务计。");
  if (actor?.voice) notes.push("该角色已有音色，新候选不会自动替换；选用后，用到该角色对白的已生成镜头会变回待生成。");
  if (busyNow) notes.push("<b>当前有任务在运行或有未释放的 GPU。</b>一次只跑一个任务，避免和 H3 渲染抢卡，等它结束后再合成。");
  $("voice-cost").innerHTML = notes.join("<br>");
  $("voice-submit").disabled = Boolean(busyNow);
}
function voicePlan() {return voiceCatalog.plan.gpus;}
async function showVoice(actor) {
  if (currentBusy()) throw Error("这个项目正在执行任务，等任务结束后再合成音色。");
  voiceCatalog ||= await request("/api/voice");
  voiceActor = actor;
  const characters = project.manifest.characters, form = $("voice-form").elements;
  $("voice-actor").textContent = castLabel(actor,characters[actor]);
  if (!$("voice-models").children.length) {
    const card = row => '<label class="model-card"><input type="radio" name="model" value="' + esc(row.id) + '"' + (row.id === "qwen3-design" ? " checked" : "")
      + '><strong>' + esc(row.label) + '<small>' + esc(row.vram) + '</small></strong><span>' + esc(row.note) + '</span></label>';
    $("voice-models").innerHTML = '<div class="group-title">Colab 预置模型（Qwen3-TTS，Apache-2.0）</div>'
      + voiceCatalog.models.filter(row => row.engine === "colab").map(card).join("")
      + '<div class="group-title">外部服务</div>' + voiceCatalog.models.filter(row => row.engine === "api").map(card).join("");
    $("voice-presets").innerHTML = voicePresets.map(text => '<button type="button" class="quiet small" data-voice-preset="' + esc(text) + '">' + esc(text.split("，")[0]) + '</button>').join("");
    form.speaker.replaceChildren(...voiceCatalog.speakers.map(([id,text]) => new Option(id + " · " + text,id)));
    form.text.value = voiceCatalog.sample;
  }
  form.instruct.value ||= characters[actor]?.description ? "符合这个角色的声音：" + characters[actor].description : "";
  form.api_url.value = voiceCatalog.api.base_url;form.api_model.value = voiceCatalog.api.model;
  form.api_voice.value = voiceCatalog.api.voice;form.api_key.value = "";
  form.api_key.placeholder = voiceCatalog.api.has_key ? "已保存，留空保留" : "不需要可留空";
  voiceStatus("");renderVoiceFields();
  $("voice-dialog").showModal();
}
$("voice-form").addEventListener("input",() => voiceCatalog && renderVoiceFields());
$("voice-form").addEventListener("change",() => voiceCatalog && renderVoiceFields());
$("voice-presets").addEventListener("click",event => {
  const preset = event.target.closest("[data-voice-preset]");
  if (preset) {$("voice-form").elements.instruct.value = preset.dataset.voicePreset;renderVoiceFields();}
});
$("voice-api-save").onclick = handle(async () => {
  const form = $("voice-form").elements;
  voiceCatalog.api = await request("/api/voice/save",{base_url:form.api_url.value.trim(),model:form.api_model.value.trim(),
    voice:form.api_voice.value.trim(),key:form.api_key.value});
  form.api_key.value = "";form.api_key.placeholder = voiceCatalog.api.has_key ? "已保存，留空保留" : "不需要可留空";
  voiceStatus("接口已保存。");
},message => voiceStatus(message,true));
$("voice-form").onsubmit = handle(async event => {
  event.preventDefault();
  const form = event.target.elements, model = voiceModel();
  if (busy() || unresolved()) throw Error("等当前任务结束或释放 GPU 后再合成音色。");
  const body = {action:"voice",project:project.id,actor:voiceActor,model:model.id,text:form.text.value,
    count:Number(form.count.value)};
  if (model.kind === "design") body.instruct = form.instruct.value;
  if (model.kind === "custom") {body.speaker = form.speaker.value;body.instruct = form.tone.value;}
  if (model.kind === "api") {
    if (!voiceCatalog.api.base_url) throw Error("先填写并保存兼容 TTS 接口。");
    body.speaker = form.api_voice.value.trim();body.instruct = form.api_instruct.value;
  }
  $("voice-submit").disabled = true;
  try {
    if (model.kind === "clone") {
      if (!form.ref.files[0]) throw Error("先选择一段 3 秒以上的录音。");
      voiceStatus("正在上传录音…");
      body.ref = await uploadFile(project.id,form.ref.files[0]);body.ref_text = form.ref_text.value;
    }
    if (model.engine === "colab") body.gpu = {gpus:voicePlan()};
    const row = await request("/api/run",body);
    focusedJob = row.id;
    $("voice-dialog").close();
    editorTab = "actors";presentEditor();
    notice("已提交音色合成。进度看右侧任务区，完成后在「角色素材」试听并选用。");
    await syncJobs();
  } finally {renderVoiceFields();}
},message => voiceStatus(message,true));

/* Assistant: local next-step guide always works; the connected model adds conversation. */
function nextSteps() {
  const steps = [], debt = unresolved(), active = activeTask();
  if (debt) steps.push({title:"先释放上次的 GPU",text:"上次申请的运行时没有确认释放，可能还在计费。",action:{type:"release",label:"释放 GPU"}});
  if (colabState === "fail") steps.push({title:"连接 Colab",text:"生成视频和合成音色都需要 Colab 授权。",action:{type:"open_setup",label:"打开连接设置"}});
  if (!project) {
    steps.push({title:"从一张参考图开始",text:"上传人物或场景图，写一句中文要求，选时长和画幅。",action:{type:"open_quick",label:"快速生成"}});
    return steps;
  }
  if (active) steps.push({title:"后台正在" + labels[active.action],text:"可以继续编辑其他内容，进度在右侧任务区。",action:null});
  if (dirty) steps.push({title:"有未保存的修改",text:"保存后才能生成或导出。",action:{type:"save",label:"保存修改"}});
  const shots = project.manifest.shots, characters = project.manifest.characters;
  const pend = pendingIds(), review = shots.filter(row => takeState(row.id) === "review").map(row => row.id);
  if (shots.length === 1 && !shots[0].source && gatewayConfig.active)
    steps.push({title:"扩展成多镜头短片",text:"用 AI 分镜把一句创意拆成带景别、运镜和对白的几个镜头。",action:{type:"director",label:"打开 AI 分镜"}});
  const speaking = new Set(shots.flatMap(row => (row.dialogue || []).map(line => line.speaker)));
  const mute = [...speaking].filter(id => characters[id] && !characters[id].voice);
  if (mute.length) steps.push({title:"给 " + mute.map(id => castLabel(id,characters[id])).join("、") + " 配音色",
    text:"有对白但没有参考音色时，H3 会随机配声音。可以上传录音或在 Colab 小卡上合成。",action:{type:"open_voice",label:"合成音色",args:{actor:mute[0]}}});
  if (pend.length && !active && !dirty) steps.push({title:pend.length + " 个镜头待生成",
    text:"会按「" + gpuSummary() + "」抢卡，显存不达标自动释放重抢。" + (gpuWarning(shots.filter(row => pend.includes(row.id))) || ""),
    action:{type:"generate",label:"抢卡生成 " + pend.map(id => id.toUpperCase()).join("、"),args:{shots:pend}}});
  if (review.length) steps.push({title:"审片：" + review.map(id => id.toUpperCase()).join("、"),
    text:"播放检查画面、口型和对白，需要时点「核对对白」，满意后确认。",action:{type:"select_shot",label:"去审 " + review[0].toUpperCase(),args:{shot:review[0]}}});
  const done = shots.every(row => takeState(row.id) === "approved"), exported = project.exports.some(row => row.current);
  if (done && !exported && !dirty) steps.push({title:"可以导出了",text:"全部镜头已确认，合成 MP4 和 SRT。",action:{type:"export",label:"导出成片"}});
  if (done && exported) steps.push({title:"成片已导出",text:"在右侧「本集成片」预览或保存 MP4。想改的话直接编辑镜头再重新生成。",action:null});
  if (!gatewayConfig.active) steps.push({title:"连接模型，解锁 AI 助手",text:"连接兼容 API 或本机 Codex 后，可以直接对话、AI 分镜和改写镜头。不连也能正常使用。",action:{type:"open_gateway",label:"连接模型网关"}});
  return steps.slice(0,4);
}
function assistantState() {
  const characters = project?.manifest.characters || {};
  const failed = jobs.find(row => ["failed","interrupted"].includes(row.status));
  const active = activeTask();
  return {
    project: project ? {id:project.id,title:project.manifest.title,aspect:project.manifest.aspect,style:project.manifest.style,unsaved:dirty,
      exported:project.exports.some(row => row.current)} : null,
    current_shot: currentShot || null,
    shots: project ? project.manifest.shots.map(row => ({id:row.id,title:row.title,seconds:row.seconds,state:takeState(row.id),
      imported:Boolean(row.source),cast:row.cast,dialogue_lines:(row.dialogue || []).length,action:(row.action || "").slice(0,140)})) : [],
    characters: Object.entries(characters).map(([id,actor]) => ({id,label:castLabel(id,actor),has_voice:Boolean(actor.voice)})),
    active_job: active ? {action:active.action,step:active.step,note:active.gpu_note || ""} : null,
    last_failure: failed ? {action:failed.action,error:(failed.error || "").slice(0,300)} : null,
    unreleased_gpu: Boolean(unresolved()), gpu_plan: gpuSummary(), colab: colabState || "unknown",
    balance: $("balance").textContent, other_projects: projects.map(row => row.title).slice(0,8)
  };
}
function renderAssistant() {
  if ($("assistant").hidden) return;
  const steps = nextSteps();
  nextActions = steps.map(step => step.action);
  replaceMarkup($("assistant-next"), steps.length ? steps.map((step,index) => '<div class="next-card"><b>' + esc(step.title) + '</b><span>'
    + esc(step.text) + '</span>' + (step.action ? '<button type="button" class="quiet small" data-assist="next:' + index + '">'
    + esc(step.action.label) + '</button>' : '') + '</div>').join("") : '<div class="next-card"><span>一切就绪。</span></div>');
  const model = gatewayConfig.profiles.find(row => row.id === gatewayConfig.active);
  $("assistant-model").textContent = model ? "已连接 " + (model.model || model.name) + " · 可以直接对话" : "未连接模型 · 显示操作引导";
  $("assistant-form").elements.q.disabled = !model;
  $("assistant-send").disabled = !model || assistantBusy;
  $("assistant-form").elements.q.placeholder = model ? "说说你想做什么，例如：帮我做一个 20 秒的雨夜重逢短片" : "连接模型网关后可以直接对话";
  replaceMarkup($("assistant-suggest"), model && !assistantMessages.length ? ["帮我把这个项目扩展成 4 个镜头","这个镜头怎么改更有电影感？",
    "上次任务为什么失败了？","帮我检查一下环境"].map(text => '<button type="button" class="quiet" data-assist-ask="' + esc(text) + '">' + esc(text) + '</button>').join("") : "");
  replaceMarkup($("assistant-chat"), assistantMessages.map((message,index) => '<div class="msg ' + (message.role === "user" ? "user" : "bot")
    + (message.error ? " error" : "") + '">' + (message.trace?.length ? '<span class="trace">查看了：'
      + esc([...new Set(message.trace)].map(name => traceLabels[name] || name).join("、")) + '</span>' : '')
    + (message.thinking ? '<span class="thinking">正在查看状态和日志…</span>' : esc(message.content))
    + (message.pending ? '<div class="approval"><b>需要你批准：' + esc(message.pending.title) + '</b><small>' + esc(pendingText(message.pending))
      + '</small>' + (message.decided ? '<span class="muted">' + esc(message.decided) + '</span>'
      : '<div class="group"><button type="button" class="primary small" data-assist-approve="' + index + ':1">批准</button>'
      + '<button type="button" class="quiet small" data-assist-approve="' + index + ':0">拒绝</button></div>') + '</div>' : '')
    + (message.actions?.length ? '<div class="group">' + message.actions.map((action,j) => '<button type="button" class="'
      + (["generate","export"].includes(action.type) ? "primary" : "quiet") + ' small" data-assist="' + index + ':' + j + '">'
      + esc(action.label) + '</button>').join("") + '</div>' : '') + '</div>').join(""));
}
function toggleAssistant(open) {
  $("assistant").hidden = !open;
  document.body.classList.toggle("assistant-open",open);
  try {localStorage.setItem("assistant",open ? "1" : "");} catch (_) {}
  if (open) {renderAssistant();if (!$("assistant-form").elements.q.disabled) $("assistant-form").elements.q.focus();}
}
async function askAssistant(text, decision) {
  if (assistantBusy || (!decision && !text.trim())) return;
  assistantBusy = true;
  if (!decision) assistantMessages.push({role:"user",content:text.trim()});
  assistantMessages.push({role:"assistant",thinking:true});
  renderAssistant();
  const scroll = () => {$("assistant-body").scrollTop = $("assistant-body").scrollHeight;};
  scroll();
  try {
    const result = await request("/api/assistant",{session:agentSession || undefined,transcript:agentTranscript.slice(-40),
      state:assistantState(),...(decision ? {decision} : {message:text.trim()})});
    agentSession = result.session;agentTranscript = result.transcript;
    assistantMessages[assistantMessages.length-1] = {role:"assistant",content:result.reply,actions:result.actions,
      trace:result.trace,pending:result.pending};
    if (decision || result.trace?.length) void syncJobs().catch(() => {});
  } catch (error) {
    assistantMessages[assistantMessages.length-1] = {role:"assistant",content:error.message,error:true};
  } finally {
    assistantBusy = false;assistantMessages = assistantMessages.slice(-24);renderAssistant();scroll();
  }
}
async function runAssistant(action) {
  const args = action.args || {}, shots = project?.manifest.shots || [];
  const known = id => shots.some(row => row.id === id);
  const needProject = () => {if (!project) throw Error("先打开或创建一个项目。");};
  switch (action.type) {
    case "open_quick": {
      showQuick();
      const form = $("quick-form").elements;
      if (typeof args.prompt === "string") form.prompt.value = args.prompt.slice(0,1800);
      if (Number.isInteger(args.seconds) && args.seconds >= 3 && args.seconds <= 12) form.seconds.value = args.seconds;
      if (["portrait","landscape"].includes(args.aspect)) form.aspect.value = args.aspect;
      syncQuickFields();return;
    }
    case "director": {
      needProject();$("director-open").click();
      const form = $("director-form").elements;
      if (typeof args.idea === "string") form.idea.value = args.idea.slice(0,1500);
      if (Number.isInteger(args.shots) && args.shots >= 1 && args.shots <= 8) {form.shots.value = args.shots;$("director-count").textContent = args.shots + " 镜";}
      if (typeof args.tone === "string") {
        const match = [...form.tone].find(input => input.value && input.value.startsWith(args.tone.slice(0,4)));
        if (match) match.checked = true;
      }
      return;
    }
    case "select_shot": needProject();if (!known(args.shot)) throw Error("找不到镜头 " + args.shot);selectShot(args.shot);return;
    case "edit_shot": {
      needProject();
      if (currentBusy() || working) throw Error("这个项目正在执行任务，等任务结束后再修改。");
      const row = shots.find(item => item.id === args.shot);
      if (!row) throw Error("找不到镜头 " + args.shot);
      if (typeof args.title === "string" && args.title.trim()) row.title = args.title.trim().slice(0,40);
      if (Number.isInteger(args.seconds) && args.seconds >= 3 && args.seconds <= 12) row.seconds = args.seconds;
      if (typeof args.action === "string" && args.action.trim()) row.action = args.action.trim().slice(0,1800);
      currentShot = row.id;editorTab = "shot";markDirty();renderProject();
      notice("已修改 " + row.id.toUpperCase() + "，检查后点「保存修改」。");return;
    }
    case "rewrite_shot": {
      needProject();if (!known(args.shot)) throw Error("找不到镜头 " + args.shot);
      selectShot(args.shot);
      await rewriteShot(args.preset || "custom",typeof args.instruction === "string" ? args.instruction : undefined);return;
    }
    case "save": needProject();await locked(save);return;
    case "generate": {
      needProject();
      const ids = (Array.isArray(args.shots) ? args.shots : [currentShot]).filter(known);
      if (!ids.length) throw Error("没有可生成的镜头。");
      if (!window.confirm("将按「" + gpuSummary() + "」抢卡并生成 " + ids.map(id => id.toUpperCase()).join("、") + "，会消耗 Colab 计算单元。继续？")) return;
      await enqueue("generate",ids);return;
    }
    case "approve": {
      needProject();
      const ids = (Array.isArray(args.shots) ? args.shots : [currentShot]).filter(id => known(id) && takeState(id) === "review");
      if (!ids.length) throw Error("没有待确认的镜头。");
      if (!window.confirm("确认你已经看过 " + ids.map(id => id.toUpperCase()).join("、") + " 并满意？")) return;
      await enqueue("approve",ids);return;
    }
    case "export": needProject();await enqueue("cut");return;
    case "open_voice": {
      needProject();
      const actor = project.manifest.characters[args.actor] ? args.actor : Object.keys(project.manifest.characters)[0];
      if (!actor) throw Error("先导入一个角色。");
      editorTab = "actors";presentEditor();await showVoice(actor);
      const form = $("voice-form").elements;
      const model = [...form.model].find(input => input.value === args.model);
      if (model) model.checked = true;
      if (typeof args.instruct === "string" && args.instruct.trim()) form.instruct.value = args.instruct.slice(0,300);
      renderVoiceFields();return;
    }
    case "add_actor": needProject();$("actor-dialog").showModal();return;
    case "set_gpu": {
      const gpus = (Array.isArray(args.gpus) ? args.gpus : []).filter((id,i,list) => ["G4","H100","A100"].includes(id) && list.indexOf(id) === i);
      if (!gpus.length) {showGpu();return;}
      gpuPlan = {...gpuPlan,gpus,...(Number.isInteger(args.rounds) && args.rounds >= 1 && args.rounds <= 30 ? {rounds:args.rounds} : {})};
      try {localStorage.setItem("gpuPlan",JSON.stringify(gpuPlan));} catch (_) {}
      renderGpuSummary();if (project) updateActions();
      notice("显卡策略已改为：" + gpuSummary());return;
    }
    case "release": {
      const debt = unresolved();
      if (!debt) throw Error("没有待释放的 GPU。");
      await enqueue("release",[],debt.id);return;
    }
    case "open_gateway": $("gateway").click();return;
    case "open_setup": $("setup").click();return;
    default: throw Error("暂不支持这个操作。");
  }
}
$("assistant-toggle").onclick = () => toggleAssistant($("assistant").hidden);
$("assistant-close").onclick = () => toggleAssistant(false);
$("assistant").addEventListener("click",handle(async event => {
  const approve = event.target.closest("[data-assist-approve]");
  if (approve) {
    const [index,allow] = approve.dataset.assistApprove.split(":"), message = assistantMessages[Number(index)];
    if (!message?.pending || message.decided || assistantBusy) return;
    message.decided = allow === "1" ? "已批准，正在执行…" : "已拒绝";
    renderAssistant();
    await askAssistant("",{...message.pending,allow:allow === "1"});
    message.decided = allow === "1" ? "已批准" : "已拒绝";
    renderAssistant();
    return;
  }
  const ask = event.target.closest("[data-assist-ask]");
  if (ask) {await askAssistant(ask.dataset.assistAsk);return;}
  const target = event.target.closest("[data-assist]");
  if (!target) return;
  const [first,second] = target.dataset.assist.split(":");
  const action = first === "next" ? nextActions[Number(second)] : assistantMessages[Number(first)]?.actions?.[Number(second)];
  if (action) await runAssistant(action);
}));
$("assistant-form").onsubmit = handle(async event => {
  event.preventDefault();
  const field = event.target.elements.q, text = field.value;
  field.value = "";
  await askAssistant(text);
});
$("assistant-form").elements.q.addEventListener("keydown",event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {event.preventDefault();$("assistant-form").requestSubmit();}
});
try {if (localStorage.getItem("assistant")) toggleAssistant(true);} catch (_) {}

function directorStatus(text,error=false) {
  $("director-status").textContent = text;
  $("director-status").classList.toggle("job-error",error);
}
function renderPlan() {
  const plan = directorPlan;
  if (!plan) {
    $("director-result").innerHTML = '<div class="director-empty">方案会显示在这里</div>';
    $("director-meta").textContent = "";$("director-apply").disabled = true;return;
  }
  const characters = project.manifest.characters;
  $("director-result").innerHTML = plan.shots.map((row,index) => '<label class="plan-shot"><input type="checkbox" checked data-plan="'
    + index + '"><strong><em>' + String(index+1).padStart(2,"0") + '</em>' + esc(row.title) + '</strong><div class="plan-tags">'
    + [row.seconds + " 秒",row.shot_size,row.camera,...row.cast.map(id => castLabel(id,characters[id]))].filter(Boolean)
      .map(tag => '<span>' + esc(tag) + '</span>').join("") + '</div><p>' + esc(row.action) + '</p>'
    + (row.dialogue.length || row.sound ? '<ul>' + row.dialogue.map(line => '<li><b>' + esc(castLabel(line.speaker,characters[line.speaker]))
      + '</b> ' + fmt(line.start) + '–' + fmt(line.end) + ' 秒：' + esc(line.text) + '</li>').join("")
      + (row.sound ? '<li>声音：' + esc(row.sound) + '</li>' : '') + '</ul>' : '') + '</label>').join("");
  updatePlanMeta();
}
function pickedShots() {
  return [...$("director-result").querySelectorAll("[data-plan]:checked")].map(input => directorPlan.shots[Number(input.dataset.plan)]);
}
function updatePlanMeta() {
  const picked = pickedShots(), total = picked.reduce((sum,row) => sum + row.seconds,0);
  // Measured on G4: about 16 s sampling plus 8 s decoding per second of video, excluding environment setup.
  $("director-meta").textContent = picked.length + " 镜 · " + total + " 秒 · 预计渲染约 " + Math.max(1,Math.round(total*24/60)) + " 分钟";
  $("director-apply").disabled = !picked.length;
}
$("director-result").addEventListener("change",updatePlanMeta);
$("director-form").addEventListener("input",event => {
  if (event.target.name === "shots") $("director-count").textContent = event.target.value + " 镜";
});
$("director-open").onclick = () => {
  if (!project) return;
  directorStatus(gatewayConfig.active ? "" : "还没有连接模型。点左下角「模型网关」连接兼容 API 或本机 Codex 后再生成方案。",!gatewayConfig.active);
  $("director-run").disabled = !gatewayConfig.active;
  renderPlan();
  $("director-dialog").showModal();
};
$("director-form").onsubmit = handle(async event => {
  event.preventDefault();
  const form = event.target.elements, characters = Object.entries(project.manifest.characters);
  if (!characters.length) throw Error("先在「角色素材」导入至少一个角色。");
  $("director-run").disabled = true;
  directorStatus("模型正在拆分镜头，通常需要 10–60 秒…");
  try {
    directorPlan = await request("/api/director/storyboard",{idea:form.idea.value,shots:Number(form.shots.value),
      tone:form.tone.value,aspect:project.manifest.aspect,locations:project.manifest.locations,
      characters:characters.map(([id,actor]) => ({id,label:castLabel(id,actor),description:actor.description}))});
    renderPlan();
    directorStatus("由 " + directorPlan.model + " 生成。取消勾选不需要的镜头，再应用到时间线。");
  } finally {$("director-run").disabled = !gatewayConfig.active;}
},message => directorStatus(message,true));
$("director-apply").onclick = handle(() => {
  if (currentBusy() || working) throw Error("这个项目正在执行任务，等任务结束后再应用。");
  const picked = pickedShots(), form = $("director-form").elements, manifest = project.manifest;
  if (!picked.length) return;
  const replace = form.mode.value === "replace";
  if (replace && !window.confirm("用方案替换当前全部 " + manifest.shots.length + " 个镜头？已生成的素材文件会保留。")) return;
  const keys = {};
  for (const [key,text] of Object.entries(directorPlan.locations)) {
    let name = key;
    while (manifest.locations[name] && manifest.locations[name] !== text) name += "-2";
    manifest.locations[name] = text;keys[key] = name;
  }
  if (form.namedItem("style").checked && directorPlan.style) manifest.style = directorPlan.style;
  let next = replace ? 1 : Math.max(0,...manifest.shots.map(row => Number(row.id.replace(/^s/,"")) || 0)) + 1;
  const rows = picked.map(row => ({id:"s" + String(next++).padStart(2,"0"),title:row.title,seconds:row.seconds,
    location:keys[row.location],cast:[...row.cast],action:row.action,dialogue:row.dialogue.map(line => ({...line})),
    seed:crypto.getRandomValues(new Uint32Array(1))[0],...(row.sound ? {sound:row.sound} : {})}));
  if (replace) manifest.shots = rows; else manifest.shots.push(...rows);
  if (!dirty && directorPlan.title && replace) manifest.title = directorPlan.title;
  currentShot = rows[0].id;exportPreview = null;editorTab = "shot";
  $("director-dialog").close();
  markDirty();renderProject();
  notice("已" + (replace ? "替换为 " : "追加 ") + rows.length + " 个镜头。检查后点「保存修改」，再逐镜抢卡生成。");
});
$("notice").onclick = () => {$("notice").hidden = true;};
const themes = [["auto","跟随系统"],["dark","深色"],["light","浅色"]];
function applyTheme(value) {
  if (value === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = value;
  $("theme-label").textContent = "外观：" + themes.find(([id]) => id === value)[1];
}
let theme = "auto";
try {theme = localStorage.getItem("theme") || "auto";} catch (_) {}
if (!themes.some(([id]) => id === theme)) theme = "auto";
applyTheme(theme);
$("theme").onclick = () => {
  theme = themes[(themes.findIndex(([id]) => id === theme) + 1) % themes.length][0];
  try {localStorage.setItem("theme",theme);} catch (_) {}
  applyTheme(theme);
};
$("new").onclick = () => $("new-dialog").showModal();
$("setup").onclick = () => $("setup-dialog").showModal();
$("refresh").onclick = refreshUsage;
$("add-shot").onclick = addShot;
$("play-trim").onclick = handle(playTrim);
$("show-shot").onclick = () => selectShot(currentShot);
$("batch").onclick = handle(() => enqueue("generate",pendingIds()));
$("export").onclick = handle(() => enqueue("cut"));
$("install").onclick = handle(async () => {
  if (window.pywebview?.api) await window.pywebview.api.open_setup("install");
  else notice("请在仓库根目录运行 bash workbench/bootstrap.sh。");
});
$("auth").onclick = handle(async () => {
  if (busy()) throw Error("任务运行期间请保持当前授权。");
  if (window.pywebview?.api) await window.pywebview.api.open_setup("auth");
  else notice("请在仓库根目录运行 bash workbench/bootstrap.sh auth。");
});
$("new-form").onsubmit = handle(async event => {
  event.preventDefault();
  if (dirty && !window.confirm("当前项目有未保存的修改。继续新建？")) return;
  await locked(async () => {
    const data = await request("/api/new",{title:event.target.elements.title.value});
    projects = [...projects,{id:data.id,title:data.manifest.title}];
    await loadProject(data.id);event.target.reset();$("new-dialog").close();
  });
});
window.addEventListener("beforeunload", event => {if (dirty) event.preventDefault();});
window.addEventListener("focus", () => {void syncJobs().catch(() => {});});
async function boot() {
  renderGpuSummary();
  void request("/api/gpus").then(data => {
    gpuOptions = data.gpus;
    gpuPlan.gpus = gpuPlan.gpus.filter(id => gpuOptions.some(row => row.id === id));
    if (!gpuPlan.gpus.length) gpuPlan = {...data.default};
    renderGpuSummary();
  }).catch(() => {});
  projects = await request("/api/projects");
  const previous = projects.find(row => row.id === localStorage.getItem("currentProject"));
  if (previous || projects.length) await loadProject((previous || projects[0]).id);
  else renderProject();
  void request("/api/gateway").then(config => {gatewayConfig = config;if (project) presentEditor(true);renderAssistant();}).catch(() => {});
  void refreshUsage();
  const poll = async () => {
    try {await syncJobs();} catch (_) {}
    setTimeout(poll,activeTask() ? 1900 : 6500);
  };
  void poll();
}
void handle(boot)();
