#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Raspberry Pi YOLO11 NCNN - Async 1080p V6.1

Main goals
----------
1. Any local/RTSP/RTMP/HTTP input is normalized to 1920x1080 @ 10 FPS.
2. The VIDEO branch never waits for YOLO.
3. The AI branch runs asynchronously at a target 5 FPS on a low-res ROI copy.
4. Detection boxes are mapped back to the 1080p frame.
5. The push branch uses low-latency libx264 settings and reports REAL wall-clock FPS.
6. RTMP reconnect is automatic.
7. Old detection boxes are hidden after max_box_age seconds.\n8. V6 can POST fish-count/track JSON to a central server without blocking video/AI threads.

Recommended starting point
--------------------------
video_fps      = 10
ai_fps         = 10
main output    = 1920x1080
ai_long_side   = 320
imgsz          = 320
conf           = 0.90
bitrate        = 1600k
x264_threads   = 2
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import cv2
import numpy as np
from ultralytics import YOLO
from ultralytics.utils.plotting import colors


# ============================================================
# OpenCV thread policy
# ============================================================

try:
    cv2.setNumThreads(1)
except Exception:
    pass


# ============================================================
# Global state
# ============================================================

STOP_EVENT = threading.Event()

FRAME_LOCK = threading.Lock()
LATEST_FRAME = None
LATEST_FRAME_SEQ = 0
LATEST_FRAME_TS = 0.0
LATEST_CONNECTION_ID = 0

DET_LOCK = threading.Lock()
LATEST_DETECTIONS = []

# AI result publication time, used for stale-result checks.
LATEST_DET_TS = 0.0

# Timestamp of the source video frame that produced the AI result.
# Used for motion compensation to the newest 1080p frame.
LATEST_DET_FRAME_TS = 0.0

LATEST_DET_SEQ = 0
LATEST_DET_CONNECTION_ID = 0

TRACK_HISTORY_LOCK = threading.Lock()
TRACK_HISTORY = {}

STATS_LOCK = threading.Lock()
STATS = {
    "capture_frames": 0,
    "capture_start_wall": 0.0,
    "input_restarts": 0,

    "ai_frames": 0,
    "ai_start_wall": 0.0,
    "ai_skipped": 0,
    "ai_resize_ms_sum": 0.0,
    "ai_track_ms_sum": 0.0,
    "ai_total_ms_sum": 0.0,
    "ai_last_ms": 0.0,

    "push_frames": 0,
    "push_start_wall": 0.0,
    "push_restarts": 0,
    "push_write_ms_sum": 0.0,
    "push_write_ms_max": 0.0,

    "report_ok": 0,
    "report_fail": 0,
    "report_last_ms": 0.0,
}


# ============================================================
# Helpers
# ============================================================

def is_network_source(source):
    s = source.lower()
    return s.startswith((
        "rtsp://",
        "rtmp://",
        "http://",
        "https://",
        "udp://",
        "tcp://",
    ))


def is_rtsp_source(source):
    return source.lower().startswith("rtsp://")


def is_http_source(source):
    return source.lower().startswith(("http://", "https://"))


# ============================================================
# Probe input size
# ============================================================

def probe_source_size(source):
    print()
    print("[PROBE] Detecting source resolution...")

    cmd = [
        "ffprobe",
        "-v",
        "error",
    ]

    if is_rtsp_source(source):
        cmd += [
            "-rtsp_transport",
            "tcp",
            "-rw_timeout",
            "5000000",
        ]

    cmd += [
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        source,
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=12,
        )

        text = result.stdout.strip()

        if text:
            first = text.splitlines()[0].strip()

            if "x" in first:
                w_text, h_text = first.split("x", 1)
                w = int(w_text)
                h = int(h_text)

                if w > 0 and h > 0:
                    print(f"[PROBE] Source size: {w}x{h}")
                    return w, h

        if result.stderr.strip():
            print("[PROBE] ffprobe stderr:")
            print(result.stderr.strip())

    except Exception as e:
        print("[PROBE] Warning:", repr(e))

    print("[PROBE] Failed to determine source size.")
    return None, None


# ============================================================
# ROI geometry
# ============================================================

def calculate_active_roi(source_w, source_h, main_w, main_h):
    scale = min(
        main_w / float(source_w),
        main_h / float(source_h),
    )

    active_w = int(round(source_w * scale))
    active_h = int(round(source_h * scale))

    active_w = max(1, min(active_w, main_w))
    active_h = max(1, min(active_h, main_h))

    roi_x = (main_w - active_w) // 2
    roi_y = (main_h - active_h) // 2

    return roi_x, roi_y, active_w, active_h


def calculate_ai_size(roi_w, roi_h, ai_long_side):
    if roi_w >= roi_h:
        ai_w = ai_long_side
        ai_h = int(round(ai_long_side * roi_h / float(roi_w)))
    else:
        ai_h = ai_long_side
        ai_w = int(round(ai_long_side * roi_w / float(roi_h)))

    ai_w = max(16, ai_w)
    ai_h = max(16, ai_h)

    if ai_w % 2:
        ai_w -= 1

    if ai_h % 2:
        ai_h -= 1

    return ai_w, ai_h


# ============================================================
# FFmpeg input
# ============================================================

