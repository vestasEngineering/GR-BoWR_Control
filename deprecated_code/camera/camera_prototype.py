import cv2
import numpy as np

def main():
    vidcap0 = cv2.VideoCapture(0)
    vidcap1 = cv2.VideoCapture(2)

    # left
    mtx0 = np.loadtxt('cam_0_mtx.txt')
    dist0 = np.loadtxt('cam_0_dist.txt')
    
    # right
    mtx1 = np.loadtxt('cam_1_mtx.txt')
    dist1 = np.loadtxt('cam_1_dist.txt')

    _, size_image = vidcap1.read()
    h, w = size_image.shape[:2]
    newCameraMtx0, roi0 = cv2.getOptimalNewCameraMatrix(mtx0, dist0, (w, h), 1, (w, h))
    newCameraMtx1, roi1 = cv2.getOptimalNewCameraMatrix(mtx1, dist1, (w, h), 1, (w, h))

    cv2.stereoRectify(mtx0, dist0, mtx1, dist1, left_image.shape[::-1], R, T)

    if vidcap0.isOpened() and vidcap1.isOpened():
        success1, success2 = True, True
        while success1 and success2:
            success1, left_image = vidcap0.read()
            success2, right_image = vidcap1.read()
            cv2.imshow('left', left_image)
            cv2.imshow('right', right_image)
            cv2.waitKey(1)
            # Undistort and crop the images
            left_image = cv2.undistort(left_image, mtx0, dist0, None, newCameraMtx0)
            right_image = cv2.undistort(right_image, mtx1, dist1, None, newCameraMtx1)
            #x, y, w, h = roi0
            #left_image = left_image[y:y + h, x:x + w]
            #right_image = right_image[y:y + h, x:x + w]

            # Set StereoBM parameters
            stereo = cv2.StereoSGBM.create(numDisparities=64, blockSize=21)
            left_image = cv2.cvtColor(left_image, cv2.COLOR_BGR2GRAY)
            right_image = cv2.cvtColor(right_image, cv2.COLOR_BGR2GRAY)


            # Compute disparity map
            disparity = stereo.compute(left_image, right_image)

            # Post-process disparity map
            disparity = cv2.medianBlur(disparity, 5)

            # Visualize disparity map
            disparity_normalized = cv2.normalize(disparity, None, 0, 255, cv2.NORM_MINMAX, cv2.CV_8U)
            cv2.imshow('Disparity Map', disparity_normalized)
            cv2.waitKey(1)

if __name__ == "__main__":
    main()