// Панель оператора: WS-приёмник кадров + командные POST'ы (design C6),
// сессионная аутентификация (design E1/E2) поверх двух независимых слоёв
// экрана. Никаких фреймворков/CDN -- робот работает без интернета.

(() => {
  "use strict";

  const WS_RECONNECT_MIN_MS = 1000;
  const WS_RECONNECT_MAX_MS = 10000;
  const LONG_PRESS_MS = 2000;
  const MISSION_STALE_S = 3.0;
  const DEFAULT_SLIDE_INTERVAL_S = 8.0;
  const DEFAULT_PROMO_INTERVAL_S = 10.0;
  const SESSION_POLL_MS = 5000;
  // Базовые URL медиа (design F2) -- единственное, чем отличается манифест
  // тура от манифеста промо для общего рендерера ниже (startCycle/
  // renderItem/handleBrokenMedia): каждый цикл несёт свой baseUrl.
  const MEDIA_BASE_URL = "/media";
  const PROMO_BASE_URL = "/promo";

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

  // -- слой 1: медиа (design E1, всегда смонтирован) --------------------------
  const idleScreen = document.getElementById("idle-screen");
  const slideLayerA = document.getElementById("slide-layer-a");
  const slideLayerB = document.getElementById("slide-layer-b");
  const titleCard = document.getElementById("title-card");
  const titleCardText = document.getElementById("title-card-text");
  const returningScreen = document.getElementById("returning-screen");
  const unlockCorner = document.getElementById("unlock-corner");
  const prefetchPool = document.getElementById("prefetch-pool");

  // -- слой 2: оверлей панели (design E1, по требованию) -----------------------
  const panelOverlay = document.getElementById("panel-overlay");
  const connBanner = document.getElementById("conn-banner");
  const messageEl = document.getElementById("message");
  const statusState = document.getElementById("status-state");
  const statusStops = document.getElementById("status-stops");
  const statusExhibit = document.getElementById("status-exhibit");
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
  const sessionExpiryEl = document.getElementById("session-expiry");
  const btnHidePanel = document.getElementById("btn-hide-panel");
  const btnLogout = document.getElementById("btn-logout");

  // -- вход оператора (design E2) ----------------------------------------------
  const authDialog = document.getElementById("auth-dialog");
  const pinInput = document.getElementById("pin-input");
  const authMessageEl = document.getElementById("auth-message");
  const btnPinOk = document.getElementById("btn-pin-ok");
  const btnRfidOk = document.getElementById("btn-rfid-ok");
  const btnAuthCancel = document.getElementById("btn-auth-cancel");

  // -- слой 3: плашки (design E1, поверх обоих слоёв) --------------------------
  const estopOverlay = document.getElementById("estop-overlay");
  const connLostOverlay = document.getElementById("conn-lost-overlay");
  const authMockBadge = document.getElementById("auth-mock-badge");

  let latestFrame = null;
  let slideIntervalS = DEFAULT_SLIDE_INTERVAL_S;

  // -- промо-петля (design F2/F3): манифест грузится один раз при
  // старте, promoRunning гейтит startPromoCycle() от повторного вызова
  // на каждый render()-тик, пока мы уже в простое (design F3).
  let promoManifest = null;
  let promoIntervalS = DEFAULT_PROMO_INTERVAL_S;
  let promoRunning = false;

  // -- сессия (design E2): токен -- переменная модульной области, страница
  // киоска не перезагружается, localStorage не нужен и не используется --
  // сессия обязана исчезать при закрытии вкладки, а не переживать её.
  let authToken = null;
  let sessionExpiresAt = null; // unix-секунды, для отображения обратного отсчёта
  let sessionOperator = "";
  let panelOpen = false; // "свёрнуто" -- отдельно от наличия токена (E2, close_session_on_panel_hide)
  let closeSessionOnPanelHide = false;
  let currentNonce = null;
  let availableAuthBackends = [];

  // -- слайды (design D1/D3/D5) ------------------------------------------------
  // lastKnownActive НЕ сбрасывается, когда latestFrame становится null
  // (обрыв WS) -- иначе потеря связи посреди тура откидывала бы экран в
  // заставку простоя вместо плашки "нет связи" поверх слайдов (design D4).
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

  function showAuthMessage(text) {
    if (!text) {
      authMessageEl.hidden = true;
      authMessageEl.textContent = "";
      return;
    }
    authMessageEl.hidden = false;
    authMessageEl.textContent = text;
  }

  // -- HTTP: api() подставляет токен, где он есть; 401 от ЛЮБОЙ команды --
  // это сигнал "сессия истекла/вытеснена", а не ошибка конкретной кнопки
  // (design E2: гейт -- middleware по списку путей, единый для всех).
  async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (authToken !== null) opts.headers.Authorization = `Bearer ${authToken}`;
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
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
    if (resp.status === 401 && authToken !== null) {
      onSessionLost("Сессия истекла или была закрыта");
      return null;
    }
    if (!resp.ok) {
      showMessage(data.message || data.error || `Ошибка ${resp.status}`);
      return null;
    }
    showMessage("");
    return data;
  }

  async function loadTours() {
    const data = await api("GET", "/api/tours");
    if (!data) return;
    if (typeof data.slide_interval_s === "number") slideIntervalS = data.slide_interval_s;
    tourSelect.innerHTML = "";
    for (const tour of data.tours || []) {
      const opt = document.createElement("option");
      opt.value = tour.id;
      opt.textContent = tour.name || tour.id;
      tourSelect.appendChild(opt);
    }
    const auth = data.auth || {};
    closeSessionOnPanelHide = Boolean(auth.close_session_on_panel_hide);
    availableAuthBackends = Array.isArray(auth.backends) ? auth.backends : [];
    btnRfidOk.disabled = !availableAuthBackends.includes("rfid");
    // Несъёмная плашка (design E3): пока auth_backends содержит "mock",
    // видна ВСЕГДА, не только во время попытки входа.
    authMockBadge.hidden = !auth.mock;
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

  // -- слой 2: оверлей панели -- видимость определяется ТОЛЬКО сессией
  // (design E1: не размонтирует и не двигает #media-layer, т.к. position: fixed) --
  function renderOverlay() {
    panelOverlay.hidden = !(panelOpen && authToken !== null);
    if (authToken === null) return;

    if (sessionExpiresAt !== null) {
      const remainingS = Math.max(0, Math.round(sessionExpiresAt - Date.now() / 1000));
      const mm = String(Math.floor(remainingS / 60)).padStart(2, "0");
      const ss = String(remainingS % 60).padStart(2, "0");
      sessionExpiryEl.textContent = `${sessionOperator || "оператор"}: сессия ${mm}:${ss}`;
    }

    const blocked = commandsBlocked();
    connBanner.hidden = !blocked;
    connBanner.textContent = blockReason();
    for (const btn of [btnStart, btnStop, btnHome, btnReset]) {
      btn.disabled = blocked;
    }

    if (latestFrame) {
      statusState.textContent = STATE_NAME_RU[latestFrame.state_name] || latestFrame.state_name;
      statusStops.textContent = latestFrame.stop_total
        ? `Остановка ${latestFrame.stop_index + 1}/${latestFrame.stop_total}`
        : "";
      statusExhibit.textContent = latestFrame.exhibit_id || "";
      statusSupervisor.textContent = latestFrame.supervisor_state || "";
      statusAge.textContent =
        latestFrame.mission_state_age_s === null
          ? "нет данных"
          : `связь ${latestFrame.mission_state_age_s.toFixed(1)} с назад`;
    } else {
      statusState.textContent = "Нет данных";
      statusStops.textContent = "";
      statusExhibit.textContent = "";
      statusSupervisor.textContent = "";
      statusAge.textContent = "";
    }
  }

  function render() {
    if (latestFrame) {
      lastKnownActive = latestFrame.state_name !== "idle" && latestFrame.state_name !== "unknown";
    }
    // latestFrame === null (WS оборвался) -- lastKnownActive НЕ трогаем,
    // иначе обрыв связи посреди тура откинул бы медиа-слой в заставку
    // простоя вместо плашки "нет связи" поверх замерших слайдов (design D4/D5).
    renderOverlay();
    updateMediaLayer();
    updateBadges();
    updateVideoPauseState();
  }

  function applyFrame(frame) {
    latestFrame = frame;
    render();
  }

  // -- слайды: манифест + выбор (design D2/D3) -- зеркалит lib/slide_select.py --

  function itemUrl(baseUrl, item) {
    return `${baseUrl}/${item.path}`;
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
        el.src = itemUrl(MEDIA_BASE_URL, item);
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

  function showIdleScreen() {
    stopCycle();
    returningScreen.hidden = true;
    titleCard.hidden = true;
    slideLayerA.classList.remove("visible");
    slideLayerB.classList.remove("visible");
    idleScreen.hidden = false;
  }

  function showTitleCard(text) {
    stopCycle();
    idleScreen.hidden = true;
    returningScreen.hidden = true;
    titleCardText.textContent = text || "…";
    titleCard.hidden = false;
    slideLayerA.classList.remove("visible");
    slideLayerB.classList.remove("visible");
  }

  function showReturningScreen() {
    stopCycle();
    idleScreen.hidden = true;
    titleCard.hidden = true;
    slideLayerA.classList.remove("visible");
    slideLayerB.classList.remove("visible");
    returningScreen.hidden = false;
  }

  function handleBrokenMedia(item) {
    if (!currentCycle) return;
    const url = itemUrl(currentCycle.baseUrl, item);
    if (!brokenMediaUrls.has(url)) {
      brokenMediaUrls.add(url);
      console.warn("operator_ui: медиа недоступно, пропускаю", url);
    }
    const remaining = currentCycle.items.filter((i) => i !== item);
    if (!remaining.length) {
      // Ничего не осталось -- фолбэк свой на каждый режим (design D1/F3):
      // тур -> титульная карточка, промо -> заставка простоя. Ни то ни
      // другое не чёрный экран.
      currentCycle.onEmpty();
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
    idleScreen.hidden = true;
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
      mediaEl.src = itemUrl(currentCycle.baseUrl, item);
      currentCycle.videoEl = mediaEl;
    } else {
      mediaEl = document.createElement("img");
      mediaEl.addEventListener("error", () => handleBrokenMedia(item));
      mediaEl.src = itemUrl(currentCycle.baseUrl, item);
      currentCycle.videoEl = null;
      // Свой дефолт-интервал на цикл (design F2): у тура -- slideIntervalS,
      // у промо -- promoIntervalS, никогда не смешиваются.
      const intervalS = item.duration_s > 0 ? item.duration_s : currentCycle.defaultIntervalS;
      currentCycle.timer = setTimeout(advanceCycle, intervalS * 1000);
    }

    hiddenLayer.appendChild(mediaEl);
    requestAnimationFrame(() => {
      hiddenLayer.classList.add("visible");
      activeLayer.classList.remove("visible");
      swapLayers();
    });
  }

  // Общий рендерер тура (D3) и промо (F2) -- baseUrl различает URL медиа,
  // defaultIntervalS -- дефолт для изображений без своего duration_s,
  // onEmpty -- что показать, если ото всех items ничего не осталось
  // (design F2, критерий 11: один рендерер, не два).
  function startCycle(items, baseUrl, defaultIntervalS, onEmpty) {
    stopCycle();
    currentCycle = {
      items,
      baseUrl,
      defaultIntervalS,
      onEmpty,
      index: 0,
      timer: null,
      videoEl: null,
    };
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
      (item) => !brokenMediaUrls.has(itemUrl(MEDIA_BASE_URL, item))
    );
    if (!items.length) {
      showTitleCard(lastManifestTitle);
      return;
    }
    startCycle(items, MEDIA_BASE_URL, slideIntervalS, () => showTitleCard(lastManifestTitle));
  }

  // -- промо-петля (design F2/F3) ----------------------------------------------

  async function loadPromo() {
    const data = await api("GET", "/api/promo");
    promoManifest = data && Array.isArray(data.items) ? data : { items: [] };
    if (typeof promoManifest.promo_interval_s === "number") {
      promoIntervalS = promoManifest.promo_interval_s;
    }
    // Холодный старт: первый render() (из самого низа файла) мог уже
    // пройти по пустому promoManifest и защёлкнуть promoRunning=true на
    // статичной заставке -- сбросить, чтобы этот render() пересобрал
    // промо-цикл заново, уже с полным манифестом, а не молчал из-за
    // гейта "уже запущено".
    promoRunning = false;
    render();
  }

  function startPromoCycle() {
    const items = (promoManifest && promoManifest.items ? promoManifest.items : []).filter(
      (item) => !brokenMediaUrls.has(itemUrl(PROMO_BASE_URL, item))
    );
    if (!items.length) {
      // Манифеста нет/пуст/все элементы битые -- статичная заставка из
      // web/, не чёрный экран (design F3, критерии 5/6).
      showIdleScreen();
      return;
    }
    startCycle(items, PROMO_BASE_URL, promoIntervalS, showIdleScreen);
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

  // -- слой 1: медиа -- всегда смонтирован, содержимое переключается по
  // состоянию тура (design E1). Открытие/закрытие оверлея панели сюда не
  // заглядывает вообще -- паузу/снятие с паузы по панели считает
  // updateVideoPauseState(), не этот код. --
  function updateMediaLayer() {
    const isActive = lastKnownActive;

    if (!isActive) {
      lastWasReturning = false;
      if (lastSlideExhibitId !== null) {
        lastSlideExhibitId = null;
        lastSlideChunkIndex = undefined;
      }
      // Промо -- НЕ на каждый тик: перезапускало бы видео с нуля при
      // каждом WS-кадре, пока мы в простое (design F3). Стартует один
      // раз при входе в простое; "с начала списка" при возврате из тура
      // (критерий 3) выходит бесплатно -- каждый вход сюда заново ставит
      // promoRunning=false->true и зовёт startCycle() с index:0.
      if (!promoRunning) {
        promoRunning = true;
        startPromoCycle();
      }
      return;
    }
    promoRunning = false;

    const connLost =
      !latestFrame ||
      latestFrame.mission_state_age_s === null ||
      latestFrame.mission_state_age_s > MISSION_STALE_S;

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

  // -- слой 3: плашки -- поверх ОБОИХ слоёв, независимо от панели (design E1) --
  function updateBadges() {
    const estopActive = Boolean(latestFrame && latestFrame.estop);
    const faultActive = Boolean(
      latestFrame &&
        (latestFrame.supervisor_state === "FAULT" || latestFrame.supervisor_state === "SHUTDOWN")
    );
    const showEstopOverlay = estopActive || faultActive;
    estopOverlay.hidden = !showEstopOverlay;

    // НЕ гейтится lastKnownActive (в отличие от старой двухрежимной
    // модели) -- промо в простое тоже должно показывать эту плашку
    // поверх себя при потере /mission/state, просто не замирать
    // (design F4, критерий 9).
    const connLost =
      !latestFrame ||
      latestFrame.mission_state_age_s === null ||
      latestFrame.mission_state_age_s > MISSION_STALE_S;
    // Не показывать обе плашки разом -- E-Stop важнее, конфликта смыслов нет.
    connLostOverlay.hidden = showEstopOverlay || !connLost;
  }

  // -- пауза видео -- ОДИН источник истины (design F4): звать
  // setVideoPaused() из двух мест по очереди рискованно, последний
  // вызов в render() побеждал бы и тихо перезаписывал состояние, заданное
  // первым -- реальный риск, вскрывшийся при добавлении паузы по
  // открытию панели (её не было вовсе до Task F). Вызывается один раз,
  // последней, из render().
  function updateVideoPauseState() {
    const overlayOpen = panelOpen && authToken !== null;
    let pauseForState = false;
    if (lastKnownActive) {
      // estop/fault/потеря связи -- пауза ТОЛЬКО для тура (design D4).
      // Промо не замирает от потери /mission/state (design F4, критерий 9).
      const connLost =
        !latestFrame ||
        latestFrame.mission_state_age_s === null ||
        latestFrame.mission_state_age_s > MISSION_STALE_S;
      const estopActive = Boolean(latestFrame && latestFrame.estop);
      const faultActive = Boolean(
        latestFrame &&
          (latestFrame.supervisor_state === "FAULT" || latestFrame.supervisor_state === "SHUTDOWN")
      );
      pauseForState = estopActive || faultActive || connLost;
    }
    setVideoPaused(overlayOpen || pauseForState);
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

  // -- сессия оператора (design E2) --------------------------------------------

  function onSessionLost(reason) {
    authToken = null;
    sessionExpiresAt = null;
    sessionOperator = "";
    panelOpen = false;
    if (!authDialog.open) showMessage(reason);
    renderOverlay();
  }

  btnHidePanel.addEventListener("click", async () => {
    panelOpen = false;
    // design E2, close_session_on_panel_hide (дефолт false): оператор
    // сворачивает панель посмотреть на слайд и по умолчанию НЕ должен
    // логиниться заново -- сессия переживает сворачивание, если параметр
    // явно не требует иного.
    if (closeSessionOnPanelHide && authToken !== null) {
      await api("POST", "/api/auth/logout");
      authToken = null;
      sessionExpiresAt = null;
      sessionOperator = "";
    }
    renderOverlay();
  });

  btnLogout.addEventListener("click", async () => {
    await api("POST", "/api/auth/logout");
    authToken = null;
    sessionExpiresAt = null;
    sessionOperator = "";
    panelOpen = false;
    renderOverlay();
  });

  // -- долгий тап в углу открывает ЭКРАН ВХОДА, а не панель напрямую
  // (design E1) -- если сессия уже валидна (панель была свёрнута), просто
  // разворачивает её обратно без повторного PIN/карты. --
  let pressTimer = null;
  const cancelPress = () => {
    if (pressTimer) clearTimeout(pressTimer);
    pressTimer = null;
  };
  unlockCorner.addEventListener("pointerdown", () => {
    pressTimer = setTimeout(async () => {
      if (authToken !== null) {
        panelOpen = true;
        renderOverlay();
        return;
      }
      pinInput.value = "";
      showAuthMessage("");
      const data = await api("POST", "/api/auth/challenge");
      if (!data) return;
      currentNonce = data.nonce;
      btnRfidOk.disabled = !((data.backends || []).includes("rfid"));
      authDialog.showModal();
      pinInput.focus();
    }, LONG_PRESS_MS);
  });
  unlockCorner.addEventListener("pointerup", cancelPress);
  unlockCorner.addEventListener("pointerleave", cancelPress);
  unlockCorner.addEventListener("pointercancel", cancelPress);

  async function verifyAndOpen(backend, extra) {
    if (!currentNonce) return;
    const resp = await fetch("/api/auth/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nonce: currentNonce, backend, ...extra }),
    });
    currentNonce = null; // одноразовый -- сервер уже погасил его этим вызовом
    let data = {};
    try {
      data = await resp.json();
    } catch {
      /* тело могло быть пустым */
    }
    if (!resp.ok) {
      showAuthMessage(data.error === "locked_out" ? "Слишком много попыток, подождите" : "Неверно");
      pinInput.value = "";
      return;
    }
    authToken = data.token;
    sessionExpiresAt = data.expires_at;
    sessionOperator = data.operator || "";
    panelOpen = true;
    authDialog.close();
    render();
  }

  btnAuthCancel.addEventListener("click", () => {
    authDialog.close();
  });
  btnPinOk.addEventListener("click", () => verifyAndOpen("pin", { pin: pinInput.value }));
  btnRfidOk.addEventListener("click", () => verifyAndOpen("rfid", {}));

  // Сервер -- единственный источник истины по истечении сессии (design
  // E2): без push-канала для auth клиент обязан сам спрашивать, иначе
  // истечение TTL без активности оператора (панель просто открыта и
  // забыта) никогда не закроет панель (criterion 13).
  setInterval(async () => {
    if (authToken === null) return;
    const data = await api("GET", "/api/auth/status");
    if (data && !data.active) onSessionLost("Сессия истекла");
  }, SESSION_POLL_MS);

  loadTours();
  loadPromo();
  connectWs();
  render();
})();