def build_input_command(source, width, height, fps, loop_file):
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
    ]

    if is_rtsp_source(source):
        cmd += [
            "-rtsp_transport",
            "tcp",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-rw_timeout",
            "5000000",
        ]

    elif is_http_source(source):
        cmd += [
            "-rw_timeout",
            "5000000",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "2",
        ]

    elif not is_network_source(source):
        # Simulate a live stream when testing with a local file.
        cmd += ["-re"]

        if loop_file:
            cmd += [
                "-stream_loop",
                "-1",
            ]

    cmd += [
        "-i",
        source,
        "-an",
    ]

    vf = (
        f"fps={fps},"
        f"scale={width}:{height}:"
        f"force_original_aspect_ratio=decrease:"
        f"flags=fast_bilinear,"
        f"pad={width}:{height}:"
        f"(ow-iw)/2:(oh-ih)/2:"
        f"color=black"
    )

    cmd += [
        "-vf",
        vf,
        "-pix_fmt",
        "bgr24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]

    return cmd


def read_exact(pipe, size):
    data = bytearray()

    while len(data) < size and not STOP_EVENT.is_set():
        chunk = pipe.read(size - len(data))

        if not chunk:
            break

        data.extend(chunk)

    return bytes(data)


def capture_worker(
    source,
    width,
    height,
    video_fps,
    loop_file,
    reconnect_delay,
):
    global LATEST_FRAME
    global LATEST_FRAME_SEQ
    global LATEST_FRAME_TS
    global LATEST_CONNECTION_ID

    frame_size = width * height * 3
    seq = 0
    connection_id = 0

    while not STOP_EVENT.is_set():
        connection_id += 1

        with STATS_LOCK:
            if STATS["capture_start_wall"] <= 0:
                STATS["capture_start_wall"] = time.perf_counter()

            if connection_id > 1:
                STATS["input_restarts"] += 1

        cmd = build_input_command(
            source,
            width,
            height,
            video_fps,
            loop_file,
        )

        print()
        print("=" * 78)
        print("[INPUT] Starting FFmpeg")
        print("=" * 78)
        print("Source      :", source)
        print("Normalize   :", f"{width}x{height} @ {video_fps:.2f} FPS")
        print("Connection  :", connection_id)
        print("[INPUT FFMPEG]", " ".join(cmd))
        print()

        process = None

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=None,
                bufsize=frame_size * 2,
            )

            while not STOP_EVENT.is_set():
                raw = read_exact(
                    process.stdout,
                    frame_size,
                )

                if len(raw) != frame_size:
                    print("[INPUT] Stream ended or disconnected.")
                    break

                frame = np.frombuffer(
                    raw,
                    dtype=np.uint8,
                ).reshape(
                    height,
                    width,
                    3,
                ).copy()

                seq += 1
                ts = time.time()

                with FRAME_LOCK:
                    LATEST_FRAME = frame
                    LATEST_FRAME_SEQ = seq
                    LATEST_FRAME_TS = ts
                    LATEST_CONNECTION_ID = connection_id

                with STATS_LOCK:
                    STATS["capture_frames"] = seq
                    capture_elapsed = (
                        time.perf_counter()
                        - STATS["capture_start_wall"]
                    )
                    capture_real_fps = (
                        seq / capture_elapsed
                        if capture_elapsed > 0
                        else 0.0
                    )

                report = max(
                    int(round(video_fps * 10.0)),
                    1,
                )

                if seq % report == 0:
                    print(
                        f"[INPUT] frames={seq} | "
                        f"real_fps={capture_real_fps:5.2f} | "
                        f"connection={connection_id}"
                    )

        except Exception as e:
            print("[INPUT ERROR]", repr(e))

        finally:
            if process is not None:
                try:
                    if process.stdout:
                        process.stdout.close()
                except Exception:
                    pass

                try:
                    process.terminate()
                    process.wait(timeout=2)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass

        if (not is_network_source(source)) and (not loop_file):
            print("[INPUT] Local file finished.")
            STOP_EVENT.set()
            return

        if not STOP_EVENT.is_set():
            print(
                f"[INPUT] Restart after "
                f"{reconnect_delay:.1f}s..."
            )
            time.sleep(reconnect_delay)


# ============================================================
# Tracker/model
# ============================================================

def reset_trackers(model):
    try:
        predictor = getattr(model, "predictor", None)

        if predictor is None:
            return

        trackers = getattr(predictor, "trackers", None)

        if not trackers:
            return

        for tracker in trackers:
            if hasattr(tracker, "reset"):
                tracker.reset()

        print("[TRACKER] Reset.")

    except Exception as e:
        print("[TRACKER WARNING]", repr(e))


def warmup_model(
    model,
    ai_width,
    ai_height,
    imgsz,
    conf,
    iou,
    tracker,
):
    print()
    print("=" * 78)
    print("[MODEL] Warming up NCNN + Tracker")
    print("=" * 78)

    dummy = np.zeros(
        (ai_height, ai_width, 3),
        dtype=np.uint8,
    )

    t1 = time.perf_counter()

    model.track(
        source=dummy,
        persist=False,
        imgsz=imgsz,
        conf=conf,
        iou=iou,
        tracker=tracker,
        verbose=False,
    )

    reset_trackers(model)

    elapsed = time.perf_counter() - t1
    print(f"[MODEL] Warmup finished: {elapsed:.2f}s")


# ============================================================
# Detection mapping
# ============================================================

def result_to_main_detections(
    result,
    roi_x,
    roi_y,
    roi_width,
    roi_height,
    ai_width,
    ai_height,
):
    if result is None or result.boxes is None:
        return []

    boxes = result.boxes.xyxy.cpu().numpy()
    confs = result.boxes.conf.cpu().numpy()

    if result.boxes.id is not None:
        track_ids = (
            result.boxes.id
            .cpu()
            .numpy()
            .astype(int)
        )
    else:
        track_ids = np.full(
            len(boxes),
            -1,
            dtype=int,
        )

    scale_x = roi_width / float(ai_width)
    scale_y = roi_height / float(ai_height)

    detections = []

    for box, conf, track_id in zip(
        boxes,
        confs,
        track_ids,
    ):
        x1 = int(round(roi_x + box[0] * scale_x))
        y1 = int(round(roi_y + box[1] * scale_y))
        x2 = int(round(roi_x + box[2] * scale_x))
        y2 = int(round(roi_y + box[3] * scale_y))

        detections.append(
            {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "confidence": float(conf),
                "track_id": int(track_id),
            }
        )

    return detections


# ============================================================
# V5 track motion estimation and box prediction
# ============================================================

def clear_track_history():

    with TRACK_HISTORY_LOCK:
        TRACK_HISTORY.clear()


def update_track_history(
    detections,
    frame_ts,
    connection_id,
    velocity_alpha,
    max_track_speed,
    history_ttl,
):
    """
    Estimate 2D center velocity for each ByteTrack track_id.

    The history is stored in 1080p output coordinates, so prediction
    happens after the 320x240 -> ROI -> 1080p coordinate mapping.
    """

    with TRACK_HISTORY_LOCK:

        stale_ids = []

        for track_id, item in TRACK_HISTORY.items():

            if (
                item["connection_id"] != connection_id
                or
                frame_ts - item["timestamp"] > history_ttl
            ):

                stale_ids.append(
                    track_id
                )

        for track_id in stale_ids:
            TRACK_HISTORY.pop(
                track_id,
                None,
            )

        for det in detections:

            track_id = int(
                det.get(
                    "track_id",
                    -1,
                )
            )

            if track_id < 0:
                continue

            x1 = float(
                det["x1"]
            )

            y1 = float(
                det["y1"]
            )

            x2 = float(
                det["x2"]
            )

            y2 = float(
                det["y2"]
            )

            cx = (
                x1 + x2
            ) * 0.5

            cy = (
                y1 + y2
            ) * 0.5

            old = TRACK_HISTORY.get(
                track_id
            )

            vx = 0.0
            vy = 0.0

            if (
                old is not None
                and
                old["connection_id"] == connection_id
            ):

                dt = (
                    frame_ts
                    -
                    old["timestamp"]
                )

                if 0.02 <= dt <= 1.0:

                    raw_vx = (
                        cx
                        -
                        old["cx"]
                    ) / dt

                    raw_vy = (
                        cy
                        -
                        old["cy"]
                    ) / dt

                    speed = (
                        raw_vx * raw_vx
                        +
                        raw_vy * raw_vy
                    ) ** 0.5

                    # Protect against jitter / ID-switch spikes.
                    if (
                        max_track_speed > 0
                        and
                        speed > max_track_speed
                    ):

                        ratio = (
                            max_track_speed
                            /
                            speed
                        )

                        raw_vx *= ratio
                        raw_vy *= ratio

                    # Exponential smoothing:
                    # larger alpha = steadier but slightly slower response.
                    vx = (
                        velocity_alpha
                        *
                        old["vx"]
                        +
                        (
                            1.0
                            -
                            velocity_alpha
                        )
                        *
                        raw_vx
                    )

                    vy = (
                        velocity_alpha
                        *
                        old["vy"]
                        +
                        (
                            1.0
                            -
                            velocity_alpha
                        )
                        *
                        raw_vy
                    )

            TRACK_HISTORY[
                track_id
            ] = {
                "cx": cx,
                "cy": cy,
                "vx": vx,
                "vy": vy,
                "timestamp": frame_ts,
                "connection_id": connection_id,
            }


