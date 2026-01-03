import json
import cv2
import numpy as np
import os
from ultralytics import YOLO
from collections import deque

CONFIG_PATH = "config.json"

# ===============================
# YOLO track memory (Ghost Boxes)
# ===============================
yolo_memory = {}  # track_id -> {"bbox": (x1,y1,x2,y2), "miss": int}
YOLO_MAX_MISS = 30  # keep YOLO box even if temporarily lost (analysis frames)


# ===============================
# MOTION stable tracks (Temporary IDs)
# ===============================
class MotionTrackManager:
    def __init__(self, max_miss=20, min_hits_to_show=3, iou_match=0.2):
        self.next_id = 1
        self.tracks = {}  # id -> {"bbox":..., "miss":..., "hits":..., "age":...}
        self.max_miss = max_miss
        self.min_hits_to_show = min_hits_to_show
        self.iou_match = iou_match

    def update(self, detections):
        assigned_tracks = set()
        assigned_dets = set()

        dets = detections[:]
        track_ids = list(self.tracks.keys())

        pairs = []
        for tid in track_ids:
            tb = self.tracks[tid]["bbox"]
            for di, db in enumerate(dets):
                iou = iou_xyxy(tb, db)
                if iou >= self.iou_match:
                    pairs.append((iou, tid, di))

        pairs.sort(reverse=True, key=lambda x: x[0])

        for iou, tid, di in pairs:
            if tid in assigned_tracks or di in assigned_dets:
                continue
            assigned_tracks.add(tid)
            assigned_dets.add(di)

            self.tracks[tid]["bbox"] = dets[di]
            self.tracks[tid]["miss"] = 0
            self.tracks[tid]["hits"] += 1
            self.tracks[tid]["age"] += 1

        for tid in list(self.tracks.keys()):
            if tid not in assigned_tracks:
                self.tracks[tid]["miss"] += 1
                self.tracks[tid]["age"] += 1
                if self.tracks[tid]["miss"] > self.max_miss:
                    del self.tracks[tid]

        for di, db in enumerate(dets):
            if di in assigned_dets:
                continue
            tid = self.next_id
            self.next_id += 1
            self.tracks[tid] = {"bbox": db, "miss": 0, "hits": 1, "age": 1}

    def get_active(self):
        out = []
        for tid, t in self.tracks.items():
            show = (t["hits"] >= self.min_hits_to_show)
            out.append((tid, t["bbox"], show))
        return out


def point_in_poly_mask(frame_shape, poly):
    mask = np.zeros(frame_shape[:2], dtype=np.uint8)
    cv2.fillPoly(mask, [poly], 255)
    return mask


def iou_xyxy(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    a_area = max(1, (ax2 - ax1) * (ay2 - ay1))
    b_area = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / float(a_area + b_area - inter)


def motion_boxes(prev_gray, gray, roi_mask):
    """
    Motion tuned for pool:
    - Reduce water shimmer (blur + higher threshold)
    - Filter "human-ish" blobs
    - Allow smaller objects for far people/heads, but stability enforced later via MotionTrackManager
    """
    diff = cv2.absdiff(gray, prev_gray)
    diff[roi_mask == 0] = 0

    diff = cv2.GaussianBlur(diff, (11, 11), 0)
    _, th = cv2.threshold(diff, 35, 255, cv2.THRESH_BINARY)

    kernel = np.ones((5, 5), np.uint8)
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel, iterations=1)
    th = cv2.dilate(th, kernel, iterations=2)

    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    for c in cnts:
        area = cv2.contourArea(c)
        if area < 700:
            continue

        x, y, w, h = cv2.boundingRect(c)

        if w < 18 or h < 18:
            continue

        aspect = h / float(w + 1e-6)
        if aspect < 0.35 or aspect > 3.2:
            continue

        boxes.append((x, y, x + w, y + h))

    return boxes


