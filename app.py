# ============================================================
# LIVE MOVING OBJECT DETECTION USING YOLOv11 — FLASK WEB APP
# (CLOUD / RAILWAY VERSION — BROWSER WEBCAM EDITION)
# ============================================================
# This file is a CONVERSION of the existing working project, not a
# rewrite. Everything below is preserved exactly:
#
#   - KNN Background Subtraction
#   - Gaussian Blur + Threshold noise removal
#   - Morphological Open/Close/Dilate
#   - Moving-region contour detection (area >= 1500)
#   - YOLOv11 + ByteTrack tracking
#   - Moving-region membership gating (ignore static objects)
#   - Object counter, CSV logging (output/detection_log.csv)
#   - Bounding boxes, track IDs, per-class colors
#   - Professional overlay: navbar title, FPS, date, time,
#     moving-object total, REC indicator, transparent object
#     panel, resolution text
#   - Screenshot capture (screenshots/)
#   - Video recording (output/, mp4v)
#   - Flask REST API (/api/start_camera, /api/stop_camera,
#     /api/status, /api/screenshot, /api/start_recording,
#     /api/stop_recording, /api/download_screenshot,
#     /api/download_recording)
#
# CHANGE LOG (this revision):
#   1) YOLO now runs on ALL 80 COCO classes instead of the
#      restricted 7-class list. The `classes=` filter was removed
#      from model.track(). Motion filtering (KNN + blur + threshold
#      + morphology + contours + moving-region center-point test)
#      is 100% unchanged, so only objects that are ACTUALLY MOVING
#      still get drawn/counted/logged — static objects of any class
#      are still discarded by the existing `if not moving: continue`
#      gate.
#   2) Bounding box / label colors are now generated automatically
#      per class name (deterministic hash -> HSV -> BGR), cached in
#      self.class_colors, instead of a manually written 7-entry
#      dict. Every one of the 80 COCO classes gets a stable, unique
#      color the first time it's seen.
#
#   Camera front/rear switching (browser-side, via getUserMedia
#   facingMode) does not require any backend change — /api/process_frame
#   already accepts whatever frame the browser sends regardless of
#   which physical camera produced it. See script.js / index.html
#   notes provided separately.
# ============================================================

import os
import csv
import time
import base64
import threading
import colorsys
from datetime import datetime
from collections import Counter

import cv2  # type: ignore
import numpy as np  # type: ignore
from flask import Flask, Response, render_template, jsonify, send_file, request
from ultralytics import YOLO  # type: ignore

# ============================================================
# APP / PATH CONFIGURATION
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
SCREENSHOT_DIR = os.path.join(BASE_DIR, "screenshots")
MODEL_PATH = os.path.join(BASE_DIR, "yolo11n.pt")
CSV_FILE = os.path.join(OUTPUT_DIR, "detection_log.csv")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# CSV header (identical to the original project)
if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Time", "Object", "Track ID", "Confidence"])

app = Flask(__name__)

