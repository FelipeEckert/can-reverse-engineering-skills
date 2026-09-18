"""
flask_sync.py - browser UI ("CANsub Reference Generator") to log a human reference
timeline (sidecar) AND drive the CAN capture from a start/stop button. Two excitation
modes, toggled in the UI (--mode both, default), each recorded as its own numbered run:

HOLDS (anchors) - a STATES list of known values. SETTLE the signal at a value, then
click it (or press its digit). Each click opens a FIXED sampling window (--window s):
only that window is recorded as the known value (a kind="anchor" tag plus dense
kind="value" rows). A windows-only reference, suitable for any signal you can park at
known levels - INCLUDING discrete states (door lock 0/1, gears P/R/N/D, wiper
off/low/high). Use it to CALIBRATE scale/offset (clean anchors) and verify absolute
levels.

SWEEP (slider) - a vertical slider spanning [--min, --max]. Drag it to track the signal
smoothly across its whole range; the slider value is logged densely (~20 Hz) as
kind="value" rows with NO anchors. Analysed WITHOUT --ref-window (a continuous
reference). Use it to IDENTIFY the field: continuous variation breaks the correlate
degeneracy that a few discrete holds cause (counters score ~0, the true field ~1.0),
and the transitions let bitsearch's resolution-refinement recover the FULL field width
(discrete holds under-read it). The sweep can be laggy/noisy because it only fixes
WHICH field and HOW WIDE - absolute calibration comes from the holds run.

Recommended live flow: capture a HOLDS run AND a SWEEP run, identify on the sweep,
calibrate on the holds. (When a machine reference exists in a recorded log - OBD2 /
GPS-to-CAN - prefer the OFFLINE workflow instead. For headless collection,
annotate.py --mode continuous is the sweep fallback; use --mode discrete with
correlate --type discrete only for a truly momentary event that cannot be held.)

PER-RUN FILES: each Start opens a NEW numbered pair trace_<label>_<N>.csv +
sidecar_<label>_<N>.csv, so trace and sidecar always match and earlier runs are
never overwritten or mixed, including across app restarts. A runs index
runs_<label>.json records capture bounds/status/settings plus lightweight phase
markers for experiment chronology. This makes multi-run / multi-excitation capture
(recommended when one run is ambiguous) clean: capture a few discriminating runs,
then compare candidates against the marked maneuver segments.

The CANsub clock is set to host time on connect, so trace and sidecar share a
reference. Capture uses the silent/ACK mode from bus.json (vehicle->silent,
bench->ACK); override with --listen-only / --normal.

Example:
    .venv/bin/python .agents/skills/can-reverse-engineering/scripts/flask_sync.py \
        --unit m/s^2 --min -9.81 --max 9.81 \
        --states 3 --state-values "-9.81,0,9.81"
    # open http://127.0.0.1:5000 -> Start, park/hold/tag each value, Stop
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path

import can
from flask import Flask, request, jsonify, render_template_string

import common  # CANsub registration is deferred until live capture starts.
import capture

ASSETS = str(Path(__file__).resolve().parent.parent / "assets")

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>CANsub Reference Generator</title>
<link rel="stylesheet" href="/static/uPlot.min.css">
<style>
  :root { --blue: #3d85c6; --orange: #ff9900; --green: #10cba9; --red: #ff6666; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #ffffff; color: #1a1d21;
         overflow-x: hidden; }
  .apptitle { font-size: 1.15rem; font-weight: 700; color: #1a1d21;
              padding: 12px 16px 0; background: #f7f8fa; }
  .capbar { display: flex; align-items: center; gap: 18px; padding: 12px 16px;
            border-bottom: 1px solid #e2e5ea; background: #f7f8fa; }
  #capbtn { font-size: 1.05rem; font-weight: 700; color: #fff; border: none;
            border-radius: 10px; padding: 12px 22px; cursor: pointer;
            transition: filter .08s; min-width: 170px; }
  #capbtn.start { background: var(--blue); }
  #capbtn.stop  { background: var(--red); }
  #capbtn:hover { filter: brightness(1.07); }
  #capbtn:disabled { opacity: .55; cursor: default; }
  .status { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px;
            font-size: .9rem; line-height: 1.35; }
  .status .lbl { color: #9aa3af; text-align: right; }
  .status .val { color: #1a1d21; font-variant-numeric: tabular-nums; }
  .status .val.errtext { color: var(--red); font-weight: 600; }
  #dot { display:inline-block; width:9px; height:9px; border-radius:50%;
         background:#c2c8d0; margin-right:6px; vertical-align: middle; }
  #dot.ok  { background: var(--green); }
  #dot.bad { background: var(--red); }
  #capstatus { font-variant-numeric: tabular-nums; color: #5b6573; }
  #capstatus.live { color: var(--green); font-weight: 600; }
  #capstatus.err  { color: var(--red); font-weight: 600; }
  .run-note { min-width:240px; max-width:420px; flex:1; border:1px solid #ccd2da;
              border-radius:7px; padding:8px 10px; font-size:.85rem; }
  .instr { padding: 10px 16px; background: #eef6ff; border-bottom: 1px solid #d8e6f5;
           color: #244; font-size: .74rem; white-space: pre-line; }
  .chronology { display:flex; align-items:center; flex-wrap:wrap; gap:6px; padding:8px 16px;
                border-bottom:1px solid #e2e5ea; background:#fbfcfd; font-size:.8rem; }
  .chronology button { border:1px solid #ccd2da; border-radius:7px; background:#fff;
                       padding:6px 10px; cursor:pointer; }
  .chronology button:disabled { opacity:.45; cursor:default; }
  .event-note { min-width:180px; flex:1; max-width:360px; border:1px solid #ccd2da;
                border-radius:7px; padding:6px 8px; }
  #markstatus { color:#5b6573; min-width:110px; }
  .holds { padding: 12px 16px 4px; }
  /* states are a column; if there are many (>6), the LIST scrolls within itself so
     the page never grows a scrollbar. */
  #states { display:flex; flex-direction:column; gap:4px; max-width:340px;
            max-height:34vh; overflow-y:auto; overflow-x:hidden; }
  .staterow { display:flex; align-items:center; gap:5px; padding:5px 8px;
              border:1px solid #d8dce2; border-radius:7px; cursor:pointer; font-size:.92rem; }
  .staterow:hover { background:#f3f5f8; }
  .staterow.sel { border-color: var(--green); background: rgba(16,203,169,.14); font-weight:700; }
  .staterow.sampling { border-color: var(--green); background: rgba(16,203,169,.22);
                       box-shadow: 0 0 0 2px rgba(16,203,169,.35); font-weight:700; }
  .staterow .sval { width:58px; background:#fff; color:#1a1d21; border:1px solid #ccd2da;
                    border-radius:5px; padding:3px 4px; text-align:right; font-size:.88rem; }
  .staterow .sunit { color:#5b6573; font-size:.82rem; }
  .staterow .skey { margin-left:auto; color:#9aa3af; font-size:.7rem; }
  .holdhint { font-size:.82rem; padding:8px 0 0; color:#5b6573;
              min-height:1.1em; font-variant-numeric: tabular-nums; }
  .holdhint.live { color: var(--green); font-weight:600; }
  .bottom { padding: 0 16px 8px; }
  #plot { background:#ffffff; border:1px solid #e2e5ea; border-radius:12px; padding:8px;}
  .runs { padding: 4px 16px 12px; font-size:.82rem; color:#5b6573; }
  .runs b { color:#1a1d21; }
  /* mode switch styled as real TABS: a strip sitting on a bottom rule, with the
     active tab's bottom edge punched out so it reads as connected to its panel. */
  .modebar { display:flex; gap:4px; padding:10px 16px 0; border-bottom:1px solid #d8dce2; }
  .modebtn { font-size:.9rem; font-weight:600; color:#5b6573; background:#f3f5f8;
             border:1px solid #d8dce2; border-bottom:none; border-radius:8px 8px 0 0;
             padding:7px 18px; cursor:pointer; position:relative; top:1px; }
  .modebtn:hover:not(.sel):not(:disabled) { background:#e9edf2; }
  .modebtn.sel { background:#fff; color:var(--blue); border-bottom:1px solid #fff; }
  .modebtn:disabled { opacity:.5; cursor:default; }
  /* holds + sweep panels share ONE fixed height so the plot below never shifts
     vertically when you switch tabs. */
  .holds, .sweep { height:33vh; box-sizing:border-box; }
  .sweep { display:flex; align-items:center; gap:20px; padding:12px 16px 4px; }
  /* slider column: max label on top, slider filling the middle, min label below. */
  .slidercol { display:flex; flex-direction:column; align-items:center; gap:8px; height:100%; }
  .sliderend { font-size:.85rem; font-weight:600; color:#5b6573; font-variant-numeric:tabular-nums; }
  /* vertical slider via the standardized writing-mode/direction route; the old
     `appearance: slider-vertical` keyword is deprecated (and slated for removal),
     so we don't set it - writing-mode is what orients the control. */
  #sweep { writing-mode: vertical-lr; direction: rtl;
           width:32px; flex:1; accent-color: var(--blue); }
  .sweepread { font-size:1.5rem; font-weight:700; color:#1a1d21; font-variant-numeric:tabular-nums; }
  .sweepread .su { font-size:.9rem; color:#5b6573; font-weight:600; margin-left:4px; }
</style></head><body>
<div class="apptitle">CANsub Reference Generator</div>
<div class="capbar">
  <button id="capbtn" class="start" tabindex="-1">start capture</button>
  <input id="experiment-note" class="run-note" maxlength="200"
         placeholder="optional experiment note before Start">
  <div class="status">
    <span class="lbl">Device</span><span class="val"><span id="dot"></span><span id="dev">…</span></span>
    <span class="lbl">Bit-rate</span><span class="val" id="rate">…</span>
    <span class="lbl">Profile</span><span class="val" id="profile">…</span>
    <span class="lbl">Status</span><span class="val"><span id="capstatus">idle</span></span>
  </div>
</div>
<div class="instr" id="instr">{{instructions}}</div>
<div class="chronology">
  <span>Mark transitions/deviations:</span>
  <button class="phasebtn" data-phase="baseline" disabled>baseline</button>
  <button class="phasebtn" data-phase="stimulus" disabled>stimulus</button>
  <button class="phasebtn" data-phase="return" disabled>return</button>
  <button class="phasebtn" data-phase="unexpected" disabled>unexpected</button>
  <input id="event-note" class="event-note" maxlength="200" disabled
         placeholder="optional short event note">
  <span id="markstatus"></span>
</div>
<div class="modebar" id="modebar" style="display:none">
  <button id="mode-sweep" class="modebtn sel" tabindex="-1">Sweep</button>
  <button id="mode-holds" class="modebtn" tabindex="-1">Anchors</button>
</div>
<div class="holds" id="holdspanel">
  <div id="states"></div>
  <div class="holdhint" id="holdhint"></div>
</div>
<div class="sweep" id="sweeppanel" style="display:none">
  <div class="slidercol">
    <div class="sliderend" id="sweepmax">–</div>
    <input type="range" id="sweep">
    <div class="sliderend" id="sweepmin">–</div>
  </div>
  <div class="sweepread"><span id="sweepval">–</span><span class="su" id="sweepunit"></span></div>
</div>
<div class="bottom"><div id="plot"></div></div>
<div class="runs" id="runs"></div>
<script src="/static/uPlot.iife.min.js"></script>
<script>
const CFG = {
  label: {{ label | tojson }}, unit: {{ unit | tojson }},
  vmin: {{ vmin }}, vmax: {{ vmax }}, nstates: {{ nstates }},
  stateValues: {{ state_values | tojson }}, heartbeatMs: 250,
  holdWindowMs: {{ hold_window_ms }}, holdGuardMs: {{ hold_guard_ms }},
  mode: {{ mode | tojson }}, sweepMs: 50,
};
let vmin = CFG.vmin, vmax = CFG.vmax, unit = CFG.unit;
const now = () => Date.now() / 1000;

let heldValue = null;          // current parked value (set only while a window is open)
let valSamples = [], anchors = [], buffer = [];
// holds windows-only model: each click opens a FIXED sampling window of
// holdWindowMs; only those windows are the reference. {t0, t1|null, v, row}.
let windows = [], activeWindow = null;
let plotT1 = now();
function logRow(kind, value){
  if (activeRun === null) return;
  buffer.push({run: activeRun, t: now(), kind, value});
}

// --- holds (states) ---
const statesEl = document.getElementById('states');
const trimNum = x => Number(x.toFixed(4)).toString();
function buildStates(values){
  statesEl.innerHTML = '';
  values.forEach((v, i) => {
    const row = document.createElement('div');
    row.className = 'staterow';
    const key = i < 9 ? (i+1) : (i===9 ? 0 : '');
    row.innerHTML = '<input class="sval" type="number" step="any" value="'+trimNum(v)+'">'
      + '<span class="sunit">'+unit+'</span>'
      + '<span class="skey">'+(key!==''?('key '+key):'')+'</span>';
    row.querySelector('.sval').addEventListener('click', e => e.stopPropagation());
    row.addEventListener('click', () => selectState(row));
    statesEl.appendChild(row);
  });
}
const fmtVal = v => (Number.isInteger(v) ? String(v) : v.toFixed(2)) + ' ' + unit;
function closeWindow(){              // end the active sampling window
  if (!activeWindow) return;
  activeWindow.t1 = now();
  if (activeWindow.row) activeWindow.row.classList.remove('sampling');
  const v = activeWindow.v;
  heldValue = null;
  valSamples.push({t: now(), v: null});   // break the value line between windows
  activeWindow = null;
  const hh = document.getElementById('holdhint');
  if (hh){ hh.className = 'holdhint'; hh.textContent = '✓ captured ' + fmtVal(v) + '. Move to the next value'; }
}
function selectState(row){
  const v = parseFloat(row.querySelector('.sval').value);
  if (!isFinite(v)) return;
  if (!capturing){                  // sampling only makes sense while recording
    const hh = document.getElementById('holdhint');
    if (hh){ hh.className='holdhint'; hh.textContent="Click 'start capture' first"; }
    return;
  }
  closeWindow();                    // finish any window still open
  document.querySelectorAll('.staterow').forEach(r => r.classList.remove('sel','sampling'));
  row.classList.add('sampling');
  const t = now();
  heldValue = v;
  anchors.push({t, v}); logRow('anchor', v);    // tag the known value
  valSamples.push({t, v}); logRow('value', v);  // seed the dense hold window
  activeWindow = {t0: t, t1: null, v, row};
  windows.push(activeWindow);
  if (window.__holdTimer) clearTimeout(window.__holdTimer);
  window.__holdTimer = setTimeout(closeWindow, CFG.holdWindowMs);
}

// --- keyboard: digit keys select a state ---
window.addEventListener('keydown', e => {
  if ((e.target.tagName||'').toLowerCase() === 'input') return;
  if (activeMode !== 'holds') return;
  if (/^Digit[0-9]$/.test(e.code)){
    const rows = document.querySelectorAll('.staterow');
    const d = +e.code.slice(5); const idx = d === 0 ? 9 : d - 1;
    if (idx < rows.length) { selectState(rows[idx]); e.preventDefault(); }
  }
});

// --- HEARTBEAT: re-log the held value densely while a window is open ---
setInterval(() => {
  if (!capturing) return;
  if (heldValue !== null && activeWindow
      && (now() - activeWindow.t0) * 1000 >= CFG.holdGuardMs) {
    valSamples.push({t: now(), v: heldValue}); logRow('value', heldValue); }
}, CFG.heartbeatMs);

// --- capture Start/Stop ---
let capturing = false;
let activeRun = null;
let stopPending = false;
let stopRun = null;
let batchSeq = 0;
let eventSeq = 0;
const CLIENT_NAMESPACE = (globalThis.crypto && crypto.randomUUID)
  ? crypto.randomUUID()
  : (Date.now().toString(36) + '-' + Math.random().toString(36).slice(2));
const capbtn = document.getElementById('capbtn');
const capstatus = document.getElementById('capstatus');
const fmtTime = s => { s=Math.floor(s); return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0'); };
const dot = document.getElementById('dot');
function applyCapState(s){
  const serverRunning = !!s.running;
  capturing = stopPending ? false : serverRunning;
  if (!stopPending && serverRunning && s.run) activeRun = s.run;
  const mh = document.getElementById('mode-holds'), ms = document.getElementById('mode-sweep');
  if (mh) mh.disabled = capturing; if (ms) ms.disabled = capturing;
  document.getElementById('experiment-note').disabled = capturing || stopPending;
  document.getElementById('event-note').disabled = !capturing;
  document.querySelectorAll('.phasebtn').forEach(b => b.disabled = !capturing);
  capbtn.textContent = capturing ? 'stop capture' : stopPending ? 'retry stop' : 'start capture';
  capbtn.className = (capturing || stopPending) ? 'stop' : 'start';
  const runtxt = s.run ? ('run ' + s.run + ' · ') : '';
  if (s.error){ capstatus.textContent = '⚠ ' + s.error; capstatus.className = 'err'; dot.className = 'bad'; }
  else if (capturing){ const e = s.errors ? ' · ' + s.errors.toLocaleString() + ' err' : '';
    capstatus.textContent = '● ' + runtxt + 'capturing ' + fmtTime(s.elapsed||0) + ' · ' + (s.frames||0).toLocaleString() + ' frames' + e;
    capstatus.className = s.errors ? 'err' : 'live'; dot.className = s.errors ? 'bad' : 'ok'; }
  else if (s.frames){ capstatus.textContent = runtxt + 'stopped · ' + s.frames.toLocaleString() + ' frames'; capstatus.className = ''; }
  else { capstatus.textContent = 'idle'; capstatus.className = ''; }
}
async function loadDevice(){
  try {
    const d = await (await fetch('/api/device')).json();
    if (!d.ok){ document.getElementById('dev').textContent = d.error || 'unavailable';
      document.getElementById('rate').textContent = '-'; dot.className = 'bad'; return; }
    const dev = document.getElementById('dev');
    dev.textContent = d.channel; document.getElementById('rate').textContent = d.bitrate;
    const prof = document.getElementById('profile');
    const mode = d.listen_only ? 'silent' : 'ACK';
    let ptxt = (d.profile ? d.profile + ' · ' : '') + mode;
    if (d.bus && d.bus.ok) ptxt += '  ·  bus ' + d.bus.state + ' ' + (d.bus.frame_rate||0) + ' fps';
    prof.textContent = ptxt;
    prof.className = 'val' + (d.bus && d.bus.ok && !d.bus.healthy ? ' errtext' : '');
    if (!capturing) dot.className = d.reachable ? 'ok' : 'bad';
  } catch(e){ document.getElementById('dev').textContent = 'error'; dot.className = 'bad'; }
}
loadDevice();
let busy = false;
const REQUEST_TIMEOUT_MS = 3000;
async function requestJson(url, payload, timeoutMs=REQUEST_TIMEOUT_MS){
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), timeoutMs);
  try {
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload), signal: abort.signal});
    const result = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(result.error || ('HTTP ' + r.status));
    return result;
  } catch(e){
    if (e.name === 'AbortError') throw new Error('request timed out; data retained for retry');
    throw e;
  } finally { clearTimeout(timer); }
}
async function toggleCapture(){
  if (busy) return; busy = true; capbtn.disabled = true;
  const wasCapturing = capturing;
  const stopping = capturing || stopPending;
  capstatus.textContent = stopping ? 'stopping…' : 'connecting…'; capstatus.className = '';
  try {
    let result;
    if (stopping){
      // Freeze reference production, wait for any in-flight flush, then hand the
      // retained idempotent batches/events to Stop before capture teardown.
      if (!stopPending) stopRun = activeRun;
      capturing = false;
      stopPending = true;
      closeWindow();
      await settleEventRequests();
      await flushRows();
      queueBufferedRows();
      const batches = ownedPending(pendingBatches, stopRun, 'reference batch');
      const events = ownedPending(pendingEvents, stopRun, 'event marker');
      result = await requestJson('/api/capture/stop', {run: stopRun, batches, events});
      requireAcknowledged(result.accepted_batch_ids, batches.map(b => b.id), 'reference batch');
      requireAcknowledged(result.accepted_event_ids, events.map(e => e.id), 'event marker');
      pendingBatches.clear(); pendingEvents.clear();
      stopPending = false;
      stopRun = null;
      activeRun = null;
    } else {
      result = await requestJson('/api/capture/start', {note: activeMode, mode: activeMode,
        experiment_note: document.getElementById('experiment-note').value.trim()});
      if (!result.running || !result.run) throw new Error(result.error || 'capture did not start');
      activeRun = result.run; batchSeq = 0; eventSeq = 0;
      pendingBatches.clear(); pendingEvents.clear(); stopPending = false; stopRun = null;
    }
    applyCapState(result);
    if (capturing && !wasCapturing){          // just started a fresh run
      windows = []; activeWindow = null; heldValue = null; valSamples = []; anchors = [];
      document.querySelectorAll('.staterow').forEach(r=>r.classList.remove('sel','sampling'));
    }
    if (!capturing){                          // just stopped
      document.querySelectorAll('.staterow').forEach(r=>r.classList.remove('sampling'));
    }
    loadRuns();
  } catch(e){
    capstatus.textContent = '⚠ ' + e + (stopping ? ' · retry Stop' : '');
    capstatus.className = 'err';
    if (stopping) stopPending = true;
  }
  finally { busy = false; capbtn.disabled = false; }
}
capbtn.addEventListener('click', toggleCapture);
setInterval(async () => { if (busy) return; try { const r = await fetch('/api/capture/status'); applyCapState(await r.json()); } catch(e){} }, 1000);

async function loadRuns(){
  try { const d = await (await fetch('/api/runs')).json();
    const el = document.getElementById('runs');
    if (!d.runs || !d.runs.length){ el.innerHTML = ''; return; }
    el.innerHTML = 'Runs: ' + d.runs.map(r =>
      `<b>${r.n}</b> ${r.note||''} ${r.status ? '['+r.status+'] ' : ''}`+
      `(${(r.frames||0).toLocaleString()} fr) → trace_${CFG.label}_${r.n}.csv`).join(' &nbsp;·&nbsp; ');
  } catch(e){}
}
loadRuns();

// Run-scoped, idempotent reference batches. Unacknowledged batches keep their ID
// across retries, including a final Stop retry after an ambiguous response.
let flushPromise = null;
const pendingBatches = new Map();
function queueBufferedRows(){
  if (!buffer.length) return;
  const queued = buffer; buffer = [];
  const rowsByRun = new Map();
  for (const row of queued){
    const run = row.run;
    if (!rowsByRun.has(run)) rowsByRun.set(run, []);
    rowsByRun.get(run).push({t: row.t, kind: row.kind, value: row.value});
  }
  for (const [run, rows] of rowsByRun){
    const id = String(run) + ':' + CLIENT_NAMESPACE + ':' + String(++batchSeq);
    pendingBatches.set(id, {id, run, rows});
  }
}
function requireAcknowledged(actual, expected, what){
  const accepted = new Set(actual || []);
  if (expected.some(id => !accepted.has(id))) throw new Error(what + ' was not acknowledged');
}
function ownedPending(items, run, what){
  const values = Array.from(items.values());
  if (run === null || values.some(item => item.run !== run))
    throw new Error(what + ' ownership does not match the pending Stop run');
  return values;
}
function flushRows(){
  if (flushPromise) return flushPromise;
  queueBufferedRows();
  const batch = pendingBatches.values().next().value;
  if (!batch) return Promise.resolve();
  flushPromise = requestJson('/api/log', {run: batch.run, batch})
    .then(result => {
      requireAcknowledged(result.accepted_batch_ids, [batch.id], 'reference batch');
      pendingBatches.delete(batch.id);
    })
    .catch(() => {}) // Retain the same batch ID; Stop or the next tick retries it.
    .finally(() => { flushPromise = null; });
  return flushPromise;
}
setInterval(() => { if (capturing) flushRows(); }, 150);

// Sparse maneuver chronology: mark transitions/deviations, not every sample.
const pendingEvents = new Map();
const eventRequests = new Set();
async function sendEvent(event){
  const result = await requestJson('/api/event', {run: event.run, event});
  requireAcknowledged(result.accepted_event_ids, [event.id], 'event marker');
  pendingEvents.delete(event.id);
}
async function settleEventRequests(){
  await Promise.allSettled(Array.from(eventRequests));
}
document.querySelectorAll('.phasebtn').forEach(button => {
  button.addEventListener('click', () => {
    if (!capturing) return;
    const phase = button.dataset.phase;
    const noteEl = document.getElementById('event-note');
    const run = activeRun;
    const id = String(run) + ':' + CLIENT_NAMESPACE + ':' + String(++eventSeq);
    const event = {id, run, epoch: now(), phase, note: noteEl.value.trim()};
    pendingEvents.set(id, event);
    const status = document.getElementById('markstatus');
    const request = sendEvent(event)
      .then(() => { noteEl.value = ''; status.textContent = '✓ ' + phase; })
      .catch(e => { status.textContent = '⚠ marker retained for Stop: ' + e; })
      .finally(() => eventRequests.delete(request));
    eventRequests.add(request);
  });
});

// --- uPlot ---
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n);
function vlines(getItems, color, label){
  return { hooks: { draw: u => {
    const ctx = u.ctx; ctx.save(); ctx.strokeStyle = color(); ctx.fillStyle = color();
    ctx.lineWidth = 1.5; ctx.font = '11px system-ui';
    for (const it of getItems()) {
      const xr = (typeof it === 'object' ? it.t : it) - plotT1;
      if (xr < u.scales.x.min || xr > u.scales.x.max) continue;
      const x = u.valToPos(xr, 'x', true);
      ctx.beginPath(); ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top+u.bbox.height); ctx.stroke();
      if (label && typeof it === 'object') ctx.fillText(String(it.v), x+2, u.bbox.top+11);
    }
    ctx.restore();
  }}};
}
// shade the FIXED sampling windows (holds windows-only) so the user sees exactly
// what time-spans were sampled (green) vs the unsampled transitions (white).
function windowBands(getWins){
  return { hooks: { draw: u => {
    const ctx = u.ctx; ctx.save(); ctx.font = '11px system-ui';
    for (const w of getWins()){
      const x0r = w.t0 - plotT1, x1r = (w.t1 || now()) - plotT1;
      if (x1r < u.scales.x.min || x0r > u.scales.x.max) continue;
      const xa = u.valToPos(Math.max(x0r, u.scales.x.min), 'x', true);
      const xb = u.valToPos(Math.min(x1r, u.scales.x.max), 'x', true);
      ctx.fillStyle = w.t1 ? 'rgba(16,203,169,0.16)' : 'rgba(16,203,169,0.30)';
      ctx.fillRect(xa, u.bbox.top, Math.max(1, xb - xa), u.bbox.height);
      ctx.fillStyle = cssVar('--green');
      ctx.fillText(String(w.v), xa + 2, u.bbox.top + 11);
    }
    ctx.restore();
  }}};
}
const WINDOW_S = 30;
const opts = {
  width: document.getElementById('plot').clientWidth - 16, height: 0.30*window.innerHeight - 40,
  scales: { x: { time: false }, y: { range: () => [vmin, vmax] } },
  series: [ {label:'t'}, {label:'value', stroke:'#3d85c6', width:2, paths: uPlot.paths.stepped({align:1})} ],
  axes: [ {stroke:'#888', grid:{stroke:'#e6e8ec'}}, {stroke:'#888', grid:{stroke:'#e6e8ec'}} ],
  plugins: [windowBands(() => windows),
            vlines(() => anchors, () => cssVar('--green'), true)],
};
const u = new uPlot(opts, [[0],[null]], document.getElementById('plot'));
window.addEventListener('resize', () => u.setSize({width: document.getElementById('plot').clientWidth-16, height: 0.30*window.innerHeight-40}));
setInterval(() => {
  const t1 = now(), t0 = t1 - WINDOW_S; plotT1 = t1;
  valSamples = valSamples.filter(s => s.t >= t0 - 1);
  anchors = anchors.filter(a => a.t >= t0 - 1);
  windows = windows.filter(w => (w.t1 || t1) >= t0 - 1);
  const xs = [], ys = [];
  for (const s of valSamples) { xs.push(s.t - t1); ys.push(s.v); }
  const last = valSamples.length ? valSamples[valSamples.length-1].v : null;
  xs.push(0); ys.push(last);
  u.setData([xs, ys]); u.setScale('x', {min: -WINDOW_S, max: 0});
  // live hint: countdown while sampling, waiting-for-first-park otherwise
  const hh = document.getElementById('holdhint');
  if (hh){
    if (activeWindow){
      const rem = Math.max(0, CFG.holdWindowMs/1000 - (t1 - activeWindow.t0));
      hh.className = 'holdhint live';
      hh.textContent = '● sampling ' + fmtVal(activeWindow.v) + ' … hold still (' + rem.toFixed(1) + 's)';
    } else if (capturing && windows.length === 0){
      hh.className = 'holdhint live';
      hh.textContent = '● Capturing. Park at your first value and click it to sample';
    } else if (!capturing){
      hh.className = 'holdhint';
      hh.textContent = '';
    }
  }
}, 100);

buildStates(CFG.stateValues);

// --- mode toggle (Holds anchors | Sweep slider) ---
// default to SWEEP when both are offered: the sweep run is the one to record FIRST
// (identify the field), so the user lands on the tab they need before run 1.
let activeMode = (CFG.mode === 'both') ? 'sweep' : CFG.mode;
const holdsPanel = document.getElementById('holdspanel');
const sweepPanel = document.getElementById('sweeppanel');
function applyMode(){
  const sweep = activeMode === 'sweep';
  holdsPanel.style.display = sweep ? 'none' : '';
  sweepPanel.style.display = sweep ? '' : 'none';
  document.getElementById('mode-holds').classList.toggle('sel', !sweep);
  document.getElementById('mode-sweep').classList.toggle('sel', sweep);
}
function setMode(m){ if (capturing) return; activeMode = m; applyMode(); }  // no switch mid-run
if (CFG.mode === 'both'){
  document.getElementById('modebar').style.display = '';
  document.getElementById('mode-holds').addEventListener('click', () => setMode('holds'));
  document.getElementById('mode-sweep').addEventListener('click', () => setMode('sweep'));
}
applyMode();

// --- sweep (continuous slider): dense kind="value" rows, NO anchors ---
let sweepValue = null;
const sweepEl = document.getElementById('sweep');
sweepEl.min = vmin; sweepEl.max = vmax; sweepEl.step = ((vmax - vmin) / 1000) || 0.001;
sweepEl.value = (vmin + vmax) / 2; sweepValue = parseFloat(sweepEl.value);
document.getElementById('sweepunit').textContent = unit;
// explicit range endpoints around the vertical slider (top = max, bottom = min)
const fmtEnd = v => (Number.isInteger(v) ? String(v) : v.toFixed(2)) + ' ' + unit;
document.getElementById('sweepmax').textContent = fmtEnd(vmax);
document.getElementById('sweepmin').textContent = fmtEnd(vmin);
function showSweep(){
  const el = document.getElementById('sweepval');
  el.textContent = Number.isInteger(sweepValue) ? String(sweepValue) : sweepValue.toFixed(2);
}
showSweep();
sweepEl.addEventListener('input', () => {
  sweepValue = parseFloat(sweepEl.value); showSweep();
  if (capturing && activeMode === 'sweep'){
    valSamples.push({t: now(), v: sweepValue}); logRow('value', sweepValue); }
});
// dense heartbeat: log the current slider value at ~20 Hz so plateaus stay sampled
setInterval(() => {
  if (!capturing || activeMode !== 'sweep' || sweepValue === null) return;
  valSamples.push({t: now(), v: sweepValue}); logRow('value', sweepValue);
}, CFG.sweepMs);

window.__debug = () => ({heldValue, anchors: anchors.length,
  valCount: valSamples.length, windows: windows.length,
  states: document.querySelectorAll('.staterow').length});
</script></body></html>"""


