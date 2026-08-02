/* ============================================================================
   CYBER NEON AI DASHBOARD — FRONTEND LOGIC (BROWSER WEBCAM EDITION)
   ------------------------------------------------------------------------
   Handles: live clock, camera controls, BROWSER webcam capture via
   getUserMedia + canvas, POSTing frames to /api/process_frame, painting the
   annotated frame returned by the server back into the dashboard, status
   polling, screenshots, recording, downloads, notifications, loading
   animation.

   NOTE: getUserMedia() requires a secure context (HTTPS or localhost).
   Railway serves the app over HTTPS by default, so camera access will work
   once deployed. On localhost during development it also works over plain
   http://localhost.
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

  // Hidden webcam capture elements (not shown to the user)
  const webcamVideo      = document.getElementById("webcam-video");
  const captureCanvas    = document.getElementById("capture-canvas");
  const captureCtx       = captureCanvas.getContext("2d");

  // These stat elements may or may not exist depending on the dashboard
  // layout in use — guarded with null checks wherever they are updated.
  const statMoving    = document.getElementById("stat-moving");
  const statTotal     = document.getElementById("stat-total");
  const statFps       = document.getElementById("stat-fps");
  const statDetection = document.getElementById("stat-detection-status");
  const statRecording = document.getElementById("stat-recording-status");

  const btnStartCamera        = document.getElementById("btn-start-camera");
  const btnStopCamera         = document.getElementById("btn-stop-camera");
  const btnScreenshot         = document.getElementById("btn-screenshot");
  const btnStartRecording     = document.getElementById("btn-start-recording");
  const btnStopRecording      = document.getElementById("btn-stop-recording");
  const btnDownloadScreenshot = document.getElementById("btn-download-screenshot");
  const btnDownloadRecording  = document.getElementById("btn-download-recording");

  let statusPollInterval = null;

  // ---- Browser webcam / frame-capture state ----
  let mediaStream       = null;   // active getUserMedia MediaStream
  let cameraRunning     = false;  // true once both webcam + server pipeline are armed
  let captureTimer      = null;   // setTimeout handle for the capture loop
  let isProcessingFrame = false;  // guards against overlapping in-flight requests

  // How often we grab a frame from the webcam and send it to the server.
  // YOLOv11 + KNN + ByteTrack on CPU (Railway has no GPU) needs real time
  // per frame, so we throttle capture to avoid flooding the server with
  // more frames than it can process — this keeps the live feed smooth
  // instead of building up a backlog of stale requests.
  const FRAME_INTERVAL_MS = 200; // ~5 FPS round trip

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

    if (!running) {
      videoFeed.src = "";
    }
  }

  function setRecordingUI(recording) {
    btnStartRecording.disabled = recording;
    btnStopRecording.disabled = !recording;
  }

  /* ---------------------------- API CALLS ---------------------------- */
  async function postJSON(url, body) {
    try {
      const options = { method: "POST" };
      if (body !== undefined) {
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify(body);
      }
      const res = await fetch(url, options);
      return await res.json();
    } catch (err) {
      showNotification("Network error: " + err.message, "error");
      return { success: false, message: "Network error" };
    }
  }

  /* ============================================================================
     BROWSER WEBCAM ACCESS
  ============================================================================ */
  async function startWebcam() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showNotification("This browser does not support camera access.", "error");
      return false;
    }

    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 1280 },
          height: { ideal: 720 },
          facingMode: "user"
        },
        audio: false
      });

      webcamVideo.srcObject = mediaStream;
      await webcamVideo.play();
      return true;

    } catch (err) {
      showNotification("Camera access denied or unavailable: " + err.message, "error");
      return false;
    }
  }

  function stopWebcam() {
    if (mediaStream) {
      mediaStream.getTracks().forEach((track) => track.stop());
      mediaStream = null;
    }
    webcamVideo.srcObject = null;
  }

  /* ============================================================================
     CAPTURE LOOP: grab a frame -> send to /api/process_frame -> display result
     ------------------------------------------------------------------------
     This replaces the old MJPEG <img src="/video_feed"> mechanism (which
     depended on a server-side cv2.VideoCapture(0)). Now the browser is the
     camera source: it pulls a frame out of the hidden <video> element via
     <canvas>, JPEG-encodes it client-side, and POSTs it to the server. The
     server runs the exact same YOLOv11 + KNN + ByteTrack pipeline as before
     and returns the annotated frame, which is displayed in #video-feed —
     visually indistinguishable from a live stream to the user.
  ============================================================================ */
  function captureAndSendFrame() {
    if (!cameraRunning) return;

    // If a previous frame is still being processed, or the video isn't
    // ready yet, just try again shortly instead of piling up requests.
    if (isProcessingFrame || webcamVideo.readyState < 2) {
      captureTimer = setTimeout(captureAndSendFrame, FRAME_INTERVAL_MS);
      return;
    }

    captureCanvas.width = webcamVideo.videoWidth;
    captureCanvas.height = webcamVideo.videoHeight;
    captureCtx.drawImage(webcamVideo, 0, 0, captureCanvas.width, captureCanvas.height);

    const frameDataUrl = captureCanvas.toDataURL("image/jpeg", 0.75);

    isProcessingFrame = true;

    fetch("/api/process_frame", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: frameDataUrl })
    })
      .then((res) => res.json())
      .then((data) => {
        if (data.success && data.image) {
          videoFeed.src = data.image;
        }
      })
      .catch((err) => {
        console.error("Frame processing request failed:", err);
      })
      .finally(() => {
        isProcessingFrame = false;
        if (cameraRunning) {
          captureTimer = setTimeout(captureAndSendFrame, FRAME_INTERVAL_MS);
        }
      });
  }

  /* ---- START CAMERA ---- */
  btnStartCamera.addEventListener("click", async () => {
    btnStartCamera.disabled = true;

    const webcamReady = await startWebcam();
    if (!webcamReady) {
      btnStartCamera.disabled = false;
      return;
    }

    const data = await postJSON("/api/start_camera");
    if (data.success) {
      cameraRunning = true;
      setCameraRunningUI(true);
      startStatusPolling();
      captureAndSendFrame();
      showNotification(data.message, "success");
    } else {
      stopWebcam();
      btnStartCamera.disabled = false;
      showNotification(data.message, "error");
    }
  });

  /* ---- STOP CAMERA ---- */
  btnStopCamera.addEventListener("click", async () => {
    const data = await postJSON("/api/stop_camera");

    cameraRunning = false;
    if (captureTimer) {
      clearTimeout(captureTimer);
      captureTimer = null;
    }
    stopWebcam();

    setCameraRunningUI(false);
    setRecordingUI(false);
    stopStatusPolling();
    showNotification(data.message, data.success ? "success" : "error");
    resetStats();
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

      if (statMoving) statMoving.textContent = data.moving_objects;
      if (statTotal) statTotal.textContent = data.moving_objects;
      if (statFps) statFps.textContent = Number(data.fps).toFixed(1);

      if (statDetection) {
        statDetection.textContent = data.detection_status;
        statDetection.className = "stat-value status-pill " + (data.camera_active ? "active" : "idle");
      }

      if (statRecording) {
        statRecording.textContent = data.recording_status;
        statRecording.className = "stat-value status-pill " + (data.recording_active ? "recording" : "stopped");
      }

      // Keep UI in sync if the server-side pipeline was stopped externally
      // (e.g. server restarted) while the browser still thinks it's live.
      if (!data.camera_active && cameraRunning) {
        cameraRunning = false;
        if (captureTimer) {
          clearTimeout(captureTimer);
          captureTimer = null;
        }
        stopWebcam();
        setCameraRunningUI(false);
        stopStatusPolling();
      }
    } catch (err) {
      console.error("Status polling error:", err);
    }
  }

  function resetStats() {
    if (statMoving) statMoving.textContent = "0";
    if (statTotal) statTotal.textContent = "0";
    if (statFps) statFps.textContent = "0.0";
    if (statDetection) {
      statDetection.textContent = "IDLE";
      statDetection.className = "stat-value status-pill idle";
    }
    if (statRecording) {
      statRecording.textContent = "STOPPED";
      statRecording.className = "stat-value status-pill stopped";
    }
  }

  /* ---------------------------- ERROR HANDLING (LIVE FEED IMAGE) ---------------------------- */
  videoFeed.addEventListener("error", () => {
    if (videoFeed.classList.contains("active")) {
      console.warn("Live feed frame failed to render.");
    }
  });

  /* ---------------------------- CLEANUP ON PAGE UNLOAD ---------------------------- */
  window.addEventListener("beforeunload", () => {
    stopWebcam();
  });

  /* ---------------------------- INITIAL UI STATE ---------------------------- */
  setCameraRunningUI(false);
  setRecordingUI(false);
});