def predict_detections_to_frame(
    detections,
    detection_frame_ts,
    current_frame_ts,
    width,
    height,
    connection_id,
    max_predict_time,
    max_predict_shift,
):
    """
    Move tracked boxes from the AI source-frame time to the newest
    1080p video-frame time.

    Only center position is predicted. Box width/height are preserved
    to avoid scale jitter.
    """

    if (
        detection_frame_ts <= 0
        or
        current_frame_ts <= 0
    ):

        return (
            list(detections),
            0.0,
            0.0,
        )

    raw_dt = (
        current_frame_ts
        -
        detection_frame_ts
    )

    if raw_dt <= 0:

        return (
            list(detections),
            0.0,
            0.0,
        )

    dt = min(
        raw_dt,
        max_predict_time,
    )

    predicted = []
    shifts = []

    with TRACK_HISTORY_LOCK:

        for det in detections:

            new_det = dict(
                det
            )

            track_id = int(
                det.get(
                    "track_id",
                    -1,
                )
            )

            history = (
                TRACK_HISTORY.get(
                    track_id
                )
                if track_id >= 0
                else None
            )

            if (
                history is None
                or
                history["connection_id"] != connection_id
            ):

                predicted.append(
                    new_det
                )

                continue

            dx = (
                history["vx"]
                *
                dt
            )

            dy = (
                history["vy"]
                *
                dt
            )

            shift = (
                dx * dx
                +
                dy * dy
            ) ** 0.5

            if (
                max_predict_shift > 0
                and
                shift > max_predict_shift
            ):

                ratio = (
                    max_predict_shift
                    /
                    shift
                )

                dx *= ratio
                dy *= ratio
                shift = max_predict_shift

            x1 = float(
                det["x1"]
            ) + dx

            y1 = float(
                det["y1"]
            ) + dy

            x2 = float(
                det["x2"]
            ) + dx

            y2 = float(
                det["y2"]
            ) + dy

            # Clip to main output canvas.
            x1 = max(
                0.0,
                min(
                    x1,
                    width - 1.0,
                )
            )

            y1 = max(
                0.0,
                min(
                    y1,
                    height - 1.0,
                )
            )

            x2 = max(
                0.0,
                min(
                    x2,
                    width - 1.0,
                )
            )

            y2 = max(
                0.0,
                min(
                    y2,
                    height - 1.0,
                )
            )

            if (
                x2 <= x1
                or
                y2 <= y1
            ):

                continue

            new_det[
                "x1"
            ] = int(
                round(x1)
            )

            new_det[
                "y1"
            ] = int(
                round(y1)
            )

            new_det[
                "x2"
            ] = int(
                round(x2)
            )

            new_det[
                "y2"
            ] = int(
                round(y2)
            )

            predicted.append(
                new_det
            )

            shifts.append(
                shift
            )

    avg_shift = (
        sum(shifts)
        /
        len(shifts)
        if shifts
        else 0.0
    )

    return (
        predicted,
        dt,
        avg_shift,
    )


# ============================================================
# AI worker
# ============================================================

def ai_worker(
    model,
    roi_x,
    roi_y,
    roi_width,
    roi_height,
    ai_width,
    ai_height,
    ai_fps,
    imgsz,
    conf,
    iou,
    tracker,
    velocity_alpha,
    max_track_speed,
    history_ttl,
):
    global LATEST_DETECTIONS
    global LATEST_DET_TS
    global LATEST_DET_FRAME_TS
    global LATEST_DET_SEQ
    global LATEST_DET_CONNECTION_ID

    ai_interval = 1.0 / ai_fps
    next_ai_time = time.perf_counter()

    last_processed_seq = 0
    last_connection_id = None

    with STATS_LOCK:
        STATS["ai_start_wall"] = time.perf_counter()

    while not STOP_EVENT.is_set():
        now = time.perf_counter()

        if now < next_ai_time:
            time.sleep(
                min(
                    next_ai_time - now,
                    0.005,
                )
            )
            continue

        if now - next_ai_time > ai_interval * 2:
            next_ai_time = now

        next_ai_time += ai_interval

        with FRAME_LOCK:
            frame = LATEST_FRAME
            frame_seq = LATEST_FRAME_SEQ
            frame_ts = LATEST_FRAME_TS
            connection_id = LATEST_CONNECTION_ID

        if frame is None:
            continue

        if frame_seq == last_processed_seq:
            continue

        if (
            last_processed_seq > 0
            and
            frame_seq > last_processed_seq + 1
        ):
            skipped = (
                frame_seq
                - last_processed_seq
                - 1
            )

            with STATS_LOCK:
                STATS["ai_skipped"] += skipped

        if (
            last_connection_id is not None
            and
            connection_id != last_connection_id
        ):
            reset_trackers(model)
            clear_track_history()

        last_connection_id = connection_id
        last_processed_seq = frame_seq

        total_start = time.perf_counter()

        roi_frame = frame[
            roi_y:roi_y + roi_height,
            roi_x:roi_x + roi_width,
        ]

        resize_start = time.perf_counter()

        ai_frame = cv2.resize(
            roi_frame,
            (ai_width, ai_height),
            interpolation=cv2.INTER_AREA,
        )

        resize_end = time.perf_counter()

        track_start = time.perf_counter()

        results = model.track(
            source=ai_frame,
            persist=True,
            imgsz=imgsz,
            conf=conf,
            iou=iou,
            tracker=tracker,
            verbose=False,
        )

        track_end = time.perf_counter()

        if results and len(results) > 0:
            detections = result_to_main_detections(
                results[0],
                roi_x,
                roi_y,
                roi_width,
                roi_height,
                ai_width,
                ai_height,
            )
        else:
            detections = []

        total_end = time.perf_counter()

        resize_ms = (
            resize_end - resize_start
        ) * 1000.0

        track_ms = (
            track_end - track_start
        ) * 1000.0

        total_ms = (
            total_end - total_start
        ) * 1000.0

        update_track_history(
            detections=detections,
            frame_ts=frame_ts,
            connection_id=connection_id,
            velocity_alpha=velocity_alpha,
            max_track_speed=max_track_speed,
            history_ttl=history_ttl,
        )

        with DET_LOCK:
            LATEST_DETECTIONS = detections

            # Publication time for stale-result logic.
            LATEST_DET_TS = time.time()

            # Actual source-frame time for motion prediction.
            LATEST_DET_FRAME_TS = frame_ts

            LATEST_DET_SEQ = frame_seq
            LATEST_DET_CONNECTION_ID = connection_id

        with STATS_LOCK:
            STATS["ai_frames"] += 1
            STATS["ai_resize_ms_sum"] += resize_ms
            STATS["ai_track_ms_sum"] += track_ms
            STATS["ai_total_ms_sum"] += total_ms
            STATS["ai_last_ms"] = total_ms

            ai_count = STATS["ai_frames"]
            ai_skipped = STATS["ai_skipped"]

            ai_elapsed = (
                time.perf_counter()
                - STATS["ai_start_wall"]
            )

            ai_real_fps = (
                ai_count / ai_elapsed
                if ai_elapsed > 0
                else 0.0
            )

        report = max(
            int(round(ai_fps * 2.0)),
            1,
        )

        if ai_count == 1 or ai_count % report == 0:
            frame_age_ms = max(
                0.0,
                (
                    time.time()
                    - frame_ts
                ) * 1000.0,
            )

            capacity_fps = (
                1000.0 / total_ms
                if total_ms > 0
                else 0.0
            )

            print(
                f"[AI {ai_count:7d}] "
                f"src_seq={frame_seq:7d} | "
                f"fish={len(detections):2d} | "
                f"resize={resize_ms:5.1f}ms | "
                f"track={track_ms:6.1f}ms | "
                f"total={total_ms:6.1f}ms | "
                f"capacity={capacity_fps:5.2f} | "
                f"real_fps={ai_real_fps:5.2f} | "
                f"skip={ai_skipped} | "
                f"frame_age={frame_age_ms:6.1f}ms"
            )


