/* Replay-driven Viewer MVP. Consumes only the backend presentation bundle. */
(() => {
  "use strict";

  const canvas = document.getElementById("map");
  const ctx = canvas.getContext("2d");
  const playBtn = document.getElementById("play");
  const scrub = document.getElementById("scrub");
  const speedSel = document.getElementById("speed");
  const clockEl = document.getElementById("clock");
  const rangeLabel = document.getElementById("range-label");
  const debugEl = document.getElementById("debug");

  let bundle = null;
  let basemap = null;
  let image = null;
  let startMs = 0;
  let totalMs = 0;
  let simMs = 0;
  let playing = true;
  let lastTs = null;
  let scale = 60;
  let refresh = 0;

  const cache = { lastIndex: -1, track: [], pending: null };

  function isoToMs(value) {
    return new Date(value).getTime();
  }

  function formatClock(ms) {
    const totalSeconds = Math.floor(ms / 1000);
    const h = String(Math.floor(totalSeconds / 3600)).padStart(2, "0");
    const m = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, "0");
    const s = String(totalSeconds % 60).padStart(2, "0");
    return `${h}:${m}:${s}`;
  }

  function project(lon, lat) {
    const b = basemap.bbox;
    const x = ((lon - b.min_lon) / (b.max_lon - b.min_lon)) * canvas.width;
    const y = ((b.max_lat - lat) / (b.max_lat - b.min_lat)) * canvas.height;
    return { x, y };
  }

  function timelineIndex(ms) {
    const tl = bundle.timeline;
    let low = 0;
    let high = tl.length - 1;
    while (low < high) {
      const mid = (low + high + 1) >> 1;
      if (isoToMs(tl[mid].t) - startMs <= ms) low = mid;
      else high = mid - 1;
    }
    return low;
  }

  function stateAt(ms) {
    const tl = bundle.timeline;
    const i = timelineIndex(ms);
    const a = tl[i];
    const b = tl[Math.min(i + 1, tl.length - 1)];
    const denom = isoToMs(b.t) - isoToMs(a.t) || 1;
    const f = Math.max(0, Math.min(1, (ms - (isoToMs(a.t) - startMs)) / denom));
    const lerp = (x, y) => x + (y - x) * f;
    cache.lastIndex = i;
    if (a.track) cache.track = a.track;
    if (Object.prototype.hasOwnProperty.call(a, "pending")) cache.pending = a.pending;
    if (a.ctl > cache.track.length) {
      cache.track = cache.track.slice(0, a.ctl);
    }
    return {
      time: startMs + ms,
      lon: lerp(a.v.lon, b.v.lon),
      lat: lerp(a.v.lat, b.v.lat),
      kn: lerp(a.v.kn ?? 0, b.v.kn ?? 0),
      status: a.v.status,
      edge: lerp(a.v.ep ?? 0, b.v.ep ?? 0),
      edgeIndex: a.v.eidx,
      active: a.arv,
      pendingRevision: a.prv,
      pendingStatus: a.prs,
      decisionTime: a.dt,
      effectiveAdoption: a.eat,
      segment: a.seg,
      track: cache.track,
      pendingRoute: cache.pending,
    };
  }

  function routeFor(revision) {
    return bundle.routes.find((route) => route.revision === revision) || null;
  }

  function lastEvent(ms) {
    let result = null;
    for (const event of bundle.events) {
      if (isoToMs(event.t) - startMs <= ms) result = event;
      else break;
    }
    return result;
  }

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (image) ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    const s = stateAt(simMs);
    const pos = project(s.lon, s.lat);

    const active = routeFor(s.active);
    if (active) {
      const future = active.waypoints.filter((w) => isoToMs(w.eta) - startMs >= simMs);
      if (future.length) {
        ctx.strokeStyle = "#3d9be9";
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.moveTo(pos.x, pos.y);
        for (const w of future) {
          const p = project(w.lon, w.lat);
          ctx.lineTo(p.x, p.y);
        }
        ctx.stroke();
      }
    }

    if (s.track && s.track.length > 1) {
      ctx.strokeStyle = "#5cc47a";
      ctx.lineWidth = 3;
      ctx.setLineDash([2, 0]);
      ctx.beginPath();
      s.track.forEach((p, index) => {
        const g = project(p.longitude ?? p.lon, p.latitude ?? p.lat);
        if (index === 0) ctx.moveTo(g.x, g.y);
        else ctx.lineTo(g.x, g.y);
      });
      ctx.stroke();
      ctx.setLineDash([]);
    }

    if (s.pendingRoute && s.pendingRoute.route && s.pendingRoute.revision !== s.active) {
      ctx.strokeStyle = "#f2b134";
      ctx.lineWidth = 2.5;
      ctx.setLineDash([8, 6]);
      ctx.beginPath();
      s.pendingRoute.route.forEach((w, index) => {
        const p = project(w.lon, w.lat);
        if (index === 0) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
      });
      ctx.stroke();
      ctx.setLineDash([]);
    }

    if (s.segment && s.segment.start_eta && s.segment.end_eta && active) {
      const seg = active.waypoints.filter(
        (w) => isoToMs(w.eta) >= isoToMs(s.segment.start_eta)
      );
      if (seg.length >= 2) {
        const a = project(seg[0].lon, seg[0].lat);
        const b = project(seg[1].lon, seg[1].lat);
        ctx.strokeStyle = "rgba(255,255,255,0.85)";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }

    ctx.beginPath();
    ctx.arc(pos.x, pos.y, 7, 0, Math.PI * 2);
    ctx.fillStyle = "#ffffff";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#0f2b3b";
    ctx.stroke();

    updateDebug(s);
  }

  function updateDebug(s) {
    const event = lastEvent(simMs);
    const rows = [
      ["simulation_time", new Date(s.time).toISOString().replace("T", " ").slice(0, 19) + "Z"],
      ["vessel lon/lat", `${s.lon.toFixed(4)} / ${s.lat.toFixed(4)}`],
      ["speed knots", (s.kn ?? 0).toFixed(2)],
      ["edge_progress", (s.edge ?? 0).toFixed(4)],
      ["active_plan_revision", s.active],
      ["pending_plan_revision", s.pendingRevision ?? "null"],
      ["pending_plan_status", s.pendingStatus ?? "none"],
      ["decision_time", s.decisionTime ?? "null"],
      ["effective_adoption_time", s.effectiveAdoption ?? "null"],
      ["last event", event ? `${event.type}@${event.t}` : "none"],
      ["L1", bundle.gates.status || "NOT_RUN"],
      ["L2", bundle.gates.l2_status || "NOT_RUN"],
    ];
    debugEl.innerHTML = rows
      .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`)
      .join("");
  }

  function frame(ts) {
    if (lastTs !== null && playing) {
      const delta = (ts - lastTs) / 1000;
      simMs += delta * scale * 1000;
      if (simMs >= totalMs) {
        simMs = totalMs;
        playing = false;
        playBtn.textContent = "Play";
      }
    }
    lastTs = ts;
    scrub.value = Math.round(simMs);
    clockEl.textContent = formatClock(simMs);
    refresh += 1;
    if (refresh % 3 === 0) draw();
    requestAnimationFrame(frame);
  }

  playBtn.addEventListener("click", () => {
    playing = !playing;
    playBtn.textContent = playing ? "Pause" : "Play";
    lastTs = null;
  });

  scrub.addEventListener("input", () => {
    simMs = Number(scrub.value);
    playing = false;
    playBtn.textContent = "Play";
  });

  speedSel.addEventListener("change", () => {
    scale = Number(speedSel.value);
  });

  async function start() {
    bundle = window.VIEWER_BUNDLE || (await (await fetch("bundle.json")).json());
    basemap = bundle.basemap;
    startMs = isoToMs(bundle.replay.start);
    const end = isoToMs(bundle.replay.end);
    totalMs = end - startMs;
    simMs = 0;
    scrub.max = String(totalMs);
    rangeLabel.textContent = `${bundle.replay.start} -> ${bundle.replay.end}`;
    document.getElementById("mode-badge").textContent = bundle.replay.scenario_mode;
    document.getElementById("gate-l1").textContent = `L1 ${bundle.gates.status}`;
    document.getElementById("gate-l2").textContent = `L2 ${bundle.gates.l2_status}`;
    document.getElementById("gate-loop").textContent =
      `preflight ${bundle.gates.status} / ${bundle.gates.l2_status || "not-run"}`;
    if (basemap) {
      canvas.width = basemap.width;
      canvas.height = basemap.height;
      image = new Image();
      image.onload = () => {
        requestAnimationFrame(draw);
      };
      image.src = window.VIEWER_BASEMAP || "gebco_basemap.png";
    }
    requestAnimationFrame(frame);
  }

  start().catch((error) => {
    document.getElementById("hover-info").textContent = `viewer error: ${error}`;
  });
})();
