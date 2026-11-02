# Face detection assets

`face_detection_yunet_2023mar.onnx` is the YuNet face detector from
[OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet).
It is used by `app/likeness.py` to crop a tight face/hair reference before an
explicit scene. OpenCV 5.0 removed `cv2.CascadeClassifier`, so the ONNX model is
the primary detector.

`haarcascade_frontalface_default.xml` is kept as a fallback for older OpenCV
builds that still ship `CascadeClassifier` but not the model file.

Both files are data assets, not application code. See the upstream OpenCV Zoo
repository for the model license and provenance.