# ============================================================
# Drawing
# ============================================================

def draw_detections(frame, detections, label):
    height, width = frame.shape[:2]

    for det in detections:
        x1 = max(
            0,
            min(
                int(det["x1"]),
                width - 1,
            )
        )

        y1 = max(
            0,
            min(
                int(det["y1"]),
                height - 1,
            )
        )

        x2 = max(
            0,
            min(
                int(det["x2"]),
                width - 1,
            )
        )

        y2 = max(
            0,
            min(
                int(det["y2"]),
                height - 1,
            )
        )

        if x2 <= x1 or y2 <= y1:
            continue

        track_id = int(det["track_id"])
        confidence = float(det["confidence"])

        if track_id >= 0:
            color = colors(
                track_id,
                True,
            )
        else:
            color = (0, 255, 0)

        cv2.rectangle(
            frame,
            (x1, y1),
            (x2, y2),
            color,
            3,
        )

        text = f"{label}: {confidence:.3f}"

        (tw, th), baseline = cv2.getTextSize(
            text,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            2,
        )

        ty = max(
            y1,
            th + 10,
        )

        right = min(
            x1 + tw + 8,
            width - 1,
        )

        cv2.rectangle(
            frame,
            (
                x1,
                ty - th - 8,
            ),
            (
                right,
                ty + baseline,
            ),
            color,
            -1,
        )

        cv2.putText(
            frame,
            text,
            (
                x1 + 4,
                ty - 4,
            ),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )


# ============================================================
# FFmpeg output V3
# ============================================================

def build_output_command(
    push_url,
    width,
    height,
    push_fps,
    bitrate,
    x264_threads,
):
    gop = max(
        int(round(push_fps * 2.0)),
        1,
    )

    # Low-latency / low-CPU x264 tuning.
    x264_params = (
        "ref=1:"
        "bframes=0:"
        "weightp=0:"
        "weightb=0:"
        "scenecut=0:"
        "rc-lookahead=0:"
        "sync-lookahead=0:"
        "mixed-refs=0:"
        "subme=0:"
        "me=dia:"
        "merange=16:"
        "sliced-threads=1"
    )

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",

        "-f",
        "rawvideo",

        "-pix_fmt",
        "bgr24",

        "-s",
        f"{width}x{height}",

        "-r",
        str(push_fps),

        "-i",
        "-",

        "-an",

        "-c:v",
        "libx264",

        "-preset",
        "ultrafast",

        "-tune",
        "zerolatency",

        "-threads",
        str(x264_threads),

        "-profile:v",
        "baseline",

        "-x264-params",
        x264_params,

        "-pix_fmt",
        "yuv420p",

        "-b:v",
        bitrate,

        "-maxrate",
        bitrate,

        "-bufsize",
        "5M",

        "-g",
        str(gop),

        "-keyint_min",
        str(gop),

        "-sc_threshold",
        "0",

        "-f",
        "flv",

        "-flvflags",
        "no_duration_filesize",

        push_url,
    ]


def start_output_ffmpeg(
    push_url,
    width,
    height,
    push_fps,
    bitrate,
    x264_threads,
):
    cmd = build_output_command(
        push_url,
        width,
        height,
        push_fps,
        bitrate,
        x264_threads,
    )

    print()
    print("=" * 78)
    print("[PUSH] Starting FFmpeg V6")
    print("=" * 78)
    print("URL          :", push_url)
    print("Resolution   :", f"{width}x{height}")
    print("Push FPS     :", push_fps)
    print("Encoder      : libx264")
    print("Bitrate      :", bitrate)
    print("x264 threads :", x264_threads)
    print("Mode         : ultrafast + zerolatency + low-CPU x264 params")
    print("[OUTPUT FFMPEG]", " ".join(cmd))
    print()

    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        bufsize=0,
    )


# ============================================================
# Push worker
# ============================================================

