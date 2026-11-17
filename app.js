const $ = (id) => document.getElementById(id);
const formatHours = (minutes) => (minutes / 60).toFixed(1);
const dayKey = (stamp) => new Date(stamp).toLocaleDateString("en-CA");
const sampleWorking = (sample) => sample.working ?? sample.mail_open;
const maxCarryMinutes = 4 * 60;
let selectedDayKey = dayKey(new Date());
let availableDayKeys = [];
let selectedWeekOffset = 0;
let averageWindowDays = 7;

async function readJson(response) {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch {
    if (response.status === 404) throw new Error("The running tracker is outdated. Quit and restart it (pkill -f '[p]ython3 tracker.py'), then reopen the dashboard.");
    throw new Error(`The tracker returned an unexpected response (HTTP ${response.status}).`);
  }
}

function durationMinutes(samples, index, interval) {
  const current = new Date(samples[index].timestamp).getTime();
  const next = samples[index + 1] ? new Date(samples[index + 1].timestamp).getTime() : current + interval * 1000;
  const gap = Math.max(0, next - current);
  const creditedGap = samples[index].manual_break ? gap : Math.min(gap, maxCarryMinutes * 60000);
  return creditedGap / 60000;
}

function renderStatus(payload) {
  const current = payload.current;
  $("mail-label").textContent = payload.mail_app;
  const signalAppInput = $("signal-app-name");
  if (signalAppInput && document.activeElement !== signalAppInput) signalAppInput.value = payload.mail_app;
  $("interval").textContent = payload.interval;
  $("mail-state").textContent = current.mail_open ? "Open" : "Closed";
  $("mail-state").style.color = current.mail_open ? "#fa896f" : "#9ca9a4";
  $("pause-button").textContent = payload.paused ? "Resume tracking" : "Pause tracking";
  $("pause-button").classList.toggle("paused", payload.paused);
  if (payload.paused) showPauseOverlay(); else hidePauseOverlay();
  $("state-note").textContent = payload.paused ? "Manual pause is active" : current.working ? `Counting time while ${payload.mail_app} is open` : `${payload.mail_app} is not open`;
  $("last-updated").textContent = `Updated ${new Date(current.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

function showPauseOverlay() {
  const overlay = $("pause-overlay");
  overlay.classList.add("visible");
  overlay.setAttribute("aria-hidden", "false");
}

function hidePauseOverlay() {
  const overlay = $("pause-overlay");
  overlay.classList.remove("visible");
  overlay.setAttribute("aria-hidden", "true");
}

function renderWorkingLine(timeline, samples, interval, chartStartHour, chartEndHour) {
  const startOfDay = new Date(samples[0].timestamp);
  startOfDay.setHours(0, 0, 0, 0);
  const chartStart = startOfDay.getTime() + chartStartHour * 60 * 60 * 1000;
  const chartLength = (chartEndHour - chartStartHour) * 60 * 60 * 1000;
  const segments = [];
  for (let index = 0; index < samples.length; index += 1) {
    const start = new Date(samples[index].timestamp).getTime();
    const end = start + durationMinutes(samples, index, interval) * 60000;
    const y = sampleWorking(samples[index]) ? 0 : 100;
    const startX = ((start - chartStart) / chartLength) * 100;
    const endX = ((end - chartStart) / chartLength) * 100;
    const previousSample = samples[index - 1];
    const previousStart = previousSample ? new Date(previousSample.timestamp).getTime() : start;
    const sameManualBreak = samples[index].manual_break_id && samples[index].manual_break_id === previousSample?.manual_break_id;
    const breakBefore = index > 0 && start - previousStart > maxCarryMinutes * 60000 && !sameManualBreak;
    const previous = segments[segments.length - 1];
    if (previous && !breakBefore && previous.y === y) {
      previous.end = endX;
    } else {
      segments.push({ start: startX, end: endX, y: y, breakBefore });
    }
  }
  if (!segments.length) return;
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  let pathData = "";
  for (let index = 0; index < segments.length; index += 1) {
    const segment = segments[index];
    const next = segments[index + 1];
    if (index === 0 || segment.breakBefore) pathData += " M " + segment.start + " " + segment.y;
    if (!next || next.breakBefore) {
      pathData += " L " + segment.end + " " + segment.y;
      continue;
    }
    const boundary = segment.end;
    const transitionHalfWidth = (10 / (24 * 60)) * 100;
    const arcYRadius = Math.min(50, transitionHalfWidth * (timeline.clientWidth / timeline.clientHeight));
    const curveStart = boundary - transitionHalfWidth;
    const curveEnd = boundary + transitionHalfWidth;
    const direction = next.y > segment.y ? 1 : -1;
    const arcStartY = segment.y + direction * arcYRadius;
    const arcEndY = next.y - direction * arcYRadius;
    const sweep = direction > 0 ? 1 : 0;
    const outgoingSweep = sweep === 1 ? 0 : 1;
    if (next.end < curveEnd) next.end = curveEnd;
    pathData += " L " + curveStart + " " + segment.y;
    pathData += " A " + transitionHalfWidth + " " + arcYRadius + " 0 0 " + sweep + " " + boundary + " " + arcStartY;
    pathData += " L " + boundary + " " + arcEndY;
    pathData += " A " + transitionHalfWidth + " " + arcYRadius + " 0 0 " + outgoingSweep + " " + curveEnd + " " + next.y;
  }
  path.setAttribute("d", pathData);
  path.setAttribute("class", "working-line-path");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  line.setAttribute("id", "working-line");
  line.setAttribute("class", "working-line");
  line.setAttribute("viewBox", "0 0 100 100");
  line.setAttribute("preserveAspectRatio", "none");
  line.setAttribute("aria-hidden", "true");
  line.appendChild(path);
  timeline.appendChild(line);
}

function renderHistory(payload) {
  const allSamples = payload.samples;
  const todayKey = dayKey(new Date());
  availableDayKeys = Array.from(new Set([
    todayKey,
    ...allSamples.filter(sampleWorking).map((sample) => dayKey(sample.timestamp)),
  ])).filter((key) => key <= todayKey).sort();
  if (!availableDayKeys.includes(selectedDayKey)) {
    selectedDayKey = availableDayKeys.filter((key) => key < selectedDayKey).at(-1) || todayKey;
  }
  const dayIndex = availableDayKeys.indexOf(selectedDayKey);
  $("previous-day").disabled = dayIndex <= 0;
  $("next-day").disabled = dayIndex < 0 || dayIndex >= availableDayKeys.length - 1;
  const isSelectedToday = selectedDayKey === todayKey;
  const selectedDate = new Date(`${selectedDayKey}T12:00:00`);
  const selectedLabel = isSelectedToday ? "Today" : selectedDate.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
  $("daily-title").textContent = isSelectedToday ? "Today at a glance" : `${selectedLabel} at a glance`;
  $("daily-estimate-label").textContent = `Working estimate · ${selectedLabel}`;
  $("today-label").textContent = selectedDate.toLocaleDateString([], { month: "short", day: "numeric" });
  $("daily-hours-label").textContent = selectedLabel;
  $("manual-break-tools").hidden = !isSelectedToday;

  const samples = allSamples.filter((sample) => dayKey(sample.timestamp) === selectedDayKey);
  const minutes = samples.reduce((total, sample, index) => total + (sampleWorking(sample) ? durationMinutes(samples, index, payload.interval) : 0), 0);
  const observedMinutes = samples.reduce((total, sample, index) => total + durationMinutes(samples, index, payload.interval), 0);
  const percent = observedMinutes ? Math.round((minutes / observedMinutes) * 100) : 0;
  const sevenDaysAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
  const recent = allSamples.filter((sample) => new Date(sample.timestamp).getTime() >= sevenDaysAgo);
  const dayTotals = {};
  const breakTotals = {};
  recent.forEach((sample) => {
    const key = dayKey(sample.timestamp);
    const duration = durationMinutes(recent, recent.indexOf(sample), payload.interval);
    dayTotals[key] = (dayTotals[key] || 0) + (sampleWorking(sample) ? duration : 0);
    breakTotals[key] = (breakTotals[key] || 0) + (sample.paused ? duration : 0);
  });
  const workingDays = Object.keys(dayTotals).filter((day) => dayTotals[day] > 0);
  const averageMinutes = workingDays.length ? workingDays.reduce((total, day) => total + dayTotals[day], 0) / workingDays.length : 0;
  const averageBreakMinutes = workingDays.length ? workingDays.reduce((total, day) => total + (breakTotals[day] || 0), 0) / workingDays.length : 0;
  $("working-hours").textContent = formatHours(minutes);
  $("today-hours").textContent = formatHours(minutes);
  $("average-hours").textContent = workingDays.length ? formatHours(averageMinutes) : "--";
  $("average-break-hours").textContent = workingDays.length ? formatHours(averageBreakMinutes) : "--";
  $("working-percent").textContent = samples.length ? `${percent}% of observed time` : "Collecting your first signal";
  $("sample-count").textContent = `${samples.length} samples`;
  $("working-meter").style.width = `${Math.min(percent, 100)}%`;
  const manualBreaks = samples.filter((sample, index) => sample.paused && !samples[index - 1]?.paused).length;
  $("break-count").textContent = samples.length ? manualBreaks : "--";
  const timeline = $("timeline");
  timeline.replaceChildren();
  if (!samples.length) {
    timeline.innerHTML = '<div class="empty">No work samples for this day.</div>';
    ["axis-start", "axis-quarter", "axis-mid", "axis-three-quarter", "axis-end"].forEach((id) => { $(id).textContent = "--"; });
    renderAverageDay(payload);
    renderWeek(payload);
    return;
  }
  const hours = Array.from({ length: 24 }, (_, hour) => ({ hour, total: 0, working: 0 }));
  samples.forEach((sample, index) => {
    const minutesInSample = durationMinutes(samples, index, payload.interval);
    const start = new Date(sample.timestamp);
    const end = new Date(start.getTime() + minutesInSample * 60000);
    let cursor = start;
    while (cursor < end) {
      const hour = cursor.getHours();
      const nextHour = new Date(cursor);
      nextHour.setHours(cursor.getHours() + 1, 0, 0, 0);
      const slice = Math.min(end.getTime(), nextHour.getTime()) - cursor.getTime();
      hours[hour].total += slice / 60000;
      if (sampleWorking(sample)) hours[hour].working += slice / 60000;
      cursor = nextHour;
    }
  });
  const workingSamples = samples.filter((sample) => sampleWorking(sample));
  const firstObservedHour = new Date(samples[0].timestamp).getHours();
  const firstWorkingHour = workingSamples.length ? new Date(workingSamples[0].timestamp).getHours() : firstObservedHour;
  const lastWorkingSample = workingSamples[workingSamples.length - 1];
  const lastWorkingIndex = lastWorkingSample ? samples.indexOf(lastWorkingSample) : -1;
  const lastWorkingEnd = lastWorkingSample ? new Date(lastWorkingSample.timestamp).getTime() + durationMinutes(samples, lastWorkingIndex, payload.interval) * 60000 : 18 * 60 * 60000;
  const lastWorkingHour = lastWorkingSample ? new Date(lastWorkingEnd).getHours() + (new Date(lastWorkingEnd).getMinutes() ? 1 : 0) : 18;
  const chartStartHour = Math.max(0, Math.min(firstWorkingHour, 23));
  const chartEndHour = Math.min(24, Math.max(18, lastWorkingHour));
  timeline.replaceChildren();
  hours.slice(chartStartHour, chartEndHour).forEach(({ hour, total, working }) => {
    const bar = document.createElement("span");
    const observedShare = total ? working / total : 0;
    const workingShare = working / 60;
    bar.className = `bar${working ? " working" : ""}`;
    bar.style.height = `${Math.min(workingShare * 100, 100)}%`;
    bar.title = `${hour}:00–${hour + 1}:00 · ${working ? `${working.toFixed(1)} working minutes` : total ? "0 working minutes" : "Not observed"}${total && observedShare < 0.99 ? ` (${Math.round(observedShare * 100)}% of observed)` : ""}`;
    timeline.appendChild(bar);
  });
  renderWorkingLine(timeline, samples, payload.interval, chartStartHour, chartEndHour);
  $("axis-start").textContent = formatAxisHour(chartStartHour);
  $("axis-quarter").textContent = formatAxisHour(chartStartHour + (chartEndHour - chartStartHour) / 4);
  $("axis-mid").textContent = formatAxisHour(chartStartHour + (chartEndHour - chartStartHour) / 2);
  $("axis-three-quarter").textContent = formatAxisHour(chartStartHour + (chartEndHour - chartStartHour) * 0.75);
  $("axis-end").textContent = formatAxisHour(chartEndHour);
  renderAverageDay(payload);
  renderWeek(payload);
}

function formatAxisHour(hour) {
  const normalized = ((hour % 24) + 24) % 24;
  const wholeHour = Math.floor(normalized);
  const minutes = Math.round((normalized - wholeHour) * 60);
  const suffix = wholeHour >= 12 ? "pm" : "am";
  const displayHour = wholeHour % 12 || 12;
  return `${displayHour}:${String(minutes).padStart(2, "0")} ${suffix}`;
}

function renderAverageDay(payload) {
  const endOfCompletedDays = new Date();
  endOfCompletedDays.setHours(0, 0, 0, 0);
  const cutoff = new Date(endOfCompletedDays);
  cutoff.setDate(cutoff.getDate() - averageWindowDays);
  const binMinutes = 10;
  const binsPerDay = (24 * 60) / binMinutes;
  const days = {};
  payload.samples.filter((sample) => {
    const timestamp = new Date(sample.timestamp).getTime();
    return timestamp >= cutoff.getTime() && timestamp < endOfCompletedDays.getTime();
  }).forEach((sample) => {
    const date = new Date(sample.timestamp);
    const key = dayKey(sample.timestamp);
    if (!days[key]) days[key] = Array(binsPerDay).fill(null);
    const bin = Math.floor((date.getHours() * 60 + date.getMinutes()) / binMinutes);
    days[key][bin] = sampleWorking(sample) ? 1 : 0;
  });
  const dayValues = Object.values(days)
    .filter((day) => day.some((value) => value === 1))
    .map((day) => day.map((value) => value ?? 0));
  const values = Array.from({ length: binsPerDay }, (_, bin) => {
    const observations = dayValues.map((day) => day[bin]).filter((value) => value !== null);
    if (!observations.length) return null;
    const mean = observations.reduce((sum, value) => sum + value, 0) / observations.length;
    const variance = observations.reduce((sum, value) => sum + (value - mean) ** 2, 0) / observations.length;
    const deviation = Math.sqrt(variance);
    return { mean, upper: Math.min(1, mean + deviation), lower: Math.max(0, mean - deviation) };
  });
  const chart = $("average-day-chart");
  chart.replaceChildren();
  if (!values.some((value) => value !== null)) { chart.innerHTML = '<div class="empty">Not enough history yet.</div>'; return; }
  const paddedStartBin = 7 * 60 / binMinutes;
  const endBin = 20 * 60 / binMinutes;
  const upper = [];
  const lower = [];
  const average = [];
  values.slice(paddedStartBin, endBin + 1).forEach((value, visibleBin) => {
    if (!value) return;
    const x = (visibleBin / (endBin - paddedStartBin)) * 100;
    upper.push({ x, y: 100 - value.upper * 100 });
    lower.push({ x, y: 100 - value.lower * 100 });
    average.push({ x, y: 100 - value.mean * 100 });
  });
  const smoothPath = (points) => {
    if (points.length < 2) return points.length ? `M ${points[0].x} ${points[0].y}` : "";
    const clampY = (value) => Math.max(0, Math.min(100, value));
    let path = `M ${points[0].x} ${points[0].y}`;
    for (let index = 0; index < points.length - 1; index += 1) {
      const previous = points[index - 1] || points[index];
      const current = points[index];
      const next = points[index + 1];
      const afterNext = points[index + 2] || next;
      const controlOne = { x: current.x + (next.x - previous.x) / 6, y: clampY(current.y + (next.y - previous.y) / 6) };
      const controlTwo = { x: next.x - (afterNext.x - current.x) / 6, y: clampY(next.y - (afterNext.y - current.y) / 6) };
      path += ` C ${controlOne.x} ${controlOne.y} ${controlTwo.x} ${controlTwo.y} ${next.x} ${next.y}`;
    }
    return path;
  };
  const bandPath = smoothPath(upper) + " " + smoothPath(lower.slice().reverse()).replace(/^M/, "L") + " Z";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "average-day-svg");
  const band = document.createElementNS("http://www.w3.org/2000/svg", "path");
  band.setAttribute("d", bandPath);
  band.setAttribute("class", "average-day-band");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
  line.setAttribute("d", smoothPath(average));
  line.setAttribute("class", "average-day-line");
  svg.append(band, line);
  chart.appendChild(svg);
  $("average-axis-start").textContent = formatAxisHour((paddedStartBin * binMinutes) / 60);
  $("average-axis-quarter").textContent = formatAxisHour((paddedStartBin + (endBin - paddedStartBin) / 4) * binMinutes / 60);
  $("average-axis-mid").textContent = formatAxisHour((paddedStartBin + (endBin - paddedStartBin) / 2) * binMinutes / 60);
  $("average-axis-three-quarter").textContent = formatAxisHour((paddedStartBin + (endBin - paddedStartBin) * 3 / 4) * binMinutes / 60);
  $("average-axis-end").textContent = formatAxisHour((endBin * binMinutes) / 60);
}

function startOfWeek(date) {
  const result = new Date(date);
  const day = result.getDay();
  result.setDate(result.getDate() - (day === 0 ? 6 : day - 1));
  result.setHours(0, 0, 0, 0);
  return result;
}

function moveSelectedDay(direction) {
  const index = availableDayKeys.indexOf(selectedDayKey);
  const target = availableDayKeys[index + direction];
  if (!target) return;
  selectedDayKey = target;
  refresh();
}

function renderWeek(payload) {
  const weekStart = startOfWeek(new Date(Date.now() + selectedWeekOffset * 7 * 24 * 60 * 60 * 1000));
  const weekEnd = new Date(weekStart);
  weekEnd.setDate(weekEnd.getDate() + 7);
  const days = Array.from({ length: 7 }, (_, index) => {
    const date = new Date(weekStart);
    date.setDate(date.getDate() + index);
    return { date, key: dayKey(date), minutes: 0, breakMinutes: 0, observed: 0 };
  });
  const weekSamples = payload.samples.filter((sample) => {
    const timestamp = new Date(sample.timestamp);
    return timestamp >= weekStart && timestamp < weekEnd;
  });
  weekSamples.forEach((sample, index) => {
    const minutes = durationMinutes(weekSamples, index, payload.interval);
    const day = days.find((item) => item.key === dayKey(sample.timestamp));
    if (!day) return;
    day.observed += minutes;
    if (sampleWorking(sample)) day.minutes += minutes;
    else if (sample.paused) day.breakMinutes += minutes;
  });
  const workingDays = days.filter((day) => day.minutes > 0);
  const average = workingDays.length ? workingDays.reduce((sum, day) => sum + day.minutes, 0) / workingDays.length : 0;
  const scaleMinutes = Math.max(60, Math.ceil(Math.max(...days.map((day) => Math.max(day.minutes, day.breakMinutes)), 0) / 60) * 60);
  $("week-y-max").textContent = `${formatHours(scaleMinutes)}h`;
  $("week-y-mid").textContent = `${formatHours(scaleMinutes / 2)}h`;
  $("week-title").textContent = `${weekStart.toLocaleDateString([], { month: "short", day: "numeric" })} – ${new Date(weekEnd - 1).toLocaleDateString([], { month: "short", day: "numeric" })}`;
  $("week-average").textContent = workingDays.length ? formatHours(average) : "--";
  $("week-total").textContent = `${formatHours(workingDays.reduce((sum, day) => sum + day.minutes, 0))} total hours`;
  $("next-week").disabled = selectedWeekOffset >= 0;
  const chart = $("week-chart");
  chart.replaceChildren();
  days.forEach((day) => {
    const column = document.createElement("div");
    column.className = "day-column";
    const bars = document.createElement("div");
    bars.className = "day-bars";
    const workBar = document.createElement("span");
    workBar.className = `day-bar${day.minutes ? " working" : ""}`;
    workBar.style.height = `${Math.min(day.minutes / scaleMinutes * 100, 100)}%`;
    workBar.title = `${day.date.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" })} · ${formatHours(day.minutes)} working hours`;
    const breakBar = document.createElement("span");
    breakBar.className = `day-bar${day.breakMinutes ? " break" : ""}`;
    breakBar.style.height = `${Math.min(day.breakMinutes / scaleMinutes * 100, 100)}%`;
    breakBar.title = `${day.date.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" })} · ${formatHours(day.breakMinutes)} break hours`;
    const label = document.createElement("small");
    label.textContent = day.date.toLocaleDateString([], { weekday: "short" });
    bars.append(workBar, breakBar);
    column.append(bars, label);
    chart.appendChild(column);
  });
}

