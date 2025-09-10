import subprocess
import re
import numpy as np
import matplotlib.pyplot as plt
import matplotlib 
import signal
import traceback
import statistics

import time
np.set_printoptions(suppress=True)
plt.ion()



def get_xs(scan):
    return (np.sin(np.radians( scan[:,0])) * scan[:,1]).reshape(-1,1)

def get_ys(scan):
    return (np.cos(np.radians( scan[:,0])) * scan[:,1]).reshape(-1,1)


def get_abs_diffs(scan):
    return np.sqrt(np.square(np.diff(scan[:, 1], append=np.zeros(1)).reshape(-1,1))) # diffs in distance



def show_me_the_data(scan):
    # theta and dist
    #ax.scatter(np.radians(scan[:,0]), scan[:,1])

    xs = get_xs(scan)
    ys = get_ys(scan)

    ax1.set_xlim(-300.0, 300.0)  # Set a reasonable range for distances
    ax1.set_ylim(-300.0, 300.0)  # Set a reasonable range for distances
    ax1.scatter(0,0)

    ax2.scatter(scan[:, 3], scan[:, 4],scan[:, 5])

    # draw roi
    for angle in [0.0, 30.0, 330.0]:
        roi_x = np.sin(np.radians(angle)) * 300.0 # 90 is added to rotate the chart
        roi_y = np.cos(np.radians(angle)) * 300.0
        ax1.plot([0, roi_x], [0, roi_y], '--', linewidth=2)

    ax1.scatter(xs,ys, marker='.', label='scan')
    plt.legend()
    plt.pause(0.1)



def grab_scan():
    scan = []
    while True:
        line = process.stdout.readline()
        if 'sync' in line:
            sync = int(re.search(r'sync:\s*([\d.]+)', line).group(1))
            theta = float(re.search(r'theta:\s*([\d.]+)', line).group(1))
            dist = float(re.search(r'Dist:\s*([\d.]+)', line).group(1))
            q = float(re.search(r'Q:\s*([\d.]+)', line).group(1))

            if sync == 1:
                return np.array(scan)
            else:
                scan.append([theta, dist, q])



def clear_out_zero_distances(scan):
    return scan[scan[:,1] > 0.0]



def get_scan_roi(scan):

    return scan[(scan[:,0] < 30.0) | (scan[:,0] > 330.0)] 

def get_x_offset(web_scan):
    # deg  dist  q   xs  ys  mode
    left = web_scan[web_scan[:,3] < 0.0]
    right = web_scan[web_scan[:,3] > 0.0]

    ax1.scatter(right[:,3], right[:,4], s=100, label='right', marker = 'x')
    ax1.scatter(left[:,3], left[:,4], s=100, label='left', marker = 'x')
    if len(right) > 0 and len(left) > 0:
        print('left', left)
        print('right', right)
        left_mins.append(min(left[:,3]))
        right_maxs.append(max(right[:,3]))

        if len(left_mins) == 20:
            ave_left = np.mean(left_mins)
            ave_right = np.mean(right_maxs)
            ax1.text(-150, -150, 'left: '+str(ave_left))
            ax1.text(-150, -160, 'right: '+str(ave_right))
            offset = ave_left + ave_right
            ax1.text(-150, -170, 'offset: '+str(offset))

            ax1.text(-150, -180, 'left len: '+str(len(left)))
            ax1.text(-150, -190, 'right len: '+str(len(right)))

            left_mins.pop(-1)
            right_maxs.pop(-1)


    return left, right

def get_mode(ys):
    '''get the mode of the last column of the scan, the y values. 
       Then multiply by a ones array to get an array of the mode'''
    # the slice [:,0] is to make this compatible with the mode function
    return statistics.mode( list( ys[:, 0].astype(int) ) )*np.ones(len(ys[:, 0]))


def get_scan_within_mode_tolerance(scan):
    mode = scan[:, -1][0] # first value all of mode should be the same....
    tolerance = 10.0
    lower_bound = mode - tolerance
    upper_bound = mode + tolerance

    # return the part of the scan where the ys are both above the lower and below the upper
    return scan[(scan[:, -3] > lower_bound) & (scan[:, -3] <= upper_bound)]

def extract_web_from_roi(scan):
    '''The web is the mode +/- a tolerance'''

def find_edges_of_web(scan, scan_num):
    '''instead of using an index we will 
    get the deg value next to the largest 
    discontinuity in order to keep the data aligned'''
    scan = clear_out_zero_distances(scan)

    scan = get_scan_roi(scan)

    xs = get_xs(scan)

    ys = get_ys(scan)

    mode = get_mode(ys) # mode as an int

    ts = np.ones(len(scan))*scan_num

    # deg dist q xs ys ts mode
    scan = np.column_stack([scan, xs, ys, ts, mode])
    print("scan", scan)

    web_scan = get_scan_within_mode_tolerance(scan)



    print("web", web_scan)
    offset = get_x_offset(web_scan)

    return scan

    #ax1.scatter(web_scan[:, -3], web_scan[:, -2], label='web')

    #right_edge = right[np.argmax(right[:, 3])+1] # this plus 1 is added due to how the differences are calculated
    #right_x = right_edge[-2] # next to last column of row that represents the edge
    #right_y = right_edge[-1] # last column



    #left_edge = left[np.argmax(left[:, 3])]
    #left_x = left_edge[-2]
    #left_y = left_edge[-1]


    #ax1.scatter(right_x,right_y)
    #ax1.scatter(left_x, left_y)
    #ax1.plot([0, right_x], [0, right_y], '--', linewidth=2)
    ##ax1.plot([0, left_x], [0, left_y], '--', linewidth=2)
    ##percent = (left_y+right_y)/left_y-right_y - 50.0
    #print(percent)
    #ax1.text(-150, -150, str(percent))
    #return percent




matplotlib.use('TkAgg')

# Use Popen to get a process handle
process = subprocess.Popen(
    ["./rplidar_sdk/output/Linux/Release/ultra_simple", "--channel", "--serial", "/dev/ttyUSB0", "460800"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    universal_newlines=True
)
#fig = plt.figure(figsize=(10,10))
#ax = fig.add_subplot(projection='polar')
fig1 = plt.figure(figsize=(10, 10))  # Increase figure size
ax1 = fig1.add_subplot(111)
fig2 = plt.figure(figsize=(10, 10))  # Increase figure size
ax2 = fig2.add_subplot(projection='3d')
left_mins = []
right_maxs = []
scans = []
scan_num = 0
while True:
    try:
        scan = grab_scan()
        if len(scan) > 0:
            
            scan = find_edges_of_web(scan, scan_num)

            show_me_the_data(scan)
            time.sleep(0.1)
            input()
            ax1.clear()
            scan_num += 1

    except Exception as e:
        traceback.print_exc()
        process.send_signal(signal.SIGINT)
        process.wait()
        exit()
    except KeyboardInterrupt:
        print("EXITED")
        process.send_signal(signal.SIGINT)
        process.wait()
        exit()