def push_worker(
    push_url,
    width,
    height,
    push_fps,
    bitrate,
    x264_threads,
    label,
    max_box_age,
    disable_prediction,
    max_predict_time,
    max_predict_shift,
):
    process = None

    frame_interval = 1.0 / push_fps
    next_send = time.perf_counter()

    with STATS_LOCK:
        STATS["push_start_wall"] = time.perf_counter()

    while not STOP_EVENT.is_set():
        now = time.perf_counter()

        if now < next_send:
            time.sleep(
                min(
                    next_send - now,
                    0.005,
                )
            )
            continue

        if now - next_send > frame_interval * 3:
            next_send = now

        next_send += frame_interval

        with FRAME_LOCK:
            raw_frame = LATEST_FRAME
            frame_seq = LATEST_FRAME_SEQ
            frame_ts = LATEST_FRAME_TS
            connection_id = LATEST_CONNECTION_ID

        if raw_frame is None:
            continue

        # Draw only on a copy.
        output_frame = raw_frame.copy()

        with DET_LOCK:
            detections = list(
                LATEST_DETECTIONS
            )
            det_ts = LATEST_DET_TS
            det_frame_ts = LATEST_DET_FRAME_TS
            det_seq = LATEST_DET_SEQ
            det_connection_id = (
                LATEST_DET_CONNECTION_ID
            )

        box_age = (
            time.time() - det_ts
            if det_ts > 0
            else 9999.0
        )

        use_boxes = (
            det_ts > 0
            and
            box_age <= max_box_age
            and
            det_connection_id == connection_id
        )

        prediction_dt = 0.0
        prediction_shift = 0.0

        if use_boxes:

            if disable_prediction:

                draw_detections(
                    output_frame,
                    detections,
                    label,
                )

                fish_count = len(
                    detections
                )

            else:

                (
                    predicted_detections,
                    prediction_dt,
                    prediction_shift,
                ) = predict_detections_to_frame(
                    detections=detections,
                    detection_frame_ts=det_frame_ts,
                    current_frame_ts=frame_ts,
                    width=width,
                    height=height,
                    connection_id=connection_id,
                    max_predict_time=max_predict_time,
                    max_predict_shift=max_predict_shift,
                )

                draw_detections(
                    output_frame,
                    predicted_detections,
                    label,
                )

                fish_count = len(
                    predicted_detections
                )

        else:

            fish_count = 0

        with STATS_LOCK:
            ai_last_ms = STATS[
                "ai_last_ms"
            ]

        # On-screen status.
        status = (
            f"Video: {push_fps:.0f} FPS  "
            f"Fish: {fish_count}  "
            f"AI: {ai_last_ms:.0f} ms"
        )

        cv2.putText(
            output_frame,
            status,
            (25, 45),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )

        if (
            process is None
            or
            process.poll() is not None
        ):
            try:
                process = start_output_ffmpeg(
                    push_url,
                    width,
                    height,
                    push_fps,
                    bitrate,
                    x264_threads,
                )

                with STATS_LOCK:
                    STATS["push_restarts"] += 1

            except Exception as e:
                print(
                    "[PUSH ERROR]",
                    repr(e),
                )
                process = None
                time.sleep(2)
                continue

        write_start = time.perf_counter()

        try:
            process.stdin.write(
                output_frame.tobytes()
            )

        except (
            BrokenPipeError,
            OSError,
            AttributeError,
        ):
            print()
            print(
                "[PUSH] RTMP pipe disconnected."
            )

            try:
                if process is not None:
                    process.kill()
            except Exception:
                pass

            process = None
            time.sleep(2)
            continue

        write_end = time.perf_counter()

        write_ms = (
            write_end - write_start
        ) * 1000.0

        with STATS_LOCK:
            STATS["push_frames"] += 1
            STATS["push_write_ms_sum"] += write_ms
            STATS["push_write_ms_max"] = max(
                STATS["push_write_ms_max"],
                write_ms,
            )

            pushed = STATS["push_frames"]

            push_elapsed = (
                time.perf_counter()
                - STATS["push_start_wall"]
            )

            real_push_fps = (
                pushed / push_elapsed
                if push_elapsed > 0
                else 0.0
            )

            avg_write_ms = (
                STATS["push_write_ms_sum"]
                / pushed
                if pushed > 0
                else 0.0
            )

        report = max(
            int(round(push_fps * 5.0)),
            1,
        )

        if pushed % report == 0:
            frame_age_ms = max(
                0.0,
                (
                    time.time()
                    - frame_ts
                ) * 1000.0,
            )

            box_age_ms = (
                box_age * 1000.0
                if det_ts > 0
                else -1.0
            )

            seq_gap = (
                frame_seq - det_seq
                if det_seq > 0
                else -1
            )

            print(
                f"[PUSH] sent={pushed:7d} | "
                f"wall_fps={real_push_fps:5.2f} | "
                f"write={write_ms:6.1f}ms | "
                f"avg_write={avg_write_ms:6.1f}ms | "
                f"video_seq={frame_seq:7d} | "
                f"frame_age={frame_age_ms:6.1f}ms | "
                f"box_age={box_age_ms:6.1f}ms | "
                f"box_gap={seq_gap:3d} | "
                f"pred_dt={prediction_dt * 1000.0:5.1f}ms | "
                f"pred_shift={prediction_shift:5.1f}px | "
                f"fish={fish_count:2d}"
            )

    if process is not None:
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass

        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


# ============================================================
# Signal handler
# ============================================================


# ============================================================
# V4 periodic system / wall-clock monitor
# ============================================================

def read_cpu_temperature_c():

    for path in (
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/thermal/thermal_zone1/temp",
    ):

        try:

            with open(
                path,
                "r",
                encoding="utf-8",
            ) as f:

                value = float(
                    f.read().strip()
                )

            if value > 1000:
                value /= 1000.0

            if 0.0 < value < 150.0:
                return value

        except Exception:
            pass

    return None


def monitor_worker(interval):

    start_wall = time.perf_counter()
    last_wall = start_wall

    last_capture = 0
    last_ai = 0
    last_push = 0

    while not STOP_EVENT.is_set():

        deadline = time.perf_counter() + interval

        while (
            not STOP_EVENT.is_set()
            and
            time.perf_counter() < deadline
        ):
            time.sleep(
                min(
                    0.2,
                    max(
                        0.0,
                        deadline - time.perf_counter()
                    )
                )
            )

        if STOP_EVENT.is_set():
            break

        now = time.perf_counter()
        dt = now - last_wall

        if dt <= 0:
            continue

        with STATS_LOCK:

            capture_frames = STATS[
                "capture_frames"
            ]

            ai_frames = STATS[
                "ai_frames"
            ]

            push_frames = STATS[
                "push_frames"
            ]

            write_sum = STATS[
                "push_write_ms_sum"
            ]

            write_max = STATS[
                "push_write_ms_max"
            ]

        input_fps = (
            capture_frames - last_capture
        ) / dt

        ai_fps = (
            ai_frames - last_ai
        ) / dt

        push_fps = (
            push_frames - last_push
        ) / dt

        avg_write_ms = (
            write_sum / push_frames
            if push_frames > 0
            else 0.0
        )

        cpu_temp = read_cpu_temperature_c()

        try:
            load1 = os.getloadavg()[0]
        except Exception:
            load1 = None

        cpu_text = (
            f"{cpu_temp:.1f}C"
            if cpu_temp is not None
            else "N/A"
        )

        load_text = (
            f"{load1:.2f}"
            if load1 is not None
            else "N/A"
        )

        print()
        print(
            f"[STAT] "
            f"wall={now - start_wall:7.1f}s | "
            f"INPUT={input_fps:5.2f} FPS | "
            f"AI={ai_fps:5.2f} FPS | "
            f"PUSH={push_fps:5.2f} FPS | "
            f"avg_write={avg_write_ms:6.1f}ms | "
            f"max_write={write_max:6.1f}ms | "
            f"CPU={cpu_text} | "
            f"load1={load_text}"
        )

        last_capture = capture_frames
        last_ai = ai_frames
        last_push = push_frames
        last_wall = now




# ============================================================
# V6 JSON detection reporting
# ============================================================

def utc_now_iso():

    return (
        datetime.now(
            timezone.utc
        )
        .isoformat(
            timespec="milliseconds"
        )
        .replace(
            "+00:00",
            "Z",
        )
    )