async function pauseTracking() {
  const button = $("pause-button");
  button.disabled = true;
  try {
    const response = await fetch("/api/pause", { method: "POST" });
    const result = await readJson(response);
    if (result.paused) showPauseOverlay();
    await refresh();
  } finally { button.disabled = false; }
}

async function resumeTracking(wasBreak) {
  const buttons = [$("overlay-break"), $("overlay-work")];
  buttons.forEach((button) => { button.disabled = true; });
  hidePauseOverlay();
  try {
    await fetch("/api/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ was_break: wasBreak }),
    });
    await refresh();
  } finally { buttons.forEach((button) => { button.disabled = false; }); }
}

function timeInputValue(date) {
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function setDefaultManualBreakTimes() {
  const end = new Date();
  end.setSeconds(0, 0);
  const start = new Date(end.getTime() - 30 * 60000);
  if (start.toDateString() !== end.toDateString()) start.setHours(0, 0, 0, 0);
  $("manual-break-start").value = timeInputValue(start);
  $("manual-break-end").value = timeInputValue(end);
}

function setManualBreakFormOpen(open) {
  const form = $("manual-break-form");
  form.hidden = !open;
  $("manual-break-toggle").setAttribute("aria-expanded", String(open));
  if (open) {
    if (!$("manual-break-start").value) setDefaultManualBreakTimes();
    $("manual-break-start").focus();
  }
}

function setSignalAppFormOpen(open) {
  const form = $("signal-app-form");
  form.hidden = !open;
  $("signal-app-toggle").setAttribute("aria-expanded", String(open));
  if (open) {
    $("signal-app-name").focus();
    $("signal-app-name").select();
  }
}

async function saveSignalApp(event) {
  event.preventDefault();
  const form = $("signal-app-form");
  const button = form.querySelector("[type=submit]");
  const message = $("signal-app-message");
  const appName = $("signal-app-name").value.trim();
  if (!appName) { message.textContent = "Enter the app's process name."; return; }

  button.disabled = true;
  message.textContent = "Saving…";
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mail_app: appName }),
    });
    const result = await readJson(response);
    if (!response.ok) throw new Error(result.error || "Could not change the work signal.");
    message.textContent = `Now watching ${result.mail_app}.`;
    await refresh();
  } catch (error) {
    message.textContent = error.message || "Could not change the work signal.";
  } finally { button.disabled = false; }
}

