// Экран робота (guide-launcher): показ (слайды экспоната / промо), публичная
// кнопка старта тура и меню оператора. Всё ходит на один origin: своё API
// launcher'а (/api/*, /promo/*) и прокси к мосту ROS (/ros/*). Никаких
// фреймворков/CDN -- робот работает без интернета.

(() => {
  "use strict";

  const WS_RECONNECT_MIN_MS = 1000;
  const WS_RECONNECT_MAX_MS = 10000;
  const LONG_PRESS_MS = 2000;
  const MISSION_STALE_S = 3.0;
  const DEFAULT_SLIDE_INTERVAL_S = 8.0;
  const DEFAULT_PROMO_INTERVAL_S = 10.0;
  const POLL_MS = 2000;
  const SESSION_POLL_MS = 5000;
  const START_DIALOG_TIMEOUT_MS = 15000;
  // Страховка для промо из одного зацикленного видео: оборота цикла у него
  // нет, манифест иначе не перечитывался бы никогда.
  const PROMO_REFETCH_MAX_MS = 60000;
  const FETCH_TIMEOUT_MS = 4000;
  // Команды ждут сервисы ROS (сброс локализации: пауза + два вызова).
  const COMMAND_TIMEOUT_MS = 20000;
  // Медиа экспонатов идут через мост (нужны только во время тура), промо --
  // с хоста, поэтому крутится и при мёртвом ROS.
  const MEDIA_BASE_URL = "/ros/media";
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

  const STACK_STATE_RU = {
    UNKNOWN: "нет данных",
    DOWN: "не запущен",
    STARTING: "запускается",
    UP: "работает",
    DEGRADED: "частично",
    FAILED: "ошибка запуска",
  };

  const PUBLIC_REFUSAL_RU = {
    rate_limited: "Подождите несколько секунд и повторите",
    ros_down: "Робот пока не готов",
    no_mission_fsm: "Нет связи с роботом",
    estop: "Аварийная остановка",
    tour_active: "Экскурсия уже идёт",
    unknown_tour: "Этот тур сейчас недоступен",
  };

  const COMMAND_ERROR_RU = {
    robot_moving: "Сначала нажмите «Стоп»",
    ros_down: "ROS не запущен",
    starting: "Запуск уже идёт",
    stack_control_disabled: "Управление стеком выключено",
    mission_state_stale: "Нет свежих данных mission_fsm",
    tour_active: "Экскурсия уже идёт",
    home_location_missing: "В карте не задана точка home",
    rejected: "mission_fsm отклонил запрос",
    service_unavailable: "Сервис ROS не отвечает",
    forbidden: "Мост ROS отклонил запрос (токен)",
    auth_required: "Нужно войти заново",
  };

  // -- слой 1: экран показа (всегда смонтирован) -------------------------------
  const idleScreen = document.getElementById("idle-screen");
  const slideLayerA = document.getElementById("slide-layer-a");
  const slideLayerB = document.getElementById("slide-layer-b");
  const titleCard = document.getElementById("title-card");
  const titleCardText = document.getElementById("title-card-text");
  const returningScreen = document.getElementById("returning-screen");
  const unlockCorner = document.getElementById("unlock-corner");
  const prefetchPool = document.getElementById("prefetch-pool");
  const startTourBtn = document.getElementById("start-tour-btn");

  // -- публичный старт ---------------------------------------------------------
  const startDialog = document.getElementById("start-dialog");
  const startText = document.getElementById("start-text");
  const startMessage = document.getElementById("start-message");
  const btnStartOk = document.getElementById("btn-start-ok");
  const btnStartCancel = document.getElementById("btn-start-cancel");

  // -- слой 2: меню оператора --------------------------------------------------
  const menuOverlay = document.getElementById("menu-overlay");
  const connBanner = document.getElementById("conn-banner");
  const messageEl = document.getElementById("message");
  const statusState = document.getElementById("status-state");
  const statusStops = document.getElementById("status-stops");
  const statusExhibit = document.getElementById("status-exhibit");
  const statusSupervisor = document.getElementById("status-supervisor");
  const statusAge = document.getElementById("status-age");
  const tabButtons = Array.from(document.querySelectorAll("#menu-tabs .tab"));
  const tabRos = document.getElementById("tab-ros");
  const sections = {
    tour: document.getElementById("section-tour"),
    robot: document.getElementById("section-robot"),
    ros: document.getElementById("section-ros"),
  };
  const tourListEl = document.getElementById("tour-list");
  const tourEmptyEl = document.getElementById("tour-empty");
  const btnStart = document.getElementById("btn-start");
  const btnStop = document.getElementById("btn-stop");
  const btnHome = document.getElementById("btn-home");
  const btnReset = document.getElementById("btn-reset");
  const btnCostmaps = document.getElementById("btn-costmaps");
  const robotSupervisor = document.getElementById("robot-supervisor");
  const robotEstop = document.getElementById("robot-estop");
  const rosStateEl = document.getElementById("ros-state");
  const checkEls = {
    container: document.getElementById("check-container"),
    launch: document.getElementById("check-launch"),
    bridge: document.getElementById("check-bridge"),
  };
  const rosErrorEl = document.getElementById("ros-error");
  const btnStackStart = document.getElementById("btn-stack-start");
  const btnStackRestart = document.getElementById("btn-stack-restart");
  const stackLogEl = document.getElementById("stack-log");
  const sessionExpiryEl = document.getElementById("session-expiry");
  const btnCloseMenu = document.getElementById("btn-close-menu");
  const btnLogout = document.getElementById("btn-logout");
  const confirmDialog = document.getElementById("confirm-dialog");
  const confirmText = document.getElementById("confirm-text");
  const btnConfirmOk = document.getElementById("btn-confirm-ok");
  const btnConfirmCancel = document.getElementById("btn-confirm-cancel");

  // -- вход оператора ----------------------------------------------------------
  const authDialog = document.getElementById("auth-dialog");
  const pinInput = document.getElementById("pin-input");
  const authMessageEl = document.getElementById("auth-message");
  const btnPinOk = document.getElementById("btn-pin-ok");
  const btnRfidOk = document.getElementById("btn-rfid-ok");
  const btnAuthCancel = document.getElementById("btn-auth-cancel");
  const pinKeypad = document.getElementById("pin-keypad");
  const btnPinBackspace = document.getElementById("btn-pin-backspace");
  const PIN_MAX_LEN = 20; // не защита, просто не даём полю расти бесконечно

  // -- слой 3: плашки ----------------------------------------------------------
  const estopOverlay = document.getElementById("estop-overlay");
  const connLostOverlay = document.getElementById("conn-lost-overlay");

  // -- настройки launcher'а (/api/config) --------------------------------------
  let alwaysPromo = true;
  let stackControl = true;
  let slideIntervalS = DEFAULT_SLIDE_INTERVAL_S;
  let launcherOk = false; // предыдущий опрос дошёл до launcher'а

  // -- состояние стека ROS (/api/stack/status) ---------------------------------
  let stackState = "UNKNOWN";
  let stackChecks = { container: false, launch: false, bridge: false };
  let stackLastError = "";
  let currentTourId = "";
  let tours = []; // [{id, name}] из /ros/api/tours, только пока стек UP
  let toursLoaded = false;
  let toursLoading = false;

  let latestFrame = null;

  // -- промо-петля: манифест перечитывается на каждом обороте цикла; цикл
  // перезапускается, только если сменился rev.
  let promoManifest = null;
  let promoRev = null;
  let promoIntervalS = DEFAULT_PROMO_INTERVAL_S;
  let promoRunning = false;
  let promoFetchedAt = 0;
  let promoFetching = false;

  // -- сессия: cookie HttpOnly, страница о токене ничего не знает --------------
  let authed = false;
  let sessionExpiresAt = null; // unix-секунды, для обратного отсчёта
  let sessionOperator = "";
  let menuOpen = false;
  let menuSection = "tour";
  let currentNonce = null;
  let tourListKey = "";
  let menuMessageTimer = null;

  // -- слайды ------------------------------------------------------------------
  // lastKnownActive НЕ сбрасывается, когда latestFrame становится null
  // (обрыв WS при живом стеке) -- иначе потеря связи посреди тура откидывала бы
  // экран в заставку вместо плашки "нет связи" поверх слайдов. Сбрасывается
  // только когда стек перестал быть UP: тогда крутится промо.
  let lastKnownActive = false;
  let lastWasReturning = false;
  // exhibit_id/chunk_index последнего ПРИМЕНЁННОГО слайда -- отдельно от
  // latestFrame: реконнект не должен перезапускать текущий слайд.
  let lastSlideExhibitId = null;
  let lastSlideChunkIndex;
  let lastManifestTitle = "";
  let lastPrefetchedExhibitId = null;
  // exhibit_id -> манифест ({title, chunk_ids, items}) либо Promise на него,
  // пока грузится -- нет параллельных повторных запросов одного экспоната.
  const manifestCache = new Map();
  const brokenMediaUrls = new Set();
  let currentCycle = null; // {items, index, timer, videoEl, ...}
  let activeLayer = slideLayerA;
  let hiddenLayer = slideLayerB;

  // -- HTTP --------------------------------------------------------------------

  // request() никогда не бросает: {ok, status, data}; сеть упала -- status 0.
  async function request(method, path, body) {
    const opts = { method, headers: {}, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const controller = new AbortController();
    opts.signal = controller.signal;
    const abortTimer = setTimeout(
      () => controller.abort(),
      method === "GET" ? FETCH_TIMEOUT_MS : COMMAND_TIMEOUT_MS
    );
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch (err) {
      console.debug(`launcher: ${method} ${path} -- fetch упал`, err);
      return { ok: false, status: 0, data: {} };
    } finally {
      clearTimeout(abortTimer);
    }
    let data = {};
    try {
      data = await resp.json();
    } catch {
      /* тело могло быть пустым */
    }
    if (resp.status === 401 && authed) {
      console.warn(`launcher: ${method} ${path} -- 401, сессия сброшена`);
      onSessionLost();
    }
    return { ok: resp.ok, status: resp.status, data };
  }

  function errorText(res) {
    if (res.status === 0) return "Нет связи с launcher";
    const data = res.data || {};
    return (
      data.message ||
      COMMAND_ERROR_RU[data.error] ||
      data.error ||
      `Ошибка ${res.status}`
    );
  }

  function showMenuMessage(text, kind) {
    clearTimeout(menuMessageTimer);
    if (!text) {
      messageEl.hidden = true;
      messageEl.textContent = "";
      return;
    }
    messageEl.hidden = false;
    messageEl.textContent = text;
    messageEl.className = kind === "ok" ? "message-ok" : "";
    if (kind === "ok") {
      menuMessageTimer = setTimeout(() => showMenuMessage(""), 3000);
    }
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

  // Команда из меню: ошибка остаётся в меню, успех -- короткое «Готово».
  async function menuCommand(path, body, okText) {
    const res = await request("POST", path, body);
    if (res.ok) {
      showMenuMessage(okText || "Готово", "ok");
    } else if (res.status !== 401) {
      showMenuMessage(errorText(res));
    }
    return res;
  }

  // -- конфиг launcher'а ---------------------------------------------------------
  async function loadConfig() {
    const res = await request("GET", "/api/config");
    if (!res.ok) return false;
    const cfg = res.data;
    const wasAlwaysPromo = alwaysPromo;
    if (typeof cfg.always_promo === "boolean") alwaysPromo = cfg.always_promo;
    if (typeof cfg.stack_control === "boolean") stackControl = cfg.stack_control;
    if (typeof cfg.slide_interval_s === "number") slideIntervalS = cfg.slide_interval_s;
    if (typeof cfg.promo_interval_s === "number" && promoManifest === null) {
      promoIntervalS = cfg.promo_interval_s;
    }
    // always_promo сменился -- пересобрать режим показа; иначе промо не трогаем.
    if (alwaysPromo !== wasAlwaysPromo) promoRunning = false;
    render();
    return true;
  }

  // -- опрос launcher'а: состояние стека и текущий тур -------------------------
  function applyStack(data) {
    const prev = stackState;
    stackState = typeof data.state === "string" ? data.state : "UNKNOWN";
    stackChecks = data.checks || stackChecks;
    stackLastError = data.last_error || "";
    if (typeof data.control === "boolean") stackControl = data.control;
    if (prev !== "UP" && stackState === "UP") onStackUp();
    if (prev === "UP" && stackState !== "UP") onStackDown();
  }

  function onStackUp() {
    manifestCache.clear();
    lastPrefetchedExhibitId = null;
    toursLoaded = false;
    connectWs();
  }

  function onStackDown() {
    disconnectWs();
    latestFrame = null;
    lastKnownActive = false;
    lastWasReturning = false;
    tours = [];
    toursLoaded = false;
    // Идущий слайд закрывает промо: при мёртвом ROS показ просто продолжается.
    lastSlideExhibitId = null;
    lastSlideChunkIndex = undefined;
    promoRunning = false;
    if (startDialog.open) startDialog.close();
  }

  async function loadTours() {
    if (toursLoading) return;
    toursLoading = true;
    try {
      const res = await request("GET", "/ros/api/tours");
      if (res.ok && Array.isArray(res.data.tours)) {
        tours = res.data.tours;
        toursLoaded = true;
        render();
      }
    } finally {
      toursLoading = false;
    }
  }

  async function pollStack() {
    const [st, cur] = await Promise.all([
      request("GET", "/api/stack/status"),
      request("GET", "/api/tour/current"),
    ]);
    if (st.ok) {
      const wasOk = launcherOk;
      launcherOk = true;
      applyStack(st.data);
      if (!wasOk) await loadConfig();
    } else {
      launcherOk = false;
    }
    if (cur.ok && typeof cur.data.tour_id === "string") {
      currentTourId = cur.data.tour_id;
    }
    if (stackState === "UP" && !toursLoaded) loadTours();
    if (promoManifest === null) refreshPromo();
    else if (promoRunning && Date.now() - promoFetchedAt > PROMO_REFETCH_MAX_MS) refreshPromo();
    if (authed && menuOpen && menuSection === "ros" && stackControl) refreshStackLog();
    render();
  }

  async function pollLoop() {
    try {
      await pollStack();
    } catch (err) {
      console.error("launcher: сбой опроса", err);
    }
    setTimeout(pollLoop, POLL_MS);
  }

  async function refreshStackLog() {
    const res = await request("GET", "/api/stack/log");
    if (!res.ok || !Array.isArray(res.data.lines)) return;
    const atBottom =
      stackLogEl.scrollTop + stackLogEl.clientHeight >= stackLogEl.scrollHeight - 8;
    stackLogEl.textContent = res.data.lines.join("\n");
    if (atBottom) stackLogEl.scrollTop = stackLogEl.scrollHeight;
  }

  // -- кадры состояния миссии (/ros/ws) -- только пока стек UP -----------------
  let ws = null;
  let wsTimer = null;
  let wsDelay = WS_RECONNECT_MIN_MS;

  function disconnectWs() {
    clearTimeout(wsTimer);
    wsTimer = null;
    if (ws) {
      const sock = ws;
      ws = null;
      sock.close();
    }
  }

  function connectWs() {
    disconnectWs();
    wsDelay = WS_RECONNECT_MIN_MS;
    openWs();
  }

  function openWs() {
    if (stackState !== "UP") return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const sock = new WebSocket(`${proto}//${location.host}/ros/ws`);
    ws = sock;
    sock.onopen = () => {
      wsDelay = WS_RECONNECT_MIN_MS;
    };
    sock.onmessage = (ev) => {
      if (ws !== sock) return;
      try {
        applyFrame(JSON.parse(ev.data));
      } catch (err) {
        console.error("launcher: malformed ws frame", err);
      }
    };
    sock.onclose = () => {
      if (ws !== sock) return; // закрыли сами (стек перестал быть UP)
      ws = null;
      // Пока не переподключились -- данных нет (возраст важнее последнего значения).
      latestFrame = null;
      render();
      wsTimer = setTimeout(openWs, wsDelay);
      wsDelay = Math.min(wsDelay * 2, WS_RECONNECT_MAX_MS);
    };
    sock.onerror = () => sock.close();
  }

  // -- условия для команд и публичного старта ----------------------------------

  function frameFresh() {
    return Boolean(
      latestFrame &&
        latestFrame.mission_state_age_s !== null &&
        latestFrame.mission_state_age_s <= MISSION_STALE_S
    );
  }

  function estopOrFault() {
    return Boolean(
      latestFrame &&
        (latestFrame.estop ||
          latestFrame.supervisor_state === "FAULT" ||
          latestFrame.supervisor_state === "SHUTDOWN")
    );
  }

  function commandsBlocked() {
    return stackState !== "UP" || !frameFresh() || estopOrFault();
  }

  function blockReason() {
    if (stackState !== "UP") return "ROS не запущен";
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

  function findTour(id) {
    for (const tour of tours) {
      if (tour.id === id) return tour;
    }
    return null;
  }

  // Те же условия, что проверяет сервер в /api/public/start_tour.
  function publicStartAllowed() {
    return (
      stackState === "UP" &&
      frameFresh() &&
      !estopOrFault() &&
      latestFrame.state_name === "idle" &&
      currentTourId !== "" &&
      findTour(currentTourId) !== null
    );
  }

  // -- публичная кнопка «Начать экскурсию» -------------------------------------
  let startTimer = null;

  function updateStartButton() {
    const allowed = publicStartAllowed();
    startTourBtn.hidden = !allowed;
    if (!allowed && startDialog.open) startDialog.close();
  }

  function armStartTimeout() {
    clearTimeout(startTimer);
    startTimer = setTimeout(() => {
      if (startDialog.open) startDialog.close();
    }, START_DIALOG_TIMEOUT_MS);
  }

  function showStartMessage(text) {
    startMessage.hidden = !text;
    startMessage.textContent = text || "";
  }

  startTourBtn.addEventListener("click", () => {
    const tour = findTour(currentTourId);
    if (!tour) return;
    startText.textContent = `Начать экскурсию «${tour.name || tour.id}»?`;
    showStartMessage("");
    btnStartOk.disabled = false;
    startDialog.showModal();
    armStartTimeout();
  });

  btnStartCancel.addEventListener("click", () => startDialog.close());
  // close приходит асинхронно: событие от прошлого закрытия не должно гасить
  // таймер уже заново открытого диалога.
  startDialog.addEventListener("close", () => {
    if (!startDialog.open) clearTimeout(startTimer);
  });

  btnStartOk.addEventListener("click", async () => {
    armStartTimeout();
    btnStartOk.disabled = true;
    showStartMessage("");
    // Тело пустое: сервер сам берёт ТЕКУЩИЙ тур, tour_id от клиента игнорируется.
    const res = await request("POST", "/api/public/start_tour");
    btnStartOk.disabled = false;
    if (res.ok) {
      startDialog.close();
      return;
    }
    const reason = res.data && res.data.reason;
    showStartMessage(
      res.status === 0
        ? "Нет связи с роботом"
        : PUBLIC_REFUSAL_RU[reason] || "Не удалось начать экскурсию"
    );
  });

  // -- меню: отрисовка ---------------------------------------------------------

  function renderStatusHeader() {
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
      statusState.textContent = stackState === "UP" ? "Нет данных" : "ROS не запущен";
      statusStops.textContent = "";
      statusExhibit.textContent = "";
      statusSupervisor.textContent = "";
      statusAge.textContent = "";
    }
  }

  function renderSessionExpiry() {
    if (!authed || sessionExpiresAt === null) {
      sessionExpiryEl.textContent = "";
      return;
    }
    const remainingS = Math.max(0, Math.round(sessionExpiresAt - Date.now() / 1000));
    const mm = String(Math.floor(remainingS / 60)).padStart(2, "0");
    const ss = String(remainingS % 60).padStart(2, "0");
    sessionExpiryEl.textContent = `${sessionOperator || "оператор"}: сессия ${mm}:${ss}`;
  }

  // Список туров пересобирается только при смене данных: кадры /ws идут часто,
  // а замена кнопки между pointerdown и click теряла бы нажатие.
  function renderTourList() {
    const rosUp = stackState === "UP";
    const key = JSON.stringify([rosUp, currentTourId, tours]);
    if (key === tourListKey) return;
    tourListKey = key;
    tourListEl.textContent = "";
    for (const tour of tours) {
      const row = document.createElement("div");
      row.className = "tour-row" + (tour.id === currentTourId ? " current" : "");
      const name = document.createElement("span");
      name.className = "tour-name";
      name.textContent = tour.name || tour.id;
      row.appendChild(name);
      if (tour.id === currentTourId) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "текущий";
        row.appendChild(badge);
      } else {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn";
        btn.textContent = "Сделать текущим";
        btn.disabled = !rosUp;
        btn.addEventListener("click", () => setCurrentTour(tour.id));
        row.appendChild(btn);
      }
      tourListEl.appendChild(row);
    }
    if (!tours.length) {
      tourEmptyEl.hidden = false;
      tourEmptyEl.textContent = currentTourId ? `Текущий тур: ${currentTourId}` : "";
    } else {
      tourEmptyEl.hidden = true;
    }
  }

  function renderMenu() {
    menuOverlay.hidden = !(menuOpen && authed);
    if (!authed || !menuOpen) return;

    tabRos.hidden = !stackControl;
    if (!stackControl && menuSection === "ros") menuSection = "tour";
    for (const btn of tabButtons) {
      btn.classList.toggle("active", btn.dataset.section === menuSection);
    }
    for (const name of Object.keys(sections)) {
      sections[name].hidden = name !== menuSection;
    }

    const rosUp = stackState === "UP";
    const blocked = commandsBlocked();
    connBanner.hidden = !rosUp || !blocked;
    connBanner.textContent = blockReason();

    for (const note of document.querySelectorAll(".ros-note")) note.hidden = rosUp;
    for (const btn of [btnStart, btnStop, btnHome]) btn.disabled = blocked;
    for (const btn of [btnReset, btnCostmaps]) btn.disabled = !rosUp;

    const current = findTour(currentTourId);
    btnStart.textContent = current ? `Старт: ${current.name || current.id}` : "Старт";

    renderStatusHeader();
    renderSessionExpiry();
    renderTourList();

    robotSupervisor.textContent = latestFrame ? latestFrame.supervisor_state || "--" : "--";
    robotEstop.textContent = latestFrame ? (latestFrame.estop ? "АКТИВЕН" : "нет") : "--";
    robotEstop.className = latestFrame && latestFrame.estop ? "bad" : "";

    rosStateEl.textContent = STACK_STATE_RU[stackState] || stackState;
    rosStateEl.className =
      "badge" + (stackState === "UP" ? " badge-ok" : stackState === "STARTING" ? " badge-warn" : " badge-danger");
    for (const name of Object.keys(checkEls)) {
      const ok = Boolean(stackChecks[name]);
      checkEls[name].textContent = ok ? "✓" : "✗";
      checkEls[name].className = ok ? "ok" : "bad";
    }
    rosErrorEl.hidden = !stackLastError;
    rosErrorEl.textContent = stackLastError;
    const busy = stackState === "STARTING";
    btnStackStart.disabled = busy || stackState === "UP";
    btnStackRestart.disabled = busy;
  }

  function render() {
    if (latestFrame) {
      lastKnownActive = latestFrame.state_name !== "idle" && latestFrame.state_name !== "unknown";
    }
    // latestFrame === null (WS оборвался) -- lastKnownActive НЕ трогаем,
    // иначе обрыв связи посреди тура откинул бы экран в заставку простоя вместо
    // плашки "нет связи" поверх замерших слайдов.
    renderMenu();
    updateStartButton();
    updateMediaLayer();
    updateBadges();
    updateVideoPauseState();
  }

  function applyFrame(frame) {
    latestFrame = frame;
    render();
  }

  // -- слайды: манифест + выбор ---------------------------------------------------

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
      const empty = { title: "", chunk_ids: [], items: [] };
      const res = await request("GET", `/ros/api/media/${encodeURIComponent(exhibitId)}`);
      if (!res.ok) return { manifest: empty, failed: true };
      return { manifest: res.data, failed: false };
    })();
    manifestCache.set(exhibitId, promise.then((r) => r.manifest));
    const { manifest, failed } = await promise;
    // Неудача не кэшируется: мост мог быть недоступен, а потом подняться.
    if (failed) manifestCache.delete(exhibitId);
    else manifestCache.set(exhibitId, manifest);
    return manifest;
  }

  function maybePrefetchNext() {
    const nextId = latestFrame && latestFrame.next_exhibit_id;
    if (!nextId || nextId === lastPrefetchedExhibitId) return;
    lastPrefetchedExhibitId = nextId;
    // Тот же вызов наполняет кэш узла и даёт браузеру hint для префетча файлов.
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

  // -- показ: цикл по выбранным элементам, кроссфейд, video/image ---------------

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
      console.warn("launcher: медиа недоступно, пропускаю", url);
    }
    const remaining = currentCycle.items.filter((i) => i !== item);
    if (!remaining.length) {
      // Ничего не осталось -- фолбэк свой на каждый режим: тур -> титульная
      // карточка, промо -> заставка простоя. Ни то ни другое не чёрный экран.
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
    const wrapped = currentCycle.index === 0;
    const onWrap = currentCycle.onWrap;
    playCycleItem();
    if (wrapped && onWrap) onWrap();
  }

  function renderItem(item) {
    idleScreen.hidden = true;
    titleCard.hidden = true;
    returningScreen.hidden = true;

    hiddenLayer.innerHTML = "";
    let mediaEl;

    if (item.kind === "video") {
      mediaEl = document.createElement("video");
      // muted ДО autoplay -- иначе браузер блокирует автозапуск. Звук только у
      // TTS, дорожка видео не звучит никогда.
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
      // Свой дефолт-интервал на цикл: у тура -- slideIntervalS, у промо --
      // promoIntervalS, никогда не смешиваются.
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

  // Общий рендерер тура и промо -- baseUrl различает URL медиа, defaultIntervalS
  // -- дефолт для изображений без своего duration_s, onEmpty -- что показать,
  // если ни одного элемента не осталось, onWrap -- вызывается на каждом обороте.
  function startCycle(items, baseUrl, defaultIntervalS, onEmpty, onWrap) {
    stopCycle();
    currentCycle = {
      items,
      baseUrl,
      defaultIntervalS,
      onEmpty,
      onWrap: onWrap || null,
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

  // -- промо-петля -------------------------------------------------------------

  function applyPromoManifest(data) {
    promoManifest = data && Array.isArray(data.items) ? data : { items: [] };
    promoRev = data && typeof data.rev === "string" ? data.rev : null;
    if (typeof promoManifest.promo_interval_s === "number") {
      promoIntervalS = promoManifest.promo_interval_s;
    }
  }

  // Перечитывает манифест; цикл перезапускается, только если сменился rev.
  // Сбой запроса оставляет прежние элементы -- промо не должно пропадать.
  async function refreshPromo() {
    if (promoFetching) return;
    promoFetching = true;
    try {
      const res = await request("GET", "/api/promo");
      promoFetchedAt = Date.now();
      if (!res.ok || !Array.isArray(res.data.items)) return;
      if (promoManifest !== null && res.data.rev === promoRev) return;
      for (const url of Array.from(brokenMediaUrls)) {
        if (url.startsWith(`${PROMO_BASE_URL}/`)) brokenMediaUrls.delete(url);
      }
      applyPromoManifest(res.data);
      // Холодный старт: первый render() мог уже защёлкнуть promoRunning=true на
      // статичной заставке с пустым манифестом -- сбросить, чтобы render()
      // пересобрал промо-цикл с полным манифестом.
      promoRunning = false;
      render();
    } finally {
      promoFetching = false;
    }
  }

  function startPromoCycle() {
    const items = (promoManifest && promoManifest.items ? promoManifest.items : []).filter(
      (item) => !brokenMediaUrls.has(itemUrl(PROMO_BASE_URL, item))
    );
    if (!items.length) {
      // Манифеста нет/пуст/все элементы битые -- статичная заставка, не чёрный экран.
      showIdleScreen();
      return;
    }
    startCycle(items, PROMO_BASE_URL, promoIntervalS, showIdleScreen, refreshPromo);
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

  // -- слой 1: экран показа -- всегда смонтирован, содержимое переключается по
  // состоянию тура. Открытие/закрытие меню сюда не заглядывает вообще -- паузу
  // по меню считает updateVideoPauseState(). --
  function updateMediaLayer() {
    const isActive = !alwaysPromo && lastKnownActive && stackState === "UP";

    if (!isActive) {
      lastWasReturning = false;
      if (lastSlideExhibitId !== null) {
        lastSlideExhibitId = null;
        lastSlideChunkIndex = undefined;
      }
      // Промо -- НЕ на каждый тик: перезапускало бы видео с нуля при каждом
      // WS-кадре, пока мы в простое. Стартует один раз при входе в простой;
      // "с начала списка" при возврате из тура выходит бесплатно.
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

    if (connLost) return; // замереть на последнем слайде

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

  // -- слой 3: плашки -- поверх ОБОИХ слоёв, независимо от меню ------------------
  function updateBadges() {
    const showEstopOverlay = estopOrFault();
    estopOverlay.hidden = !showEstopOverlay;

    // Пока стек не UP, кадров нет по определению и промо просто крутится без
    // плашки. При живом стеке потеря /mission/state видна и поверх промо.
    const connLost =
      !latestFrame ||
      latestFrame.mission_state_age_s === null ||
      latestFrame.mission_state_age_s > MISSION_STALE_S;
    // Не показывать обе плашки разом -- E-Stop важнее, конфликта смыслов нет.
    connLostOverlay.hidden = showEstopOverlay || !connLost || stackState !== "UP";
  }

  // -- пауза видео -- ОДИН источник истины: вызывается один раз, последней,
  // из render(), иначе последний вызов молча перезаписывал бы предыдущий. --
  function updateVideoPauseState() {
    const overlayOpen = menuOpen && authed;
    let pauseForState = false;
    if (!alwaysPromo && lastKnownActive && stackState === "UP") {
      // estop/fault/потеря связи -- пауза ТОЛЬКО для тура. Промо не замирает
      // от потери /mission/state.
      pauseForState = estopOrFault() || !frameFresh();
    }
    setVideoPaused(overlayOpen || pauseForState);
  }

  // -- меню: действия ------------------------------------------------------------

  function askConfirm(text, okLabel) {
    return new Promise((resolve) => {
      confirmText.textContent = text;
      btnConfirmOk.textContent = okLabel;
      const onClose = () => {
        confirmDialog.removeEventListener("close", onClose);
        resolve(confirmDialog.returnValue === "ok");
      };
      confirmDialog.addEventListener("close", onClose);
      confirmDialog.returnValue = "cancel";
      confirmDialog.showModal();
    });
  }

  btnConfirmOk.addEventListener("click", () => confirmDialog.close("ok"));
  btnConfirmCancel.addEventListener("click", () => confirmDialog.close("cancel"));

  async function setCurrentTour(tourId) {
    const res = await request("POST", "/api/tour/current", { tour_id: tourId });
    if (res.ok) {
      currentTourId = res.data.tour_id || tourId;
      showMenuMessage("Текущий тур изменён", "ok");
      render();
    } else if (res.status !== 401) {
      showMenuMessage(errorText(res));
    }
  }

  btnStart.addEventListener("click", () => menuCommand("/api/op/tour/start", {}, "Тур запущен"));
  btnStop.addEventListener("click", () => menuCommand("/api/op/tour/stop", {}, "Остановка запрошена"));
  btnHome.addEventListener("click", () => menuCommand("/api/op/go_home", {}, "Еду домой"));
  btnCostmaps.addEventListener("click", () =>
    menuCommand("/api/op/costmaps/clear", {}, "Костмапы очищены")
  );
  btnReset.addEventListener("click", async () => {
    const ok = await askConfirm("Убедитесь, что робот физически стоит на базе.", "Подтвердить сброс");
    if (ok) await menuCommand("/api/op/localization/reset", {}, "Локализация сброшена");
  });

  async function stackAction(path, question, label) {
    const ok = await askConfirm(question, label);
    if (!ok) return;
    const res = await menuCommand(path, undefined, "Команда принята");
    if (res.ok) pollStack();
  }

  btnStackStart.addEventListener("click", () =>
    stackAction("/api/stack/start", "Запустить стек ROS 2?", "Запустить")
  );
  btnStackRestart.addEventListener("click", () =>
    stackAction(
      "/api/stack/restart",
      "Перезапустить стек ROS 2? Робот на время остановится.",
      "Перезапустить"
    )
  );

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => {
      menuSection = btn.dataset.section;
      showMenuMessage("");
      if (menuSection === "ros" && stackControl) refreshStackLog();
      render();
    });
  }

  // -- сессия оператора ----------------------------------------------------------

  function clearSession() {
    authed = false;
    sessionExpiresAt = null;
    sessionOperator = "";
    menuOpen = false;
    tourListKey = "";
  }

  function onSessionLost() {
    clearSession();
    render();
  }

  btnCloseMenu.addEventListener("click", () => {
    menuOpen = false;
    render();
  });

  btnLogout.addEventListener("click", async () => {
    await request("POST", "/api/auth/logout");
    clearSession();
    render();
  });

  function openMenu() {
    menuOpen = true;
    showMenuMessage("");
    // Без запущенного ROS полезен только раздел ROS 2.
    menuSection = stackState !== "UP" && stackControl ? "ros" : "tour";
    if (menuSection === "ros") refreshStackLog();
    render();
  }

  // Долгий тап в углу открывает ЭКРАН ВХОДА, а не меню напрямую. Если сессия
  // ещё валидна (меню было закрыто), просто открывает меню без повторного PIN.
  let pressTimer = null;
  const cancelPress = () => {
    if (pressTimer) clearTimeout(pressTimer);
    pressTimer = null;
  };

  // nonce одноразовый -- сервер гасит его при ЛЮБОМ /api/auth/verify. Без
  // нового запроса после неудачной попытки currentNonce остаётся null, и второй
  // клик по "Войти"/"Приложить карту" молча ничего не делал бы.
  async function refreshChallenge() {
    const res = await request("POST", "/api/auth/challenge");
    if (!res.ok) return false;
    currentNonce = res.data.nonce;
    btnRfidOk.disabled = !(res.data.backends || []).includes("rfid");
    return true;
  }

  unlockCorner.addEventListener("pointerdown", () => {
    pressTimer = setTimeout(async () => {
      if (authed) {
        openMenu();
        return;
      }
      pinInput.value = "";
      showAuthMessage("");
      if (!(await refreshChallenge())) return;
      authDialog.showModal();
      pinInput.focus();
    }, LONG_PRESS_MS);
  });
  unlockCorner.addEventListener("pointerup", cancelPress);
  unlockCorner.addEventListener("pointerleave", cancelPress);
  unlockCorner.addEventListener("pointercancel", cancelPress);

  async function verifyAndOpen(backend, extra) {
    if (!currentNonce) return;
    const res = await request("POST", "/api/auth/verify", {
      nonce: currentNonce,
      backend,
      ...extra,
    });
    currentNonce = null; // одноразовый -- сервер уже погасил его этим вызовом
    if (!res.ok) {
      console.warn(`launcher: вход по ${backend} отклонён`, res.data);
      if (res.status === 0) {
        showAuthMessage("Нет связи с launcher");
      } else if (res.data.error === "locked_out") {
        showAuthMessage("Слишком много попыток, подождите");
      } else if (res.data.reason === "rfid_no_card") {
        showAuthMessage("Карта не считалась -- приложите её ещё раз и не убирайте");
      } else {
        showAuthMessage("Неверно");
      }
      pinInput.value = "";
      await refreshChallenge();
      return;
    }
    authed = true;
    sessionExpiresAt = res.data.expires_at;
    sessionOperator = res.data.operator || "";
    authDialog.close();
    openMenu();
  }

  btnAuthCancel.addEventListener("click", () => {
    authDialog.close();
  });
  btnPinOk.addEventListener("click", () => verifyAndOpen("pin", { pin: pinInput.value }));
  btnRfidOk.addEventListener("click", () => verifyAndOpen("rfid", {}));

  // Цифровая клавиатура PIN: панель сенсорная, системной клавиатуры нет --
  // pin-input readonly, ввод только этими кнопками.
  pinKeypad.addEventListener("click", (ev) => {
    const digit = ev.target.dataset.digit;
    if (digit === undefined) return;
    if (pinInput.value.length >= PIN_MAX_LEN) return;
    pinInput.value += digit;
    showAuthMessage("");
  });
  btnPinBackspace.addEventListener("click", () => {
    pinInput.value = pinInput.value.slice(0, -1);
  });

  // Сервер -- единственный источник истины по истечению сессии: без push-канала
  // клиент сам спрашивает, иначе забытое открытым меню не закрылось бы по TTL.
  setInterval(async () => {
    if (!authed) return;
    const res = await request("GET", "/api/auth/status");
    if (!res.ok) return;
    if (!res.data.active) {
      onSessionLost();
    } else if (typeof res.data.expires_at === "number") {
      sessionExpiresAt = res.data.expires_at;
    }
  }, SESSION_POLL_MS);

  setInterval(() => {
    if (authed && menuOpen) renderSessionExpiry();
  }, 1000);

  refreshPromo();
  pollLoop();
  render();
})();
