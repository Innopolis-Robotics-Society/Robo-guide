// Execute with gjs test/test_wakeword_review.js; no browser or npm dependencies.
"use strict";
const GLib = imports.gi.GLib;
const ByteArray = imports.byteArray;
const [, sourceBytes] = GLib.file_get_contents("scripts/wakeword_review_assets/review.js");
const source = ByteArray.toString(sourceBytes);
// Function also parses the complete browser script, though DOM code is not run here.
const createReviewStore = new Function(source.split("// Browser interface.")[0] +
                                      "\nreturn createReviewStore;")();
new Function(source);
function assert(value, message) { if (!value) throw new Error(message); }
function throws(action) {
  let failed = false;
  try { action(); } catch (_) { failed = true; }
  assert(failed, "Expected validation failure");
}
const data = {
  dataset_fingerprint: "test-run-02", run_name: "run_02",
  rows: [{candidate: "positive/a.wav"}, {candidate: "negative/b.wav"}]
};
const values = new Map();
const storage = {
  getItem(key) { return values.get(key) || null; },
  setItem(key, value) { values.set(key, value); }
};
let store = createReviewStore(data, storage);
assert(store.storageOk && store.counts().pending === 2, "Initial state");
store.set("positive/a.wav", "accepted", "слово целиком");
store.set("negative/b.wav", "rejected", "есть имя");
assert(store.counts().accepted === 1 && store.counts().rejected === 1, "Independent choices");
const saved = JSON.parse(JSON.stringify(store.snapshot()));
store = createReviewStore(data, storage);
assert(store.get("positive/a.wav").note === "слово целиком", "Reload restores decisions");
assert(store.get("negative/b.wav").choice === "rejected", "Negative not silently accepted");
store.set("positive/a.wav", "pending");
assert(store.get("positive/a.wav") === null && store.counts().pending === 1, "Undo");
const undoSnapshot = JSON.parse(JSON.stringify(store.snapshot()));
store.import(saved);
assert(store.counts().pending === 0, "Import restores snapshot");
store.import(undoSnapshot);
assert(store.get("positive/a.wav") === null, "Import preserves cancelled decisions");
assert(store.snapshot().semantics.positive_rejected.includes("НЕ отрицательная"), "Rejection semantics");
throws(() => store.import({...saved, dataset_fingerprint: "run-01"}));
throws(() => store.import({...saved, schema_version: 99}));
throws(() => store.import({...saved, decisions: []}));
throws(() => store.import({...saved, decisions: {"missing.wav": saved.decisions["positive/a.wav"]}}));
throws(() => store.import({...saved, decisions: {"positive/a.wav": {choice: "accepted"}}}));
throws(() => store.set("missing.wav", "accepted"));
throws(() => store.set("positive/a.wav", "maybe"));
assert(store.get("positive/a.wav") === null, "Invalid import changes nothing");
const snapshot = store.snapshot();
snapshot.decisions["negative/b.wav"].choice = "accepted";
assert(store.get("negative/b.wav").choice === "rejected", "Snapshots do not mutate state");
const noStorage = createReviewStore(data, null);
assert(!noStorage.storageOk, "Missing storage must be reported");
noStorage.set("positive/a.wav", "accepted");
assert(noStorage.counts().accepted === 1 && !noStorage.storageOk, "Export usable without persistence");
const deniedStorage = createReviewStore(data, {getItem() { return null; }, setItem() { throw new Error("denied"); }});
deniedStorage.set("negative/b.wav", "rejected");
assert(!deniedStorage.storageOk && deniedStorage.get("negative/b.wav"), "Failed write retains memory");
const corruptStorage = createReviewStore(data, {getItem() { return "broken JSON"; }, setItem() {}});
assert(corruptStorage.initialError && !corruptStorage.storageOk, "Corrupt JSON not silently lost");
const otherRun = createReviewStore({...data, dataset_fingerprint: "run-01"}, storage);
assert(otherRun.counts().pending === 2, "Runs must not share choices");
print("review state tests passed");
