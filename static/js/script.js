/* ============================================================================
   CYBER NEON AI DASHBOARD — FRONTEND LOGIC
   Handles: live clock, camera controls, MJPEG stream binding, status polling,
   screenshots, recording, downloads, notifications, loading overlay.
============================================================================ */

document.addEventListener("DOMContentLoaded", () => {

  /* ---------------------------- ELEMENT REFERENCES ---------------------------- */
  const loadingOverlay   = document.getElementById("loading-overlay");
  const notifContainer   = document.getElementById("notification-container");

  const liveDateEl       = document.getElementById("live-date");
  const liveTimeEl       = document.getElementById("live-time");
  const systemStatusDot  = document.getElementById("system-status-dot");
  const systemStatusText = document.getElementById("system-status-text");

  const videoFeed        = document.getElementById("video-feed");
  const videoPlaceholder = document.getElementById("video-placeholder");

  const statMoving    = document.getElementById("stat-moving");
  const statFps       = document.getElementById("stat-fps");
  const statDetection = document.getElementById("stat-detection-status");
  const statRecording = document.getElementById("stat-recording-status");

  const btnStartCamera       = document.getElementById("btn-start-camera");
  const btnStopCamera        = document.getElementById("btn-stop-camera");
  const btnScreenshot        = document.getElementById("btn-screenshot");
  const btnStartRecording    = document.getElementById("btn-start-recording");
  const btnStopRecording     = document.getElementById("btn-stop-recording");
  const btnDownloadScreenshot = document.getElementById("btn-download-screenshot");
  const btnDownloadRecording  = document.getElementById("btn-download-recording");

  let statusPollInterval = null;

  /* ---------------------------- LOADING OVERLAY ---------------------------- */
  window.addEventListener("load", () => {
    setTimeout(() => loadingOverlay.classList.add("hidden"), 1200);
  });

  /* ---------------------------- LIVE DATE & TIME ---------------------------- */
  function updateDateTime() {
    const now = new Date();
    const dateOptions = { weekday: "short", year: "numeric", month: "short", day: "numeric" };
    liveDateEl.textContent = now.toLocaleDateString("en-US", dateOptions);
    liveTimeEl.textContent = now.toLocaleTimeString("en-US", { hour12: false });
  }
  updateDateTime();
  setInterval(updateDateTime, 1000);

  /* ---------------------------- NOTIFICATIONS ---------------------------- */
  function showNotification(message, type = "info") {
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = message;
    notifContainer.appendChild(toast);
    setTimeout(() => toast.remove(), 4200);
  }

  /* ---------------------------- BUTTON STATE HELPERS ---------------------------- */
  function setCameraRunningUI(running) {
    btnStartCamera.disabled = running;
    btnStopCamera.disabled = !running;
    btnScreenshot.disabled = !running;
    btnStartRecording.disabled = !running;

    videoFeed.classList.toggle("active", running);
    videoPlaceholder.classList.toggle("hidden", running);

    systemStatusDot.classList.toggle("online", running);
    systemStatusDot.classList.toggle("offline", !running);
    systemStatusText.textContent = running ? "SYSTEM ONLINE" : "SYSTEM IDLE";

    if (running) {
      // Cache-bust the MJPEG stream URL to force a fresh connection
      videoFeed.src = "/video_feed?t=" + Date.now();
    } else {
      videoFeed.src = "";
    }
  }

  function setRecordingUI(recording) {
    btnStartRecording.disabled = recording;
    btnStopRecording.disabled = !recording;
  }

  /* ---------------------------- API CALLS ---------------------------- */
  async function postJSON(url) {
    try {
      const res = await fetch(url, { method: "POST" });
      return await res.json();
    } catch (err) {
      showNotification("Network error: " + err.message, "error");
      return { success: false, message: "Network error" };
    }
  }

  /* ---- START CAMERA ---- */
  btnStartCamera.addEventListener("click", async () => {
    btnStartCamera.disabled = true;
    const data = await postJSON("/api/start_camera");
    if (data.success) {
      setCameraRunningUI(true);
      startStatusPolling();
      showNotification(data.message, "success");
    } else {
      btnStartCamera.disabled = false;
      showNotification(data.message, "error");
    }
  });

  /* ---- STOP CAMERA ---- */
  btnStopCamera.addEventListener("click", async () => {
    const data = await postJSON("/api/stop_camera");
    if (data.success) {
      setCameraRunningUI(false);
      setRecordingUI(false);
      stopStatusPolling();
      showNotification(data.message, "success");
      resetStats();
    } else {
      showNotification(data.message, "error");
    }
  });

  /* ---- SCREENSHOT ---- */
  btnScreenshot.addEventListener("click", async () => {
    const data = await postJSON("/api/screenshot");
    showNotification(data.message, data.success ? "success" : "error");
  });

  /* ---- START RECORDING ---- */
  btnStartRecording.addEventListener("click", async () => {
    const data = await postJSON("/api/start_recording");
    if (data.success) {
      setRecordingUI(true);
      showNotification(data.message, "success");
    } else {
      showNotification(data.message, "error");
    }
  });

  /* ---- STOP RECORDING ---- */
  btnStopRecording.addEventListener("click", async () => {
    const data = await postJSON("/api/stop_recording");
    if (data.success) {
      setRecordingUI(false);
      showNotification(data.message, "success");
    } else {
      showNotification(data.message, "error");
    }
  });

  /* ---- DOWNLOAD SCREENSHOT ---- */
  btnDownloadScreenshot.addEventListener("click", () => {
    window.location.href = "/api/download_screenshot";
  });

  /* ---- DOWNLOAD RECORDING ---- */
  btnDownloadRecording.addEventListener("click", () => {
    window.location.href = "/api/download_recording";
  });

  /* ---------------------------- STATUS POLLING ---------------------------- */
  function startStatusPolling() {
    if (statusPollInterval) return;
    statusPollInterval = setInterval(fetchStatus, 1000);
  }

  function stopStatusPolling() {
    clearInterval(statusPollInterval);
    statusPollInterval = null;
  }

  async function fetchStatus() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();

      statMoving.textContent = data.moving_objects;
      statFps.textContent = Number(data.fps).toFixed(1);

      statDetection.textContent = data.detection_status;
      statDetection.className = "stat-value status-pill " + (data.camera_active ? "active" : "idle");

      statRecording.textContent = data.recording_status;
      statRecording.className = "stat-value status-pill " + (data.recording_active ? "recording" : "stopped");

      // Keep UI in sync if camera was stopped externally (e.g. server restarted)
      if (!data.camera_active && videoFeed.classList.contains("active")) {
        setCameraRunningUI(false);
        stopStatusPolling();
      }
    } catch (err) {
      console.error("Status polling error:", err);
    }
  }

  function resetStats() {
    statMoving.textContent = "0";
    statFps.textContent = "0.0";
    statDetection.textContent = "IDLE";
    statDetection.className = "stat-value status-pill idle";
    statRecording.textContent = "STOPPED";
    statRecording.className = "stat-value status-pill stopped";
  }

  /* ---------------------------- ERROR HANDLING (VIDEO STREAM) ---------------------------- */
  videoFeed.addEventListener("error", () => {
    if (videoFeed.classList.contains("active")) {
      showNotification("Video stream interrupted. Attempting reconnect...", "error");
    }
  });

  /* ---------------------------- INITIAL UI STATE ---------------------------- */
  setCameraRunningUI(false);
  setRecordingUI(false);
});