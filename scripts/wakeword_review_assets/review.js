"use strict";

function createReviewStore(data, storage) {
  const key = "firaya-review-v1:" + data.dataset_fingerprint;
  const validIds = new Set(data.rows.map(row => row.candidate));
  let decisions = Object.create(null);
  let storageOk = false;
  let initialError = "";

  function validate(payload) {
    if (!payload || payload.schema_version !== 1 ||
        payload.dataset_fingerprint !== data.dataset_fingerprint) {
      throw new Error("Этот файл относится к другой нарезке. Выберите JSON именно этого запуска.");
    }
    if (!payload.decisions || typeof payload.decisions !== "object" ||
        Array.isArray(payload.decisions)) {
      throw new Error("В JSON нет корректных решений.");
    }
    const checked = Object.create(null);
    for (const [id, value] of Object.entries(payload.decisions)) {
      if (!validIds.has(id) || !value ||
          !["accepted", "rejected"].includes(value.choice) ||
          typeof value.note !== "string" || value.note.length > 2000 ||
          typeof value.checked_at !== "string" || !Number.isFinite(Date.parse(value.checked_at))) {
        throw new Error("Некорректное решение в JSON: " + id);
      }
      checked[id] = { choice: value.choice, note: value.note, checked_at: value.checked_at };
    }
    return checked;
  }

  function snapshot() {
    return {
      schema_version: 1,
      dataset_fingerprint: data.dataset_fingerprint,
      run_name: data.run_name,
      exported_at: new Date().toISOString(),
      semantics: {
        positive_accepted: "Полное имя осталось в кандидате; исходник positive независимо от решения",
        positive_rejected: "Отклонена нарезка, НЕ отрицательная метка и НЕ удаление исходника",
        negative_accepted: "Во фрагменте нет имени и его обрезанных частей",
        negative_rejected: "Не использовать как negative: имя есть или проверяющий сомневается",
        pending: "Отсутствие решения не означает согласие; не использовать как проверенный пример"
      },
      decisions: Object.fromEntries(Object.entries(decisions).map(([id, value]) => [id, {...value}]))
    };
  }

  function persist() {
    try {
      if (!storage) throw new Error("storage unavailable");
      storage.setItem(key, JSON.stringify(snapshot()));
      storageOk = true;
    } catch (_) {
      storageOk = false;
    }
    return storageOk;
  }

  try {
    const saved = storage ? storage.getItem(key) : null;
    if (saved) decisions = validate(JSON.parse(saved));
    persist();
  } catch (_) {
    initialError = "Не удалось восстановить сохранённые решения. Загрузите резервный JSON.";
  }

  return {
    snapshot,
    get storageOk() { return storageOk; },
    get initialError() { return initialError; },
    get(id) { return decisions[id] ? {...decisions[id]} : null; },
    set(id, choice, note = "") {
      if (!validIds.has(id) || !["accepted", "rejected", "pending"].includes(choice) ||
          typeof note !== "string" || note.length > 2000) {
        throw new Error("Некорректное решение.");
      }
      if (choice === "pending") delete decisions[id];
      else decisions[id] = {choice, note, checked_at: new Date().toISOString()};
      return persist();
    },
    import(payload) {
      // Сначала проверяем весь файл. При ошибке текущие решения не меняются.
      const incoming = validate(payload);
      decisions = incoming;
      return persist();
    },
    counts() {
      const counts = {accepted: 0, rejected: 0, pending: validIds.size};
      for (const value of Object.values(decisions)) {
        counts[value.choice] += 1;
        counts.pending -= 1;
      }
      return counts;
    }
  };
}

