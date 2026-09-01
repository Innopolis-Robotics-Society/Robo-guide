// Рендер лица: одна inline-SVG, две группы глаз. Анимация -- линейная
// интерполяция к целевому набору параметров (requestAnimationFrame,
// коэффициент ~0.16/кадр, §4.2). Моргание -- оверлей поверх твина, не
// состояние (§4.3). Добавление состояния требует только правки
// config/face_states.yaml, не этого файла (§4.2).

(() => {
  "use strict";

  const TWEEN_FACTOR = 0.16;
  const WS_RECONNECT_MIN_MS = 1000;
  const WS_RECONNECT_MAX_MS = 10000;
  const MAX_GAZE_PX = 60; // масштаб gaze_az (-90..90 град) -> px, §4.4
  const TWEEN_KEYS = ["w", "h", "rr", "rot", "curve", "gx", "gy"];

  // §2.3: если /states.json недоступен -- рендерить idle из этого
  // хардкода, но не оставлять экран пустым.
  const FALLBACK_STATES = {
    global: {
      view_w: 1000,
      view_h: 600,
      eye_cx_left: 320,
      eye_cx_right: 680,
      cy: 300,
      eye_color: "#eaf6ff",
      bg_color: "#05070a",
      blink_interval_min_s: 2.5,
      blink_interval_max_s: 6.0,
      blink_duration_ms: 140,
    },
    states: {
      idle: { w: 130, h: 170, rr: 40, rot: 0, curve: 0.0, gx: 0, gy: 0 },
    },
  };

  const svg = document.getElementById("face");
  const eyeLeft = document.getElementById("eye-left");
  const eyeRight = document.getElementById("eye-right");

  let statesDoc = null;
  let current = null;
  let target = null;
  let faceState = "idle";
  let gazeAz = 0;

  let blinking = false;
  let blinkEndAt = 0;
  let nextBlinkAt = 0;

  function pickTarget(stateName) {
    const table = statesDoc.states;
    const s = table[stateName] || table.idle;
    return {
      w: s.w,
      h: s.h,
      rr: s.rr,
      rot: s.rot,
      curve: s.curve,
      gx: s.gx,
      gy: s.gy,
    };
  }

  function scheduleNextBlink() {
    const g = statesDoc.global;
    const spanMs = (g.blink_interval_max_s - g.blink_interval_min_s) * 1000;
    nextBlinkAt = performance.now() + g.blink_interval_min_s * 1000 + Math.random() * spanMs;
  }

  function applyGlobalStyle() {
    const g = statesDoc.global;
    document.documentElement.style.setProperty("--eye-color", g.eye_color);
    document.documentElement.style.setProperty("--bg-color", g.bg_color);
    svg.setAttribute("viewBox", `0 0 ${g.view_w} ${g.view_h}`);
  }

  async function loadStates() {
    try {
      const resp = await fetch("/states.json");
      if (!resp.ok) throw new Error(`status ${resp.status}`);
      statesDoc = await resp.json();
      if (!statesDoc.states || !statesDoc.states.idle) throw new Error("no idle state");
    } catch (err) {
      console.error("face: /states.json unavailable, falling back to hard-coded idle", err);
      statesDoc = FALLBACK_STATES;
    }
    applyGlobalStyle();
    target = pickTarget(faceState);
    current = { ...target };
    scheduleNextBlink();
  }

  function applyFrame(frame) {
    if (!statesDoc.states[frame.state]) {
      console.error(`face: unknown state '${frame.state}' from server, ignoring frame`);
      return;
    }
    faceState = frame.state;
    gazeAz = typeof frame.gaze_az === "number" ? frame.gaze_az : 0;
    target = pickTarget(faceState);
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
          console.error("face: malformed ws frame", err);
        }
      };
      ws.onclose = () => {
        // Лицо продолжает рендерить последнее известное состояние, пока
        // не переподключимся (§4.6) -- ничего не очищаем здесь.
        setTimeout(open, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, WS_RECONNECT_MAX_MS);
      };
      ws.onerror = () => ws.close();
    };
    open();
  }

  function clampGazePx(az) {
    const clamped = Math.max(-90, Math.min(90, az));
    return (clamped / 90) * MAX_GAZE_PX;
  }

  function updateEyeGroup(group, cx, rot, extraGx) {
    const w = current.w;
    const h = current.h;
    const rr = Math.min(current.rr, w / 2, h / 2);
    const tx = cx + current.gx + extraGx;
    const ty = statesDoc.global.cy + current.gy;
    group.setAttribute("transform", `translate(${tx},${ty}) rotate(${rot})`);

    const halo = group.querySelector(".halo");
    halo.setAttribute("x", -w * 0.65);
    halo.setAttribute("y", -h * 0.65);
    halo.setAttribute("width", w * 1.3);
    halo.setAttribute("height", h * 1.3);
    halo.setAttribute("rx", rr * 1.3);

    const body = group.querySelector(".body");
    body.setAttribute("x", -w / 2);
    body.setAttribute("y", -h / 2);
    body.setAttribute("width", w);
    body.setAttribute("height", h);
    body.setAttribute("rx", rr);

    const lidBottom = group.querySelector(".lid-bottom");
    const rise = current.curve * h * 0.9;
    const yBottom = h / 2 + 2;
    const yRise = h / 2 - rise;
    const halfW = w / 2 + 2;
    lidBottom.setAttribute(
      "d",
      `M ${-halfW} ${yBottom} L ${-halfW} ${yRise} ` +
        `Q 0 ${yRise - rise * 0.6} ${halfW} ${yRise} L ${halfW} ${yBottom} Z`
    );

    const lidTop = group.querySelector(".lid-top");
    const blinkH = blinking ? h + 4 : 0;
    lidTop.setAttribute("x", -w / 2 - 2);
    lidTop.setAttribute("y", -h / 2 - 2);
    lidTop.setAttribute("width", w + 4);
    lidTop.setAttribute("height", blinkH);
  }

  function tick() {
    requestAnimationFrame(tick);
    if (!current || !target) return;

    for (const key of TWEEN_KEYS) {
      current[key] += (target[key] - current[key]) * TWEEN_FACTOR;
    }

    const now = performance.now();
    if (faceState === "sleep") {
      blinking = false;
    } else if (!blinking && now >= nextBlinkAt) {
      blinking = true;
      blinkEndAt = now + statesDoc.global.blink_duration_ms;
    } else if (blinking && now >= blinkEndAt) {
      blinking = false;
      scheduleNextBlink();
    }

    const gazePx = clampGazePx(gazeAz);
    updateEyeGroup(eyeLeft, statesDoc.global.eye_cx_left, current.rot, gazePx);
    updateEyeGroup(eyeRight, statesDoc.global.eye_cx_right, -current.rot, gazePx);
  }

  loadStates().then(() => {
    connectWs();
    requestAnimationFrame(tick);
  });
})();