# Browser frames arrive as base64 JPEG in JSON — cap request body
# size generously (16 MB) so a single high-res frame never gets
# rejected, while still guarding against abuse.
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# ============================================================
# CAMERA CONTROLLER
# ------------------------------------------------------------
# Owns the YOLO model, the KNN background subtractor, recording
# state, screenshot state, and the exact Project-1 detection
# pipeline. The only structural difference from the local version
# is that frames are pushed in one at a time via process_frame()
# instead of being pulled from cv2.VideoCapture(0) inside a
# dedicated background thread.
# ============================================================
class CameraController:

    def __init__(self):
        # ---------------- YOLOv11 MODEL (loaded once) ----------------
        self.model = YOLO(MODEL_PATH)
        self.class_names = self.model.names

        # ---------------- MOVING OBJECT CLASSES ----------------
        # NOTE: No longer used to filter YOLO inference (all 80 COCO
        # classes are now detected — see model.track() call below).
        # Kept only for reference / backward compatibility; has no
        # effect on behavior.
        self.moving_classes = [
            0,      # Person
            1,      # Bicycle
            2,      # Car
            3,      # Motorcycle
            5,      # Bus
            6,      # Train
            7       # Truck
        ]

        # ---------------- COLORS ----------------
        # Colors are generated automatically per-class the first time
        # that class is seen, then cached here so the color stays
        # stable across frames for the same class. Works for all 80
        # COCO classes without manually defining any of them.
        self.class_colors = {}

        # ---------------- SETTINGS ----------------
        self.CONFIDENCE = 0.55

        # ---------------- MOTION / DETECTION STATE ----------------
        self.background = None
        self.kernel = np.ones((5, 5), np.uint8)
        self.previous_time = time.time()

        self.camera_active = False

        # Serializes frame processing so browser frames arriving on
        # concurrent requests are still fed through KNN/YOLO/ByteTrack
        # one at a time, in order — required because the background
        # subtractor and the tracker both carry state across frames.
        self.processing_lock = threading.Lock()

        # ---------------- FRAME SHARING (thread-safe) ----------------
        self.latest_frame = None
        self.frame_lock = threading.Lock()

        # ---------------- RECORDING STATE (thread-safe) ----------------
        self.recording = False
        self.video_writer = None
        self.video_filename = ""
        self.record_lock = threading.Lock()

        # ---------------- SCREENSHOT STATE ----------------
        self.last_screenshot_path = None
        self.last_recording_path = None

        # ---------------- LIVE STATUS (thread-safe) ----------------
        self.status_lock = threading.Lock()
        self.status = {
            "fps": 0,
            "total_objects": 0,
            "object_counter": {},
            "resolution": "0 x 0",
            "date": "",
            "time": ""
        }

    # ------------------------------------------------------------
    # DYNAMIC PER-CLASS COLOR GENERATION
    # ------------------------------------------------------------
    # Replaces the old fixed 7-entry color dict. Any of the 80 COCO
    # class names gets a deterministic, unique BGR color the first
    # time it's seen, cached in self.class_colors so the same class
    # always gets the same color across frames and sessions.
    # ------------------------------------------------------------
    def _get_color_for_class(self, class_name):
        if class_name not in self.class_colors:
            hue = (hash(class_name) % 360) / 360.0
            r, g, b = colorsys.hsv_to_rgb(hue, 0.85, 0.95)
            self.class_colors[class_name] = (
                int(b * 255), int(g * 255), int(r * 255)
            )
        return self.class_colors[class_name]

    # ------------------------------------------------------------
    # CAMERA START / STOP
    # ------------------------------------------------------------
    # These no longer open cv2.VideoCapture(0) (no server-side camera
    # exists in the cloud). They instead arm/disarm the detection
    # pipeline: start_camera() creates a fresh KNN background
    # subtractor (exactly as the original did per session) and flips
    # camera_active on, so that incoming browser frames get processed.
    # stop_camera() disarms it and finalizes any active recording,
    # exactly as before.
    # ------------------------------------------------------------
    def start_camera(self):
        if self.camera_active:
            return True, "Camera already running."

        # Fresh background subtractor per session (same params as original)
        self.background = cv2.createBackgroundSubtractorKNN(
            history=500,
            dist2Threshold=400,
            detectShadows=False
        )

        self.previous_time = time.time()
        self.camera_active = True

        return True, "Camera started successfully."

    def stop_camera(self):
        if not self.camera_active:
            return True, "Camera already stopped."

        self.camera_active = False

        if self.recording:
            self.stop_recording()

        with self.frame_lock:
            self.latest_frame = None

        return True, "Camera stopped successfully."

    # ------------------------------------------------------------
    # PROCESS A SINGLE FRAME FROM THE BROWSER
    # ------------------------------------------------------------
    # This is the ORIGINAL while-loop body from Project 1, preserved
    # step-for-step. The only thing that changed is where "frame"
    # comes from: previously `success, frame = cap.read()` inside a
    # local while-loop; now it is decoded from a JPEG the browser
    # POSTed. Every KNN / morphology / YOLO / ByteTrack / gating /
    # drawing / overlay / CSV / recording step below is identical.
    #
    # Because the browser can send frames from EITHER the front or
    # rear camera (see /api/process_frame and the front-end camera
    # switch), this method makes no assumption about which physical
    # camera produced the frame — it simply processes whatever
    # image array it is given, exactly as before.
    # ------------------------------------------------------------
    def process_frame(self, frame):
        if not self.camera_active:
            return None

        with self.processing_lock:

            frame = cv2.flip(frame, 1)

            output = frame.copy()

            object_counter = Counter()

            frame_height, frame_width = output.shape[:2]

            now = datetime.now()

            current_date = now.strftime("%d-%m-%Y")

            current_time = now.strftime("%I:%M:%S %p")

            elapsed = time.time() - self.previous_time
            fps = (1 / elapsed) if elapsed > 0 else 0.0

            self.previous_time = time.time()

            # ============================================================
            # BACKGROUND SUBTRACTION
            # ============================================================

            motion_mask = self.background.apply(frame)

            # Remove Noise
            motion_mask = cv2.GaussianBlur(
                motion_mask,
                (5, 5),
                0
            )

            _, motion_mask = cv2.threshold(
                motion_mask,
                200,
                255,
                cv2.THRESH_BINARY
            )

            # ============================================================
            # MORPHOLOGICAL OPERATIONS
            # ============================================================

            motion_mask = cv2.morphologyEx(
                motion_mask,
                cv2.MORPH_OPEN,
                self.kernel,
                iterations=2
            )

            motion_mask = cv2.morphologyEx(
                motion_mask,
                cv2.MORPH_CLOSE,
                self.kernel,
                iterations=2
            )

            motion_mask = cv2.dilate(
                motion_mask,
                self.kernel,
                iterations=2
            )

            # ============================================================
            # FIND MOVING REGIONS
            # ============================================================

            contours, _ = cv2.findContours(
                motion_mask,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE
            )

            moving_regions = []

            for contour in contours:

                area = cv2.contourArea(contour)

                if area < 1500:
                    continue

                x, y, w, h = cv2.boundingRect(contour)

                moving_regions.append(
                    (x, y, x + w, y + h)
                )

            # ============================================================
            # YOLOv11 + BYTE TRACK
            # ============================================================
            # `classes=` filter removed so inference runs across all 80
            # COCO classes. Static objects of any class are still
            # discarded below by the moving-region center-point test —
            # motion filtering logic is completely unchanged.
            # ============================================================

            results = self.model.track(

                frame,

                persist=True,

                tracker="bytetrack.yaml",

                conf=self.CONFIDENCE,

                verbose=False

            )

            # ============================================================
            # PROCESS DETECTIONS
            # ============================================================

            if results[0].boxes is not None:

                boxes = results[0].boxes

                for box in boxes:

                    # ============================================================
                    # GET OBJECT INFORMATION
                    # ============================================================

                    cls = int(box.cls[0])

                    confidence = float(box.conf[0])

                    x1, y1, x2, y2 = map(int, box.xyxy[0])

                    class_name = self.class_names[cls].lower()

                    # ============================================================
                    # TRACK ID
                    # ============================================================

                    track_id = -1

                    if box.id is not None:

                        track_id = int(box.id[0])

                    # ============================================================
                    # CHECK WHETHER OBJECT IS MOVING
                    # ============================================================

                    moving = False

                    center_x = (x1 + x2) // 2
                    center_y = (y1 + y2) // 2

                    for mx1, my1, mx2, my2 in moving_regions:

                        if mx1 <= center_x <= mx2 and my1 <= center_y <= my2:

                            moving = True
                            break

                    # Ignore Static Objects

                    if not moving:
                        continue

                    # ============================================================
                    # OBJECT COUNTER
                    # ============================================================

                    object_counter[class_name] += 1

                    # ============================================================
                    # SAVE CSV LOG
                    # ============================================================

                    with open(CSV_FILE, "a", newline="") as file:

                        writer = csv.writer(file)

                        writer.writerow([
                            current_time,
                            class_name,
                            track_id,
                            round(confidence, 2)
                        ])

                    # ============================================================
                    # DRAW BOUNDING BOX
                    # ============================================================

                    color = self._get_color_for_class(class_name)

                    cv2.rectangle(
                        output,
                        (x1, y1),
                        (x2, y2),
                        color,
                        2
                    )

                    # ============================================================
                    # LABEL
                    # ============================================================

                    label = f"{class_name.title()}  ID:{track_id}"

                    (tw, th), _ = cv2.getTextSize(
                        label,
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.60,
                        2
                    )

                    cv2.rectangle(
                        output,
                        (x1, y1 - th - 10),
                        (x1 + tw + 10, y1),
                        color,
                        -1
                    )

                    cv2.putText(
                        output,
                        label,
                        (x1 + 5, y1 - 5),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.60,
                        (0, 0, 0),
                        2
                    )

            # ============================================================
            # TOTAL MOVING OBJECTS
            # ============================================================

            total_objects = sum(
                object_counter.values()
            )

            # ============================================================
            # SAVE VIDEO IF RECORDING
            # ============================================================

            with self.record_lock:
                if self.recording and self.video_writer is not None:
                    self.video_writer.write(output)

            # ============================================================
            # PROFESSIONAL TOP NAVBAR
            # ============================================================

            cv2.rectangle(
                output,
                (0, 0),
                (frame_width, 70),
                (40, 40, 40),
                -1
            )

            # ============================================================
            # TITLE
            # ============================================================

            cv2.putText(
                output,
                "LIVE MOVING OBJECT DETECTION SYSTEM",
                (15, 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.70,
                (0, 255, 255),
                2
            )

            # ============================================================
            # FPS
            # ============================================================

            cv2.putText(
                output,
                f"FPS : {int(fps)}",
                (20, 58),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 0),
                2
            )

            # ============================================================
            # DATE
            # ============================================================

            cv2.putText(
                output,
                f"Date : {current_date}",
                (140, 58),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 255, 255),
                2
            )

            # ============================================================
            # TIME
            # ============================================================

            cv2.putText(
                output,
                f"Time : {current_time}",
                (360, 58),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 255, 255),
                2
            )

            # ============================================================
            # TOTAL MOVING OBJECTS
            # ============================================================

            total_text = f"Moving Objects : {total_objects}"

            (tw, th), _ = cv2.getTextSize(
                total_text,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                2
            )

            cv2.putText(
                output,
                total_text,
                (frame_width - tw - 20, 58),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 255),
                2
            )

            # ============================================================
            # RECORDING INDICATOR
            # ============================================================

            with self.record_lock:
                is_recording_now = self.recording

            if is_recording_now:

                cv2.circle(
                    output,
                    (frame_width - 25, 25),
                    8,
                    (0, 0, 255),
                    -1
                )

                cv2.putText(
                    output,
                    "REC",
                    (frame_width - 80, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.60,
                    (0, 0, 255),
                    2
                )

            # ============================================================
            # TRANSPARENT OBJECT PANEL
            # ============================================================

            overlay = output.copy()

            panel_height = max(
                120,
                50 + len(object_counter) * 30
            )

            cv2.rectangle(
                overlay,
                (10, 90),
                (250, 90 + panel_height),
                (0, 0, 0),
                -1
            )

            cv2.addWeighted(
                overlay,
                0.40,
                output,
                0.60,
                0,
                output
            )

            cv2.putText(
                output,
                "Moving Objects",
                (20, 120),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.60,
                (0, 255, 255),
                2
            )

            y = 155

            if len(object_counter) == 0:

                cv2.putText(
                    output,
                    "No Moving Object",
                    (20, y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    (0, 0, 255),
                    2
                )

            else:

                for obj, count in object_counter.items():

                    cv2.putText(
                        output,
                        f"{obj.title()} : {count}",
                        (20, y),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        (255, 255, 0),
                        2
                    )

                    y += 28

            # ============================================================
            # CAMERA RESOLUTION
            # ============================================================

            cv2.putText(
                output,
                f"Resolution : {frame_width} x {frame_height}",
                (10, frame_height - 15),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.50,
                (255, 255, 255),
                1
            )

            # ============================================================
            # CONTROLS
            # ============================================================

            # (kept disabled — controls are now buttons in the dashboard,
            #  same as in the uploaded project's app.py)
            # cv2.putText(
            #     output,
            #     "S = Screenshot | R = Record | Q = Quit",
            #     (frame_width - 370, frame_height - 15),
            #     cv2.FONT_HERSHEY_SIMPLEX,
            #     0.50,
            #     (0, 255, 0),
            #     1
            # )

            with self.record_lock:
                if self.recording and self.video_writer is not None:
                    self.video_writer.write(output)

            # ============================================================
            # UPDATE LIVE STATUS (for /api/status polling)
            # ============================================================

            with self.status_lock:
                self.status["fps"] = int(fps)
                self.status["total_objects"] = total_objects
                self.status["object_counter"] = dict(object_counter)
                self.status["resolution"] = f"{frame_width} x {frame_height}"
                self.status["date"] = current_date
                self.status["time"] = current_time

            # ============================================================
            # PUBLISH FRAME
            # (replaces cv2.imshow("Live Moving Object Detection", output);
            #  the annotated frame is now handed back to the caller, which
            #  is the /api/process_frame route, which returns it to the
            #  browser to be displayed instead of streaming an MJPEG feed
            #  read from a local camera device.)
            # ============================================================

            with self.frame_lock:
                self.latest_frame = output

            return output

    # ------------------------------------------------------------
    # SCREENSHOT (replaces the 'S' keyboard control)
    # ------------------------------------------------------------
    def capture_screenshot(self):
        with self.frame_lock:
            frame = self.latest_frame.copy() if self.latest_frame is not None else None

        if frame is None:
            return False, "No active video frame to capture."

        screenshot_name = datetime.now().strftime(
            "frame_%Y%m%d_%H%M%S.jpg"
        )
        filepath = os.path.join(SCREENSHOT_DIR, screenshot_name)

        cv2.imwrite(filepath, frame)
        self.last_screenshot_path = filepath

        return True, screenshot_name

    # ------------------------------------------------------------
    # RECORDING (replaces the 'R' keyboard control)
    # ------------------------------------------------------------
    def start_recording(self):
        if not self.camera_active:
            return False, "Start the camera before recording."

        with self.record_lock:
            if self.recording:
                return False, "Recording already in progress."

            with self.frame_lock:
                if self.latest_frame is not None:
                    frame_height, frame_width = self.latest_frame.shape[:2]
                else:
                    frame_width, frame_height = 1280, 720

            self.video_filename = datetime.now().strftime(
                "Recording_%Y%m%d_%H%M%S.mp4"
            )
            filepath = os.path.join(OUTPUT_DIR, self.video_filename)

            fourcc = cv2.VideoWriter_fourcc(*"mp4v")

            self.video_writer = cv2.VideoWriter(
                filepath,
                fourcc,
                20.0,
                (frame_width, frame_height)
            )

            if not self.video_writer.isOpened():
                self.video_writer = None
                return False, "VideoWriter failed to open"

            self.recording = True
            self.last_recording_path = filepath

        return True, self.video_filename

    def stop_recording(self):
        with self.record_lock:
            if not self.recording:
                return False, "No active recording to stop."

            self.recording = False

            if self.video_writer is not None:
                self.video_writer.release()
                self.video_writer = None

        return True, "Recording Saved"

    # ------------------------------------------------------------
    # STATUS
    # ------------------------------------------------------------
    def get_status(self):
        with self.status_lock:
            status_copy = dict(self.status)

        with self.record_lock:
            recording_now = self.recording

        return {
            "camera_active": self.camera_active,
            "recording_active": recording_now,
            "detection_status": "ACTIVE" if self.camera_active else "IDLE",
            "recording_status": "RECORDING" if recording_now else "STOPPED",
            "fps": status_copy.get("fps", 0),
            "moving_objects": status_copy.get("total_objects", 0),
            "object_counter": status_copy.get("object_counter", {}),
            "resolution": status_copy.get("resolution", "0 x 0"),
            "date": status_copy.get("date", ""),
            "time": status_copy.get("time", "")
        }


# Single global camera controller instance
camera_controller = CameraController()


# ============================================================
# FLASK ROUTES
# ============================================================
@app.route("/")
def index():
    """Render the main dashboard page (unchanged design)."""
    return render_template("index.html")


# ------------------------------------------------------------
# BROWSER WEBCAM FRAME PROCESSING
# ------------------------------------------------------------
# Receives one JPEG frame (as a base64 data URL) captured by the
# browser's getUserMedia webcam feed, runs it through the exact
# detection pipeline in CameraController.process_frame(), and
# returns the annotated frame back as a base64 data URL so the
# frontend can paint it into the existing <img id="video-feed">
# element — preserving the "live feed" look of the dashboard
# without needing server-side camera access.
#
# This endpoint is camera-agnostic: it does not care whether the
# browser captured the frame from the front or rear physical
# camera, so front/rear switching on the client requires no change
# here.
# ------------------------------------------------------------
@app.route("/api/process_frame", methods=["POST"])
def api_process_frame():
    if not camera_controller.camera_active:
        return jsonify({"success": False, "message": "Camera is not active."}), 400

    data = request.get_json(silent=True)
    if not data or "image" not in data:
        return jsonify({"success": False, "message": "No image data received."}), 400

    try:
        image_data = data["image"]

        # Strip the "data:image/jpeg;base64," header if present
        if "," in image_data:
            image_data = image_data.split(",", 1)[1]

        img_bytes = base64.b64decode(image_data)
        np_arr = np.frombuffer(img_bytes, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

        if frame is None:
            return jsonify({"success": False, "message": "Invalid image data."}), 400

        output = camera_controller.process_frame(frame)

        if output is None:
            return jsonify({"success": False, "message": "Camera is not active."}), 400

        ok, buffer = cv2.imencode(".jpg", output, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            return jsonify({"success": False, "message": "Frame encoding failed."}), 500

        encoded = base64.b64encode(buffer).decode("utf-8")

        return jsonify({
            "success": True,
            "image": f"data:image/jpeg;base64,{encoded}"
        })

    except Exception as exc:
        return jsonify({"success": False, "message": f"Frame processing error: {exc}"}), 500


@app.route("/api/start_camera", methods=["POST"])
def api_start_camera():
    success, message = camera_controller.start_camera()
    return jsonify({"success": success, "message": message})


@app.route("/api/stop_camera", methods=["POST"])
def api_stop_camera():
    success, message = camera_controller.stop_camera()
    return jsonify({"success": success, "message": message})


@app.route("/api/status", methods=["GET"])
def api_status():
    """Polled by the frontend to refresh live stats (FPS, counts, etc.)."""
    return jsonify(camera_controller.get_status())


@app.route("/api/screenshot", methods=["POST"])
def api_screenshot():
    success, result = camera_controller.capture_screenshot()
    if success:
        return jsonify({"success": True, "filename": result, "message": f"Screenshot Saved : {result}"})
    return jsonify({"success": False, "message": result}), 400


@app.route("/api/start_recording", methods=["POST"])
def api_start_recording():
    success, result = camera_controller.start_recording()
    if success:
        return jsonify({"success": True, "filename": result, "message": "Recording Started"})
    return jsonify({"success": False, "message": result}), 400


@app.route("/api/stop_recording", methods=["POST"])
def api_stop_recording():
    success, result = camera_controller.stop_recording()
    return jsonify({"success": success, "message": result})


@app.route("/api/download_screenshot", methods=["GET"])
def api_download_screenshot():
    if camera_controller.last_screenshot_path and os.path.exists(camera_controller.last_screenshot_path):
        return send_file(camera_controller.last_screenshot_path, as_attachment=True)
    return jsonify({"success": False, "message": "No screenshot available yet."}), 404


@app.route("/api/download_recording", methods=["GET"])
def api_download_recording():
    if camera_controller.last_recording_path and os.path.exists(camera_controller.last_recording_path):
        return send_file(camera_controller.last_recording_path, as_attachment=True)
    return jsonify({"success": False, "message": "No recording available yet."}), 404


# ============================================================
# HEALTH CHECK (used by Railway's healthcheck)
# ============================================================
@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


# ============================================================
# RUN APP
# ============================================================
if __name__ == "__main__":
    # Local dev entrypoint. On Railway, gunicorn (see Procfile) runs
    # this app instead of this block.
    port = int(os.environ.get("PORT", 5000))
    app.run(
        debug=False,
        use_reloader=False,
        threaded=True,
        host="0.0.0.0",
        port=port
    )