// Browser interface.
(() => {
  const data = JSON.parse(document.getElementById("review-data").textContent);
  let storage = null;
  try { storage = window.localStorage; } catch (_) { /* file:// may forbid storage */ }
  const store = createReviewStore(data, storage);
  const cards = Array.from(document.querySelectorAll(".review-card"));
  const rows = data.rows;
  const kindFilter = document.getElementById("kind-filter");
  const decisionFilter = document.getElementById("decision-filter");
  const warningsOnly = document.getElementById("warnings-only");
  const autoNext = document.getElementById("auto-next");
  const message = document.getElementById("message");

  function notify(text, error = false) {
    message.textContent = text;
    message.classList.toggle("error", error);
  }

  function render() {
    let visible = 0;
    cards.forEach((card, index) => {
      const row = rows[index];
      const decision = store.get(row.candidate);
      const state = decision ? decision.choice : "pending";
      const crowd = row.kind === "positive" && row.source.split("/").pop().startsWith("crowd_");
      const kindMatches = kindFilter.value === "all" || kindFilter.value === row.kind ||
        (kindFilter.value === "crowd" && crowd) ||
        (kindFilter.value === "quiet" && row.kind === "positive" && !crowd);
      card.hidden = !(kindMatches &&
        (decisionFilter.value === "all" || decisionFilter.value === state) &&
        (!warningsOnly.checked || row.review_required));
      if (!card.hidden) visible += 1;
      else card.querySelectorAll("audio").forEach(audio => audio.pause());
      card.dataset.decision = state;
      card.querySelector(".badge").textContent = {
        pending: "Не проверено", accepted: "Да — принято", rejected: "Нет — отклонено"
      }[state];
      card.querySelectorAll("[data-choice]").forEach(button => {
        button.setAttribute("aria-pressed", String(button.dataset.choice === state));
      });
    });
    const counts = store.counts();
    document.getElementById("progress").textContent =
      `Принято: ${counts.accepted} · Отклонено: ${counts.rejected} · Осталось: ${counts.pending} / ${rows.length}`;
    document.getElementById("visible-count").textContent = `Показано записей: ${visible}`;
    document.getElementById("empty").hidden = visible !== 0;
    document.getElementById("storage-status").textContent = store.storageOk
      ? "Решения автоматически сохранены в этом браузере. Для резервной копии и передачи сохраните JSON."
      : "Браузер НЕ сохраняет решения. Они есть только в открытой вкладке: обязательно скачайте JSON перед закрытием!";
  }

  cards.forEach((card, index) => {
    const row = rows[index];
    const note = card.querySelector(".note");
    note.value = store.get(row.candidate)?.note || "";
    card.querySelectorAll("[data-choice]").forEach(button => {
      button.addEventListener("click", () => {
        const choice = button.dataset.choice;
        store.set(row.candidate, choice, note.value);
        if (choice === "pending") note.value = "";
        card.querySelectorAll("audio").forEach(audio => audio.pause());
        notify(choice === "accepted" ? "Фрагмент принят." :
          choice === "rejected" ? "Фрагмент отклонён. Исходник и его метка не изменены." :
          "Решение отменено; фрагмент снова требует проверки.");
        render();
        if (autoNext.checked && choice !== "pending") {
          const ordered = cards.slice(index + 1).concat(cards.slice(0, index));
          const next = ordered.find(item => !item.hidden && item.dataset.decision === "pending");
          card.open = false;
          if (next) {
            next.open = true;
            // Начало карточки не должно оказаться под фиксированной панелью.
            next.style.scrollMarginTop = `${document.querySelector(".toolbar").offsetHeight + 12}px`;
            next.scrollIntoView({behavior: "smooth", block: "start"});
          }
        }
      });
    });
    note.addEventListener("change", () => {
      const decision = store.get(row.candidate);
      if (decision) { store.set(row.candidate, decision.choice, note.value); render(); }
    });
  });

  [kindFilter, decisionFilter, warningsOnly].forEach(control => {
    control.addEventListener("change", () => {
      render();
      const first = cards.find(card => !card.hidden);
      if (first) first.open = true;
    });
  });

  document.querySelectorAll("audio").forEach(audio => {
    audio.addEventListener("play", () => {
      document.querySelectorAll("audio").forEach(other => { if (other !== audio) other.pause(); });
    });
    audio.addEventListener("loadedmetadata", () => {
      if (audio.dataset.originalStart) {
        audio.currentTime = Math.min(Number(audio.dataset.originalStart), audio.duration);
      }
    });
  });

  document.getElementById("export").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(store.snapshot(), null, 2)], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `review_decisions_${data.run_name}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    notify("JSON отправлен на скачивание. Сохраните его — это файл ваших решений.");
  });
  const input = document.getElementById("import-file");
  const importButton = document.getElementById("import");
  importButton.addEventListener("click", () => input.click());
  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file) return;
    importButton.disabled = true;
    try {
      if (file.size > 5 * 1024 * 1024) throw new Error("Слишком большой JSON (максимум 5 МБ).");
      const payload = JSON.parse(await file.text());
      if ((store.counts().accepted || store.counts().rejected) &&
          !window.confirm("Восстановить решения из JSON? Текущие решения в этой вкладке будут заменены.")) {
        return;
      }
      store.import(payload);
      cards.forEach((card, index) => {
        card.querySelector(".note").value = store.get(rows[index].candidate)?.note || "";
      });
      render();
      notify("Решения восстановлены из JSON. Текущие решения заменены содержимым файла.");
    } catch (error) {
      notify("Не удалось загрузить решения: " + error.message, true);
    } finally {
      input.value = "";
      importButton.disabled = false;
    }
  });
  render();
  if (store.initialError) notify(store.initialError, true);
  const first = cards.find(card => !card.hidden);
  if (first) first.open = true;
})();