def build_ai_report_payload(
    camera_id,
    source,
    push_url,
    width,
    height,
    video_fps,
    ai_fps,
    imgsz,
):
    """
    Build the compact JSON schema expected by ai_report_server.py.

    POST body example:
    {
        "camera_id": "pond_01",
        "timestamp": 1789012345.235,
        "fish_count": 2,
        "objects": [
            {
                "track_id": 1,
                "class": "fish",
                "confidence": 0.94,
                "bbox": {"x1": 100, "y1": 200, "x2": 300, "y2": 400},
                "center": {"x": 200, "y": 300},
                "velocity": {"vx": 10.5, "vy": -2.5}
            }
        ]
    }

    The function keeps the original V6 tracking/motion data internally, but
    only sends fields required by the current business-server API.  This avoids
    FastAPI 422 validation errors caused by the old nested V6 payload schema.
    """

    with DET_LOCK:
        detections = [
            dict(det)
            for det in LATEST_DETECTIONS
        ]
        det_frame_ts = LATEST_DET_FRAME_TS
        det_seq = LATEST_DET_SEQ

    with TRACK_HISTORY_LOCK:
        track_history = {
            int(track_id): dict(item)
            for track_id, item
            in TRACK_HISTORY.items()
        }

    now_ts = time.time()
    objects = []

    for det in detections:
        track_id = int(
            det.get(
                "track_id",
                -1,
            )
        )

        x1 = float(
            det.get(
                "x1",
                0.0,
            )
        )
        y1 = float(
            det.get(
                "y1",
                0.0,
            )
        )
        x2 = float(
            det.get(
                "x2",
                0.0,
            )
        )
        y2 = float(
            det.get(
                "y2",
                0.0,
            )
        )

        cx = (x1 + x2) * 0.5
        cy = (y1 + y2) * 0.5

        history = track_history.get(
            track_id
        )

        vx = 0.0
        vy = 0.0

        if history is not None:
            vx = float(
                history.get(
                    "vx",
                    0.0,
                )
            )
            vy = float(
                history.get(
                    "vy",
                    0.0,
                )
            )

        objects.append(
            {
                "track_id": track_id,
                "class": "fish",
                "confidence": round(
                    float(
                        det.get(
                            "confidence",
                            0.0,
                        )
                    ),
                    4,
                ),
                "bbox": {
                    "x1": round(x1, 1),
                    "y1": round(y1, 1),
                    "x2": round(x2, 1),
                    "y2": round(y2, 1),
                },
                "center": {
                    "x": round(cx, 1),
                    "y": round(cy, 1),
                },
                "velocity": {
                    "vx": round(vx, 2),
                    "vy": round(vy, 2),
                },
            }
        )

    # Prefer the timestamp of the source frame that produced the latest AI
    # result.  Before the first detection result exists, fall back to now.
    edge_timestamp = (
        float(det_frame_ts)
        if det_frame_ts > 0
        else float(now_ts)
    )

    payload = {
        "camera_id": camera_id,
        "timestamp": edge_timestamp,
        "fish_count": len(objects),
        "objects": objects,
    }

    # Internal-only field used by report_worker for readable console logging.
    # It is removed before HTTP transmission by post_json().
    payload["_frame_seq"] = int(det_seq)

    return payload



def post_json(
    url,
    payload,
    timeout,
    token,
):
    # _frame_seq is only for local console logging and is deliberately not
    # included in the server JSON schema.
    wire_payload = {
        key: value
        for key, value in payload.items()
        if not key.startswith("_")
    }

    body = json.dumps(
        wire_payload,
        ensure_ascii=False,
        separators=(
            ",",
            ":",
        ),
    ).encode(
        "utf-8"
    )

    headers = {
        "Content-Type": (
            "application/json; charset=utf-8"
        ),
        "User-Agent": (
            "fish-edge-ai-v6.1"
        ),
    }

    # Must match ai_report_server.py on the business server.
    if token:
        headers[
            "X-API-Token"
        ] = token

    request = urllib.request.Request(
        url=url,
        data=body,
        headers=headers,
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=timeout,
    ) as response:
        status = int(
            getattr(
                response,
                "status",
                200,
            )
        )
        response.read(
            4096
        )

    return status



def report_worker(
    report_url,
    camera_id,
    source,
    push_url,
    width,
    height,
    video_fps,
    ai_fps,
    imgsz,
    report_interval,
    report_timeout,
    report_token,
):

    if not report_url:

        print()
        print(
            "[REPORT] Disabled "
            "(--report-url not set)"
        )

        return

    print()
    print("=" * 78)
    print("[REPORT] JSON reporter started")
    print("=" * 78)
    print(
        "Camera ID :",
        camera_id,
    )
    print(
        "Report URL:",
        report_url,
    )
    print(
        "Interval  :",
        f"{report_interval:.2f}s",
    )
    print(
        "Timeout   :",
        f"{report_timeout:.2f}s",
    )
    print("=" * 78)

    next_report = (
        time.perf_counter()
        +
        report_interval
    )

    while not STOP_EVENT.is_set():

        now = time.perf_counter()

        if now < next_report:

            STOP_EVENT.wait(
                min(
                    next_report - now,
                    0.2,
                )
            )

            continue

        if (
            now
            -
            next_report
            >
            report_interval * 2.0
        ):

            next_report = now

        next_report += (
            report_interval
        )

        payload = (
            build_ai_report_payload(
                camera_id=camera_id,
                source=source,
                push_url=push_url,
                width=width,
                height=height,
                video_fps=video_fps,
                ai_fps=ai_fps,
                imgsz=imgsz,
            )
        )

        fish_count = int(
            payload.get(
                "fish_count",
                0,
            )
        )

        frame_seq = int(
            payload.get(
                "_frame_seq",
                0,
            )
        )

        t1 = time.perf_counter()

        try:

            status = post_json(
                url=report_url,
                payload=payload,
                timeout=report_timeout,
                token=report_token,
            )

            elapsed_ms = (
                time.perf_counter()
                -
                t1
            ) * 1000.0

            with STATS_LOCK:

                STATS[
                    "report_ok"
                ] += 1

                STATS[
                    "report_last_ms"
                ] = elapsed_ms

            print(
                f"[REPORT] OK "
                f"status={status} | "
                f"camera={camera_id} | "
                f"seq={frame_seq} | "
                f"fish={fish_count} | "
                f"http={elapsed_ms:.1f}ms"
            )

        except urllib.error.HTTPError as e:

            elapsed_ms = (
                time.perf_counter()
                -
                t1
            ) * 1000.0

            with STATS_LOCK:
                STATS[
                    "report_fail"
                ] += 1
                STATS[
                    "report_last_ms"
                ] = elapsed_ms

            try:
                error_body = e.read(4096).decode(
                    "utf-8",
                    errors="replace",
                )
            except Exception:
                error_body = ""

            print(
                f"[REPORT ERROR] HTTP {e.code}: "
                f"{error_body or e.reason}"
            )

        except Exception as e:

            elapsed_ms = (
                time.perf_counter()
                -
                t1
            ) * 1000.0

            with STATS_LOCK:
                STATS[
                    "report_fail"
                ] += 1
                STATS[
                    "report_last_ms"
                ] = elapsed_ms

            print(
                f"[REPORT ERROR] "
                f"{type(e).__name__}: "
                f"{e}"
            )



