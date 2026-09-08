// Панель оператора: WS-приёмник кадров + командные POST'ы (design C6).
// Никаких фреймворков/CDN -- робот работает без интернета.

(() => {
  "use strict";

  const WS_RECONNECT_MIN_MS = 1000;
  const WS_RECONNECT_MAX_MS = 10000;
  const LONG_PRESS_MS = 2000;
  const MISSION_STALE_S = 3.0;
  const DEFAULT_SLIDE_INTERVAL_S = 8.0;

  const STATE_NAME_RU = {
    idle: "Ожидание",
    greeting: "Приветствие",
    navigating: "Едет",
    narrating: "Рассказывает",
    answering: "Отвечает на вопрос",
    awaiting_confirm: "Ждёт подтверждения",
    paused: "На паузе",
    held: "Удержан (safety)",
    returning: "Едет на базу",
    unknown: "Нет данных",
  };

  const panelEl = document.getElementById("panel");
  const activeEl = document.getElementById("active");
  const connBanner = document.getElementById("conn-banner");
  const messageEl = document.getElementById("message");
  const statusState = document.getElementById("status-state");
  const statusStops = document.getElementById("status-stops");
  const statusExhibit = document.getElementById("status-exhibit");
  const statusEstop = document.getElementById("status-estop");
  const statusSupervisor = document.getElementById("status-supervisor");
  const statusAge = document.getElementById("status-age");
  const tourSelect = document.getElementById("tour-select");
  const btnStart = document.getElementById("btn-start");
  const btnStop = document.getElementById("btn-stop");
  const btnHome = document.getElementById("btn-home");
  const btnReset = document.getElementById("btn-reset");
  const resetConfirmDialog = document.getElementById("reset-confirm");
  const btnResetConfirm = document.getElementById("btn-reset-confirm");
  const btnResetCancel = document.getElementById("btn-reset-cancel");
  const unlockCorner = document.getElementById("unlock-corner");
  const pinDialog = document.getElementById("pin-dialog");
  const pinInput = document.getElementById("pin-input");
  const btnPinOk = document.getElementById("btn-pin-ok");
  const btnPinCancel = document.getElementById("btn-pin-cancel");
  const slideLayerA = document.getElementById("slide-layer-a");
  const slideLayerB = document.getElementById("slide-layer-b");
  const titleCard = document.getElementById("title-card");
  const titleCardText = document.getElementById("title-card-text");
  const returningScreen = document.getElementById("returning-screen");
  const estopOverlay = document.getElementById("estop-overlay");
  const connLostOverlay = document.getElementById("conn-lost-overlay");
  const prefetchPool = document.getElementById("prefetch-pool");

  let latestFrame = null;
  // Не секрет (задание явно: "не выдавай его за безопасность") -- защита
  // от случайного тапа посетителя, не от того, кто откроет devtools.
  // Отдаётся вместе с /api/tours -- отдельный endpoint ради одного поля
  // не заводим (design "не изобретай лишнего").
  let operatorPin = null;
  let panelUnlocked = false;
  let slideIntervalS = DEFAULT_SLIDE_INTERVAL_S;

  // -- слайды (design D1/D3/D5) ------------------------------------------------
  // lastKnownActive НЕ сбрасывается, когда latestFrame становится null
  // (обрыв WS) -- иначе потеря связи посреди тура откидывала бы экран в
  // панель управления вместо плашки "нет связи" поверх слайдов (design D4).
  let lastKnownActive = false;
  let lastWasReturning = false;
  // exhibit_id/chunk_index последнего ПРИМЕНЁННОГО слайда -- отдельно от
  // latestFrame, тем же смыслом: реконнект не должен перезапускать текущий
  // слайд, если оба поля не изменились (design D5).
  let lastSlideExhibitId = null;
  let lastSlideChunkIndex;
  let lastManifestTitle = "";
  let lastPrefetchedExhibitId = null;
  // exhibit_id -> манифест ({title, chunk_ids, items}) либо Promise на него,
  // пока грузится -- предотвращает параллельные повторные запросы одного
  // и того же экспоната.
  const manifestCache = new Map();
  const brokenMediaUrls = new Set();
  let currentCycle = null; // {items, index, timer, videoEl}
  let activeLayer = slideLayerA;
  let hiddenLayer = slideLayerB;

  function showMessage(text) {
    if (!text) {
      messageEl.hidden = true;
      messageEl.textContent = "";
      return;
    }
    messageEl.hidden = false;
    messageEl.textContent = text;
  }

  async function api(method, path, body) {
    const opts = { method };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch (err) {
      showMessage(`Нет связи с operator_ui: ${err}`);
      return null;
    }
    let data = {};
    try {
      data = await resp.json();
    } catch {
      /* тело могло быть пустым (200 без body) */
    }
    if (!resp.ok) {
      showMessage(data.message || `Ошибка ${resp.status}`);
      return null;
    }
    showMessage("");
    return data;
  }

  async function loadTours() {
    const data = await api("GET", "/api/tours");
    if (!data) return;
    if (typeof data.operator_pin === "string") operatorPin = data.operator_pin;
    if (typeof data.slide_interval_s === "number") slideIntervalS = data.slide_interval_s;
    tourSelect.innerHTML = "";
    for (const tour of data.tours || []) {
      const opt = document.createElement("option");
      opt.value = tour.id;
      opt.textContent = tour.name || tour.id;
      tourSelect.appendChild(opt);
    }
  }

  function commandsBlocked() {
    if (!latestFrame) return true;
    if (latestFrame.mission_state_age_s === null) return true;
    if (latestFrame.mission_state_age_s > MISSION_STALE_S) return true;
    if (latestFrame.estop) return true;
    return latestFrame.supervisor_state === "FAULT" || latestFrame.supervisor_state === "SHUTDOWN";
  }

  function blockReason() {
    if (!latestFrame || latestFrame.mission_state_age_s === null) {
      return "Нет связи с mission_fsm";
    }
    if (latestFrame.mission_state_age_s > MISSION_STALE_S) {
      return "Данные mission_fsm устарели";
    }
    if (latestFrame.estop) return "E-Stop активен";
    if (latestFrame.supervisor_state === "FAULT" || latestFrame.supervisor_state === "SHUTDOWN") {
      return `Супервизор: ${latestFrame.supervisor_state}`;
    }
    return "";
  }

  function render() {
    const blocked = commandsBlocked();
    connBanner.hidden = !blocked;
    connBanner.textContent = blockReason();

    for (const btn of [btnStart, btnStop, btnHome, btnReset]) {
      btn.disabled = blocked;
    }

    if (latestFrame) {
      lastKnownActive = latestFrame.state_name !== "idle" && latestFrame.state_name !== "unknown";
    }
    // latestFrame === null (WS оборвался) -- lastKnownActive НЕ трогаем,
    // иначе обрыв связи посреди тура откинул бы экран в панель управления
    // вместо плашки "нет связи" поверх замерших слайдов (design D4/D5).
    const isActive = lastKnownActive;
    // Возврат в idle обязан снова запереть панель -- иначе долгий тап на
    // ПРЕДЫДУЩЕМ туре остаётся в силе и на следующем без PIN.
    if (!isActive) panelUnlocked = false;
    panelEl.hidden = isActive && !panelUnlocked;
    activeEl.hidden = !isActive;

    if (latestFrame) {
      statusState.textContent = STATE_NAME_RU[latestFrame.state_name] || latestFrame.state_name;
      statusStops.textContent = latestFrame.stop_total
        ? `Остановка ${latestFrame.stop_index + 1}/${latestFrame.stop_total}`
        : "";
      statusExhibit.textContent = latestFrame.exhibit_id || "";
      statusEstop.hidden = !latestFrame.estop;
      statusEstop.textContent = latestFrame.estop ? "E-STOP" : "";
      statusSupervisor.textContent = latestFrame.supervisor_state || "";
      statusAge.textContent =
        latestFrame.mission_state_age_s === null
          ? "нет данных"
          : `связь ${latestFrame.mission_state_age_s.toFixed(1)} с назад`;
    } else {
      statusState.textContent = "Нет данных";
      statusStops.textContent = "";
      statusExhibit.textContent = "";
      statusEstop.hidden = true;
      statusSupervisor.textContent = "";
      statusAge.textContent = "";
    }

    if (isActive) updateActiveView();
  }

  function applyFrame(frame) {
    latestFrame = frame;
    render();
  }

  // -- слайды: манифест + выбор (design D2/D3) -- зеркалит lib/slide_select.py --

  function mediaUrl(item) {
    return `/media/${item.path}`;
  }

  function resolveChunkId(chunkIds, chunkIndex) {
    if (chunkIndex >= 0 && chunkIndex < chunkIds.length) return chunkIds[chunkIndex];
    return null;
  }

  function selectMedia(items, chunkId) {
    if (chunkId !== null) {
      const matching = items.filter((item) => item.chunk_id === chunkId);
      if (matching.length) return matching;
    }
    return items.filter((item) => item.chunk_id === "");
  }

  async function fetchManifest(exhibitId) {
    if (manifestCache.has(exhibitId)) return manifestCache.get(exhibitId);
    const promise = (async () => {
      try {
        const resp = await fetch(`/api/media/${encodeURIComponent(exhibitId)}`);
        if (!resp.ok) return { title: "", chunk_ids: [], items: [] };
        return await resp.json();
      } catch (err) {
        console.error("operator_ui: не удалось загрузить манифест", exhibitId, err);
        return { title: "", chunk_ids: [], items: [] };
      }
    })();
    manifestCache.set(exhibitId, promise);
    const manifest = await promise;
    manifestCache.set(exhibitId, manifest); // заменить Promise на разрешённое значение
    return manifest;
  }

  function maybePrefetchNext() {
    const nextId = latestFrame && latestFrame.next_exhibit_id;
    if (!nextId || nextId === lastPrefetchedExhibitId) return;
    lastPrefetchedExhibitId = nextId;
    // design D2: тот же вызов одновременно наполняет кэш узла и даёт
    // браузеру hint для префетча файлов -- второй сервер-side механизм не нужен.
    fetchManifest(nextId).then((manifest) => {
      prefetchPool.innerHTML = "";
      for (const item of manifest.items || []) {
        const el = document.createElement(item.kind === "video" ? "video" : "img");
        el.src = mediaUrl(item);
        el.preload = "auto";
        if (item.kind === "video") el.muted = true;
        prefetchPool.appendChild(el);
      }
    });
  }

  // -- показ: цикл по выбранным элементам, кроссфейд, video/image (design D3) --

  function swapLayers() {
    [activeLayer, hiddenLayer] = [hiddenLayer, activeLayer];
  }

  function stopCycle() {
    if (currentCycle) {
      clearTimeout(currentCycle.timer);
    }
    currentCycle = null;
  }

  function showTitleCard(text) {
    stopCycle();
    returningScreen.hidden = true;
    titleCardText.textContent = text || "…";
    titleCard.hidden = false;
    slideLayerA.classList.remove("visible");
    slideLayerB.classList.remove("visible");
  }

  function showReturningScreen() {
    stopCycle();
    titleCard.hidden = true;
    slideLayerA.classList.remove("visible");
    slideLayerB.classList.remove("visible");
    returningScreen.hidden = false;
  }

  function handleBrokenMedia(item) {
    const url = mediaUrl(item);
    if (!brokenMediaUrls.has(url)) {
      brokenMediaUrls.add(url);
      console.warn("operator_ui: медиа недоступно, пропускаю", url);
    }
    if (!currentCycle) return;
    const remaining = currentCycle.items.filter((i) => i !== item);
    if (!remaining.length) {
      // Ничего не осталось от резолва текущего чанка -- design D1's
      // фолбэк "манифест пуст -> титульная карточка", не чёрный экран.
      showTitleCard(lastManifestTitle || lastSlideExhibitId || "");
      return;
    }
    currentCycle.items = remaining;
    currentCycle.index = currentCycle.index % remaining.length;
    playCycleItem();
  }

  function playCycleItem() {
    if (!currentCycle || !currentCycle.items.length) return;
    renderItem(currentCycle.items[currentCycle.index]);
  }

  function advanceCycle() {
    if (!currentCycle || !currentCycle.items.length) return;
    currentCycle.index = (currentCycle.index + 1) % currentCycle.items.length;
    playCycleItem();
  }

  function renderItem(item) {
    titleCard.hidden = true;
    returningScreen.hidden = true;

    hiddenLayer.innerHTML = "";
    let mediaEl;

    if (item.kind === "video") {
      mediaEl = document.createElement("video");
      // muted ДО autoplay -- иначе браузер блокирует автозапуск (design D3).
      // Звук только у TTS, дорожка видео не звучит никогда.
      mediaEl.muted = true;
      mediaEl.autoplay = true;
      mediaEl.playsInline = true;
      const soloVideo = currentCycle.items.length === 1;
      mediaEl.loop = soloVideo; // loop только когда элемент в группе один
      if (!soloVideo) {
        mediaEl.addEventListener("ended", advanceCycle, { once: true });
      }
      mediaEl.addEventListener("error", () => handleBrokenMedia(item));
      mediaEl.addEventListener("stalled", () => handleBrokenMedia(item));
      mediaEl.src = mediaUrl(item);
      currentCycle.videoEl = mediaEl;
    } else {
      mediaEl = document.createElement("img");
      mediaEl.addEventListener("error", () => handleBrokenMedia(item));
      mediaEl.src = mediaUrl(item);
      currentCycle.videoEl = null;
      const intervalS = item.duration_s > 0 ? item.duration_s : slideIntervalS;
      currentCycle.timer = setTimeout(advanceCycle, intervalS * 1000);
    }

    hiddenLayer.appendChild(mediaEl);
    requestAnimationFrame(() => {
      hiddenLayer.classList.add("visible");
      activeLayer.classList.remove("visible");
      swapLayers();
    });
  }

  function startCycle(items) {
    stopCycle();
    currentCycle = { items, index: 0, timer: null, videoEl: null };
    playCycleItem();
  }

  async function updateSlide(exhibitId, chunkIndex) {
    if (!exhibitId) {
      lastManifestTitle = "";
      showTitleCard("");
      return;
    }
    const manifest = await fetchManifest(exhibitId);
    // Гонка: exhibit_id мог уже смениться, пока манифест грузился.
    if (exhibitId !== lastSlideExhibitId) return;
    lastManifestTitle = manifest.title || exhibitId;
    const chunkId = resolveChunkId(manifest.chunk_ids || [], chunkIndex);
    const items = selectMedia(manifest.items || [], chunkId).filter(
      (item) => !brokenMediaUrls.has(mediaUrl(item))
    );
    if (!items.length) {
      showTitleCard(lastManifestTitle);
      return;
    }
    startCycle(items);
  }

  function setVideoPaused(shouldPause) {
    const videoEl = currentCycle && currentCycle.videoEl;
    if (!videoEl) return;
    if (shouldPause) {
      if (!videoEl.paused) videoEl.pause();
    } else if (videoEl.paused) {
      videoEl.play().catch(() => {});
    }
  }

  function updateActiveView() {
    const estopActive = Boolean(latestFrame && latestFrame.estop);
    const faultActive = Boolean(
      latestFrame &&
        (latestFrame.supervisor_state === "FAULT" || latestFrame.supervisor_state === "SHUTDOWN")
    );
    const connLost =
      !latestFrame ||
      latestFrame.mission_state_age_s === null ||
      latestFrame.mission_state_age_s > MISSION_STALE_S;

    const showEstopOverlay = estopActive || faultActive;
    estopOverlay.hidden = !showEstopOverlay;
    // Не показывать обе плашки разом -- E-Stop важнее, конфликта смыслов нет.
    connLostOverlay.hidden = showEstopOverlay || !connLost;
    setVideoPaused(showEstopOverlay || connLost);

    if (connLost) return; // замереть на последнем слайде (design D4/D5)

    const stateName = latestFrame.state_name;
    if (stateName === "returning") {
      if (!lastWasReturning) {
        showReturningScreen();
        lastWasReturning = true;
        lastSlideExhibitId = null;
        lastSlideChunkIndex = undefined;
      }
      return;
    }
    lastWasReturning = false;

    const exhibitId = latestFrame.exhibit_id || "";
    const chunkIndex = latestFrame.chunk_index;
    if (exhibitId !== lastSlideExhibitId || chunkIndex !== lastSlideChunkIndex) {
      lastSlideExhibitId = exhibitId;
      lastSlideChunkIndex = chunkIndex;
      updateSlide(exhibitId, chunkIndex);
    }

    maybePrefetchNext();
  }

  function connectWs() {
    let reconnectDelay = WS_RECONNECT_MIN_MS;
    const open = () => {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/ws`);
      ws.onopen = () => {
        reconnectDelay = WS_RECONNECT_MIN_MS;
      };
      ws.onmessage = (ev) => {
        try {
          applyFrame(JSON.parse(ev.data));
        } catch (err) {
          console.error("operator_ui: malformed ws frame", err);
        }
      };
      ws.onclose = () => {
        // Пока не переподключились -- считаем, что данных нет (design C4:
        // возраст важнее последнего известного значения).
        latestFrame = null;
        render();
        setTimeout(open, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, WS_RECONNECT_MAX_MS);
      };
      ws.onerror = () => ws.close();
    };
    open();
  }

  btnStart.addEventListener("click", async () => {
    const tourId = tourSelect.value;
    if (!tourId) {
      showMessage("Нет доступных туров");
      return;
    }
    await api("POST", "/api/tour/start", { tour_id: tourId });
  });

  btnStop.addEventListener("click", async () => {
    await api("POST", "/api/tour/stop");
  });

  btnHome.addEventListener("click", async () => {
    await api("POST", "/api/go_home");
  });

  btnReset.addEventListener("click", () => {
    resetConfirmDialog.showModal();
  });
  btnResetCancel.addEventListener("click", () => resetConfirmDialog.close());
  btnResetConfirm.addEventListener("click", async () => {
    resetConfirmDialog.close();
    await api("POST", "/api/localization/reset", { confirm: true });
  });

  // -- долгий тап в углу + PIN, чтобы открыть панель поверх активного тура --
  let pressTimer = null;
  const cancelPress = () => {
    if (pressTimer) clearTimeout(pressTimer);
    pressTimer = null;
  };
  unlockCorner.addEventListener("pointerdown", () => {
    pressTimer = setTimeout(() => {
      pinInput.value = "";
      pinDialog.showModal();
    }, LONG_PRESS_MS);
  });
  unlockCorner.addEventListener("pointerup", cancelPress);
  unlockCorner.addEventListener("pointerleave", cancelPress);
  unlockCorner.addEventListener("pointercancel", cancelPress);

  btnPinCancel.addEventListener("click", () => pinDialog.close());
  btnPinOk.addEventListener("click", () => {
    if (operatorPin !== null && pinInput.value === operatorPin) {
      panelUnlocked = true;
      pinDialog.close();
      render();
    } else {
      pinInput.value = "";
    }
  });

  loadTours();
  connectWs();
  render();
})();
