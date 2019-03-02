# -*- coding=utf-8 -*-
import cv2
import time
import math
import os
import numpy as np
import tensorflow as tf
from utils.tools import bbox_overlaps, cal_TP, cal_FN,cal_FP

import locality_aware_nms as nms_locality
import lanms

tf.app.flags.DEFINE_string('gpu_list', '0', '')
tf.app.flags.DEFINE_string('checkpoint_path', 'E:\\ai\\models\\east_icdar2015_resnet_v1_50_rbox_v4', '')
tf.app.flags.DEFINE_string('output_dir', 'E:\\ai\\models\\images\\result', '')
tf.app.flags.DEFINE_bool('no_write_images', False, 'do not write images')
# val_img_dir = '/home/give/Game/OCR/data/ICPR/icpr_mtwi_task2/image_test'
# val_pred_dir = '/home/give/Game/OCR/data/ICPR/icpr_mtwi_task2/pred_txt'
# val_pred_img_dir = '/home/give/Game/OCR/data/ICPR/icpr_mtwi_task2/pred_img'
val_img_dir = '/home/give/Game/OCR/data/ICPR/icpr_mtwi_task2/test'
val_pred_dir = '/home/give/Game/OCR/data/ICPR/icpr_mtwi_task2/test/pred_result'
val_pred_img_dir = '/home/give/Game/OCR/data/ICPR/icpr_mtwi_task2/test/pred_result'
tf.app.flags.DEFINE_string('test_data_path', val_img_dir, '')
import model
from icdar import restore_rectangle, shrink_poly
from tools import show_image, calculate_boundingbox_score
import shutil
import numpy as np
from glob import glob
FLAGS = tf.app.flags.FLAGS

def get_images():
    '''
    find image files in test data path
    :return: list of files found
    '''
    files = []
    exts = ['jpg', 'png', 'jpeg', 'JPG']
    for parent, dirnames, filenames in os.walk(FLAGS.test_data_path):
        for filename in filenames:
            for ext in exts:
                if filename.endswith(ext):
                    print 'file_path: ', os.path.join(parent, filename)
                    if os.path.join(parent, filename).find('pred') != -1:
                        continue
                    files.append(os.path.join(parent, filename))
                    break
    print('Find {} images'.format(len(files)))
    return files


