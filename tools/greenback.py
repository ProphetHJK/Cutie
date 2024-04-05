from concurrent.futures import ThreadPoolExecutor
import cv2
import numpy as np
import glob
import configparser,sys
from PIL import Image
import os
import time
import ffmpeg
import threading
import subprocess as sp
import shlex
import copy
import matplotlib.pyplot as plt
cupy_enable = True
try:
    import cupy as cp
except ImportError:
    print("Can't find cupy，see readme.md for more imformation.")
    cupy_enable = False

"""
本程序用于生成绿幕视频，方便PR等视频剪辑工具导入

"""
class GreenBack():
    def __init__(self) -> None:
        self.cupyflag = cupy_enable
        
        # if self.cupyflag:
        #     self.codec = '-c:v hevc_nvenc -preset p7 -tune hq -rc-lookahead 20'
        # else:
        #     self.codec = '-c:v libx265 -crf 26'
        # self.codec = '-c:v libx265 -crf 25'
        self.codec = '-c:v libvpx-vp9 -b:v 0 -crf 25 -auto-alt-ref 0'
        # 使用opencv提供的VideoWriter功能，否则使用ffmpeg
        self.videowriter_flag = False
        # 使用opencv提供的VideoCapture功能，否则使用ffmpeg
        self.videoreader_flag = True
        # 测试模式
        self.test_mode = False
        # 源文件名
        self.file_name = ""
        # 卷积和
        self.ekernel = None
        # mask中值滤波的ksize，越大越平滑，但细节丢失也越大,高斯41等效中值21
        self.mask_ksize = 0
        # mask侵蚀的ksize,608 5,1080p 7,2160 12
        self.mask_esize = 7
        # mask范围，1-254，越大绿幕的范围越大
        self.mask_range = 180
        # 是否启用跟踪
        self.focus_flag = False
        # 必须保证focus_width和focus_height是二的倍数
        self.focus_phase = 1
        # 跟踪步进，越小越灵敏
        self.focus_step = 20
        # focus模式下强制输出宽高
        self.focus_out_h = 0
        self.focus_out_w = 0
        # 是否启用softmask
        self.soft_mask = False
        # 多线程处理mask,根据电脑实际情况配置，videowriter是瓶颈就减少，mask处理是瓶颈就增加
        self.sem = threading.Semaphore(14)
        # 图片写入视频线程所用队列的信号
        self.write_sem = threading.Semaphore(1)
        # 写入完成信号
        self.end_sem = threading.Semaphore(0)
        # 每一帧的矩形信息
        self.focus_box_list = {}
        # 每一帧的缩放信息
        self.focus_scale_list = {}
        # 总处理帧数
        self.max_frame_num = None
        # mask列表
        self.appended_red_mask_list = {}
        # 输出帧列表
        self.out_crop_list = {}
        # 输出流
        self.out = None
        # 绿幕图片
        self.green_img = None
        # 背景图片
        self.backimg = None
        # 马赛克背景
        self.masaikeimg = None
        # 读入帧高
        self.h = 0
        # 读入帧宽
        self.w = 0
        # 输出帧高
        self.out_h = 0
        # 输出帧宽
        self.out_w = 0

    def overlay_image_with_mask(self,background, overlay, mask, x=0, y=0):
        """利用mask将BGR图像叠加到背景上"""
        h, w = overlay.shape[:2]
        alpha = mask / 255.0
        tmpbackground = copy.deepcopy(background)
        for c in range(0, 3):
            tmpbackground[y:y+h, x:x+w, c] = (alpha * overlay[:, :, c] +
                                        (1-alpha) * tmpbackground[y:y+h, x:x+w, c])
        return tmpbackground
    def create_checkerboard(self,bwidth:int, bheight:int, tile_size:int=100):
        """创建一个灰白相间的马赛克背景"""
        # 计算每种颜色的方块数量
        num_tiles_x = bwidth // tile_size
        num_tiles_y = bheight // tile_size
        # 创建一个空的图像
        checkerboard = np.zeros((bheight, bwidth, 3), dtype=np.uint8)
        # 填充方块
        for y in range(num_tiles_y):
            for x in range(num_tiles_x):
                if (x + y) % 2:
                    checkerboard[y*tile_size:(y+1)*tile_size, x*tile_size:(x+1)*tile_size] = 255  # 白色
                else:
                    checkerboard[y*tile_size:(y+1)*tile_size, x*tile_size:(x+1)*tile_size] = 127  # 灰色
        return checkerboard
    def convert_str_to_float(self, s: str) -> float:
        """Convert rational or decimal string to float
        """
        if '/' in s:
            num, denom = s.split('/')
            return float(num) / float(denom)
        return float(s)

    def print_all_numpy(self, n,file):
        np.set_printoptions(threshold=np.inf)
        with open(file, "w+") as external_file:
            print(n, file=external_file)


    def apply_mask(self,frame_index,img):
        w = self.w
        h = self.h
        red_mask_file_list = self.appended_red_mask_list[frame_index]
        if red_mask_file_list is None:
            # print('\nError,frame_num error,frame_num:%d\n' % (frame_num))
            if self.backimg is None:
                crop = self.green_img
            else:
                crop = self.backimg
        else:
            if self.soft_mask == True:
                # 使用'L'模式确保图片是灰度的
                tmpimgs = [np.array(Image.open(img_path1).convert('L')) for img_path1 in red_mask_file_list]
                # 使用求和的方式叠加图片
                # 注意：这里直接求和可能导致数值超出255的有效灰度范围
                combined_image = np.sum(tmpimgs, axis=0)

                # 为了防止溢出，我们将数值限制在0到255的范围内
                red_mask_img_grey = np.clip(combined_image, 0, 255).astype(np.uint8)
            else:
                red_mask_file = red_mask_file_list[0]
                # 读取为BGR
                if self.cupyflag:
                    red_mask_img = cp.asarray(cv2.imread(red_mask_file))
                else:
                    red_mask_img = cv2.imread(red_mask_file)
                
                if self.focus_flag == True:
                    if self.cupyflag:
                        red_pixels = cp.logical_and(red_mask_img[:, :, 2] == 128, red_mask_img[:, :, 1] == 0)
                    else:
                        red_pixels = np.logical_and(red_mask_img[:, :, 2] == 128, red_mask_img[:, :, 1] == 0)
                    # print(red_pixels)
                    red_mask_img[red_pixels] = [255, 255, 255]
                else:
                    # 背景为黑色，构造黑色mask
                    if self.cupyflag:
                        black_mask = ((red_mask_img[:, :, 2] == 0) & (red_mask_img[:, :, 1] == 0) & (red_mask_img[:, :, 0] == 0))
                    else:
                        black_mask = ((red_mask_img[:, :, 2] == 0) & (red_mask_img[:, :, 1] == 0) & (red_mask_img[:, :, 0] == 0))

                    # 非黑色mask合并成白色
                    red_mask_img[~black_mask] = [255,255,255]

                if self.cupyflag:
                    red_mask_img = cp.asnumpy(red_mask_img)

                # 转为灰度图
                red_mask_img_grey = cv2.cvtColor(red_mask_img, cv2.COLOR_BGR2GRAY)   
            # 图片缩放
            red_mask_img_grey = cv2.resize(red_mask_img_grey, (w, h),cv2.INTER_CUBIC)
            # 消除噪点：使用腐蚀加扩展消除噪点，效果不好，暂时不用
            # red_mask_img = cv2.dilate(red_mask_img, kernel, iterations = 1)
            red_mask_img_grey = cv2.erode(red_mask_img_grey, self.ekernel, iterations = 1)
            
            # 抗锯齿：使用中值滤波消除锯齿，效果不错，但很吃cpu
            # red_mask_img_grey = cv2.medianBlur(red_mask_img_grey, mask_ksize)
            # 抗锯齿：使用高斯滤波，性能和效果都不错
            # if self.focus_flag == False:
            #     red_mask_img_grey = cv2.GaussianBlur(red_mask_img_grey, (self.mask_ksize,self.mask_ksize),0)

            # 提取黑色（0）及其临近色（1-127）构建mask矩阵，处理后mask矩阵中黑色为255，非黑色为0，和像素点一一对应
            # 第一个参数：原始值
            # 第二个参数：lower_red指的是图像中低于这个lower_red的值，图像值变为0
            # 第三个参数：upper_red指的是图像中高于这个upper_red的值，图像值变为0
            # 而在lower_red～upper_red之间的值变成255
            # 临近色0-127
            # mask = cv2.inRange(red_mask_img_grey, 0, self.mask_range)
            mask = red_mask_img_grey

            # 一些调试打印信息
            # cv2.imwrite(dst_file+'.jpg',red_mask_img_grey)
            # sys.exit()
            # print_all_numpy(mask,'aftersmooth.txt')
            # cv2.imshow("Mask", mask)
            # if cv2.waitKey(1000) == ord('q'):
            #     sys.exit()
            if self.focus_flag == True:
                if self.focus_phase == 1:
                    if frame_index % self.focus_step != 0:
                        self.focus_box_list[frame_index] = None
                    else:
                        left = -1
                        right = -1
                        top = -1
                        bottom = -1
                        # 将绿色mask部分填充为背景
                        rows, cols = mask.shape
                        if self.cupyflag:
                            arr = cp.zeros((rows, cols), dtype=int)
                            arr[mask == 0] = 255
                            row_indexes, col_indexes = cp.nonzero(arr)
                            if len(col_indexes) > 0 and len(row_indexes) > 0:
                                left = cp.min(col_indexes)
                                right = cp.max(col_indexes)
                                top = cp.min(row_indexes)
                                bottom = cp.max(row_indexes)
                        else:
                            arr = np.zeros((rows, cols), dtype=int)
                            arr[mask == 0] = 255
                            row_indexes, col_indexes = np.nonzero(arr)
                            if len(col_indexes) > 0 and len(row_indexes) > 0:
                                left, top = np.min(col_indexes), np.min(row_indexes)
                                right, bottom = np.max(col_indexes), np.max(row_indexes)
                        radio = w/cols
                        left = int(left * radio)
                        right = int(right * radio)
                        top = int(top*radio)
                        bottom =int(bottom * radio)
                        self.focus_box_list[frame_index] = [left,right,top,bottom]
                    
                elif self.focus_phase == 2:
                    # if self.cupyflag:
                    #     arr = cp.zeros((self.h, self.w), dtype=int)
                    # else:
                    #     arr = np.zeros((self.h, self.w), dtype=int)

                    if self.cupyflag:
                        crop = cp.asarray(img)
                    else:
                        crop = img
                    info = self.focus_box_list[frame_index]
                    out_left,out_right,out_top,out_bottom = info[0],info[1],info[2],info[3]
                    crop = crop[out_top:out_bottom, out_left:out_right]
                    # print(out_top,out_bottom,out_left,out_right)
                    # print(crop.shape)

            else:
                if self.cupyflag:
                    crop = cp.asarray(img)
                else:
                    crop = img

                if self.backimg is None:
                    # 为透明通道置位
                    if self.cupyflag:
                        rgba_image = cp.dstack((crop, mask))
                    else:
                        rgba_image = np.dstack((crop, mask))
                    # rgba_image[mask == 0, 3] = 255
                    crop = rgba_image
                else: 
                    # 将绿色mask部分填充为背景
                    rows, cols = crop.shape[:2]
                    for i in range(rows):
                        for j in range(cols):
                            if mask[i,j] == 255:
                                crop[i, j] = self.backimg[i, j]

        # save
        if self.focus_flag == False or self.focus_phase != 1:
            if self.cupyflag:
                crop = cp.asnumpy(crop)
            if self.test_mode == True:
                outputimg = self.overlay_image_with_mask(self.masaikeimg, crop , mask)
                save_path = 'testimg{}/result_with_mask-{}-{}-{}.jpg'.format(self.file_name,self.mask_ksize,self.mask_esize,frame_index)

                # 检查目录是否存在，如果不存在则创建
                if not os.path.exists(os.path.dirname(save_path)):
                    os.makedirs(os.path.dirname(save_path))

                # 现在可以安全地保存图像
                cv2.imwrite(save_path, outputimg)
                
                # #显示原图
                # plt.subplot(3,2,1),plt.imshow(img,cmap = "gray")
                # plt.title("Original")
                # #显示处理后的图
                # plt.subplot(3,2,2),plt.imshow(opening,cmap ="gray")
                # plt.title("opening")
                
                # plt.subplot(3,2,3),plt.imshow(img1,cmap = "gray")
                # plt.title("Original")
                # #显示处理后的图
                # plt.subplot(3,2,4),plt.imshow(closing,cmap ="gray")
                # plt.title("closing")
                
                # plt.subplot(3,2,5),plt.imshow(img2,cmap = "gray")
                # plt.title("Original")
                # #显示处理后的图
                # plt.subplot(3,2,6),plt.imshow(gradient,cmap ="gray")
                # plt.title("gradient")
                # plt.show()
  
            else:
                self.out_crop_list[frame_index] = crop
            # print(crop.shape)
        self.sem.release()

    def write_out_crop(self):
        if self.focus_flag == True and self.focus_phase == 1:
            self.end_sem.release()
            return
        now_frame_index = 0
        while True:
            try:
                if self.videowriter_flag == True:
                    self.out.write(self.out_crop_list[now_frame_index])
                else:
                    self.out.stdin.write(self.out_crop_list[now_frame_index].astype(np.uint8).tobytes())
            except KeyError as ex:
                if self.max_frame_num == now_frame_index:
                    break
                else:
                    # 队列里没有要处理的就先阻塞
                    self.write_sem.acquire()
            else:
                self.out_crop_list.pop(now_frame_index)
                now_frame_index = now_frame_index + 1
        self.end_sem.release()
    # 去抖动
    def debounce(self):
        # 获取裁剪输出分辨率
        if self.focus_out_h != 0 and self.focus_out_w !=0:
            self.out_h = self.focus_out_h
            self.out_w = self.focus_out_w
        else:
            width_list = []
            height_list = []
            for index in range(0,len(self.focus_box_list)):
                if self.focus_box_list[index] == None:
                    continue
                info = self.focus_box_list[index]
                left,right,top,bottom = info[0],info[1],info[2],info[3]
                tmp_width = right - left
                tmp_height = bottom - top
                if tmp_width > 0 and tmp_height > 0:
                    width_list.append(tmp_width)
                    height_list.append(tmp_height)
            
            # 取90%的阈值
            height_list.sort()
            threshold = int(len(height_list) * 0.9)
            self.out_h = height_list[threshold]
            width_list.sort()
            threshold = int(len(width_list) * 0.9)
            self.out_w = width_list[threshold]
            
            # 取平均值
            # self.out_h = int(np.mean(height_list))
            # self.out_w = int(np.mean(width_list))       
            
            # 取最大值
            # max_height = 0
            # max_width = 0
            # for index in range(0,len(self.focus_box_list)):
            #     info = self.focus_box_list[index]
            #     if info == None:
            #         continue
            #     left,right,top,bottom = info[0],info[1],info[2],info[3]
            #     tmp_width = right - left
            #     tmp_height = bottom - top
            #     max_width = max(max_width,tmp_width)
            #     max_height = max(max_height,tmp_height)
                
            # self.out_h = max_height
            # self.out_w = max_width

        if self.out_h >= int(self.h * 0.91):
            self.out_h = self.h
        if self.out_w >= int(self.w * 0.91):
            self.out_w = self.w
        
        if self.out_h % 2 != 0:
            self.out_h = self.out_h - 1
        if self.out_w % 2 != 0:
            self.out_w = self.out_w - 1
        # print(self.out_h,self.out_w)
        mid_out_w = int(self.out_w/2)
        mid_out_h = int(self.out_h/2)
        step = self.focus_step
        for index in range(0,len(self.focus_box_list)):
            if index % step != 0:
                self.focus_box_list[index] = None
                continue
            info = self.focus_box_list[index]
            left,right,top,bottom = info[0],info[1],info[2],info[3]
            if left >= 0 and right >= 0 and top >= 0 and bottom >= 0:
                # 不剪裁画面，最小矩形外填充绿色
                # arr[top:bottom, left:right] = 1
                # crop[arr == 0] = [0,255,0]
                # 裁剪画面
                # print(left,right,top,bottom)
                mid_w = int((left + right)/2)
                mid_h = int((top + bottom)/2)
                # print(mid_w,mid_h,mid_out_w,mid_out_h)
                if mid_w < mid_out_w:
                    mid_w = mid_out_w 
                if mid_w > (self.w - mid_out_w):
                    mid_w = self.w - mid_out_w 
                out_left = int(mid_w - mid_out_w)
                out_right = int(mid_w + mid_out_w)
                # print(out_left,out_right)
                if mid_h < mid_out_h:
                    mid_h = mid_out_h
                if mid_h > (self.h - mid_out_h):
                    mid_h = self.h - mid_out_h
                out_top = int(mid_h - mid_out_h)
                out_bottom =int(mid_h + mid_out_h)
                self.focus_box_list[index] = [out_left,out_right,out_top,out_bottom]
            else:
                self.focus_box_list[index] = [0,self.out_w,0,self.out_h]
        first_range = len(self.focus_box_list) - 1 - (len(self.focus_box_list)-1) % step
        for index in range(0,first_range):
            if self.focus_box_list[index] == None:
                prev_list = self.focus_box_list[index - index % step]
                # print(index,step)
                last_list = self.focus_box_list[index + step - index % step]
                move_right = (last_list[1] - prev_list[1])/step*(index % step)
                move_bottom = (last_list[3] - prev_list[3])/step*(index % step)
                self.focus_box_list[index] = [int(prev_list[0]+move_right),int(prev_list[1]+move_right),int(prev_list[2]+move_bottom),int(prev_list[3]+move_bottom)]
                
        for index in range(first_range, len(self.focus_box_list)):
            self.focus_box_list[index] = self.focus_box_list[first_range]
    def run(self):
        start = time.time()
        
        self.end_sem = threading.Semaphore(0)

        config = configparser.ConfigParser()
        config.read('tools/config.ini',encoding='utf8')
        src_file_name = config['config']['src_file_name']
        self.file_name = src_file_name
        src_file_ext = config['config']['src_file_ext']
        object_num = config['config']['object_num']
        object_num = int(object_num)
        self.focus_flag = config.getboolean('config','focus_mode')
        self.soft_mask = config.getboolean('config', 'soft_mask')
        self.test_mode = config.getboolean('config', 'test_mode')
        self.ekernel = np.ones((self.mask_esize,self.mask_esize), np.uint8)
        # mask图片中非mask部分的hsv通道中的h，不能为黑色，红0，绿120，蓝240，Xmem分割单对象默认为红色
        none_mask_color_hue = 0

        src_file = "source/%s.%s" % (src_file_name,src_file_ext)
        if self.focus_flag == True:
            dst_file = 'workspace/%s/focus.mp4' % src_file_name
        else:
            dst_file = 'workspace/%s/greenback.webm' % src_file_name

        # open up video
        cap = cv2.VideoCapture(src_file)
        if self.soft_mask == True:
            tmp_mask_list = []
            for i in range(0,object_num):
                tmp_mask_list.append(sorted(glob.glob('workspace/{}/soft_masks/{}/*.png'.format(src_file_name,i+1))))
            red_mask_list = [list(group) for group in zip(*tmp_mask_list)]
            with open('output.txt', 'w') as file:
                # 将数组转换为字符串，并写入文件
                file.write(str(red_mask_list))
        else:
            red_mask_list = sorted(glob.glob('workspace/%s/masks/*.png' % src_file_name))
            red_mask_list = red_mask_list.reshape(-1, 1)
        backimg_path = None
        # backimg_path = "1.jpg"

        # grab one frame
        scale = 1
        _, frame = cap.read()
        self.h,self.w = frame.shape[:2]
        self.h = int(self.h*scale)
        self.w = int(self.w*scale)
        
        # 获取帧率,TODO:用r_frame_rate还是avg_frame_rate
        videoinfo = ffmpeg.probe(src_file)
        vs = next(c for c in videoinfo['streams'] if c['codec_type'] == 'video')
        framerate = vs['r_frame_rate']
        try:
            total_framenum = vs['nb_frames']
        except Exception as ex:
            print(ex)
            total_framenum = 99999
        # old
        # outstr = "".join(os.popen("ffprobe -v quiet -show_streams -select_streams v:0 %s |grep \"r_frame_rate\"" % src_file))
        # framerate = re.search("r_frame_rate=(.*)",outstr).group(1)

        if self.focus_flag == True:
            fr = self.convert_str_to_float(framerate)
            self.focus_step = int(fr)
            if self.focus_phase == 2:
                self.debounce()
        else:
            self.out_h = self.h
            self.out_w = self.w
        
        print(self.out_w,self.out_h)

        if backimg_path is not None:
            backimg_file = Image.open(backimg_path)
            backimg = backimg_file.convert("RGB")
            backimg_file.close()
            backimg = backimg.resize(((self.out_w, self.out_w)))
            backimg = np.array(backimg)
            if self.cupyflag:
                backimg = cp.asarray(backimg)

        # videowriter 
        res = (self.out_w, self.out_h)

        if os.path.exists(dst_file):
            os.remove(dst_file)
        if self.videowriter_flag == True:
            fr = self.convert_str_to_float(framerate)
            fourcc = cv2.VideoWriter_fourcc(*'XVID')
            if os.path.exists(dst_file+'.avi'):
                os.remove(dst_file+'.avi')
            self.out = cv2.VideoWriter(dst_file+'.avi',fourcc, fr, res)
        else:
            # Open ffmpeg application as sub-process
            # FFmpeg input PIPE: RAW images in BGR color format
            # FFmpeg output MP4 file encoded with HEVC codec.
            # Arguments list:
            # -y                   Overwrite output file without asking
            # -s {width}x{height}  Input resolution width x height (1344x756)
            # -pixel_format bgr24  Input frame color format is BGR with 8 bits per color component
            # -f rawvideo          Input format: raw video
            # -r {fps}             Frame rate: fps (25fps)
            # -i pipe:             ffmpeg input is a PIPE
            # -vcodec libx265      Video codec: H.265 (HEVC)
            # -pix_fmt yuv420p     Output video color space YUV420 (saving space compared to YUV444)
            # -crf 24              Constant quality encoding (lower value for higher quality and larger output file).
            # {output_filename}    Output file name: output_filename (output.mp4)
            # 如遇错误：Picture width must be an integer multiple of the specified chroma subsampling，是指yuv420p格式下视频长宽必须是2（或4）的倍数，源图片需要缩放大小
            # self.out = sp.Popen(shlex.split(f'ffmpeg -y -loglevel warning -f rawvideo -pix_fmt bgr24 -s {self.out_w}x{self.out_h} -r {framerate} -thread_queue_size 64 -i pipe: -i {src_file} -map 0:v -map 1:a? {self.codec} -pix_fmt yuv420p -c:a copy -shortest {dst_file}'), stdin=sp.PIPE)
            # ffmpeg -i input.mp4 -c:v libvpx-vp9 -b:v 2M -auto-alt-ref 0 -c:a libopus output.webm
            self.out = sp.Popen(shlex.split(f'ffmpeg -y -loglevel warning -f rawvideo -pix_fmt bgra -s {self.out_w}x{self.out_h} -r {framerate} -thread_queue_size 64 -i pipe: -i {src_file} -map 0:v -map 1:a? {self.codec} -pix_fmt yuva420p -c:a libopus -b:a 96k -shortest {dst_file}'), stdin=sp.PIPE)
            

        if self.videoreader_flag == False:
            cap.release()
            cap = sp.Popen(shlex.split(f'ffmpeg -i {src_file} -f rawvideo -pix_fmt bgr24 pipe:'), stdout=sp.PIPE)

        src_max_framenum = int(total_framenum) - 1
        self.max_frame_num = total_framenum

        #源视频帧序号
        frame_index = 0

        for i in range(0,src_max_framenum):
            self.appended_red_mask_list[i] = None

        for i in range(0,len(red_mask_list)):
            mask_filename = os.path.splitext(os.path.basename(red_mask_list[i][0]))[0]
            mask_filename_int = int(mask_filename)
            self.appended_red_mask_list[mask_filename_int] = red_mask_list[i]
        
        if self.appended_red_mask_list[0] is None:
            self.appended_red_mask_list[0] = self.appended_red_mask_list[1]

        max_red_mask_file_index = int(os.path.splitext(os.path.basename(red_mask_list[len(red_mask_list)-1][0]))[0])

        # 创建纯绿色图片
        self.green_img = np.zeros([self.out_h, self.out_w, 3], np.uint8)
        self.green_img[:, :, 1] = np.zeros([self.out_h, self.out_w]) + 255
        
        self.masaikeimg = self.create_checkerboard(self.out_w,self.out_h)

        end = time.time() - start
        print('初始化完成：用时：{}'.format(end))
        start = time.time()

        write_thread = threading.Thread(target=self.write_out_crop)
        write_thread.start()

        img_size = self.w * self.h * 3
        thread_pool = ThreadPoolExecutor(max_workers=12)
        # loop
        while True:
            ret = 1
            # get frame
            if self.videoreader_flag == True:
                if frame_index != 0:
                    ret, img = cap.read()
                else: 
                    # 第一帧之前读过了,跳过读取
                    img = frame
            else:
                raw_image = cap.stdout.read(img_size)  # 从管道里读取一帧，字节数为(宽*高*3)有三个通道
                if not raw_image:
                    ret = 0
                else:
                    img = np.frombuffer(raw_image, dtype=np.uint8).reshape((self.h, self.w ,3))  # 把读取到的二进制数据转换成numpy数组
                    img = img.copy()
                    # img = img.reshape((h, w, 3))  # 把图像转变成应有形状
                    # cap.stdout.flush()  # 充管道
            
            if not ret:
                # 全部读取完成
                self.max_frame_num = frame_index
                self.write_sem.release()
                print('All frames have been read,break now')
                break
            
            if frame_index > max_red_mask_file_index:
                self.max_frame_num = frame_index
                self.write_sem.release()
                print('Src video length longer than mask,break now')
                break
                
            print('进度：%d/%d,队列:%d.\r' % (frame_index,src_max_framenum,len(self.out_crop_list)),end='')
            # thread = threading.Thread(target=apply_mask, args=(
            #                 appended_red_mask_list[frame_index],frame_index,img,w,h,None,out_crop_list,cupyflag,img_green,object_num))
            # sem.acquire()
            # thread.start()
            if self.test_mode == True:
                indices = np.linspace(60, max_red_mask_file_index - 60, 10, dtype=int)
                if frame_index not in indices:
                    frame_index = frame_index + 1
                    continue
            self.sem.acquire()
            thread_pool.submit(self.apply_mask,frame_index,img)
            self.write_sem.release()
            # 队列里待处理的过多，暂停一下
            if len(self.out_crop_list) > 20:
                time.sleep(1)
            frame_index  = frame_index + 1

        if self.focus_phase == 1 and self.focus_flag == True:
            self.max_frame_num = 0

        # 等待帧全部写入完成
        while self.end_sem._value == 0:
            self.write_sem.release()
            time.sleep(0.01)

        write_thread.join()
        thread_pool.shutdown()

        end = time.time() - start
        print('avi生成完成：用时：{}'.format(end))
        start = time.time()

        # close out
        if self.videowriter_flag == True:
            self.out.release()
            #  -shortest 用于音频长于视频时缩短音频长度使两者等长，libx265 crf 26质量基本不会损失画质且视频占用空间能缩小很多
            os.system('ffmpeg -i %s -i %s -map 0:v -map 1:a? %s -c:a copy -shortest %s' % (dst_file+'.avi',src_file, self.codec, dst_file))
        else:
            self.out.stdin.close()
            self.out.wait()
            self.out.terminate()
            self.out = None

        if self.videoreader_flag:
            cap.release()
        else:
            cap.terminate()

        end = time.time() - start
        print('mp4生成完成：用时：{}'.format(end))
        start = time.time()

if __name__=="__main__":
    greenback = GreenBack()
    greenback.run()
    if greenback.focus_flag == True:
        greenback.focus_phase = 2
        greenback.run()