def signal_handler(signum, frame):
    STOP_EVENT.set()


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Raspberry Pi YOLO11 NCNN "
            "1080p async AI V6.1 + low-latency x264 + JSON report"
        )
    )

    parser.add_argument(
        "--source",
        required=True,
    )

    parser.add_argument(
        "--model",
        default=(
            "model/"
            "best_320_ncnn_fp16_ncnn_model"
        ),
    )

    parser.add_argument(
        "--push-url",
        default=(
            "rtmp://124.222.161.224:1935/"
            "live/fish_ai_1080p_1600k_ai10_v6_report"
        ),
    )

    # Main video
    parser.add_argument(
        "--width",
        type=int,
        default=1920,
    )

    parser.add_argument(
        "--height",
        type=int,
        default=1080,
    )

    parser.add_argument(
        "--video-fps",
        type=float,
        default=10.0,
    )

    # AI branch
    parser.add_argument(
        "--ai-fps",
        type=float,
        default=10.0,
    )

    parser.add_argument(
        "--ai-long-side",
        type=int,
        default=320,
    )

    parser.add_argument(
        "--imgsz",
        type=int,
        default=320,
    )

    parser.add_argument(
        "--conf",
        type=float,
        default=0.90,
    )

    parser.add_argument(
        "--iou",
        type=float,
        default=0.45,
    )

    parser.add_argument(
        "--tracker",
        default="bytetrack.yaml",
    )

    # Encoding
    parser.add_argument(
        "--bitrate",
        default="1600k",
    )

    parser.add_argument(
        "--x264-threads",
        type=int,
        default=2,
    )

    parser.add_argument(
        "--label",
        default="Rainbow Trout",
    )

    parser.add_argument(
        "--max-box-age",
        type=float,
        default=0.30,
        help="Hide AI results older than this many seconds",
    )

    parser.add_argument(
        "--disable-prediction",
        action="store_true",
        help="Disable V5 motion prediction for A/B testing",
    )

    parser.add_argument(
        "--predict-max-time",
        type=float,
        default=0.15,
        help="Maximum motion extrapolation time in seconds",
    )

    parser.add_argument(
        "--predict-max-shift",
        type=float,
        default=120.0,
        help="Maximum predicted shift per frame in 1080p pixels",
    )

    parser.add_argument(
        "--velocity-alpha",
        type=float,
        default=0.65,
        help="Velocity smoothing factor, recommended 0.60-0.75",
    )

    parser.add_argument(
        "--max-track-speed",
        type=float,
        default=1500.0,
        help="Maximum estimated track speed in 1080p pixels/second",
    )

    parser.add_argument(
        "--history-ttl",
        type=float,
        default=2.0,
        help="Motion-history expiration time in seconds",
    )

    # Input behavior
    parser.add_argument(
        "--loop-file",
        action="store_true",
    )

    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=2.0,
    )

    parser.add_argument(
        "--source-width",
        type=int,
        default=0,
    )

    parser.add_argument(
        "--source-height",
        type=int,
        default=0,
    )

    parser.add_argument(
        "--stats-interval",
        type=float,
        default=5.0,
        help="Periodic wall-clock performance report interval",
    )

    # AI JSON reporting
    parser.add_argument(
        "--camera-id",
        default="pond_01",
        help="Logical camera/device ID used in JSON reports",
    )

    parser.add_argument(
        "--report-url",
        default="",
        help="HTTP POST endpoint for AI JSON results; empty disables reporting",
    )

    parser.add_argument(
        "--report-interval",
        type=float,
        default=1.0,
        help="JSON report interval in seconds",
    )

    parser.add_argument(
        "--report-timeout",
        type=float,
        default=1.5,
        help="HTTP report timeout in seconds",
    )

    parser.add_argument(
        "--report-token",
        default="",
        help="Optional Bearer token for the report endpoint",
    )

    args = parser.parse_args()

    # Validation
    if args.width <= 0 or args.height <= 0:
        raise ValueError(
            "--width and --height must be > 0"
        )

    if args.video_fps <= 0:
        raise ValueError(
            "--video-fps must be > 0"
        )

    if args.ai_fps <= 0:
        raise ValueError(
            "--ai-fps must be > 0"
        )

    if args.ai_long_side <= 0:
        raise ValueError(
            "--ai-long-side must be > 0"
        )

    if args.x264_threads <= 0:
        raise ValueError(
            "--x264-threads must be > 0"
        )

    if args.stats_interval <= 0:
        raise ValueError(
            "--stats-interval must be > 0"
        )

    if args.predict_max_time < 0:
        raise ValueError(
            "--predict-max-time must be >= 0"
        )

    if args.predict_max_shift < 0:
        raise ValueError(
            "--predict-max-shift must be >= 0"
        )

    if not 0.0 <= args.velocity_alpha <= 1.0:
        raise ValueError(
            "--velocity-alpha must be between 0 and 1"
        )

    if args.max_track_speed < 0:
        raise ValueError(
            "--max-track-speed must be >= 0"
        )

    if args.history_ttl <= 0:
        raise ValueError(
            "--history-ttl must be > 0"
        )

    if args.report_interval <= 0:
        raise ValueError(
            "--report-interval must be > 0"
        )

    if args.report_timeout <= 0:
        raise ValueError(
            "--report-timeout must be > 0"
        )

    if not os.path.exists(
        args.model
    ):
        raise FileNotFoundError(
            f"Model not found:\n"
            f"{args.model}"
        )

    if (
        not is_network_source(
            args.source
        )
        and
        not os.path.isfile(
            args.source
        )
    ):
        raise FileNotFoundError(
            f"Input source not found:\n"
            f"{args.source}"
        )

    # Source geometry
    if (
        args.source_width > 0
        and
        args.source_height > 0
    ):
        source_width = (
            args.source_width
        )
        source_height = (
            args.source_height
        )

        print()
        print(
            f"[PROBE] Using manual "
            f"source size: "
            f"{source_width}x"
            f"{source_height}"
        )

    else:
        (
            source_width,
            source_height,
        ) = probe_source_size(
            args.source
        )

    if (
        source_width is None
        or
        source_height is None
    ):
        print()
        print(
            "[WARNING] Source aspect ratio unknown; "
            "assuming 16:9."
        )
        print(
            "[WARNING] For RTSP, pass "
            "--source-width/--source-height if needed."
        )

        source_width = args.width
        source_height = args.height

    (
        roi_x,
        roi_y,
        roi_width,
        roi_height,
    ) = calculate_active_roi(
        source_width,
        source_height,
        args.width,
        args.height,
    )

    (
        ai_width,
        ai_height,
    ) = calculate_ai_size(
        roi_width,
        roi_height,
        args.ai_long_side,
    )

    print()
    print("=" * 78)
    print("1080p Async-AI NCNN V6 Motion Prediction + JSON Report")
    print("=" * 78)
    print("Input source   :", args.source)
    print(
        "Source size    :",
        f"{source_width}x{source_height}",
    )
    print(
        "Main video     :",
        f"{args.width}x{args.height}",
    )
    print(
        "Video FPS      :",
        args.video_fps,
    )
    print(
        "Active ROI     :",
        f"x={roi_x}, y={roi_y}, "
        f"w={roi_width}, h={roi_height}",
    )
    print(
        "AI frame       :",
        f"{ai_width}x{ai_height}",
    )
    print(
        "AI target FPS  :",
        args.ai_fps,
    )
    print(
        "YOLO imgsz     :",
        args.imgsz,
    )
    print(
        "Confidence     :",
        args.conf,
    )
    print(
        "IoU            :",
        args.iou,
    )
    print(
        "Tracker        :",
        args.tracker,
    )
    print(
        "Bitrate        :",
        args.bitrate,
    )
    print(
        "x264 threads   :",
        args.x264_threads,
    )
    print(
        "Max box age    :",
        args.max_box_age,
    )
    print(
        "Push URL       :",
        args.push_url,
    )
    print(
        "Stats interval :",
        args.stats_interval,
    )

    print(
        "Box prediction :",
        "OFF"
        if args.disable_prediction
        else "ON",
    )

    print(
        "Predict max dt :",
        args.predict_max_time,
    )

    print(
        "Predict max px :",
        args.predict_max_shift,
    )

    print(
        "Velocity alpha :",
        args.velocity_alpha,
    )

    print(
        "Max track speed:",
        args.max_track_speed,
    )

    print("=" * 78)

    # Load model
    print()
    print(
        "[MODEL] Loading NCNN model..."
    )

    model = YOLO(
        args.model
    )

    warmup_model(
        model,
        ai_width,
        ai_height,
        args.imgsz,
        args.conf,
        args.iou,
        args.tracker,
    )

    print(
        "[MODEL] Ready."
    )

    capture_thread = threading.Thread(
        target=capture_worker,
        args=(
            args.source,
            args.width,
            args.height,
            args.video_fps,
            args.loop_file,
            args.reconnect_delay,
        ),
        daemon=True,
        name="capture",
    )

    ai_thread = threading.Thread(
        target=ai_worker,
        args=(
            model,
            roi_x,
            roi_y,
            roi_width,
            roi_height,
            ai_width,
            ai_height,
            args.ai_fps,
            args.imgsz,
            args.conf,
            args.iou,
            args.tracker,
            args.velocity_alpha,
            args.max_track_speed,
            args.history_ttl,
        ),
        daemon=True,
        name="ai",
    )

    push_thread = threading.Thread(
        target=push_worker,
        args=(
            args.push_url,
            args.width,
            args.height,
            args.video_fps,
            args.bitrate,
            args.x264_threads,
            args.label,
            args.max_box_age,
            args.disable_prediction,
            args.predict_max_time,
            args.predict_max_shift,
        ),
        daemon=True,
        name="push",
    )

    monitor_thread = threading.Thread(
        target=monitor_worker,
        args=(
            args.stats_interval,
        ),
        daemon=True,
        name="monitor",
    )

    report_thread = threading.Thread(
        target=report_worker,
        args=(
            args.report_url,
            args.camera_id,
            args.source,
            args.push_url,
            args.width,
            args.height,
            args.video_fps,
            args.ai_fps,
            args.imgsz,
            args.report_interval,
            args.report_timeout,
            args.report_token,
        ),
        daemon=True,
        name="report",
    )

    start_wall = (
        time.perf_counter()
    )

    capture_thread.start()
    ai_thread.start()
    push_thread.start()
    monitor_thread.start()
    report_thread.start()

    print()
    print("=" * 78)
    print("Pipeline Running")
    print("=" * 78)
    print(
        f"Video branch : "
        f"{args.width}x{args.height} "
        f"@ {args.video_fps:.1f} FPS"
    )
    print(
        f"AI branch    : "
        f"{ai_width}x{ai_height} "
        f"@ target {args.ai_fps:.1f} FPS"
    )
    print(
        "Push log now reports TRUE wall-clock FPS "
        "and FFmpeg pipe write time."
    )
    print(
        f"JSON report  : "
        f"{args.report_url if args.report_url else 'DISABLED'}"
    )
    print(
        f"Camera ID    : "
        f"{args.camera_id}"
    )
    print(
        "Press Ctrl+C to stop."
    )
    print("=" * 78)

    try:
        while not STOP_EVENT.is_set():
            time.sleep(0.5)

    except KeyboardInterrupt:
        print()
        print(
            "[INFO] Ctrl+C received."
        )
        STOP_EVENT.set()

    finally:
        STOP_EVENT.set()

        for thread in (
            capture_thread,
            ai_thread,
            push_thread,
            monitor_thread,
            report_thread,
        ):
            try:
                thread.join(
                    timeout=2.0
                )
            except Exception:
                pass

        elapsed = (
            time.perf_counter()
            - start_wall
        )

        with STATS_LOCK:
            snapshot = dict(
                STATS
            )

        ai_frames = snapshot[
            "ai_frames"
        ]

        push_frames = snapshot[
            "push_frames"
        ]

        avg_resize = (
            snapshot[
                "ai_resize_ms_sum"
            ] / ai_frames
            if ai_frames > 0
            else 0.0
        )

        avg_track = (
            snapshot[
                "ai_track_ms_sum"
            ] / ai_frames
            if ai_frames > 0
            else 0.0
        )

        avg_ai_total = (
            snapshot[
                "ai_total_ms_sum"
            ] / ai_frames
            if ai_frames > 0
            else 0.0
        )

        avg_push_write = (
            snapshot[
                "push_write_ms_sum"
            ] / push_frames
            if push_frames > 0
            else 0.0
        )

        actual_ai_rate = (
            ai_frames / elapsed
            if elapsed > 0
            else 0.0
        )

        actual_push_rate = (
            push_frames / elapsed
            if elapsed > 0
            else 0.0
        )

        actual_capture_rate = (
            snapshot[
                "capture_frames"
            ] / elapsed
            if elapsed > 0
            else 0.0
        )

        print()
        print("=" * 78)
        print(
            "Async V6 Pipeline Finished"
        )
        print("=" * 78)

        print(
            f"Wall time            : "
            f"{elapsed:.2f} s"
        )

        print(
            f"Capture frames       : "
            f"{snapshot['capture_frames']}"
        )

        print(
            f"Real capture FPS     : "
            f"{actual_capture_rate:.2f}"
        )

        print(
            f"Input restarts       : "
            f"{snapshot['input_restarts']}"
        )

        print(
            f"JSON reports OK      : "
            f"{snapshot['report_ok']}"
        )

        print(
            f"JSON reports failed  : "
            f"{snapshot['report_fail']}"
        )

        print(
            f"Last report HTTP     : "
            f"{snapshot['report_last_ms']:.2f} ms"
        )

        print(
            f"AI frames            : "
            f"{ai_frames}"
        )

        print(
            f"AI source skipped    : "
            f"{snapshot['ai_skipped']}"
        )

        print(
            f"Average AI resize    : "
            f"{avg_resize:.2f} ms"
        )

        print(
            f"Average AI track     : "
            f"{avg_track:.2f} ms"
        )

        print(
            f"Average AI total     : "
            f"{avg_ai_total:.2f} ms"
        )

        print(
            f"Real AI FPS          : "
            f"{actual_ai_rate:.2f}"
        )

        print(
            f"Pushed frames        : "
            f"{push_frames}"
        )

        print(
            f"Real push FPS        : "
            f"{actual_push_rate:.2f}"
        )

        print(
            f"Average pipe write   : "
            f"{avg_push_write:.2f} ms"
        )

        print(
            f"Maximum pipe write   : "
            f"{snapshot['push_write_ms_max']:.2f} ms"
        )

        print(
            f"Push restarts        : "
            f"{snapshot['push_restarts']}"
        )

        print("=" * 78)


if __name__ == "__main__":
    signal.signal(
        signal.SIGINT,
        signal_handler,
    )

    signal.signal(
        signal.SIGTERM,
        signal_handler,
    )

    main()
