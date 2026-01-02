import cv2
import json
import os

VIDEO_PATH = os.path.join("data", "demo_video.mp4")
OUT_CONFIG = os.path.join("config.json")

points = []

def on_mouse(event, x, y, flags, param):
    global points
    if event == cv2.EVENT_LBUTTONDOWN:
        points.append([int(x), int(y)])

def draw_overlay(img):
    # Draw points
    for p in points:
        cv2.circle(img, (p[0], p[1]), 5, (0, 255, 0), -1)

    # Draw polygon lines
    if len(points) >= 2:
        for i in range(len(points) - 1):
            cv2.line(img, tuple(points[i]), tuple(points[i + 1]), (0, 255, 0), 2)

    # Close polygon preview
    if len(points) >= 3:
        cv2.line(img, tuple(points[-1]), tuple(points[0]), (0, 255, 0), 2)

    # Help text
    cv2.putText(
        img,
        "Click to add points | ENTER=save | Z=undo | R=reset | ESC=exit",
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 255, 255),
        2
    )

def main():
    global points   

    cap = cv2.VideoCapture(VIDEO_PATH)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {VIDEO_PATH}")

    ret, frame = cap.read()
    cap.release()
    if not ret:
        raise RuntimeError("Cannot read first frame from video")

    window = "Select ROI (Polygon)"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, 1200, 700)
    cv2.setMouseCallback(window, on_mouse)

    while True:
        vis = frame.copy()
        draw_overlay(vis)
        cv2.imshow(window, vis)

        key = cv2.waitKey(20) & 0xFF

        if key == 27:  # ESC
            print("Exit without saving.")
            break

        if key in (10, 13):  # ENTER
            if len(points) < 3:
                print("Need at least 3 points for a polygon.")
                continue

            config = {
                "video_path": VIDEO_PATH,
                "roi_polygon": points,
                "notes": "ROI polygon for analysis area (shadow side)",
                "analysis_fps": 2
            }

            with open(OUT_CONFIG, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)

            print(f"Saved ROI to {OUT_CONFIG}")
            break

        if key in (ord('z'), ord('Z')):
            if points:
                points.pop()
                print("Undo last point.")

        if key in (ord('r'), ord('R')):
            points.clear()
            print("Reset all points.")

    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
