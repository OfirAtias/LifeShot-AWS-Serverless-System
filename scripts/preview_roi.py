import json
import cv2
import numpy as np

CONFIG_PATH = "config.json"

def main():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    video_path = cfg["video_path"]
    roi_poly = np.array(cfg["roi_polygon"], dtype=np.int32)

    cap = cv2.VideoCapture(video_path)
    ret, frame = cap.read()
    cap.release()

    if not ret:
        raise RuntimeError("Cannot read video frame")

    cv2.polylines(frame, [roi_poly], True, (0, 255, 0), 3)
    cv2.putText(
        frame,
        "ROI Preview",
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (0, 255, 0),
        2
    )

    cv2.imshow("ROI Preview", frame)
    cv2.waitKey(0)
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
