import json
import cv2
import numpy as np
import os
from ultralytics import YOLO

CONFIG_PATH = "config.json"

def point_in_poly_mask(frame_shape, poly):
    mask = np.zeros(frame_shape[:2], dtype=np.uint8)
    cv2.fillPoly(mask, [poly], 255)
    return mask

def main():
    # Load config
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    video_path = cfg["video_path"]
    roi_poly = np.array(cfg["roi_polygon"], dtype=np.int32)

    # להגדיל זמנית ל-5 כדי לייצב IDs (אחר כך נחזיר ל-2 ללוגיקת טביעה)
    analysis_fps = int(cfg.get("analysis_fps", 5))

    tracker_path = os.path.join("trackers", "bytetrack.yaml")
    if not os.path.exists(tracker_path):
        raise RuntimeError(f"Tracker YAML not found: {tracker_path} (create it first)")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    src_fps = cap.get(cv2.CAP_PROP_FPS)
    if not src_fps or src_fps <= 1:
        src_fps = 30.0

    frame_skip = max(1, int(round(src_fps / analysis_fps)))
    print(f"Source FPS: {src_fps:.2f} | Analysis FPS: {analysis_fps} | Frame skip: {frame_skip}")

    # ROI mask (same for all frames)
    ret, first = cap.read()
    if not ret:
        raise RuntimeError("Cannot read first frame")
    mask = point_in_poly_mask(first.shape, roi_poly)

    # Rewind to start
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    # Stronger model for crowded pool scenes (more stable detections)
    model = YOLO("yolov8s.pt")

    win = "Tracking in ROI"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 1200, 700)

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1
        if frame_idx % frame_skip != 0:
            continue

        # Apply ROI mask (keep only ROI area, black out the rest)
        roi_frame = frame.copy()
        roi_frame[mask == 0] = 0

        # Track persons only (class 0 in COCO)
        results = model.track(
            source=roi_frame,
            persist=True,
            tracker=tracker_path,
            classes=[0],
            conf=0.5,
            iou=0.6,
            verbose=False
        )

        out = frame.copy()

        # Draw ROI polygon
        cv2.polylines(out, [roi_poly], True, (0, 255, 0), 2)

        r = results[0]
        if r.boxes is not None and len(r.boxes) > 0:
            boxes = r.boxes.xyxy.cpu().numpy()
            ids = None
            if r.boxes.id is not None:
                ids = r.boxes.id.cpu().numpy().astype(int)

            for i, b in enumerate(boxes):
                x1, y1, x2, y2 = b.astype(int)

                # Ignore boxes outside ROI (extra safety)
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                if cy < 0 or cy >= mask.shape[0] or cx < 0 or cx >= mask.shape[1]:
                    continue
                if mask[cy, cx] == 0:
                    continue

                track_id = ids[i] if ids is not None else -1
                cv2.rectangle(out, (x1, y1), (x2, y2), (255, 255, 0), 2)
                cv2.putText(
                    out,
                    f"ID {track_id}",
                    (x1, max(20, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (255, 255, 0),
                    2
                )

        cv2.imshow(win, out)
        key = cv2.waitKey(1) & 0xFF
        if key == 27:  # ESC
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
