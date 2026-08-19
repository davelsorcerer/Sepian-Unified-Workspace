import os
import sys
import time
import cv2
import os
import json
import tempfile
from collections import deque

SIGNAL_FILE = "/home/davel/Sepian-Unified-Workspace/sepian_person_signal"

# Ensure the parent directory exists
os.makedirs(os.path.dirname(SIGNAL_FILE), exist_ok=True)

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
SIGNAL_FILE = "/home/davel/Sepian-Unified-Workspace/sepian_person_signal"

# Camera index that works on your machine (you already verified it)
# Set to None to auto-detect (tries indices 0,1,2 with V4L2 then default backend)
CAM_INDEX = None

# Camera indices to try when CAM_INDEX is None (in order)
CAM_INDEX_FALLBACKS = [0, 1, 2]

# Detection parameters (tweak if you get too many/too few detections)
HAAR_SCALE_FACTOR = 1.1      # How much the image size is reduced at each image scale
HAAR_MIN_NEIGHBORS = 5       # How many neighbors each candidate rectangle should have
HAAR_MIN_SIZE = (30, 30)     # Minimum possible face size

# Debounce / hysteresis settings
DEBOUNCE_FRAMES = 5          # Need N consecutive frames with a face to become "present"
ABSENT_THRESHOLD = 10        # Need N consecutive frames without a face to become "absent"

# Video settings (lower resolution = less CPU / USB bandwidth)
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
TARGET_FPS = 15              # Try 15 fps first; lower if you still see crashes
BUFFER_SIZE = 1              # Keep only the most recent frame in the internal buffer
# Keep the forward-facing default within the middle quarter of the frame.
GAZE_LEFT_THRESHOLD = 0.375
GAZE_RIGHT_THRESHOLD = 0.625


def write_signal(face_center_x, frame_width):
    """Publish presence and normalized face position atomically."""
    normalized_x = face_center_x / max(frame_width, 1)
    if normalized_x < GAZE_LEFT_THRESHOLD:
        gaze = "left"
    elif normalized_x > GAZE_RIGHT_THRESHOLD:
        gaze = "right"
    else:
        gaze = "center"
    payload = {
        "state": "PERSON_DETECTED",
        "gaze": gaze,
        "face_center_x": round(face_center_x, 1),
        "frame_width": frame_width,
    }
    directory = os.path.dirname(SIGNAL_FILE)
    fd, temp_path = tempfile.mkstemp(prefix=".sepian_person_signal.", dir=directory)
    try:
        with os.fdopen(fd, "w") as signal:
            json.dump(payload, signal)
        os.replace(temp_path, SIGNAL_FILE)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return gaze

