/* ============================================================================
   CYBER NEON AI DASHBOARD — FRONTEND LOGIC (BROWSER WEBCAM EDITION)
   ------------------------------------------------------------------------
   Handles: live clock, camera controls, BROWSER webcam capture via
   getUserMedia + canvas, POSTing frames to /api/process_frame, painting the
   annotated frame returned by the server back into the dashboard, status
   polling, screenshots, recording, downloads, notifications, loading
   animation, and front/rear camera switching.

   NOTE: getUserMedia() requires a secure context (HTTPS or localhost).
   Railway serves the app over HTTPS by default, so camera access will work
   once deployed. On localhost during development it also works over plain
   http://localhost.

   CAMERA SWITCHING NOTE:
   Camera switching is implemented using navigator.mediaDevices.enumerateDevices()
   and explicit deviceId targeting (constraint: { exact: deviceId } ), instead of
   the "facingMode: user/environment" approach. facingMode is unreliable on many
   Android devices (especially Samsung's browser and multi-lens phones), where it
   can throw OverconstrainedError / "Could not switch video source" because the
   browser cannot map the abstract facingMode hint to one of several physical
   lenses. Enumerating actual videoinput devices and cycling through their
   concrete deviceIds is the professional, cross-platform-safe approach.
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
  const btnSwitchCamera       = document.getElementById("btn-switch-camera");
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

  // ---- Multi-camera (device enumeration) switching state ----
  // Replaces the old facingMode "user"/"environment" approach, which is
  // unreliable on many Android devices (notably Samsung's browser and
  // phones with multiple rear lenses). Instead we enumerate every actual
  // videoinput device the browser can see and cycle through their real
  // deviceIds, which is supported consistently across platforms.
  let videoDevices      = [];     // array of { deviceId, label } videoinput devices
  let currentDeviceIndex = 0;     // index into videoDevices of the camera in use
  let isSwitchingCamera  = false; // guards against double-clicks mid-switch

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
    // Switch Camera stays disabled whenever the camera isn't running, and is
    // re-enabled only if the camera IS running AND more than one physical
    // camera device was found during enumeration (see refreshSwitchButtonUI).
    btnSwitchCamera.disabled = !running || videoDevices.length <= 1;
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

  // Re-applies the Switch Camera button's enabled/disabled state based on
  // how many cameras are currently known, without touching any other UI.
  function refreshSwitchButtonUI() {
    btnSwitchCamera.disabled = !cameraRunning || videoDevices.length <= 1;
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
     DEVICE ENUMERATION
     ------------------------------------------------------------------------
     Lists every physical camera the browser can see. Device labels are only
     populated by the browser once camera permission has been granted at
     least once, so this is called AFTER the first successful getUserMedia()
     call (both on Start Camera and again defensively before switching).
  ============================================================================ */
  async function refreshVideoDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
      videoDevices = [];
      return;
    }

    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      videoDevices = devices.filter((d) => d.kind === "videoinput");
    } catch (err) {
      console.error("enumerateDevices failed:", err);
      videoDevices = [];
    }
  }

  // Matches the deviceId actually in use by a live stream's video track
  // against our enumerated videoDevices list, so currentDeviceIndex stays
  // accurate even if the browser picked a different default camera than
  // index 0 (this can happen on some devices/OS camera policies).
  function syncCurrentDeviceIndex(stream) {
    if (!stream) return;
    const track = stream.getVideoTracks()[0];
    if (!track) return;

    const settings = track.getSettings ? track.getSettings() : {};
    const activeDeviceId = settings.deviceId;
    if (!activeDeviceId) return;

    const idx = videoDevices.findIndex((d) => d.deviceId === activeDeviceId);
    if (idx !== -1) {
      currentDeviceIndex = idx;
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
      // Initial camera request. We don't yet know device IDs (labels are
      // hidden until permission is granted), so we ask for the default
      // front-facing camera the same way the browser always has. Once
      // permission is granted this unlocks enumerateDevices() labels,
      // which we use for ALL subsequent switching.
      mediaStream = await navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 1280 },
          height: { ideal: 720 },
          facingMode: { ideal: "user" }
        },
        audio: false
      });

      webcamVideo.srcObject = mediaStream;
      await webcamVideo.play();

      // Now that permission is granted, build the real device list and
      // figure out which physical camera we actually landed on.
      await refreshVideoDevices();
      currentDeviceIndex = 0;
      syncCurrentDeviceIndex(mediaStream);

      if (videoDevices.length <= 1) {
        showNotification("Only one camera available.", "info");
      }
      refreshSwitchButtonUI();

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
     SWITCH CAMERA (cycles through every enumerated camera device)
     ------------------------------------------------------------------------
     Uses navigator.mediaDevices.enumerateDevices() results and opens the
     next physical camera via deviceId: { exact: deviceId } — NOT facingMode.
     This is far more reliable across Android Chrome, Samsung Browser, Edge,
     desktop Chrome/laptop webcams, and iPhone Safari, because it targets a
     concrete hardware device instead of an abstract "front/back" hint that
     some devices/browsers can't resolve (causing OverconstrainedError /
     "Could not switch video source").

     The capture loop (captureAndSendFrame) keeps running the entire time
     and simply keeps pulling frames from webcamVideo — since it reads
     whatever is currently attached, once srcObject is swapped the very
     next capture tick starts sending frames from the new camera
     automatically. This means detection, recording, and screenshots all
     continue working without any interruption or page reload.

     The old stream's tracks are only stopped AFTER the new stream is
     successfully acquired, so if the switch fails the current camera
     keeps running instead of going dark.
  ============================================================================ */
  async function switchCamera() {
    if (!cameraRunning || isSwitchingCamera) return;

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showNotification("This browser does not support camera switching.", "error");
      return;
    }

    // Defensive re-enumeration in case devices changed (e.g. a USB webcam
    // was plugged in) since the camera was started.
    await refreshVideoDevices();

    if (videoDevices.length <= 1) {
      showNotification("Only one camera available.", "info");
      refreshSwitchButtonUI();
      return;
    }

    isSwitchingCamera = true;
    btnSwitchCamera.disabled = true;

    const nextIndex = (currentDeviceIndex + 1) % videoDevices.length;
    const nextDevice = videoDevices[nextIndex];

    try {
      const newStream = await navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 1280 },
          height: { ideal: 720 },
          deviceId: { exact: nextDevice.deviceId }
        },
        audio: false
      });

      // Stop the previous MediaStream only now that the new one is ready.
      if (mediaStream) {
        mediaStream.getTracks().forEach((track) => track.stop());
      }

      mediaStream = newStream;
      webcamVideo.srcObject = mediaStream;
      await webcamVideo.play();

      currentDeviceIndex = nextIndex;
      // Re-sync in case the browser didn't honor the exact deviceId 1:1
      // (rare, but keeps state consistent if it happens).
      syncCurrentDeviceIndex(mediaStream);

      const label = nextDevice.label || `Camera ${nextIndex + 1}`;
      showNotification(`Switched to ${label}`, "success");

    } catch (err) {
      showNotification("Camera switch failed: " + err.message, "error");
      // Old stream (if any) was never stopped, so detection keeps running
      // on the previous camera uninterrupted.
    } finally {
      isSwitchingCamera = false;
      refreshSwitchButtonUI();
    }
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

    // Always start on the default (first/front) camera.
    currentDeviceIndex = 0;

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
    currentDeviceIndex = 0; // reset so next Start Camera opens the default camera

    setCameraRunningUI(false);
    setRecordingUI(false);
    stopStatusPolling();
    showNotification(data.message, data.success ? "success" : "error");
    resetStats();
  });

  /* ---- SWITCH CAMERA ---- */
  btnSwitchCamera.addEventListener("click", switchCamera);

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
        currentDeviceIndex = 0;
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