# ============================================================
# LIVE MOVING OBJECT DETECTION USING YOLOv11 — FLASK WEB APP
# ============================================================
# This file merges TWO existing projects:
#
#   PROJECT 1 (preserved exactly): KNN Background Subtraction +
#   Morphological Filtering + Moving-Region Gating + YOLOv11 +
#   ByteTrack detection pipeline, CSV logging, screenshot/recording,
#   professional on-frame overlay (navbar, FPS, date, time, object
#   panel, resolution, recording indicator).
#
#   PROJECT 2 (used only for integration): Flask routes, MJPEG
#   streaming, threading/locking pattern.
#
# THE ONLY CHANGE MADE TO PROJECT 1's LOGIC:
#   cv2.imshow(...) / cv2.waitKey(...) desktop display + keyboard
#   control loop is replaced with Flask MJPEG streaming + REST API
#   endpoints that toggle the same recording/screenshot behavior.
#   The detection pipeline itself (KNN -> morphology -> moving
#   regions -> YOLOv11 -> ByteTrack -> moving-region membership
#   check -> ignore static objects -> draw boxes/labels/overlay ->
#   CSV log -> recording write) is untouched and runs in the exact
#   same order as Project 1.
# ============================================================

import os
import csv
import time
import threading
from datetime import datetime
from collections import Counter

import cv2 # type: ignore
import numpy as np # type: ignore
from flask import Flask, Response, render_template, jsonify, send_file
from ultralytics import YOLO # type: ignore

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

# CSV header (identical to Project 1)
if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Time", "Object", "Track ID", "Confidence"])

app = Flask(__name__)


# ============================================================
# CAMERA CONTROLLER
# ------------------------------------------------------------
# Wraps Project 1's exact detection pipeline inside a background
# thread so it can feed a Flask MJPEG stream instead of an
# OpenCV desktop window. No detection/motion logic is altered —
# only the display + keyboard-control layer is replaced with
# thread-safe state controlled via Flask API endpoints.
# ============================================================
class CameraController:

    def __init__(self):
        # ---------------- YOLOv11 MODEL (loaded once) ----------------
        self.model = YOLO(MODEL_PATH)
        self.class_names = self.model.names

        # ---------------- MOVING OBJECT CLASSES ----------------
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
        self.colors = {
            "person": (0, 255, 0),
            "bicycle": (255, 255, 0),
            "car": (255, 0, 0),
            "motorcycle": (255, 0, 255),
            "bus": (0, 165, 255),
            "train": (0, 255, 255),
            "truck": (128, 0, 255)
        }

        # ---------------- SETTINGS ----------------
        self.CONFIDENCE = 0.55

        # ---------------- CAMERA / THREAD STATE ----------------
        self.cap = None
        self.background = None
        self.kernel = np.ones((5, 5), np.uint8)
        self.previous_time = time.time()

        self.camera_active = False
        self.thread = None
        self.stop_event = threading.Event()

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
    # CAMERA START / STOP
    # ------------------------------------------------------------
    def start_camera(self):
        if self.camera_active:
            return True, "Camera already running."

        self.cap = cv2.VideoCapture(0)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

        if not self.cap.isOpened():
            self.cap = None
            return False, "Unable to Open Camera"

        # Fresh background subtractor per session (same params as Project 1)
        self.background = cv2.createBackgroundSubtractorKNN(
            history=500,
            dist2Threshold=400,
            detectShadows=False
        )

        self.previous_time = time.time()
        self.camera_active = True
        self.stop_event.clear()

        self.thread = threading.Thread(target=self._processing_loop, daemon=True)
        self.thread.start()

        return True, "Camera started successfully."

    def stop_camera(self):
        if not self.camera_active:
            return True, "Camera already stopped."

        self.stop_event.set()
        self.camera_active = False

        if self.recording:
            self.stop_recording()

        if self.thread:
            self.thread.join(timeout=2)

        if self.cap is not None:
            self.cap.release()
            self.cap = None

        with self.frame_lock:
            self.latest_frame = None

        return True, "Camera stopped successfully."

    # ------------------------------------------------------------
    # MAIN PROCESSING LOOP
    # (Project 1's while-loop body, preserved exactly — only the
    #  cv2.imshow/cv2.waitKey desktop-display block and keyboard
    #  handling have been removed, since those are replaced by
    #  Flask MJPEG streaming and the REST API below.)
    # ------------------------------------------------------------
    def _processing_loop(self):

        while not self.stop_event.is_set():

            success, frame = self.cap.read()

            if not success:
                break

            frame = cv2.flip(frame, 1)

            output = frame.copy()

            object_counter = Counter()

            frame_height, frame_width = output.shape[:2]

            now = datetime.now()

            current_date = now.strftime("%d-%m-%Y")

            current_time = now.strftime("%I:%M:%S %p")

            fps = 1 / (time.time() - self.previous_time)

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

            results = self.model.track(

                frame,

                persist=True,

                tracker="bytetrack.yaml",

                classes=self.moving_classes,

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

                    color = self.colors.get(
                        class_name,
                        (255, 255, 255)
                    )

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
            # PUBLISH FRAME FOR MJPEG STREAMING
            # (replaces cv2.imshow("Live Moving Object Detection", output))
            # ============================================================

            with self.frame_lock:
                self.latest_frame = output

        # ---------------- LOOP ENDED: RELEASE RESOURCES ----------------
        with self.record_lock:
            if self.video_writer is not None:
                self.video_writer.release()
                self.video_writer = None
                self.recording = False

    # ------------------------------------------------------------
    # MJPEG STREAM GENERATOR
    # ------------------------------------------------------------
    def generate_mjpeg(self):
        while self.camera_active:
            with self.frame_lock:
                frame = self.latest_frame.copy() if self.latest_frame is not None else None

            if frame is None:
                time.sleep(0.03)
                continue

            ok, buffer = cv2.imencode(".jpg", frame)
            if not ok:
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
            )
            time.sleep(0.03)

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
    """Render the main dashboard page."""
    return render_template("index.html")


@app.route("/video_feed")
def video_feed():
    """MJPEG streaming route consumed by the <img> tag on the frontend."""
    if not camera_controller.camera_active:
        return Response(status=204)
    return Response(
        camera_controller.generate_mjpeg(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


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
# RUN APP
# ============================================================
if __name__ == "__main__":
    # threaded=True allows concurrent handling of MJPEG stream + status polling
    app.run(
    debug=True,
    use_reloader=False,
    threaded=True,
    host="0.0.0.0",
    port=5000
)