def main():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    video_path = cfg["video_path"]
    roi_poly = np.array(cfg["roi_polygon"], dtype=np.int32)

    analysis_fps = int(cfg.get("analysis_fps", 5))

    tracker_path = os.path.join("trackers", "bytetrack.yaml")
    if not os.path.exists(tracker_path):
        raise RuntimeError(f"Tracker YAML not found: {tracker_path}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    src_fps = cap.get(cv2.CAP_PROP_FPS)
    if not src_fps or src_fps <= 1:
        src_fps = 30.0

    frame_skip = max(1, int(round(src_fps / analysis_fps)))
    print(f"Source FPS: {src_fps:.2f} | Analysis FPS: {analysis_fps} | Frame skip: {frame_skip}")

    ret, first = cap.read()
    if not ret:
        raise RuntimeError("Cannot read first frame")

    roi_mask = point_in_poly_mask(first.shape, roi_poly)
    prev_gray = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    model = YOLO("yolov8s.pt")

    motion_mgr = MotionTrackManager(max_miss=20, min_hits_to_show=3, iou_match=0.2)

    # ===============================
    # Count-based drowning alert
    # ===============================
    COUNT_WINDOW = int(analysis_fps * 5)         # ~5 seconds history
    DROP_CONFIRM_FRAMES = int(analysis_fps * 3)  # ~3 seconds sustained drop
    count_history = deque(maxlen=max(10, COUNT_WINDOW))
    drop_counter = 0
    scene_alert = False

    win = "Tracking in ROI (Count-Based Alert)"
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

        out = frame.copy()
        cv2.polylines(out, [roi_poly], True, (0, 255, 0), 2)

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # ----------------------------
        # 1) YOLO tracking
        # ----------------------------
        roi_frame = frame.copy()
        roi_frame[roi_mask == 0] = 0

        results = model.track(
            source=roi_frame,
            persist=True,
            tracker=tracker_path,
            classes=[0],
            conf=0.15,
            iou=0.45,
            imgsz=1280,
            verbose=False
        )

        yolo_boxes_all = []
        seen_yolo_ids = set()

        r = results[0]
        if r.boxes is not None and len(r.boxes) > 0:
            boxes = r.boxes.xyxy.cpu().numpy()
            ids = r.boxes.id.cpu().numpy().astype(int) if r.boxes.id is not None else None

            if ids is not None:
                for i, b in enumerate(boxes):
                    x1, y1, x2, y2 = b.astype(int)
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

                    if cy < 0 or cy >= roi_mask.shape[0] or cx < 0 or cx >= roi_mask.shape[1]:
                        continue
                    if roi_mask[cy, cx] == 0:
                        continue

                    tid = ids[i]
                    seen_yolo_ids.add(tid)

                    bb = (x1, y1, x2, y2)
                    yolo_boxes_all.append(bb)

                    yolo_memory[tid] = {"bbox": bb, "miss": 0}

                    cv2.rectangle(out, (x1, y1), (x2, y2), (255, 255, 0), 2)
                    cv2.putText(out, f"ID {tid}", (x1, max(20, y1 - 8)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

        # hold YOLO boxes
        for tid in list(yolo_memory.keys()):
            if tid in seen_yolo_ids:
                continue

            yolo_memory[tid]["miss"] += 1
            if yolo_memory[tid]["miss"] > YOLO_MAX_MISS:
                del yolo_memory[tid]
                continue

            x1, y1, x2, y2 = yolo_memory[tid]["bbox"]
            bb = (x1, y1, x2, y2)
            yolo_boxes_all.append(bb)

            cv2.rectangle(out, (x1, y1), (x2, y2), (255, 255, 0), 1)
            cv2.putText(out, f"ID {tid} (hold)", (x1, max(20, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)

        # ----------------------------
        # 2) Motion -> stable IDs
        # ----------------------------
        m_dets = motion_boxes(prev_gray, gray, roi_mask)
        motion_mgr.update(m_dets)

        motion_boxes_used = []
        for mid, mb, show in motion_mgr.get_active():
            if not show:
                continue

            overlapped = False
            for pb in yolo_boxes_all:
                if iou_xyxy(mb, pb) > 0.15:
                    overlapped = True
                    break
            if overlapped:
                continue

            x1, y1, x2, y2 = mb
            motion_boxes_used.append(mb)

            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 200, 255), 2)
            cv2.putText(out, f"M{mid}", (x1, max(20, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)

        prev_gray = gray

        # ----------------------------
        # 3) Count-based alert (no IDs)
        # ----------------------------
        current_count = len(yolo_boxes_all) + len(motion_boxes_used)
        count_history.append(current_count)

        # show debug count
        cv2.putText(out, f"COUNT: {current_count}", (20, out.shape[0] - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)

        if len(count_history) >= count_history.maxlen:
            max_count = max(count_history)

            # If count dropped by >=1 and still people remain in scene
            if (max_count - current_count) >= 1 and current_count >= 1:
                drop_counter += 1
            else:
                drop_counter = 0

            # Confirm sustained drop
            if drop_counter >= DROP_CONFIRM_FRAMES:
                scene_alert = True

        # Draw alert banner
        if scene_alert:
            cv2.rectangle(out, (0, 0), (out.shape[1], 70), (0, 0, 255), -1)
            cv2.putText(out, "🚨 POSSIBLE DROWNING – PERSON COUNT DROPPED 🚨",
                        (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (255, 255, 255), 3)

        cv2.imshow(win, out)
        if cv2.waitKey(1) & 0xFF == 27:
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