# ----------------------------------------------------------------------
# Helper: load the Haar cascade (OpenCV ships it with the Python package)
# ----------------------------------------------------------------------
def load_haar_cascade():
    cascade_path = "/home/davel/Sepian-Unified-Workspace/plugins/haarcascade_frontalface_default(1).xml"
    if not os.path.exists(cascade_path):
        sys.stderr.write(f"ERROR: Haar cascade file not found at {cascade_path}\n")
        sys.exit(1)
    return cv2.CascadeClassifier(cascade_path)

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    # ---- Load detector -------------------------------------------------
    face_cascade = load_haar_cascade()
    print("Haar cascade loaded ✓")

    # ---- Open camera ---------------------------------------------------
    # Build the list of (index, backend_label, backend_const) candidates to try
    if CAM_INDEX is not None:
        indices_to_try = [CAM_INDEX]
    else:
        indices_to_try = CAM_INDEX_FALLBACKS

    candidates = []
    for idx in indices_to_try:
        candidates.append((idx, "V4L2", cv2.CAP_V4L2))
        candidates.append((idx, "DEFAULT", None))  # None -> OpenCV picks default backend

    cap = None
    opened_index = None
    opened_backend = None
    for idx, label, backend in candidates:
        try:
            if backend is None:
                print(f"Opening camera index {idx} with {label} backend …")
                c = cv2.VideoCapture(idx)
            else:
                print(f"Opening camera index {idx} with {label} backend …")
                c = cv2.VideoCapture(idx, backend)
        except Exception as e:
            print(f"  -> {label} raised exception: {e}")
            continue
        if not c.isOpened():
            print(f"  -> {label} could not open /dev/video{idx}")
            try:
                c.release()
            except Exception:
                pass
            continue
        # Verify we can actually read a frame
        ret, _ = c.read()
        if not ret:
            print(f"  -> {label} opened /dev/video{idx} but cannot read frames")
            try:
                c.release()
            except Exception:
                pass
            continue
        cap = c
        opened_index = idx
        opened_backend = label
        break

    if cap is None:
        sys.stderr.write(
            f"ERROR: Could not open any camera. Tried indices {indices_to_try} "
            f"with V4L2 and DEFAULT backends.\n"
            f"  - Check that the USB cam is plugged in: ls /dev/video*\n"
            f"  - Check perms: you may need to be in the 'video' group\n"
            f"  - Try a different USB port or cable\n"
        )
        sys.exit(1)
    print(f"Camera opened and reads frames OK on index {opened_index} ({opened_backend} backend) ✓")
    print(f"Camera opened and streaming frames ✓")

    # Optional: flush a few frames to clear any stale buffers
    for _ in range(5):
        cap.read()

    # ---- Set video properties -------------------------------------------
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, BUFFER_SIZE)

    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    print(f"Resolution set to: {actual_w}×{actual_h} @ {actual_fps:.2f} FPS")

    # ---- Detection state ------------------------------------------------
    present = False          # Has a person been detected recently?
    pos_run = 0              # Consecutive frames WITH a face
    neg_run = 0              # Consecutive frames WITHOUT a face
    face_center_history = deque(maxlen=5)

    print("\nPerson detector ACTIVE")
    print("👀 LOOK FOR THE OPENCV WINDOW:")
    print("   - Green rectangle = Face detected")
    print("   - Red text = No face seen")
    print("   - Press 'q' in the video window to quit\n")

    try:
        while True:
            # ---- Grab a frame -------------------------------------------
            ret, frame = cap.read()
            if not ret:
                # If we can't read, pause briefly and try again (avoid busy‑loop)
                time.sleep(0.01)
                continue

            # ---- Convert to grayscale for Haar ---------------------------
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # ---- Detect faces --------------------------------------------
            faces = face_cascade.detectMultiScale(
                gray,
                scaleFactor=HAAR_SCALE_FACTOR,
                minNeighbors=HAAR_MIN_NEIGHBORS,
                minSize=HAAR_MIN_SIZE,
                flags=cv2.CASCADE_SCALE_IMAGE
            )

            # ---- Prepare display frame (we'll draw on this) -------------
            display = frame.copy()

            # ---- Update detection state ----------------------------------
            if len(faces) > 0:
                # FACE SEEN
                pos_run += 1
                neg_run = 0

                # Draw GREEN boxes around all faces
                for (x, y, w, h) in faces:
                    cv2.rectangle(display, (x, y), (x + w, y + h), (0, 255, 0), 2)

                # Keep the gaze direction current once a face is present.
                largest_face = max(faces, key=lambda face: face[2] * face[3])
                face_x = largest_face[0] + largest_face[2] / 2.0
                face_center_history.append(face_x)
                smoothed_face_x = sum(face_center_history) / len(face_center_history)
                if present:
                    write_signal(smoothed_face_x, frame.shape[1])

                # Check if we just STARTED seeing a person
                if not present and pos_run >= DEBOUNCE_FRAMES:
                    present = True
                    pos_run = 0  # reset counter
                    # Write the first presence signal with gaze metadata.
                    write_signal(smoothed_face_x, frame.shape[1])
                    print("🔔 PERSON DETECTED → Signal sent")
            else:
                # NO FACE SEEN
                neg_run += 1
                pos_run = 0
                face_center_history.clear()

                # Draw status text (no face)
                cv2.putText(
                    display,
                    "NO FACE",
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 0, 255),  # Red
                    2,
                    cv2.LINE_AA
                )

                # Check if person just LEFT
                if present and neg_run >= ABSENT_THRESHOLD:
                    present = False
                    if os.path.exists(SIGNAL_FILE):
                        try:
                            os.remove(SIGNAL_FILE)
                        except OSError:
                            pass
                    print("👋 PERSON LEFT → Signal cleared")

            # ---- Overlay status information -------------------------------
            status_text = (
                f"Faces: {len(faces)} | "
                f"Present: {present} | "
                f"+Run: {pos_run}/{DEBOUNCE_FRAMES} | "
                f"-Run: {neg_run}/{ABSENT_THRESHOLD}"
            )
            cv2.putText(
                display,
                status_text,
                (10, display.shape[0] - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (255, 255, 255),  # White
                1,
                cv2.LINE_AA
            )

            # ---- Show the frame -------------------------------------------
            cv2.imshow("Sepian Person Detector (Press Q to quit)", display)

            # ---- Exit on 'q' key -----------------------------------------
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

            # Small delay to ease CPU load (adjust if needed)
            time.sleep(0.01)

    except KeyboardInterrupt:
        print("\n🛑 Detector stopped by user (Ctrl+C)")
    finally:
        # ---- Cleanup ----------------------------------------------------
        print("Releasing camera and cleaning up …")
        cap.release()
        cv2.destroyAllWindows()
        if os.path.exists(SIGNAL_FILE):
            try:
                os.remove(SIGNAL_FILE)
                print("Signal file cleaned up.")
            except OSError as e:
                print(f"Warning: Could not remove signal file: {e}")

if __name__ == "__main__":
    main()