def resize_image(im, max_side_len=2400):
    '''
    resize image to a size multiple of 32 which is required by the network
    :param im: the resized image
    :param max_side_len: limit of max image size to avoid out of memory in gpu
    :return: the resized image and the resize ratio
    '''
    h, w, _ = im.shape

    resize_w = w
    resize_h = h

    # limit the max side
    if max(resize_h, resize_w) > max_side_len:
        ratio = float(max_side_len) / resize_h if resize_h > resize_w else float(max_side_len) / resize_w
    else:
        ratio = 1.
    resize_h = int(resize_h * ratio)
    resize_w = int(resize_w * ratio)

    resize_h = resize_h if resize_h % 32 == 0 else (resize_h // 32 - 1) * 32
    resize_w = resize_w if resize_w % 32 == 0 else (resize_w // 32 - 1) * 32
    im = cv2.resize(im, (int(resize_w), int(resize_h)))

    ratio_h = resize_h / float(h)
    ratio_w = resize_w / float(w)

    return im, (ratio_h, ratio_w)


def detect(score_map, geo_map, timer, score_map_thresh=0.8, box_thresh=0.1, nms_thres=0.2):
    '''
    restore text boxes from score map and geo map
    :param score_map:
    :param geo_map:[W,H,5]
    :param timer:
    :param score_map_thresh: threshhold for score map
    :param box_thresh: threshhold for boxes
    :param nms_thres: threshold for nms
    :return:
    '''
    if len(score_map.shape) == 4:
        score_map = score_map[0, :, :, 0]
        geo_map = geo_map[0, :, :, ]
    # filter the score map
    xy_text = np.argwhere(score_map > score_map_thresh)
    # sort the text boxes via the y axis
    xy_text = xy_text[np.argsort(xy_text[:, 0])]
    # restore
    start = time.time()
    # xy_text[:, ::-1]*4 满足条件的pixel的坐标
    # geo_map[xy_text[:, 0], xy_text[:, 1], :] 得到对应点到ｂｏｕｎｄｉｎｇ　ｂｏｘ　的距离
    text_box_restored = restore_rectangle(xy_text[:, ::-1], geo_map[xy_text[:, 0], xy_text[:, 1], :]) # N*4*2
    print('{} text boxes before nms'.format(text_box_restored.shape[0]))
    boxes = np.zeros((text_box_restored.shape[0], 9), dtype=np.float32)
    boxes[:, :8] = text_box_restored.reshape((-1, 8))
    boxes[:, 8] = score_map[xy_text[:, 0], xy_text[:, 1]]
    timer['restore'] = time.time() - start

    # Modify Start
    # 我们以bounding box内的平均值作为nms的标准而不是一个点的值
    new_boxes = np.copy(boxes)
    for i, box in enumerate(new_boxes):
        mask = np.zeros_like(score_map, dtype=np.uint8)
        cv2.fillPoly(mask, box[:8].reshape((-1, 4, 2)).astype(np.int32), 1)
        new_boxes[i, 8] = cv2.mean(score_map, mask)[0]
    # end

    # nms part
    start = time.time()
    # boxes = nms_locality.nms_locality(boxes.astype(np.float64), nms_thres)
    # boxes = lanms.merge_quadrangle_n9(boxes.astype('float32'), nms_thres)
    boxes = lanms.merge_quadrangle_n9(new_boxes.astype('float32'), nms_thres)
    timer['nms'] = time.time() - start

    if boxes.shape[0] == 0:
        return None, timer

    # here we filter some low score boxes by the average score map, this is different from the orginal paper
    for i, box in enumerate(boxes):
        mask = np.zeros_like(score_map, dtype=np.uint8)
        cv2.fillPoly(mask, box[:8].reshape((-1, 4, 2)).astype(np.int32), 1)
        boxes[i, 8] = cv2.mean(score_map, mask)[0]
    boxes = boxes[boxes[:, 8] > box_thresh]

    return boxes, timer


def sort_poly(p):
    min_axis = np.argmin(np.sum(p, axis=1))
    p = p[[min_axis, (min_axis+1)%4, (min_axis+2)%4, (min_axis+3)%4]]
    if abs(p[0, 0] - p[1, 0]) > abs(p[0, 1] - p[1, 1]):
        return p
    else:
        return p[[0, 3, 2, 1]]

def read_from_gt(gt_file):
    with open(gt_file) as file:
        lines = file.readlines()
        gt_bboxs = []
        for line in lines:
            splited_line = line.split(',')
            splited_line = splited_line[:8]
            splited_line = [int(float(ele)) for ele in splited_line]
            gt_bboxs.append(splited_line)
        return gt_bboxs

def main(argv=None):
    import os
    import shutil
    os.environ['CUDA_VISIBLE_DEVICES'] = FLAGS.gpu_list


    try:
        print FLAGS.output_dir
        os.makedirs(FLAGS.output_dir)
    except OSError as e:
        if e.errno != 17:
            raise
    # gt_dir = '/home/give/Game/OCR/data/ICPR/rename/1000/whole'
    # gt_dir = '/home/give/Game/OCR/data/ICPR/txt_test'
    # 每次从新生成都清空文件夹
    if os.path.exists(FLAGS.output_dir):
        shutil.rmtree(FLAGS.output_dir)
        os.makedirs(FLAGS.output_dir)
    else:
        os.makedirs(FLAGS.output_dir)
    P = 0.0
    R = 0.0
    with tf.get_default_graph().as_default():
        input_images = tf.placeholder(tf.float32, shape=[None, None, None, 3], name='input_images')
        global_step = tf.get_variable('global_step', [], initializer=tf.constant_initializer(0), trainable=False)
        # f_score, f_geometry = model.model_InceptionResNet_BLSTM(input_images, is_training=False)
        f_score, f_geometry = model.model_InceptionResNet(input_images, is_training=False)
        # f_score, f_geometry = model.model_InceptionResNet_symmetry(input_images, is_training=False)
        variable_averages = tf.train.ExponentialMovingAverage(0.997, global_step)
        saver = tf.train.Saver(variable_averages.variables_to_restore())

        with tf.Session(config=tf.ConfigProto(allow_soft_placement=True)) as sess:
            ckpt_state = tf.train.get_checkpoint_state(FLAGS.checkpoint_path)
            model_path = os.path.join(FLAGS.checkpoint_path, os.path.basename(ckpt_state.model_checkpoint_path))
            print('Restore from {}'.format(model_path))
            saver.restore(sess, model_path)

            im_fn_list = get_images()
            for im_fn in im_fn_list:
                basename = os.path.basename(im_fn).split('.')[0]
                # gt_path = os.path.join(val_gt_dir, basename + '.txt')
                # gt_bboxs = read_from_gt(gt_path)
                im = cv2.imread(im_fn)[:, :, ::-1]
                start_time = time.time()
                im_resized, (ratio_h, ratio_w) = resize_image(im, max_side_len=2400)

                timer = {'net': 0, 'restore': 0, 'nms': 0}
                start = time.time()
                score, geometry = sess.run([f_score, f_geometry], feed_dict={input_images: [im_resized]})
                timer['net'] = time.time() - start
                # show_image(im_resized)
                # show_image(np.asarray(np.squeeze(score) * 255, np.uint8))
                boundingboxs = calculate_boundingbox_score(np.squeeze(score))
                boundingboxs = np.asarray(boundingboxs, np.float32)
                # print np.shape(boundingboxs)
                # print boundingboxs
                boundingboxs[::2] /= ratio_w
                boundingboxs[1::2] /= ratio_h
                # print 'Pred Bounding Box shape is ', np.shape(boundingboxs)
                boxes, timer = detect(score_map=score, geo_map=geometry, timer=timer)
                print('{} : net {:.0f}ms, restore {:.0f}ms, nms {:.0f}ms'.format(
                    im_fn, timer['net']*1000, timer['restore']*1000, timer['nms']*1000))
                if boxes is not None:
                    boxes = boxes[:, :8].reshape((-1, 4, 2))
                    boxes[:, :, 0] /= ratio_w
                    boxes[:, :, 1] /= ratio_h

                duration = time.time() - start_time
                print('[timing] {}'.format(duration))
                new_boxes = []
                # save to file
                if boxes is not None:
                    basename = os.path.basename(im_fn)
                    basename = basename[:basename.rfind('.')]
                    res_file = os.path.join(
                        val_pred_dir,
                        '{}.txt'.format(
                            basename))

                    with open(res_file, 'w') as f:
                        for box in boxes:
                            # to avoid submitting errors
                            box = sort_poly(box.astype(np.int32))
                            new_box = []
                            new_box.append(box[0, 0])
                            new_box.append(box[0, 1])
                            new_box.append(box[3, 0])
                            new_box.append(box[3, 1])
                            new_box.append(box[2, 0])
                            new_box.append(box[2, 1])
                            new_box.append(box[1, 0])
                            new_box.append(box[1, 1])
                            # ld modify start
                            # r = [None, None, None, None]
                            # poly = new_box
                            # for i in range(4):
                            #     r[i] = min(np.linalg.norm(poly[i] - poly[(i + 1) % 4]),
                            #                np.linalg.norm(poly[i] - poly[(i - 1) % 4]))
                            # box = shrink_poly(np.reshape(new_box, [4, 2]), r=r, R=0.0001)
                            #
                            # box = sort_poly(box.astype(np.int32))
                            # new_box = []
                            # new_box.append(box[0, 0])
                            # new_box.append(box[0, 1])
                            # new_box.append(box[3, 0])
                            # new_box.append(box[3, 1])
                            # new_box.append(box[2, 0])
                            # new_box.append(box[2, 1])
                            # new_box.append(box[1, 0])
                            # new_box.append(box[1, 1])
                            # end
                            new_boxes.append(new_box)
                            # print np.shape(box)
                            if np.linalg.norm(box[0] - box[1]) < 5 or np.linalg.norm(box[3]-box[0]) < 5:
                                continue
                            f.write('{},{},{},{},{},{},{},{}\r\n'.format(
                                box[0, 0], box[0, 1], box[1, 0], box[1, 1], box[2, 0], box[2, 1], box[3, 0], box[3, 1],
                            ))
                            cv2.polylines(im[:, :, ::-1], [box.astype(np.int32).reshape((-1, 1, 2))], True,
                                          color=(255, 255, 0), thickness=1)
                        # for gt_bbox in gt_bboxs:
                        #     gt_bbox = np.array(gt_bbox)
                        #     cv2.polylines(im[:, :, ::-1], [gt_bbox.astype(np.int32).reshape((-1, 1, 2))], True,
                        #                   color=(0, 0, 255), thickness=1)
                    img_path = os.path.join(val_pred_img_dir, basename + '.jpg')
                    cv2.imwrite(img_path, im[:, :, ::-1])
                # if not FLAGS.no_write_images:
                #     img_path = os.path.join(FLAGS.output_dir, os.path.basename(im_fn))
                #     cv2.imwrite(img_path, im[:, :, ::-1])
                #     path_dir, path_base = os.path.split(img_path)
                #     score_map_path = os.path.join(path_dir, path_base.split('.')[0] + '_score_map.png')
                #     cv2.imwrite(score_map_path, np.asarray(np.squeeze(score) * 255.0, np.uint8))
                # if len(new_boxes) == 0:
                #     continue
                # overlaps = bbox_overlaps(np.array(new_boxes), np.array(gt_bboxs), np.shape(im)[:2])
                # if len(boundingboxs) == 0:
                #     continue
                # overlaps = bbox_overlaps(np.array(boundingboxs), np.array(gt_bboxs), np.shape(im)[:2])
                # print np.max(overlaps, 1)
                # threshold = 0.7
                # TP = cal_TP(overlaps, threshold=threshold)
                # FP = cal_FP(overlaps, len(overlaps), threshold=threshold)
                # FN = cal_FN(overlaps, len(gt_bboxs), threshold=threshold)
                # # print 'TP %.4f, FP %.4f, FN %.4f' % (TP, FP, FN)
                # if (TP + FP) == 0:
                #     precision = 0.0
                # else:
                #     precision = (TP * 1.0) / ((TP + FP) * 1.0)
                # recall = (TP * 1.0) / ((TP + FN) * 1.0)
                # [height, width] = np.shape(im)[:2]
                # scale = (1.0 * width) / (1.0 * height)
                # # scale = 1.0
                # P += scale * precision
                # R += scale * recall
        # P = P / (len(im_fn_list) * 1.0)
        # R = R / (len(im_fn_list) * 1.0)
        # print P
        # print R
        # F1 = (2 * P * R) / (P + R)
        # print('Precision is %.4f, Recall is %.4f, F1 score is %.4f' % (P, R, F1))
    # submit_dir = os.path.join(val_pred_dir, 'txt')
    # print 'submit_dir is ', submit_dir
    # if not os.path.exists(submit_dir):
    #     os.makedirs(submit_dir)
    # files = glob( + '/*.txt')
    # for file_path in files:
    #     shutil.copy2(
    #         file_path,
    #         os.path.join(submit_dir, os.path.basename(file_path))
    #     )
if __name__ == '__main__':
    tf.app.run()
