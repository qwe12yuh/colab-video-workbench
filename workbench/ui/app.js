"use strict";

const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const statusText = {pending:"待生成",review:"待审片",approved:"已确认",running:"生成中"};
const labels = {generate:"生成视频",qa:"对白转写",approve:"确认镜头",cut:"导出成片",release:"释放 GPU"};
const statuses = {queued:"排队",running:"执行中",complete:"完成",failed:"失败",cancelled:"已取消",interrupted:"已中断"};
const stages = {queued:"准备",auth:"验证授权",allocate:"申请 G4",render:"准备渲染",
                release:"释放 GPU",qa:"转写对白",approve:"检查素材",cut:"剪辑并验证",done:"完成"};
let project, projects = [], jobs = [], currentShot = "", focusedJob = "", editorTab = "shot";
let savedManifest, dirty = false, working = false, exportPreview = null, previewEnd = null;
let gatewayConfig = {active:"",profiles:[]}, quickThumb = "", agentText = "";
let syncing, ownerState = "", usageAt = 0, usageRequest, jobFingerprints = new Map(), changedProjects = new Set();
const cancelling = new Set();

async function request(path, body) {
  const init = body === undefined ? {} : {method:"POST",
    headers:{"Content-Type":"application/json","X-Workbench":"1"},body:JSON.stringify(body)};
  const response = await fetch(path, init);
  const value = await response.json();
  if (!response.ok) throw Error(value.error || ("请求失败 " + response.status));
  return value;
}
function notice(message, error=false) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
  $("notice").classList.toggle("error",error);
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
function renderTimeline() {
  replaceMarkup($("shots"), project.manifest.shots.map(row => {
    const state = takeState(row.id), selected = row.id === currentShot && !exportPreview;
    return '<button type="button" class="shot-chip' + (selected ? ' on' : '') + '" data-action="shot" data-value="'
      + esc(row.id) + '" aria-pressed="' + selected + '"><strong>' + esc(row.id.toUpperCase() + " · " + row.title)
      + '</strong><span class="group"><small>' + esc(row.seconds) + ' 秒</small><span class="badge ' + state + '">'
      + statusText[state] + '</span></span></button>';
  }).join(""));
}
function updateStage() {
  if (!project || !shot()) return;
  const current = shot(), take = takeFor(current.id), state = takeState(current.id);
  const clip = exportPreview || take;
  const stage = $("stage");
  const url = clip?.url || "";
  const icon = state === "running" ? "◌" : "▷";
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
    const text = state === "running" ? "右侧任务区显示进度，回传后可直接播放。"
      : "生成后，预览将显示在这里。";
    replaceMarkup(stage,'<div class="stage-empty"><span class="stage-symbol" aria-hidden="true">' + icon
      + '</span><b>' + title + '</b><p>' + text + '</p></div>');
  }
  const trim = Number(current.trim_start || 0);
  const trimNote = !exportPreview && take?.url ? "原片预览 · 成片取段 " + fmt(trim) + "–" + fmt(trim + current.seconds) + " 秒"
    + (take.asr && !dirty ? " · 转写：" + (take.asr.heard || "未识别到对白") : "") : "";
  $("preview-note").textContent = trimNote;
  $("preview-note").hidden = !trimNote;
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
        + '<label>参考音色<input type="file" data-asset="voice" accept="audio/wav,audio/mpeg"></label>'
        + (actor.voice ? button("移除音色","voice-remove",id) : "") + '</article>').join("");
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
        + area(quick ? "中文要求" : "画面要求 · 动作、表演与运镜","action",current.action) + options
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
  else if (canGenerate) next = button("生成当前镜头","generate","","primary",generateBlocked ? "disabled" : "");
  else if (take?.state === "review" || dirty && take?.url && (trimChanged(current) || current.source))
    next = button("确认镜头","approve","","primary",blocked ? "disabled" : "");
  else next = '<span class="badge approved">已确认</span>';
  const regenerate = take?.url && !current.source ? button("准备新版本","seed","","quiet",currentBusy() ? "disabled" : "") : "";
  replaceMarkup($("editor-actions"),'<span class="muted">' + (dirty ? "修改尚未保存" : project.manifest.shots.length + " 镜 · "
    + fmt(project.manifest.shots.reduce((sum,row) => sum + Number(row.seconds),0)) + " 秒") + '</span><div class="group">'
    + saveButton + qa + regenerate + next + '</div>');
  const batchCount = pendingIds().length;
  $("batch").hidden = batchCount < 2;
  $("batch").textContent = "生成 " + batchCount + " 镜";
  $("batch").disabled = generateBlocked;
  $("quick-render").disabled = generateBlocked;
  $("add-shot").disabled = working || currentBusy();
  if (!$("quick-hint").classList.contains("job-error")) $("quick-hint").textContent = unresolved() ? "请先处理任务区尚未释放的 GPU。"
    : busy() ? "后台任务运行中，可以先保存草稿。"
    : "生成会使用 Colab 算力，完成后自动释放 GPU。";
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
  const row = await request("/api/run",{action,project:project?.id,shots:ids,target});
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
function renderJobs(detail) {
  const active = activeTask(), debt = unresolved();
  status($("count"),active ? "运行中" : debt ? "待释放" : "空闲",active ? "running" : debt ? "review" : "");
  let html;
  if (active) {
    const pendingCancel = active.cancel_requested || cancelling.has(active.id);
    html = '<div class="job-run"><div class="section-head"><strong>' + esc(labels[active.action])
      + '</strong><span class="badge running">' + esc(statuses[active.status]) + '</span></div><p class="job-line">'
      + esc([projectTitle(active.project),active.shots?.map(id => id.toUpperCase()).join("、")].filter(Boolean).join(" · "))
      + '</p><p class="job-line">' + esc(taskStage(active,detail?.id === active.id ? detail.log : "")) + '</p><div class="group">'
      + button("日志","job",active.id)
      + (active.step !== "release" ? button(pendingCancel ? "取消已请求" : "取消任务","cancel",active.id,
        "quiet",pendingCancel ? "disabled" : "") : "") + '</div></div>';
  } else if (debt) {
    html = '<div class="job-run"><strong>上次申请的 GPU 未确认释放</strong><p class="job-line">'
      + esc(projectTitle(debt.project)) + '</p>' + button("释放 GPU","release",debt.id) + '</div>';
  } else {
    html = '<p class="job-line">当前没有正在执行的任务。</p>';
  }
  replaceMarkup($("active-job"),html);
  const history = jobs.filter(row => row.id !== active?.id);
  $("task-history").hidden = !history.length;
  $("history-count").textContent = "(" + history.length + ")";
  replaceMarkup($("jobs"), history.slice(0,8).map(row =>
    '<button class="job-card' + (row.id === focusedJob ? " selected" : "")
    + '" data-action="job" data-value="' + esc(row.id) + '"><strong>' + esc(labels[row.action])
    + ' · ' + esc(statuses[row.status]) + '</strong><small>' + esc(projectTitle(row.project)) + '</small></button>').join(""));
  if (detail !== undefined) renderJobDetail(detail);
}
async function updateJobs() {
  const rows = await request("/api/jobs");
  for (const row of rows) {
    const stamp = String(row.updated) + "|" + row.status;
    if (jobFingerprints.get(row.id) !== stamp && !isActive(row) && row.project) changedProjects.add(row.project);
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
    status($("cloud-status"),data.assignments ? data.assignments + " 台 · " + data.rate.toFixed(2) + " 单元/时" : "Colab 已连接","ok");
    $("setup-info").textContent = "账户余额 " + data.balance.toFixed(2) + " 单元。"
      + "使用工作台专用授权。";
  }).catch(error => {
    $("balance").textContent = "—";
    status($("cloud-status"),"Colab 未连通","fail");
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
  updateActions();
  $("quick-dialog").showModal();
  $("polish").hidden = !gatewayConfig.active;
}
$("quick").onclick = $("welcome-new").onclick = showQuick;
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
  } else gatewayMessage("在 WSL 运行 codex login，再回来检查本机登录。");
},gatewayMessage);
$("gateway-form").onsubmit = handle(async event => {
  event.preventDefault();
  $("gateway-save").disabled = true;
  try {
    gatewayConfig = await request("/api/gateway/save",gatewayData());
    loadGatewayProfiles();
    gatewayMessage("已保存并启用。快速生成时可选用它润色提示词。");
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
  $("copy-agent").textContent = await copyText(agentText) ? "已复制" : "按 Ctrl+C 复制";
});
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
  else notice("请在 Ubuntu WSL 的项目目录运行 bash workbench/bootstrap.sh。");
});
$("auth").onclick = handle(async () => {
  if (busy()) throw Error("任务运行期间请保持当前授权。");
  if (window.pywebview?.api) await window.pywebview.api.open_setup("auth");
  else notice("请在 Ubuntu WSL 的项目目录运行 bash workbench/bootstrap.sh auth。");
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
  projects = await request("/api/projects");
  const previous = projects.find(row => row.id === localStorage.getItem("currentProject"));
  if (previous || projects.length) await loadProject((previous || projects[0]).id);
  else renderProject();
  void request("/api/gateway").then(config => {gatewayConfig = config;}).catch(() => {});
  void refreshUsage();
  const poll = async () => {
    try {await syncJobs();} catch (_) {}
    setTimeout(poll,activeTask() ? 1900 : 6500);
  };
  void poll();
}
void handle(boot)();
