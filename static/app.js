(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const ALLOWED_EXTENSIONS = new Set([
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".ts",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
  ]);
  let MAX_UPLOAD_BYTES = 3 * 1024 * 1024 * 1024;
  const STORAGE_KEY = "rapWordSplitterJobId";
  const HUE_PALETTE = [78, 48, 24, 160, 190, 108, 330];

  const state = {
    file: null,
    jobId: null,
    job: null,
    timer: null,
    pollFailures: 0,
    filter: "all",
    query: "",
    visible: 120,
    editWords: [],
    selected: new Set(),
    searchTimer: null,
    view: null,
  };
  let appInitialized = false;

  const els = {
    hero: $("#hero"),
    processing: $("#processing"),
    results: $("#results"),
    dropzone: $("#dropzone"),
    fileInput: $("#fileInput"),
    fileChip: $("#fileChip"),
    fileName: $("#fileName"),
    fileSize: $("#fileSize"),
    fileRemove: $("#fileRemove"),
    startButton: $("#startButton"),
    modelSelect: $("#modelSelect"),
    languageSelect: $("#languageSelect"),
    granularitySelect: $("#granularitySelect"),
    paddingSelect: $("#paddingSelect"),
    formatSelect: $("#formatSelect"),
    qualitySelect: $("#qualitySelect"),
    autoCutCheck: $("#autoCutCheck"),
    processEyebrow: $("#processEyebrow"),
    processTitle: $("#processTitle"),
    processMessage: $("#processMessage"),
    processPercent: $("#processPercent"),
    progressBar: $("#progressBar"),
    stepRow: $("#stepRow"),
    processError: $("#processError"),
    errorTitle: $("#errorTitle"),
    errorText: $("#errorText"),
    errorReset: $("#errorReset"),
    resultTitle: $("#resultTitle"),
    resultSub: $("#resultSub"),
    downloadZip: $("#downloadZip"),
    stats: $("#stats"),
    searchInput: $("#searchInput"),
    filterGroup: $("#filterGroup"),
    copyWordsButton: $("#copyWordsButton"),
    editWordsButton: $("#editWordsButton"),
    transcriptText: $("#transcriptText"),
    copyTranscriptButton: $("#copyTranscriptButton"),
    clipGrid: $("#clipGrid"),
    emptyResults: $("#emptyResults"),
    loadMoreButton: $("#loadMoreButton"),
    resultsFootnote: $("#resultsFootnote"),
    newJobButton: $("#newJobButton"),
    mediaModal: $("#mediaModal"),
    mediaPreview: $("#mediaPreview"),
    mediaWord: $("#mediaWord"),
    mediaTime: $("#mediaTime"),
    mediaSentence: $("#mediaSentence"),
    mediaDownloads: $("#mediaDownloads"),
    editorModal: $("#editorModal"),
    editorSearch: $("#editorSearch"),
    editorBody: $("#editorBody"),
    editorCount: $("#editorCount"),
    selectAllCheck: $("#selectAllCheck"),
    mergeSelectedButton: $("#mergeSelectedButton"),
    deleteSelectedButton: $("#deleteSelectedButton"),
    resegmentWordButton: $("#resegmentWordButton"),
    resegmentCharButton: $("#resegmentCharButton"),
    editorPadding: $("#editorPadding"),
    editorFormat: $("#editorFormat"),
    editorQuality: $("#editorQuality"),
    saveRecutButton: $("#saveRecutButton"),
    toasts: $("#toasts"),
    authGate: $("#authGate"),
    authForm: $("#authForm"),
    authPassword: $("#authPassword"),
    authButton: $("#authButton"),
    authError: $("#authError"),
  };

  function show(element) {
    if (element) element.classList.remove("hidden");
  }

  function hide(element) {
    if (element) element.classList.add("hidden");
  }

  function formatBytes(bytes) {
    const value = Number(bytes || 0);
    if (value < 1024) return `${value} B`;
    if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
    if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
    return `${(value / 1024 ** 3).toFixed(2)} GB`;
  }

  function formatClock(seconds) {
    const value = Math.max(0, Number(seconds || 0));
    const minutes = Math.floor(value / 60);
    const rest = value - minutes * 60;
    return `${String(minutes).padStart(2, "0")}:${rest.toFixed(2).padStart(5, "0")}`;
  }

  function formatDuration(seconds) {
    const value = Math.max(0, Number(seconds || 0));
    if (value >= 3600) {
      const hours = Math.floor(value / 3600);
      const minutes = Math.floor((value % 3600) / 60);
      return `${hours} 小时 ${minutes} 分`;
    }
    if (value >= 60) {
      const minutes = Math.floor(value / 60);
      const rest = Math.round(value % 60);
      return `${minutes} 分 ${rest} 秒`;
    }
    return `${value.toFixed(value < 10 ? 1 : 0)} 秒`;
  }

  function textHue(text) {
    let hash = 0;
    for (let index = 0; index < String(text).length; index += 1) {
      hash = (hash * 31 + String(text).charCodeAt(index)) | 0;
    }
    return HUE_PALETTE[Math.abs(hash) % HUE_PALETTE.length];
  }

  function extensionOf(name) {
    const dot = String(name || "").lastIndexOf(".");
    return dot >= 0 ? String(name).slice(dot).toLowerCase() : "";
  }

  function setView(view) {
    const changed = state.view !== view;
    hide(els.hero);
    hide(els.processing);
    hide(els.results);
    if (view === "upload") show(els.hero);
    if (view === "processing") show(els.processing);
    if (view === "results") show(els.results);
    state.view = view;
    if (changed) window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function toast(message, type = "") {
    if (!els.toasts) return;
    const node = document.createElement("div");
    node.className = `toast ${type}`.trim();
    node.textContent = message;
    els.toasts.appendChild(node);
    window.setTimeout(() => {
      node.style.opacity = "0";
      node.style.transform = "translateY(6px)";
      window.setTimeout(() => node.remove(), 220);
    }, 3600);
  }

  async function checkAccess() {
    try {
      const response = await fetch("/api/session", { cache: "no-store" });
      const payload = await response.json();
      if (payload.required && !payload.authenticated) {
        show(els.authGate);
        window.setTimeout(() => els.authPassword.focus(), 80);
        return false;
      }
      hide(els.authGate);
      return true;
    } catch (error) {
      // Let the actual upload flow show the connection error.
      return true;
    }
  }

  async function submitAuth(event) {
    event.preventDefault();
    const password = els.authPassword.value;
    if (!password) {
      els.authError.textContent = "请输入访问口令。";
      show(els.authError);
      return;
    }
    els.authButton.disabled = true;
    els.authButton.textContent = "验证中…";
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "访问口令不对。");
      hide(els.authGate);
      els.authError.classList.add("hidden");
      els.authPassword.value = "";
      initApp();
    } catch (error) {
      els.authError.textContent = error.message || "验证失败，请稍后再试。";
      show(els.authError);
    } finally {
      els.authButton.disabled = false;
      els.authButton.textContent = "进入素材机";
    }
  }

  function bindAuth() {
    els.authForm.addEventListener("submit", submitAuth);
  }

  function setProgress(percent, message) {
    const value = Math.max(0, Math.min(100, Number(percent || 0)));
    els.progressBar.style.width = `${value}%`;
    els.processPercent.textContent = `${Math.round(value)}%`;
    if (typeof message === "string" && message) {
      els.processMessage.textContent = message;
    }
  }

  function clearFile() {
    state.file = null;
    els.fileInput.value = "";
    hide(els.fileChip);
    els.startButton.disabled = true;
    const title = $(".drop-title", els.dropzone);
    const sub = $(".drop-sub", els.dropzone);
    if (title) title.textContent = "把视频拖到这里";
    if (sub) sub.textContent = `或点击选择文件 · 建议 ${Math.round(MAX_UPLOAD_BYTES / 1024 / 1024)}MB 以内`;
  }

  function selectFile(file) {
    if (!file) return;
    const extension = extensionOf(file.name);
    if (!ALLOWED_EXTENSIONS.has(extension)) {
      toast(`不支持 ${extension || "这种"} 格式，请换 MP4 / MOV / MKV / MP3 等常见格式。`, "error");
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      toast(`文件超过 ${Math.round(MAX_UPLOAD_BYTES / 1024 / 1024)}MB，请先剪短或压缩一下。`, "error");
      return;
    }
    if (file.size === 0) {
      toast("这个文件是空的。", "error");
      return;
    }
    state.file = file;
    els.fileName.textContent = file.name;
    els.fileSize.textContent = formatBytes(file.size);
    show(els.fileChip);
    els.startButton.disabled = false;
    const title = $(".drop-title", els.dropzone);
    const sub = $(".drop-sub", els.dropzone);
    if (title) title.textContent = "文件已经选好";
    if (sub) sub.textContent = "点右边的按钮开始拆词，或重新拖一个文件";
    toast(`已选择：${file.name}`, "success");
  }

  function openUploadPicker() {
    els.fileInput.click();
  }

  function bindUpload() {
    els.dropzone.addEventListener("click", (event) => {
      if (event.target.closest("#fileRemove")) return;
      openUploadPicker();
    });
    els.dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openUploadPicker();
      }
    });
    els.fileInput.addEventListener("change", () => {
      selectFile(els.fileInput.files?.[0]);
    });
    els.fileRemove.addEventListener("click", (event) => {
      event.stopPropagation();
      clearFile();
    });
    ["dragenter", "dragover"].forEach((name) => {
      els.dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        event.stopPropagation();
        els.dropzone.classList.add("dragging");
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      els.dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        event.stopPropagation();
        els.dropzone.classList.remove("dragging");
      });
    });
    els.dropzone.addEventListener("drop", (event) => {
      const file = event.dataTransfer?.files?.[0];
      selectFile(file);
    });
    els.startButton.addEventListener("click", startJob);
  }

  function startJob() {
    if (!state.file) {
      toast("先选择一个视频文件。", "error");
      return;
    }
    clearTimeout(state.timer);
    state.job = null;
    state.jobId = null;
    state.pollFailures = 0;
    hide(els.processError);
    setView("processing");
    els.processTitle.textContent = "正在上传视频";
    els.processEyebrow.textContent = "UPLOADING";
    setProgress(0, "把视频交给本机处理器…");
    updateSteps("queued");

    const form = new FormData();
    form.append("file", state.file);
    form.append("model", els.modelSelect.value);
    form.append("language", els.languageSelect.value);
    form.append("granularity", els.granularitySelect.value);
    form.append("padding", els.paddingSelect.value);
    form.append("format", els.formatSelect.value);
    form.append("quality", els.qualitySelect.value);
    form.append("auto_cut", els.autoCutCheck.checked ? "true" : "false");

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const percent = (event.loaded / event.total) * 8;
      setProgress(percent, `正在上传 · ${Math.round((event.loaded / event.total) * 100)}%`);
    });
    xhr.addEventListener("load", () => {
      let payload = {};
      try {
        payload = JSON.parse(xhr.responseText || "{}");
      } catch (error) {
        payload = {};
      }
      if (xhr.status >= 200 && xhr.status < 300 && payload.id) {
        state.jobId = payload.id;
        state.job = payload;
        localStorage.setItem(STORAGE_KEY, payload.id);
        setProgress(8, "上传完成，开始读取视频…");
        pollJob();
      } else {
        showProcessError("上传没有完成", payload.error || `服务器返回 ${xhr.status}`);
      }
    });
    xhr.addEventListener("error", () => {
      showProcessError("连接失败", "浏览器没有连上本机服务。请确认黑色命令行窗口还在运行。");
    });
    xhr.send(form);
  }

  const STAGE_INDEX = {
    queued: 0,
    probe: 0,
    extract: 1,
    model: 2,
    transcribe: 2,
    postprocess: 2,
    ready: 2,
    cutting: 3,
    zip: 4,
    done: 4,
    error: 0,
  };

  function updateSteps(stage) {
    const active = STAGE_INDEX[stage] ?? 0;
    $$(".step", els.stepRow).forEach((element, index) => {
      element.classList.toggle("done", index < active);
      element.classList.toggle("active", index === active);
    });
  }

  function processTitleFor(job) {
    if (job.status === "transcribe" || job.stage === "transcribe") return "正在逐词识别";
    if (job.status === "cutting" || job.stage === "cutting") return "正在切出词片";
    if (job.stage === "zip") return "正在打包素材";
    if (job.status === "queued") return "正在排队";
    if (job.status === "probe" || job.stage === "probe") return "正在读取视频";
    if (job.stage === "extract") return "正在提取音轨";
    if (job.stage === "model") return "正在准备语音模型";
    return "正在处理";
  }

  function updateProcessing(job) {
    const progress = Number(job.progress || 0);
    setProgress(progress, job.message || "");
    els.processTitle.textContent = processTitleFor(job);
    els.processEyebrow.textContent = progress >= 78 ? "ALMOST THERE" : "PROCESSING";
    updateSteps(job.stage || job.status || "queued");
    if (job.recognized_count && (job.stage === "transcribe" || job.status === "transcribe")) {
      els.processMessage.textContent = `${job.message || "正在逐词听写…"} · 已识别 ${job.recognized_count} 个词`;
    }
  }

  function showProcessError(title, detail) {
    setView("processing");
    els.processTitle.textContent = "处理没有完成";
    els.processEyebrow.textContent = "NEEDS ATTENTION";
    els.processPercent.textContent = "!";
    els.errorTitle.textContent = title || "处理失败";
    els.errorText.textContent = detail || "未知错误";
    show(els.processError);
  }

  async function pollJob() {
    clearTimeout(state.timer);
    if (!state.jobId) return;
    try {
      const response = await fetch(`/api/jobs/${state.jobId}`, { cache: "no-store" });
      if (response.status === 404) {
        localStorage.removeItem(STORAGE_KEY);
        throw new Error("任务不存在，可能已经被清理。请重新上传。");
      }
      const job = await response.json();
      if (!response.ok) throw new Error(job.error || `服务器返回 ${response.status}`);
      state.job = job;
      state.pollFailures = 0;
      localStorage.setItem(STORAGE_KEY, job.id);

      if (job.status === "done") {
        setView("results");
        renderResults(job);
        return;
      }
      if (job.status === "ready") {
        renderReady(job);
        return;
      }
      if (job.status === "error") {
        updateProcessing(job);
        showProcessError("处理失败", job.error || job.message);
        return;
      }
      setView("processing");
      updateProcessing(job);
      state.timer = window.setTimeout(pollJob, 1000);
    } catch (error) {
      state.pollFailures += 1;
      if (state.pollFailures <= 5) {
        state.timer = window.setTimeout(pollJob, 1600);
      } else {
        showProcessError("连接中断", error.message || "无法读取任务状态。");
      }
    }
  }

  function makeStat(label, value, unit = "") {
    const card = document.createElement("div");
    card.className = "stat";
    const labelNode = document.createElement("span");
    labelNode.className = "stat-label";
    labelNode.textContent = label;
    const valueNode = document.createElement("span");
    valueNode.className = "stat-value";
    valueNode.textContent = String(value);
    if (unit) {
      const unitNode = document.createElement("small");
      unitNode.textContent = unit;
      valueNode.appendChild(unitNode);
    }
    card.append(labelNode, valueNode);
    return card;
  }

  function renderResults(job) {
    state.job = job;
    state.jobId = job.id;
    const clips = job.clips || [];
    const words = job.words || [];
    els.resultTitle.textContent = "词库已就绪";
    els.resultSub.textContent = `${job.source?.name || "视频"} · 原片 ${formatDuration(job.source?.duration)} · ${
      job.settings?.granularity === "char" ? "按字切分" : "按词切分"
    }`;

    els.stats.replaceChildren(
      makeStat("WORDS", job.word_count || words.length, "个词"),
      makeStat("CLIPS", clips.length || job.clip_count || 0, "段素材"),
      makeStat("SOURCE", formatDuration(job.source?.duration), ""),
      makeStat("TIME", job.elapsed_seconds ? formatDuration(job.elapsed_seconds) : "—", ""),
    );

    if (job.zip) {
      els.downloadZip.href = `/api/jobs/${job.id}/download.zip`;
      els.downloadZip.classList.remove("is-disabled");
      els.downloadZip.removeAttribute("aria-disabled");
    } else {
      els.downloadZip.href = "#";
      els.downloadZip.classList.add("is-disabled");
      els.downloadZip.setAttribute("aria-disabled", "true");
    }

    els.transcriptText.textContent = job.transcript || "（没有识别到文本）";
    const errorCount = Array.isArray(job.cut_errors) ? job.cut_errors.length : 0;
    let footnote = `识别模型：${job.model_label || job.settings?.model || "—"}。词边界来自语音识别，偶尔会偏几十毫秒；先试听，再在「编辑词表」里微调。`;
    if (errorCount) footnote += ` 这次有 ${errorCount} 个片段切片失败，可以重新切片再试。`;
    els.resultsFootnote.textContent = footnote;

    state.visible = 120;
    state.query = els.searchInput.value.trim().toLowerCase();
    state.filter = "all";
    $$("button", els.filterGroup).forEach((button) => {
      button.classList.toggle("active", button.dataset.filter === "all");
    });
    renderGrid();
    setView("results");
  }

  function renderReady(job) {
    state.job = job;
    state.jobId = job.id;
    els.resultTitle.textContent = "识别完成，先校对一下";
    els.resultSub.textContent = `${job.source?.name || "视频"} · 原片 ${formatDuration(job.source?.duration)} · 还没有切片`;
    els.stats.replaceChildren(
      makeStat("WORDS", job.word_count || (job.words || []).length, "个词"),
      makeStat("MODE", "待校对", ""),
      makeStat("SOURCE", formatDuration(job.source?.duration), ""),
      makeStat("MODEL", job.settings?.model || "—", ""),
    );
    els.downloadZip.href = "#";
    els.downloadZip.classList.add("is-disabled");
    els.downloadZip.setAttribute("aria-disabled", "true");
    els.transcriptText.textContent = job.transcript || "（没有识别到文本）";
    els.clipGrid.replaceChildren();
    els.emptyResults.querySelector("span").textContent = "Aa";
    els.emptyResults.querySelector("p").textContent = "词表已经准备好";
    els.emptyResults.querySelector("small").textContent = "改掉错字、调整时间，再点「保存并重新切片」。";
    show(els.emptyResults);
    hide(els.loadMoreButton);
    els.resultsFootnote.textContent = "你选择了先校对再切片。编辑词表时可以合并、删除、改字和微调时间点。";
    setView("results");
    window.setTimeout(() => openEditor(), 380);
  }

  function filteredClips() {
    const job = state.job;
    if (!job) return [];
    const clips = job.clips || [];
    const counts = new Map();
    clips.forEach((clip) => {
      counts.set(clip.text, (counts.get(clip.text) || 0) + 1);
    });
    const scored = [];
    clips.forEach((clip) => {
      if (state.filter === "repeat" && (counts.get(clip.text) || 0) < 2) return;
      if (state.filter === "low" && Number(clip.confidence || 0) >= 0.55) return;
      let score = 0;
      if (state.query) {
        const word = String(clip.text || "").toLowerCase();
        const sentence = String(clip.sentence || "").toLowerCase();
        if (word === state.query) score = 0;
        else if (word.includes(state.query)) score = 1;
        else if (sentence.includes(state.query)) score = 2;
        else return;
      }
      scored.push({ clip, score });
    });
    scored.sort((a, b) => a.score - b.score || a.clip.index - b.clip.index);
    return scored.map((item) => item.clip);
  }

  function makeDownloadButton(clip, filename, kind) {
    const button = document.createElement("button");
    button.className = "clip-download";
    button.type = "button";
    button.title = kind === "wav" ? "下载 WAV" : "下载 MP4";
    button.setAttribute("aria-label", kind === "wav" ? "下载 WAV" : "下载 MP4");
    button.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2v8m0 0 3-3m-3 3L5 7M3 12.5v1h10v-1"/></svg>';
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const url = `/api/jobs/${state.job.id}/media/${clip.index}/${kind}?download=1`;
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename || "";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    });
    return button;
  }

  function makeClipCard(clip) {
    const card = document.createElement("article");
    card.className = "clip-card";
    card.tabIndex = 0;
    card.style.setProperty("--hue", String(textHue(clip.text)));
    if (!clip.files?.mp4) card.classList.add("audio-only");
    card.addEventListener("click", () => openMedia(clip));
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openMedia(clip);
      }
    });

    const visual = document.createElement("div");
    visual.className = "clip-visual";

    const index = document.createElement("span");
    index.className = "clip-index";
    index.textContent = `#${String(clip.index).padStart(3, "0")}`;

    const word = document.createElement("strong");
    word.className = "clip-word";
    word.textContent = clip.text;

    const play = document.createElement("span");
    play.className = "clip-play";
    play.setAttribute("aria-hidden", "true");

    visual.append(index, word, play);
    if (Number(clip.confidence || 0) < 0.55) {
      const confidence = document.createElement("span");
      confidence.className = "clip-confidence";
      confidence.textContent = "低置信度";
      visual.appendChild(confidence);
    }

    const body = document.createElement("div");
    body.className = "clip-body";
    const meta = document.createElement("div");
    meta.className = "clip-meta";
    const timeWrap = document.createElement("span");
    const time = document.createElement("span");
    time.className = "clip-time";
    time.textContent = `${formatClock(clip.start)} → ${formatClock(clip.end)}`;
    const duration = document.createElement("span");
    duration.className = "clip-duration";
    duration.textContent = ` ${Number(clip.duration || 0).toFixed(2)}s`;
    timeWrap.append(time, duration);
    meta.append(timeWrap);

    const preferredKind = clip.files?.mp4 ? "mp4" : "wav";
    const preferredFile = clip.files?.[preferredKind];
    if (preferredFile) {
      meta.appendChild(makeDownloadButton(clip, preferredFile, preferredKind));
    }
    body.appendChild(meta);

    const sentence = document.createElement("p");
    sentence.className = "clip-sentence";
    sentence.textContent = clip.sentence || "原句未记录";
    sentence.title = clip.sentence || "";
    body.appendChild(sentence);

    card.append(visual, body);
    return card;
  }

  function renderGrid() {
    const clips = filteredClips();
    const visible = clips.slice(0, state.visible);
    els.clipGrid.replaceChildren(...visible.map(makeClipCard));
    const noResults = clips.length === 0;
    els.emptyResults.classList.toggle("hidden", !noResults);
    if (noResults) {
      els.emptyResults.querySelector("span").textContent = "0";
      els.emptyResults.querySelector("p").textContent = "没有找到匹配的词";
      els.emptyResults.querySelector("small").textContent = "换个关键词，或点「全部」看看。";
    }
    els.loadMoreButton.classList.toggle("hidden", clips.length <= state.visible);
    if (clips.length > state.visible) {
      els.loadMoreButton.textContent = `加载更多（还有 ${clips.length - state.visible} 个）`;
    }
  }

  function openMedia(clip) {
    if (!state.job) return;
    els.mediaWord.textContent = clip.text;
    els.mediaTime.textContent = `${formatClock(clip.start)} → ${formatClock(clip.end)} · ${Number(clip.duration || 0).toFixed(2)} 秒`;
    els.mediaSentence.textContent = clip.sentence || "原句未记录";
    els.mediaPreview.replaceChildren();
    els.mediaDownloads.replaceChildren();

    const mp4 = clip.files?.mp4;
    const wav = clip.files?.wav;
    if (mp4) {
      const video = document.createElement("video");
      video.controls = true;
      video.autoplay = true;
      video.playsInline = true;
      video.preload = "metadata";
      video.src = `/api/jobs/${state.job.id}/media/${clip.index}/mp4`;
      els.mediaPreview.appendChild(video);
    } else if (wav) {
      const art = document.createElement("div");
      art.className = "media-audio-art";
      const bars = document.createElement("div");
      bars.className = "audio-bars";
      for (let index = 0; index < 42; index += 1) {
        const bar = document.createElement("span");
        const height = 12 + Math.abs(Math.sin(index * 1.7 + clip.index)) * 76;
        bar.style.height = `${height}px`;
        bar.style.opacity = `${0.32 + Math.abs(Math.cos(index * 0.9)) * 0.52}`;
        bars.appendChild(bar);
      }
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.autoplay = true;
      audio.preload = "metadata";
      audio.src = `/api/jobs/${state.job.id}/media/${clip.index}/wav`;
      art.append(bars, audio);
      els.mediaPreview.appendChild(art);
    }

    if (mp4) {
      const link = document.createElement("a");
      link.href = `/api/jobs/${state.job.id}/media/${clip.index}/mp4?download=1`;
      link.download = mp4;
      link.innerHTML = "下载 MP4 <span>VIDEO</span>";
      els.mediaDownloads.appendChild(link);
    }
    if (wav) {
      const link = document.createElement("a");
      link.href = `/api/jobs/${state.job.id}/media/${clip.index}/wav?download=1`;
      link.download = wav;
      link.innerHTML = "下载 WAV <span>AUDIO</span>";
      els.mediaDownloads.appendChild(link);
    }
    openModal(els.mediaModal);
  }

  function openModal(element) {
    element.classList.remove("hidden");
    document.body.style.overflow = "hidden";
  }

  function closeModal(element) {
    if (!element || element.classList.contains("hidden")) return;
    element.classList.add("hidden");
    $$("video, audio", element).forEach((media) => {
      media.pause();
      media.removeAttribute("src");
      media.load();
    });
    if (element === els.mediaModal) els.mediaPreview.replaceChildren();
    if (!document.querySelector(".modal:not(.hidden)")) {
      document.body.style.overflow = "";
    }
  }

  function bindModals() {
    $$("[data-close]").forEach((button) => {
      button.addEventListener("click", () => {
        closeModal(document.getElementById(button.dataset.close));
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const open = Array.from(document.querySelectorAll(".modal:not(.hidden)")).pop();
      if (open) closeModal(open);
    });
  }

  function syncEditorSettings() {
    const settings = state.job?.settings || {};
    const padding = String(settings.padding ?? 0.06);
    els.editorPadding.value = ["0.03", "0.06", "0.10"].includes(padding) ? padding : "0.06";
    els.editorFormat.value = ["video", "both", "audio"].includes(settings.format) ? settings.format : "video";
    els.editorQuality.value = ["720p", "original", "1080p"].includes(settings.quality) ? settings.quality : "720p";
  }

  function openEditor() {
    if (!state.job?.words?.length) {
      toast("还没有可编辑的词表。", "error");
      return;
    }
    state.editWords = state.job.words.map((word) => ({ ...word }));
    state.selected.clear();
    els.editorSearch.value = "";
    els.selectAllCheck.checked = false;
    syncEditorSettings();
    renderEditor();
    openModal(els.editorModal);
  }

  function confidenceClass(value) {
    return Number(value || 0) < 0.55 ? "confidence-pill low" : "confidence-pill";
  }

  function renderEditor() {
    els.editorBody.replaceChildren();
    state.editWords.forEach((word, index) => {
      const row = document.createElement("tr");
      row.dataset.index = String(index);
      if (state.selected.has(index)) row.classList.add("selected");

      const checkCell = document.createElement("td");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "row-check";
      checkbox.checked = state.selected.has(index);
      checkbox.setAttribute("aria-label", `选择第 ${index + 1} 个词`);
      checkCell.appendChild(checkbox);

      const indexCell = document.createElement("td");
      indexCell.innerHTML = `<span class="editor-row-index">${String(index + 1).padStart(3, "0")}</span>`;

      const textCell = document.createElement("td");
      const textInput = document.createElement("input");
      textInput.type = "text";
      textInput.className = "editor-text";
      textInput.value = word.text || "";
      textInput.dataset.field = "text";
      textInput.maxLength = 80;
      textCell.appendChild(textInput);

      const startCell = document.createElement("td");
      const startInput = document.createElement("input");
      startInput.type = "number";
      startInput.className = "editor-time";
      startInput.step = "0.001";
      startInput.min = "0";
      startInput.value = Number(word.start || 0).toFixed(3);
      startInput.dataset.field = "start";
      startCell.appendChild(startInput);

      const endCell = document.createElement("td");
      const endInput = document.createElement("input");
      endInput.type = "number";
      endInput.className = "editor-time";
      endInput.step = "0.001";
      endInput.min = "0";
      endInput.value = Number(word.end || 0).toFixed(3);
      endInput.dataset.field = "end";
      endCell.appendChild(endInput);

      const confidenceCell = document.createElement("td");
      const confidence = document.createElement("span");
      confidence.className = confidenceClass(word.probability);
      confidence.textContent = `${Math.round(Number(word.probability || 0) * 100)}%`;
      confidenceCell.appendChild(confidence);

      const sentenceCell = document.createElement("td");
      const sentence = document.createElement("span");
      sentence.className = "editor-sentence";
      sentence.textContent = word.sentence || "—";
      sentence.title = word.sentence || "";
      sentenceCell.appendChild(sentence);

      row.append(checkCell, indexCell, textCell, startCell, endCell, confidenceCell, sentenceCell);
      els.editorBody.appendChild(row);
    });
    updateEditorCount();
    filterEditorRows();
  }

  function updateEditorCount() {
    const selected = state.selected.size;
    els.editorCount.textContent = `${state.editWords.length} 个词${selected ? ` · 已选 ${selected}` : ""}`;
    els.selectAllCheck.checked = state.editWords.length > 0 && selected === state.editWords.length;
  }

  function filterEditorRows() {
    const query = els.editorSearch.value.trim().toLowerCase();
    $$("tr", els.editorBody).forEach((row) => {
      if (!query) {
        row.classList.remove("editor-row-hidden");
        return;
      }
      const index = Number(row.dataset.index);
      const word = state.editWords[index];
      const haystack = `${word?.text || ""} ${word?.sentence || ""}`.toLowerCase();
      row.classList.toggle("editor-row-hidden", !haystack.includes(query));
    });
  }

  function bindEditor() {
    els.editorBody.addEventListener("input", (event) => {
      const input = event.target;
      if (!(input instanceof HTMLInputElement) || !input.dataset.field) return;
      const row = input.closest("tr");
      if (!row) return;
      const index = Number(row.dataset.index);
      const word = state.editWords[index];
      if (!word) return;
      if (input.dataset.field === "text") {
        word.text = input.value;
      } else {
        const value = Number(input.value);
        if (Number.isFinite(value)) word[input.dataset.field] = value;
      }
    });
    els.editorBody.addEventListener("change", (event) => {
      const input = event.target;
      if (!(input instanceof HTMLInputElement) || !input.classList.contains("row-check")) return;
      const row = input.closest("tr");
      const index = Number(row?.dataset.index);
      if (input.checked) state.selected.add(index);
      else state.selected.delete(index);
      row?.classList.toggle("selected", input.checked);
      updateEditorCount();
    });
    els.selectAllCheck.addEventListener("change", () => {
      state.selected.clear();
      if (els.selectAllCheck.checked) {
        state.editWords.forEach((_, index) => state.selected.add(index));
      }
      renderEditor();
    });
    els.editorSearch.addEventListener("input", filterEditorRows);
    els.deleteSelectedButton.addEventListener("click", () => {
      if (!state.selected.size) {
        toast("先勾选要删除的词。");
        return;
      }
      state.editWords = state.editWords.filter((_, index) => !state.selected.has(index));
      state.selected.clear();
      renderEditor();
      toast("已删除选中的词。", "success");
    });
    els.mergeSelectedButton.addEventListener("click", mergeSelected);
    els.resegmentWordButton.addEventListener("click", () => resegment("word"));
    els.resegmentCharButton.addEventListener("click", () => resegment("char"));
    els.saveRecutButton.addEventListener("click", saveRecut);
  }

  function mergeSelected() {
    if (state.selected.size < 2) {
      toast("至少勾选两个相邻的词。");
      return;
    }
    const indices = Array.from(state.selected).sort((a, b) => a - b);
    const contiguous = indices[indices.length - 1] - indices[0] + 1 === indices.length;
    if (!contiguous) {
      toast("只能合并相邻的词。", "error");
      return;
    }
    const selectedWords = indices.map((index) => state.editWords[index]);
    const needsSpace = selectedWords.some((word) => /[A-Za-z0-9]/.test(word.text || ""));
    const mergedText = selectedWords.map((word) => word.text).join(needsSpace ? " " : "");
    const merged = {
      ...selectedWords[0],
      text: mergedText,
      start: Math.min(...selectedWords.map((word) => Number(word.start || 0))),
      end: Math.max(...selectedWords.map((word) => Number(word.end || 0))),
      probability: selectedWords.reduce((sum, word) => sum + Number(word.probability || 0), 0) / selectedWords.length,
      sentence: selectedWords[0].sentence || "",
    };
    const insertAt = indices[0];
    state.editWords = state.editWords.filter((_, index) => !state.selected.has(index));
    state.editWords.splice(insertAt, 0, merged);
    state.selected.clear();
    renderEditor();
    toast("已合并选中的词。", "success");
  }

  async function resegment(granularity) {
    if (!state.jobId) return;
    const label = granularity === "char" ? "按字" : "按词";
    if (!window.confirm(`会丢弃当前词表里的手动修改，按${label}重新分词并切片。继续吗？`)) return;
    try {
      const response = await fetch(`/api/jobs/${state.jobId}/resegment`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ granularity }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `服务器返回 ${response.status}`);
      state.job = payload;
      closeModal(els.editorModal);
      hide(els.processError);
      setView("processing");
      setProgress(80, "正在按新粒度重新分词…");
      updateSteps("cutting");
      pollJob();
    } catch (error) {
      toast(error.message || "重新分词失败。", "error");
    }
  }

  async function saveRecut() {
    if (!state.jobId) return;
    const words = state.editWords
      .map((word) => ({
        text: String(word.text || "").trim(),
        start: Number(word.start),
        end: Number(word.end),
        sentence: word.sentence || "",
        sentence_index: word.sentence_index || 0,
        probability: Number(word.probability || 0),
      }))
      .filter((word) => word.text);
    if (!words.length) {
      toast("词表不能为空。", "error");
      return;
    }
    const invalid = words.find((word) => !Number.isFinite(word.start) || !Number.isFinite(word.end) || word.end <= word.start);
    if (invalid) {
      toast(`「${invalid.text}」的时间不对：结束时间要大于开始时间。`, "error");
      return;
    }
    try {
      const response = await fetch(`/api/jobs/${state.jobId}/recut`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          words,
          padding: Number(els.editorPadding.value),
          format: els.editorFormat.value,
          quality: els.editorQuality.value,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `服务器返回 ${response.status}`);
      state.job = payload;
      closeModal(els.editorModal);
      hide(els.processError);
      setView("processing");
      setProgress(80, "正在按新词表重新切片…");
      updateSteps("cutting");
      pollJob();
      toast("词表已保存，开始重新切片。", "success");
    } catch (error) {
      toast(error.message || "保存失败。", "error");
    }
  }

  async function copyText(text, successMessage) {
    if (!text) {
      toast("没有可复制的文字。");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast(successMessage || "已复制。", "success");
    } catch (error) {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
      toast(successMessage || "已复制。", "success");
    }
  }

  function bindResults() {
    els.searchInput.addEventListener("input", () => {
      clearTimeout(state.searchTimer);
      state.searchTimer = window.setTimeout(() => {
        state.query = els.searchInput.value.trim().toLowerCase();
        state.visible = 120;
        renderGrid();
      }, 120);
    });
    els.filterGroup.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-filter]");
      if (!button) return;
      state.filter = button.dataset.filter;
      state.visible = 120;
      $$("button", els.filterGroup).forEach((item) => item.classList.toggle("active", item === button));
      renderGrid();
    });
    els.loadMoreButton.addEventListener("click", () => {
      state.visible += 120;
      renderGrid();
    });
    els.copyWordsButton.addEventListener("click", () => {
      const words = (state.job?.words || []).map((word) => word.text);
      copyText(words.join("　"), `已复制 ${words.length} 个词。`);
    });
    els.copyTranscriptButton.addEventListener("click", () => {
      copyText(state.job?.transcript || "", "识别文本已复制。");
    });
    els.editWordsButton.addEventListener("click", openEditor);
    els.newJobButton.addEventListener("click", resetToUpload);
    els.errorReset.addEventListener("click", resetToUpload);
    els.downloadZip.addEventListener("click", (event) => {
      if (els.downloadZip.classList.contains("is-disabled")) event.preventDefault();
    });
  }

  function resetToUpload() {
    clearTimeout(state.timer);
    state.job = null;
    state.jobId = null;
    state.pollFailures = 0;
    state.visible = 120;
    state.query = "";
    state.filter = "all";
    state.editWords = [];
    state.selected.clear();
    localStorage.removeItem(STORAGE_KEY);
    els.searchInput.value = "";
    els.editorSearch.value = "";
    hide(els.processError);
    clearFile();
    setView("upload");
  }

  async function restoreJob() {
    const savedId = localStorage.getItem(STORAGE_KEY);
    if (!savedId) return;
    try {
      const response = await fetch(`/api/jobs/${savedId}`, { cache: "no-store" });
      if (!response.ok) {
        localStorage.removeItem(STORAGE_KEY);
        return;
      }
      const job = await response.json();
      state.job = job;
      state.jobId = job.id;
      if (job.status === "done") {
        renderResults(job);
      } else if (job.status === "ready") {
        renderReady(job);
      } else if (job.status === "error") {
        updateProcessing(job);
        showProcessError("上次处理没有完成", job.error || job.message);
      } else {
        setView("processing");
        updateProcessing(job);
        pollJob();
      }
    } catch (error) {
      // A saved job should never block a fresh upload.
    }
  }

  async function checkHealth() {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      const health = await response.json();
      if (!health.ok) {
        toast("没有找到 ffmpeg，视频处理会失败。请检查服务器是否安装了 ffmpeg。", "error");
      }

      if (health.max_upload_mb) {
        MAX_UPLOAD_BYTES = Number(health.max_upload_mb) * 1024 * 1024;
        const sub = $(".drop-sub", els.dropzone);
        if (sub && !state.file) {
          sub.textContent = `或点击选择文件 · 建议 ${health.max_upload_mb}MB 以内`;
        }
      }

      if (health.default_model) {
        const defaultOption = els.modelSelect.querySelector(`option[value="${health.default_model}"]`);
        if (defaultOption) {
          els.modelSelect.value = health.default_model;
          if (health.models && health.models[health.default_model] === false) {
            defaultOption.textContent += " · 首次需下载";
          }
        }
        const hint = $(".upload-hint", document);
        if (hint) {
          hint.textContent = `默认 ${health.default_model} 模型；首次使用某个模型时会自动下载。词与词黏在一起时会自动收窄留白，避免串进相邻词。`;
        }
      }
    } catch (error) {
      // The upload flow will surface a connection error if the server is down.
    }
  }

  function initApp() {
    if (appInitialized) return;
    appInitialized = true;
    bindUpload();
    bindModals();
    bindEditor();
    bindResults();
    checkHealth();
    restoreJob();
  }

  async function startApp() {
    bindAuth();
    const allowed = await checkAccess();
    if (allowed) initApp();
  }

  startApp();
})();
