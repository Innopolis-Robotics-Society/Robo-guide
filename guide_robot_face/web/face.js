// Рендер лица: одна inline-SVG, две группы глаз. Анимация -- линейная
// интерполяция к целевому набору параметров (requestAnimationFrame,
// коэффициент ~0.16/кадр). Моргание, дыхание и саккады -- оверлей
// поверх твина, не состояния. Добавление состояния требует только
// правки config/face_states.yaml, не этого файла.

(() => {
  "use strict";

  const TWEEN_FACTOR = 0.16;
  const WS_RECONNECT_MIN_MS = 1000;
  const WS_RECONNECT_MAX_MS = 10000;
  const MAX_GAZE_PX = 80; // масштаб gaze_az (-90..90 град) -> px
  const TWEEN_KEYS = ["w", "h", "rr", "rot", "curve", "gx", "gy"];
  const BREATH_AMP = 0.03;
  const BREATH_PERIOD_MS = 4000;
  const SPEAK_BREATH_AMP = 0.055;
  const SPEAK_BREATH_PERIOD_MS = 1700;
  const SACCAD_TWEEN = 0.12;
  const SACCAD_MIN_MS = 2500;
  const SACCAD_SPAN_MS = 5000;

  const FALLBACK_STATES = {
    global: {
      view_w: 1024,
      view_h: 768,
      eye_cx_left: 300,
      eye_cx_right: 724,
      cy: 384,
      rotate_deg: 0,
      eye_color: "#eaf6ff",
      bg_color: "#05070a",
      blink_interval_min_s: 2.5,
      blink_interval_max_s: 6.0,
      blink_duration_ms: 280,
    },
    states: {
      idle: { w: 210, h: 270, rr: 64, rot: 0, curve: 0.0, gx: 0, gy: 0 },
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
  let blinkStartAt = 0;
  let nextBlinkAt = 0;

  let saccadeGx = 0;
  let saccadeGy = 0;
  let saccadeTargetGx = 0;
  let saccadeTargetGy = 0;
  let nextSaccadeAt = 0;

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
    // speaking моргает чаще idle (~1.5×); интервал тот же YAML.
    const rate = faceState === "speaking" ? 1 / 1.5 : 1;
    const spanMs = (g.blink_interval_max_s - g.blink_interval_min_s) * 1000 * rate;
    nextBlinkAt =
      performance.now() + g.blink_interval_min_s * 1000 * rate + Math.random() * spanMs;
  }

  function scheduleNextSaccade(now) {
    nextSaccadeAt = now + SACCAD_MIN_MS + Math.random() * SACCAD_SPAN_MS;
    if (Math.random() < 0.4) {
      saccadeTargetGx = (Math.random() - 0.5) * 24;
      saccadeTargetGy = (Math.random() - 0.5) * 16;
    } else {
      saccadeTargetGx = 0;
      saccadeTargetGy = 0;
    }
  }

  function applyGlobalStyle() {
    const g = statesDoc.global;
    document.documentElement.style.setProperty("--eye-color", g.eye_color);
    document.documentElement.style.setProperty("--bg-color", g.bg_color);
    const rotate = typeof g.rotate_deg === "number" ? g.rotate_deg : 0;
    document.documentElement.style.setProperty("--face-rotate", `${rotate}deg`);
    svg.setAttribute("viewBox", `0 0 ${g.view_w} ${g.view_h}`);
  }

  function isPreview() {
    return new URLSearchParams(location.search).get("preview") === "1";
  }

  async function loadStates() {
    const urls = ["/states.json", "states.json"];
    let loaded = null;
    for (const url of urls) {
      try {
        const resp = await fetch(url);
        if (!resp.ok) continue;
        const doc = await resp.json();
        if (doc.states && doc.states.idle) {
          loaded = doc;
          break;
        }
      } catch {
        /* следующий url */
      }
    }
    if (!loaded) {
      console.error("face: states.json unavailable, falling back to hard-coded idle");
      statesDoc = FALLBACK_STATES;
    } else {
      statesDoc = loaded;
    }
    if (isPreview()) {
      statesDoc.global.rotate_deg = 0;
    }
    applyGlobalStyle();
    const wanted = new URLSearchParams(location.search).get("state");
    if (wanted && statesDoc.states[wanted]) faceState = wanted;
    target = pickTarget(faceState);
    current = { ...target };
    scheduleNextBlink();
    scheduleNextSaccade(performance.now());
  }

  function setState(name) {
    applyFrame({ state: name, gaze_az: 0 });
  }

  function mountPreview() {
    document.body.classList.add("preview");
    const names = Object.keys(statesDoc.states);
    const bar = document.createElement("nav");
    bar.id = "preview-bar";
    bar.setAttribute("aria-label", "состояния лица");
    for (const name of names) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = name;
      btn.dataset.state = name;
      if (name === faceState) btn.classList.add("active");
      btn.addEventListener("click", () => {
        setState(name);
        for (const b of bar.querySelectorAll("button")) {
          b.classList.toggle("active", b.dataset.state === name);
        }
      });
      bar.appendChild(btn);
    }
    document.body.appendChild(bar);
    document.addEventListener("keydown", (ev) => {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      const i = names.indexOf(faceState);
      const next =
        ev.key === "ArrowRight"
          ? names[(i + 1) % names.length]
          : names[(i - 1 + names.length) % names.length];
      setState(next);
      for (const b of bar.querySelectorAll("button")) {
        b.classList.toggle("active", b.dataset.state === next);
      }
    });
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
        // не переподключимся -- ничего не очищаем здесь.
        setTimeout(open, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, WS_RECONNECT_MAX_MS);
      };
      ws.onerror = () => ws.close();
    };
    open();
  }

  function roundedRectPath(x, y, w, h, r) {
    r = Math.max(0, Math.min(r, w / 2, h / 2));
    return (
      `M${x + r},${y}h${w - 2 * r}a${r},${r} 0 0 1 ${r},${r}` +
      `v${h - 2 * r}a${r},${r} 0 0 1 ${-r},${r}` +
      `h${-(w - 2 * r)}a${r},${r} 0 0 1 ${-r},${-r}` +
      `v${-(h - 2 * r)}a${r},${r} 0 0 1 ${r},${-r}z`
    );
  }

  function clampGazePx(az) {
    const clamped = Math.max(-90, Math.min(90, az));
    return (clamped / 90) * MAX_GAZE_PX;
  }

  function updateEyeGroup(group, cx, rot, extraGx, extraGy, breath, blinkAmt) {
    const w = current.w * breath;
    const h = current.h * breath;
    const rr = Math.min(current.rr, w / 2, h / 2);
    const tx = cx + current.gx + extraGx;
    const ty = statesDoc.global.cy + current.gy + extraGy;
    group.setAttribute("transform", `translate(${tx},${ty}) rotate(${rot})`);

    // Обод поверх века: evenodd-кольцо, внутренний вырез чуть меньше тела,
    // чтобы серый край накрывал моргание. Веко рисуется под ним и видно
    // в отверстии (не clipPath -- иначе твин высоты пропадает).
    const inset = 3;
    const holeX = -w / 2 + inset;
    const holeY = -h / 2 + inset;
    const holeW = w - inset * 2;
    const holeH = h - inset * 2;
    const holeR = Math.max(0, rr - inset);
    const halo = group.querySelector(".halo");
    halo.setAttribute(
      "d",
      roundedRectPath(-w * 0.65, -h * 0.65, w * 1.3, h * 1.3, rr * 1.3) +
        roundedRectPath(holeX, holeY, holeW, holeH, holeR)
    );

    const body = group.querySelector(".body");
    body.setAttribute("x", -w / 2);
    body.setAttribute("y", -h / 2);
    body.setAttribute("width", w);
    body.setAttribute("height", h);
    body.setAttribute("rx", rr);

    const err = group.querySelector(".err-label");
    if (faceState === "error") {
      err.textContent = "ERROR";
      err.setAttribute("font-size", String(Math.round(Math.min(w, h) * 0.22)));
    } else {
      err.textContent = "";
    }

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

    // Шторка: край едет сверху вниз (translate), не scaleY — сжатие
    // выглядело вспышкой. Overshoot под обод, чтобы AA не светил склеру.
    const lidTop = group.querySelector(".lid-top");
    const overshoot = 6;
    const lidH = holeH + overshoot + 2;
    lidTop.setAttribute("x", holeX - overshoot);
    lidTop.setAttribute("y", holeY - overshoot);
    lidTop.setAttribute("width", holeW + overshoot * 2);
    lidTop.setAttribute("height", lidH);
    lidTop.setAttribute("rx", 0);
    lidTop.setAttribute("transform", `translate(0 ${(1 - blinkAmt) * -lidH})`);
  }

  function tick() {
    requestAnimationFrame(tick);
    if (!current || !target) return;

    for (const key of TWEEN_KEYS) {
      current[key] += (target[key] - current[key]) * TWEEN_FACTOR;
    }

    const now = performance.now();
    const blinkDur = statesDoc.global.blink_duration_ms;
    let blinkAmt = 0;
    if (faceState === "sleep") {
      blinking = false;
    } else if (!blinking && now >= nextBlinkAt) {
      blinking = true;
      blinkStartAt = now;
    } else if (blinking && now >= blinkStartAt + blinkDur) {
      blinking = false;
      scheduleNextBlink();
    }
    if (blinking) {
      const t = Math.max(0, Math.min(1, (now - blinkStartAt) / blinkDur));
      blinkAmt = Math.sin(t * Math.PI);
    }

    let breath = 1;
    let extraGx = clampGazePx(gazeAz);
    let extraGy = 0;
    if (faceState === "idle" || faceState === "speaking") {
      const amp = faceState === "speaking" ? SPEAK_BREATH_AMP : BREATH_AMP;
      const period = faceState === "speaking" ? SPEAK_BREATH_PERIOD_MS : BREATH_PERIOD_MS;
      breath = 1 + amp * Math.sin((now / period) * 2 * Math.PI);
    }
    if (faceState === "idle") {
      saccadeGx += (saccadeTargetGx - saccadeGx) * SACCAD_TWEEN;
      saccadeGy += (saccadeTargetGy - saccadeGy) * SACCAD_TWEEN;
      if (now >= nextSaccadeAt) scheduleNextSaccade(now);
      extraGx += saccadeGx;
      extraGy += saccadeGy;
    } else {
      saccadeGx = 0;
      saccadeGy = 0;
      saccadeTargetGx = 0;
      saccadeTargetGy = 0;
    }

    updateEyeGroup(
      eyeLeft,
      statesDoc.global.eye_cx_left,
      current.rot,
      extraGx,
      extraGy,
      breath,
      blinkAmt
    );
    updateEyeGroup(
      eyeRight,
      statesDoc.global.eye_cx_right,
      -current.rot,
      extraGx,
      extraGy,
      breath,
      blinkAmt
    );
  }

  loadStates().then(() => {
    if (isPreview()) {
      mountPreview();
    } else {
      connectWs();
    }
    requestAnimationFrame(tick);
  });
})();