async function addManualBreak(event) {
  event.preventDefault();
  const message = $("manual-break-message");
  const submit = $("manual-break-form").querySelector("[type=submit]");
  const [startHour, startMinute] = $("manual-break-start").value.split(":").map(Number);
  const [endHour, endMinute] = $("manual-break-end").value.split(":").map(Number);
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), startHour, startMinute);
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate(), endHour, endMinute);
  if (start >= end) { message.textContent = "The end time must be after the start time."; return; }
  if (end > now) { message.textContent = "The break must have ended already."; return; }

  submit.disabled = true;
  message.textContent = "Saving…";
  try {
    const response = await fetch("/api/manual-break", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start: start.toISOString(), end: end.toISOString() }),
    });
    const result = await readJson(response);
    if (!response.ok) throw new Error(result.error || "Could not save the break.");
    message.textContent = "Break added for today.";
    $("manual-break-start").value = "";
    $("manual-break-end").value = "";
    await refresh();
  } catch (error) {
    message.textContent = error.message || "Could not save the break.";
  } finally { submit.disabled = false; }
}

async function refresh() {
  try {
    const [statusResponse, historyResponse] = await Promise.all([fetch("/api/status"), fetch("/api/history")]);
    const status = await readJson(statusResponse);
    const history = await readJson(historyResponse);
    renderStatus(status); renderHistory(history);
  } catch { $("last-updated").textContent = "Tracker offline"; }
}

$("pause-button").addEventListener("click", pauseTracking);
$("overlay-break").addEventListener("click", () => resumeTracking(true));
$("overlay-work").addEventListener("click", () => resumeTracking(false));
$("today-label").textContent = new Date().toLocaleDateString([], { month: "short", day: "numeric" });
$("previous-day").addEventListener("click", () => moveSelectedDay(-1));
$("next-day").addEventListener("click", () => moveSelectedDay(1));
$("previous-week").addEventListener("click", () => { selectedWeekOffset -= 1; refresh(); });
$("next-week").addEventListener("click", () => { selectedWeekOffset += 1; refresh(); });
$("average-window").addEventListener("change", (event) => { averageWindowDays = Number(event.target.value); refresh(); });
$("signal-app-toggle").addEventListener("click", () => setSignalAppFormOpen($("signal-app-form").hidden));
$("signal-app-form").addEventListener("submit", saveSignalApp);
$("manual-break-toggle").addEventListener("click", () => setManualBreakFormOpen($("manual-break-form").hidden));
$("manual-break-cancel").addEventListener("click", () => setManualBreakFormOpen(false));
$("manual-break-form").addEventListener("submit", addManualBreak);
refresh();
setInterval(refresh, 30000);