class RunConflictError(ValueError):
    """A request belongs to a run other than the active capture."""


class CaptureController:
    """Start/stop in-process CANsub captures as NUMBERED per-run file pairs.

    Each start() opens a fresh trace_<label>_<N>.csv and a fresh
    sidecar_<label>_<N>.csv, so trace and sidecar always match and earlier runs
    are never overwritten or mixed, including after a restart. Sidecar rows are
    routed to the CURRENT run only while running. The silent/ACK mode comes from
    bus.json unless mode_override forces it.
    """

    EVENT_PHASES = frozenset({"baseline", "stimulus", "return", "unexpected"})

    def __init__(self, label: str, outdir: Path, mode_override: bool | None = None,
                 reference_settings: dict | None = None):
        self.label = label
        self.outdir = Path(outdir)
        self.mode_override = mode_override
        self.reference_settings = dict(reference_settings or {})
        self.listen_only = None
        self.run_n = 0
        self.note = ""
        self.current_trace: Path | None = None
        self.current_sidecar: Path | None = None
        self.runs: list[dict] = self._load_index()
        self._lock = threading.Lock()
        self._filelock = threading.Lock()
        self.bus = self.notifier = self.logger = self.counter = None
        self.started_at = None
        self.error = None
        self._accepted_batches: dict[int, dict[str, str]] = {}
        self._accepted_events: dict[int, dict[str, str]] = {}
        self._final_persisted: dict[int, bool] = {
            self._record_run(record): True for record in self.runs
        }
        self.run_n = max(self._max_index_run(), self._max_artifact_run())
        stale = False
        for record in self.runs:
            if record.get("status") == "running":
                record["status"] = "incomplete"
                stale = True
        if stale:
            self._write_index()

    @property
    def running(self) -> bool:
        return self.notifier is not None

    def start(self, note: str = "", *, mode: str | None = None,
              experiment_note: str = "") -> dict:
        with self._lock:
            if self.notifier is not None:
                return self._status()
            self.error = None
            try:
                cfg = capture.resolve_config()
                self.listen_only = (self.mode_override if self.mode_override is not None
                                    else cfg["listen_only"])
                timing = common.make_timing(cfg["nominal"], cfg["data"], cfg["sample_point"])
                self.run_n = max(self.run_n, self._max_artifact_run()) + 1
                self.note = note or ""
                self.outdir.mkdir(parents=True, exist_ok=True)
                self.current_trace = self.outdir / f"trace_{self.label}_{self.run_n}.csv"
                self.current_sidecar = self.outdir / f"sidecar_{self.label}_{self.run_n}.csv"
                with self.current_sidecar.open("x", encoding="utf-8") as sidecar:
                    sidecar.write("epoch;kind;label;value\n")
                # Persist the boundary before opening the bus, so even backend-queued
                # frames cannot predate the recorded run start.
                self.started_at = time.time()
                record = {
                    "n": self.run_n,
                    "note": self.note,
                    "frames": 0,
                    "trace": str(self.current_trace),
                    "sidecar": str(self.current_sidecar),
                    "mode": mode or self.note,
                    "status": "running",
                    "started_at": self.started_at,
                    "ended_at": None,
                    "duration_s": None,
                    "errors": 0,
                    "reference_settings": dict(self.reference_settings),
                    "events": [],
                }
                clean_note = str(experiment_note or "").strip()
                if clean_note:
                    record["experiment_note"] = clean_note[:200]
                self._replace_run(record)
                self._write_index()
                self._final_persisted[self.run_n] = False
                self._accepted_batches[self.run_n] = {}
                self._accepted_events[self.run_n] = {}
                self.bus = can.Bus(interface=cfg["interface"], channel=cfg["channel"],
                                   timing=timing, listen_only=self.listen_only,
                                   error_frames=True)
                self.logger = can.Logger(str(self.current_trace))
                self.counter = capture._Counter()
                self.notifier = can.Notifier(
                    [self.bus], [capture._DataOnly(self.logger), self.counter])
            except Exception as exc:  # noqa: BLE001 - surface to the UI
                teardown_errors = self._teardown()
                self.error = self._error_text(exc, teardown_errors)
                record = self._current_record()
                if record is not None and record.get("status") == "running":
                    record["status"] = "incomplete"
                    record["error"] = self.error
                    self._write_index()
                    self._final_persisted[self.run_n] = True
            return self._status()

    def stop(self, run_id: int | None = None, note: str = "", *,
             batches: list[dict] | None = None,
             events: list[dict] | None = None) -> dict:
        """Finalize one run after accepting retry-safe final batches and events."""
        with self._lock:
            if run_id is None and not self.running:
                return self._status()
            requested_run = self.run_n if run_id is None else self._run_id(run_id)
            record = self._record_for_run(requested_run)
            finalized_retry = (
                record is not None
                and record.get("status") in {"complete", "incomplete"}
                and requested_run in self._accepted_batches
            )
            if finalized_retry and (not self.running or requested_run != self.run_n):
                accepted_batches = self._confirm_batches(requested_run, batches or [])
                accepted_events = self._confirm_events(requested_run, events or [])
                if not self._final_persisted.get(requested_run, False):
                    self._write_index()
                    self._final_persisted[requested_run] = True
                return self._record_status(record, accepted_batches, accepted_events)
            if not self.running:
                raise RunConflictError(f"run {requested_run} is not active")
            self._require_active_run(requested_run)
            if note:
                self.note = note
            accepted_batches = [
                self._accept_batch(requested_run, batch)[0] for batch in (batches or [])
            ]
            accepted_events = [
                self._accept_event(requested_run, event)[0] for event in (events or [])
            ]
            rec = self._current_record()
            if rec is None and self.current_trace is not None and self.run_n > 0:
                rec = {"n": self.run_n, "note": self.note, "frames": 0,
                       "trace": str(self.current_trace), "sidecar": str(self.current_sidecar),
                       "mode": self.note, "status": "running",
                       "started_at": self.started_at, "ended_at": None,
                       "duration_s": None, "errors": 0,
                       "reference_settings": dict(self.reference_settings), "events": []}
            started_at = self.started_at
            # Notifier.stop() drains/stops delivery; close the logger and bus before
            # sampling counters and the final boundary so metadata brackets the trace.
            teardown_errors = self._teardown()
            frames = self.counter.n if self.counter else 0
            errors = self.counter.errors if self.counter else 0
            ended_at = time.time()
            self.error = self._error_text(None, teardown_errors) or None
            if rec is not None:
                rec.update({
                    "note": self.note,
                    "frames": frames,
                    "status": "incomplete" if teardown_errors else "complete",
                    "ended_at": ended_at,
                    "duration_s": max(0.0, ended_at - started_at)
                    if started_at is not None else None,
                    "errors": errors,
                })
                if self.error:
                    rec["error"] = self.error
                else:
                    rec.pop("error", None)
            if rec is not None:
                self._replace_run(rec)
                self._final_persisted[requested_run] = False
                self._write_index()
                self._final_persisted[requested_run] = True
                return self._record_status(rec, accepted_batches, accepted_events)
            status = self._status()
            status.update(accepted_batch_ids=accepted_batches,
                          accepted_event_ids=accepted_events)
            return status

    def log_batch(self, run_id: int, batch: dict) -> dict:
        """Append one run-scoped batch exactly once and return its acknowledgement."""
        with self._lock:
            run_id = self._run_id(run_id)
            self._require_active_run(run_id)
            batch_id, count = self._accept_batch(run_id, batch)
            return {"ok": True, "n": count, "accepted_batch_ids": [batch_id]}

    def add_event(self, run_id: int, event: dict) -> str:
        """Persist one run-scoped, retry-safe maneuver marker."""
        with self._lock:
            run_id = self._run_id(run_id)
            self._require_active_run(run_id)
            event_id, _ = self._accept_event(run_id, event)
            self._write_index()
            return event_id

    def _accept_event(self, run_id: int, event: dict) -> tuple[str, bool]:
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        event_id = self._item_id(event, "event", run_id)
        phase = event.get("phase", "")
        if phase not in self.EVENT_PHASES:
            raise ValueError(f"phase must be one of: {', '.join(sorted(self.EVENT_PHASES))}")
        epoch = float(event.get("epoch"))
        if not math.isfinite(epoch):
            raise ValueError("event epoch must be finite")
        stored = {"epoch": epoch, "phase": phase}
        clean_note = str(event.get("note", "") or "").strip()
        if clean_note:
            stored["note"] = clean_note[:200]
        digest = self._digest(stored)
        accepted = self._accepted_events.setdefault(run_id, {})
        if event_id in accepted:
            if accepted[event_id] != digest:
                raise RunConflictError(f"event id {event_id!r} was reused with different data")
            return event_id, True
        record = self._current_record()
        if record is None:
            raise RunConflictError(f"run {run_id} has no active record")
        record.setdefault("events", []).append(stored)
        record["events"].sort(key=lambda item: item["epoch"])
        accepted[event_id] = digest
        return event_id, False

    def _accept_batch(self, run_id: int, batch: dict) -> tuple[str, int]:
        if not isinstance(batch, dict):
            raise ValueError("batch must be an object")
        batch_id = self._item_id(batch, "batch", run_id)
        rows = batch.get("rows", [])
        payload, count = self._render_rows(rows)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        accepted = self._accepted_batches.setdefault(run_id, {})
        if batch_id in accepted:
            if accepted[batch_id] != digest:
                raise RunConflictError(f"batch id {batch_id!r} was reused with different rows")
            return batch_id, count
        self._append_payload(payload)
        accepted[batch_id] = digest
        return batch_id, count

    def _render_rows(self, rows: list[dict]) -> tuple[str, int]:
        if not isinstance(rows, list):
            raise ValueError("batch rows must be a list")
        rendered = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("sidecar row must be an object")
            epoch = float(row["t"])
            if not math.isfinite(epoch):
                raise ValueError("sidecar epoch must be finite")
            kind = str(row["kind"])
            if ";" in kind or "\n" in kind or "\r" in kind:
                raise ValueError("sidecar kind contains a delimiter")
            value = str(row.get("value", ""))
            if ";" in value or "\n" in value or "\r" in value:
                raise ValueError("sidecar value contains a delimiter")
            rendered.append(f"{epoch:.6f};{kind};{self.label};{value}\n")
        return "".join(rendered), len(rows)

    def _append_payload(self, payload: str) -> None:
        """Append one validated batch, rolling back any partial write on failure."""
        if not payload:
            return
        if self.current_sidecar is None:
            raise RunConflictError("no active sidecar")
        with self._filelock, open(self.current_sidecar, "a+", encoding="utf-8") as sidecar:
            sidecar.seek(0, 2)
            start = sidecar.tell()
            try:
                written = sidecar.write(payload)
                if written != len(payload):
                    raise OSError("partial sidecar write")
                sidecar.flush()
            except Exception:
                sidecar.seek(start)
                sidecar.truncate()
                sidecar.flush()
                raise

    @staticmethod
    def _digest(value: dict) -> str:
        document = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(document.encode("utf-8")).hexdigest()

    @staticmethod
    def _item_id(item: dict, kind: str, run_id: int) -> str:
        item_id = str(item.get("id", "")).strip()
        if not item_id or len(item_id) > 100:
            raise ValueError(f"{kind} id must be 1..100 characters")
        try:
            item_run = CaptureController._run_id(item.get("run"))
        except ValueError as exc:
            raise RunConflictError(f"{kind} has no valid immutable run owner") from exc
        if item_run != run_id:
            raise RunConflictError(
                f"{kind} belongs to run {item_run}, not submitted run {run_id}")
        match = re.fullmatch(
            r"(?P<run>[1-9][0-9]*):"
            r"(?P<namespace>[A-Za-z0-9][A-Za-z0-9._-]{0,63}):"
            r"(?P<sequence>[1-9][0-9]*)",
            item_id,
        )
        if match is None:
            raise ValueError(
                f"{kind} id must use run:page-namespace:sequence")
        if int(match.group("run")) != run_id:
            raise RunConflictError(
                f"{kind} id {item_id!r} does not belong to run {run_id}")
        return item_id

    @staticmethod
    def _run_id(run_id: int) -> int:
        if isinstance(run_id, bool) or not (
                isinstance(run_id, int)
                or (isinstance(run_id, str) and run_id.isdigit())):
            raise ValueError("run must be an integer")
        value = int(run_id)
        if value <= 0:
            raise ValueError("run must be positive")
        return value

    def _require_active_run(self, run_id: int) -> None:
        if not self.running or run_id != self.run_n:
            active = self.run_n if self.running else "none"
            raise RunConflictError(f"run {run_id} is not active (active run: {active})")

    def _confirm_batches(self, run_id: int, batches: list[dict]) -> list[str]:
        accepted = self._accepted_batches.get(run_id, {})
        confirmed = []
        for batch in batches:
            batch_id = self._item_id(batch, "batch", run_id)
            payload, _ = self._render_rows(batch.get("rows", []))
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if accepted.get(batch_id) != digest:
                raise RunConflictError(
                    f"run {run_id} is complete and batch {batch_id!r} was not accepted")
            confirmed.append(batch_id)
        return confirmed

    def _confirm_events(self, run_id: int, events: list[dict]) -> list[str]:
        accepted = self._accepted_events.get(run_id, {})
        confirmed = []
        for event in events:
            event_id = self._item_id(event, "event", run_id)
            stored = {"epoch": float(event.get("epoch")), "phase": event.get("phase", "")}
            clean_note = str(event.get("note", "") or "").strip()
            if clean_note:
                stored["note"] = clean_note[:200]
            if accepted.get(event_id) != self._digest(stored):
                raise RunConflictError(
                    f"run {run_id} is complete and event {event_id!r} was not accepted")
            confirmed.append(event_id)
        return confirmed

    def status(self) -> dict:
        with self._lock:
            return self._status()

    def _write_index(self) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        idx = self._index_path()
        tmp = idx.with_suffix(idx.suffix + ".tmp")
        tmp.write_text(json.dumps(sorted(self.runs, key=self._record_run), indent=2),
                       encoding="utf-8")
        tmp.replace(idx)

    def _index_path(self) -> Path:
        return self.outdir / f"runs_{self.label}.json"

    @staticmethod
    def _record_run(record: dict) -> int:
        try:
            return int(record.get("n", 0))
        except (TypeError, ValueError):
            return 0

    def _load_index(self) -> list[dict]:
        idx = self._index_path()
        if not idx.exists():
            return []
        document = json.loads(idx.read_text(encoding="utf-8"))
        if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
            raise ValueError(f"run index must be a JSON list of records: {idx}")
        return document

    def _max_index_run(self) -> int:
        return max((self._record_run(record) for record in self.runs), default=0)

    def _max_artifact_run(self) -> int:
        if not self.outdir.is_dir():
            return 0
        pattern = re.compile(
            rf"^(?:trace|sidecar)_{re.escape(self.label)}_(\d+)\.csv$")
        return max(
            (int(match.group(1)) for path in self.outdir.iterdir()
             if (match := pattern.match(path.name))),
            default=0,
        )

    def _current_record(self) -> dict | None:
        return self._record_for_run(self.run_n)

    def _record_for_run(self, run_id: int) -> dict | None:
        return next(
            (record for record in self.runs if self._record_run(record) == run_id),
            None,
        )

    def _record_status(self, record: dict, accepted_batches: list[str],
                       accepted_events: list[str]) -> dict:
        return {
            "running": False,
            "run": self._record_run(record),
            "status": record.get("status"),
            "frames": int(record.get("frames", 0) or 0),
            "errors": int(record.get("errors", 0) or 0),
            "listen_only": self.listen_only,
            "elapsed": round(float(record.get("duration_s", 0) or 0), 1),
            "trace": str(record.get("trace", "")),
            "error": record.get("error"),
            "accepted_batch_ids": accepted_batches,
            "accepted_event_ids": accepted_events,
        }

    def _replace_run(self, record: dict) -> None:
        number = self._record_run(record)
        self.runs = [item for item in self.runs if self._record_run(item) != number]
        self.runs.append(record)

    @staticmethod
    def _error_text(error: Exception | None, teardown_errors: list[str]) -> str:
        parts = ([str(error)] if error is not None else []) + teardown_errors
        return "; ".join(part for part in parts if part)

    def _teardown(self) -> list[str]:
        errors = []
        for obj, meth in ((self.notifier, "stop"), (self.logger, "stop"),
                          (self.bus, "shutdown")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception as exc:  # noqa: BLE001 - complete remaining teardown
                    errors.append(f"{type(obj).__name__}.{meth}: {exc}")
        self.notifier = self.logger = self.bus = None
        self.started_at = None
        return errors

    def _status(self) -> dict:
        frames = self.counter.n if self.counter else 0
        errors = self.counter.errors if self.counter else 0
        elapsed = (time.time() - self.started_at) if self.started_at else 0.0
        return {"running": self.running, "run": self.run_n, "frames": frames,
                "errors": errors, "listen_only": self.listen_only,
                "elapsed": round(elapsed, 1),
                "trace": str(self.current_trace) if self.current_trace else "",
                "error": self.error}


def _fmt_rate(hz: int) -> str:
    return f"{hz/1e6:g} Mbit/s" if hz >= 1e6 else f"{hz/1e3:g} kbit/s"


def device_info() -> dict:
    """Resolve device + bit-rate (bus.json) + mDNS reachability for the UI."""
    try:
        cfg = capture.resolve_config()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    nominal, data = cfg["nominal"], cfg["data"]
    bitrate = _fmt_rate(nominal) + (f"  /  {_fmt_rate(data)} data" if data != nominal else "")
    reachable = False
    try:
        reachable = any(c["channel"] == cfg["channel"] for c in common.detect_configs())
    except Exception:
        pass
    status = common.device_status(cfg["channel"])
    healthy, warns = common.summarize_health(status, profile=cfg.get("profile"))
    bus = {"ok": bool(status.get("ok")), "state": status.get("state"),
           "frame_rate": status.get("frame_rate"), "healthy": healthy,
           "warn": warns[0] if warns else ""}
    return {"ok": True, "channel": cfg["channel"], "interface": cfg["interface"],
            "nominal": nominal, "data": data, "bitrate": bitrate,
            "profile": cfg.get("profile"), "listen_only": cfg.get("listen_only"),
            "reachable": reachable, "bus": bus}


def _port_in_use(host: str, port: int) -> bool:
    """True if something is already LISTENING on host:port.

    Uses a connect probe rather than a bind probe on purpose: on Windows the dev
    server binds with SO_REUSEADDR, which lets multiple processes share the exact
    same address - so a fresh launch silently 'succeeds' while a STALE Reference
    Generator from an earlier session keeps serving (and shadows the new one with
    old code). A successful connect is the reliable cross-platform signal that a
    server is already there.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def default_instructions(window: float, mode: str = "both") -> str:
    holds = (f"HOLDS: settle the signal at a value below, then click it (or press its digit) to "
             f"sample {window:g}s as that known value; repeat for each value.")
    sweep = ("SWEEP: drag the vertical slider to track the signal smoothly across its whole range "
             "- a few slow full sweeps.")
    if mode == "holds":
        return "Click 'start capture'. " + holds
    if mode == "sweep":
        return "Click 'start capture'. " + sweep
    return ("Capture two runs:\n"
            "1) Sweep tab: Align signal & slider value. Start capture. Drag the slider to track "
            "the signal smoothly across its whole range. Repeat for a few sweeps. Stop capture.\n"
            "2) Anchors tab: Align signal & anchor value. Start capture. Click the anchor button "
            f"to sample {window:g}s of data. Align signal to new anchor value and click. Repeat. "
            "Stop capture.")


def make_app(controller: CaptureController, label: str, unit: str,
             vmin: float, vmax: float, nstates: int, state_values: list[float],
             instructions: str, hold_window: float, hold_guard: float,
             mode: str) -> Flask:
    app = Flask(__name__, static_folder=ASSETS, static_url_path="/static")

    @app.get("/")
    def index():
        return render_template_string(
            PAGE, label=label, unit=unit, vmin=vmin, vmax=vmax,
            nstates=nstates, state_values=state_values, instructions=instructions,
            hold_window=f"{hold_window:g}", hold_window_ms=int(hold_window * 1000),
            hold_guard_ms=int(hold_guard * 1000), mode=mode)

    @app.post("/api/log")
    def log():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(controller.log_batch(data.get("run"), data.get("batch")))
        except RunConflictError as exc:
            return jsonify(ok=False, error=str(exc)), 409
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify(ok=False, error=str(exc)), 400

    @app.post("/api/capture/start")
    def cap_start():
        data = request.get_json(silent=True) or {}
        note = data.get("note", "")
        return jsonify(controller.start(
            note,
            mode=data.get("mode") or note,
            experiment_note=data.get("experiment_note", ""),
        ))

    @app.post("/api/capture/stop")
    def cap_stop():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(controller.stop(
                data.get("run"), data.get("note", ""),
                batches=data.get("batches", []), events=data.get("events", [])))
        except RunConflictError as exc:
            return jsonify(ok=False, error=str(exc)), 409
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify(ok=False, error=str(exc)), 400

    @app.post("/api/event")
    def event():
        data = request.get_json(silent=True) or {}
        try:
            event_id = controller.add_event(data.get("run"), data.get("event"))
        except RunConflictError as exc:
            return jsonify(ok=False, error=str(exc)), 409
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify(ok=False, error=str(exc)), 400
        return jsonify(ok=True, accepted_event_ids=[event_id])

    @app.get("/api/capture/status")
    def cap_status():
        return jsonify(controller.status())

    @app.get("/api/runs")
    def runs():
        return jsonify(runs=sorted(controller.runs, key=controller._record_run))

    @app.get("/api/device")
    def device():
        return jsonify(device_info())

    return app


def _state_values(args) -> tuple[int, list[float]]:
    if args.state_values:
        vals = [float(x) for x in args.state_values.split(",") if x.strip() != ""]
        return len(vals), vals
    n = max(2, min(10, args.states))
    if n == 1:
        return 1, [args.vmin]
    step = (args.vmax - args.vmin) / (n - 1)
    return n, [round(args.vmin + i * step, 4) for i in range(n)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="run")
    ap.add_argument("--outdir", default="temp-output", help="where per-run files go")
    ap.add_argument("--mode", choices=["holds", "sweep", "both"], default="both",
                    help="holds = steady-hold anchors only; sweep = continuous slider only; "
                         "both = UI toggle to record a Holds run AND a Sweep run (default)")
    ap.add_argument("--unit", default="%")
    ap.add_argument("--min", dest="vmin", type=float, default=0.0)
    ap.add_argument("--max", dest="vmax", type=float, default=100.0)
    ap.add_argument("--states", type=int, default=5, help="holds: number of known values")
    ap.add_argument("--state-values", help="holds: explicit comma list, e.g. \"-9.81,0,9.81\"")
    ap.add_argument("--window", type=float, default=2.0,
                    help="holds: seconds sampled per click (the fixed reference window; "
                         "settle BEFORE clicking). Default 2.0")
    ap.add_argument("--guard", type=float, default=0.0,
                    help="holds: seconds to skip at the start of each window (click jitter)")
    ap.add_argument("--instructions", help="override the on-screen instruction banner")
    mode_grp = ap.add_mutually_exclusive_group()
    mode_grp.add_argument("--listen-only", dest="busmode", action="store_const",
                          const="silent", help="force SILENT (no ACK) - multi-node bus")
    mode_grp.add_argument("--normal", dest="busmode", action="store_const",
                          const="ack", help="force NORMAL/ACK - single-node bench")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()

    if _port_in_use(args.host, args.port):
        print(f"[!] A server is already listening on {args.host}:{args.port} - probably a stale "
              f"Reference Generator from an earlier session.")
        print(f"    On Windows it would silently shadow this one with OLD code (no Sweep/Holds tabs, "
              f"etc.), so refusing to start.")
        print(f"    Fix: close that other terminal / kill the process, or relaunch with --port <N>.")
        return 2

    override = {"silent": True, "ack": False}.get(args.busmode)
    nstates, state_values = _state_values(args)
    instructions = args.instructions or default_instructions(args.window, args.mode)
    reference_settings = {
        "unit": args.unit,
        "min": args.vmin,
        "max": args.vmax,
        "state_values": state_values,
        "hold_window_s": args.window,
        "hold_guard_s": args.guard,
    }
    controller = CaptureController(
        args.label, Path(args.outdir), mode_override=override,
        reference_settings=reference_settings)
    app = make_app(controller, args.label, args.unit, args.vmin, args.vmax,
                   nstates, state_values, instructions, args.window, args.guard, args.mode)
    busmode = ("forced silent" if override is True else "forced ACK" if override is False
               else "from bus.json profile")
    print(f"Sync UI on http://{args.host}:{args.port}  (mode: {args.mode}, capture: {busmode})")
    print(f"Per-run files -> {args.outdir}/trace_{args.label}_<N>.csv + sidecar_{args.label}_<N>.csv")
    print("Each run is tagged holds/sweep with timestamped phase markers in the runs index. "
          "Click Start to record. Ctrl-C to stop.")
    try:
        app.run(host=args.host, port=args.port, threaded=True)
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
