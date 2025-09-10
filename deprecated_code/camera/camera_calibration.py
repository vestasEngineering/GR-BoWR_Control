
import numpy as np
import cv2
import time

def setup_camera(c: int):
    return cv2.VideoCapture(c)

def initialize_lists():
    objp = np.zeros((6*5,3), np.float32)
    objp[:,:2] = np.mgrid[0:5,0:6].T.reshape(-1,2)
    objpoints = [] # 3d point in real world space
    imgpoints = [] # 2d points in image plane.
    return objp, objpoints, imgpoints

def get_gray_and_image(vidcap):
    _, image = vidcap.read()
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), image



def get_30_images(name: str, cam:int):
    vidcap = setup_camera(cam)
    objp, objpoints, imgpoints = initialize_lists()

    i = 0
    while i < 30:
        gray, image = get_gray_and_image(vidcap)
        # Find the chess board corners
        ret, corners = cv2.findChessboardCorners(gray, (5,6), None)
        # If found, add object points, image points (after refining them)

        if ret:
            cv2.drawChessboardCorners(image, (5,6), corners, ret)
            cv2.imshow('img', image)
            myInput = cv2.waitKey(0)
            if myInput == ord('r'):
                print('skipped')
                continue
            if myInput == ord('a'):
                objpoints.append(objp)
                criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
                corners2 = cv2.cornerSubPix(gray,corners, (11,11), (-1,-1), criteria)
                imgpoints.append(corners2)
                # Draw and display the corners
                cv2.drawChessboardCorners(image, (7,6), corners2, ret)
                cv2.imwrite(name+'_'+str(i)+'.jpg', gray)
                i = i + 1
                print(i)
        else:
            cv2.imshow('img', image)
            cv2.waitKey(10)

    ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(objpoints, imgpoints, gray.shape[::-1], None, None)
    np.savetxt(name+'_mtx.txt', mtx)
    np.savetxt(name+'_dist.txt', dist)
    calculate_mean_error_on_image_data(objpoints, imgpoints, rvecs, tvecs, mtx, dist)


def calculate_mean_error_on_image_data(objpoints, imgpoints, rvecs, tvecs, mtx, dist):
    this_mean_error = 0.0
    for i in range(len(objpoints)):
        imgpoints2, _ = cv2.projectPoints(objpoints[i], rvecs[i], tvecs[i], mtx, dist)
        error = cv2.norm(imgpoints[i], imgpoints2, cv2.NORM_L2)/len(imgpoints2)
        this_mean_error += error
    print( "total error: {}".format(this_mean_error/len(objpoints)) )
    return this_mean_error



def test_calibration(name: str, cam: int):
    mtx = np.loadtxt( name+'mtx.txt')
    dist = np.loadtxt(name+'dist.txt')


    img = cv2.imread(name+'0.jpg')
    h,  w = img.shape[:2]
    newcameramtx, roi = cv2.getOptimalNewCameraMatrix(mtx, dist, (w,h), 1, (w,h))
    # undistort
    dst = cv2.undistort(img, mtx, dist, None, newcameramtx)
    # crop the image
    x, y, w, h = roi
    #dst = dst[y:y+h, x:x+w]
    cv2.imwrite(name+'0_calibrated.jpg', dst)




def calibrate():

    #get_30_images('cam_0', 0)
    
    test_calibration('cam_0_', 0)

calibrate()




