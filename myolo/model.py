import os
import random
import datetime
import re
import math
import logging
from collections import OrderedDict
import multiprocessing
import numpy as np
import tensorflow as tf
import keras
import keras.backend as K
import keras.layers as KL
import keras.engine as KE
import keras.models as KM
from myolo.config import Config as config

# used for buliding mobilenet backbone
from keras_applications import get_keras_submodule
from keras_applications.mobilenet import _depthwise_conv_block

# Requires TensorFlow 1.3+ and Keras 2.0.8+.
from distutils.version import LooseVersion
assert LooseVersion(tf.__version__) >= LooseVersion("1.3")
assert LooseVersion(keras.__version__) >= LooseVersion('2.0.8')


backend = get_keras_submodule('backend')

############################################################
# Build an incomplete mobilenetv1 graph as backbone
############################################################


def relu6(x):
    return backend.relu(x, max_value=6)


def conv_block(inputs, filters, alpha=1.0, kernel=(3,3), strides=(1,1)):
    channel_axis = 1 if backend.image_data_format() == 'channels_first' else -1
    filters = int(filters * alpha)
    x = KL.ZeroPadding2D(padding=(1, 1), name='conv1_pad')(inputs)
    x = KL.Conv2D(filters, kernel,
                      padding='valid',
                      use_bias=False,
                      strides=strides,
                      name='conv1')(x)
    x = KL.BatchNormalization(axis=channel_axis, name='conv1_bn')(x)
    return KL.Activation(relu6, name='conv1_relu')(x)


def mobilenet_graph(input_image, architecture, stage5=False, alpha=1.0, depth_multiplier=1):
    """ Build a incompleted mobilenetv1 graph so as to generate enough spatial feature map
    resolution (28x28), one more depthwise block added for bigger d dimension.
    architecture: can be mobilenet, resnet50 or resnet100
    stage5: boolean. if create stage5 for the network or not
    alpha and depth_multiplier are parameters for mobilenet, the regular setting is 1 for both
    """
    assert architecture == 'mobilenet'

    # 224x224x3
    x = conv_block(input_image, 32, strides=(2, 2))

    # 112x112x32
    x = _depthwise_conv_block(x, 64, alpha, depth_multiplier, block_id=1)
    x = _depthwise_conv_block(x, 64, alpha, depth_multiplier, strides=(2, 2), block_id=2)

    # 56x56x64
    x = _depthwise_conv_block(x, 128, alpha, depth_multiplier, block_id=3)
    x = _depthwise_conv_block(x, 256, alpha, depth_multiplier, strides=(2, 2), block_id=4)

    # 28x28x256
    x = _depthwise_conv_block(x, 256, alpha, depth_multiplier, block_id=5)
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=6)  # added by me

    return x   # output feature map shape [28x28x512]


############################################################
# YOLO branch to generat bbox and objectness prob
############################################################

def yolo_custom_loss(y_true, y_pred, true_boxes):
    mask_shape = tf.shape(y_true)[:4]

    cell_x = tf.to_float(tf.reshape(tf.tile(tf.range(config.GRID_W), [config.GRID_H]), (1, config.GRID_H, config.GRID_W, 1, 1)))
    cell_y = tf.transpose(cell_x, (0, 2, 1, 3, 4))

    cell_grid = tf.tile(tf.concat([cell_x, cell_y], -1), [config.BATCH_SIZE, 1, 1, 5, 1])

    coord_mask = tf.zeros(mask_shape)
    conf_mask = tf.zeros(mask_shape)
    class_mask = tf.zeros(mask_shape)

    seen = tf.Variable(0.)
    total_recall = tf.Variable(0.)

    """
    Adjust prediction
    """
    ### adjust x and y
    pred_box_xy = tf.sigmoid(y_pred[..., :2]) + cell_grid

    ### adjust w and h
    pred_box_wh = tf.exp(y_pred[..., 2:4]) * np.reshape(config.ANCHORS, [1, 1, 1, config.N_BOX, 2])

    ### adjust confidence
    pred_box_conf = tf.sigmoid(y_pred[..., 4])

    ### adjust class probabilities
    pred_box_class = y_pred[..., 5:]

    """
    Adjust ground truth
    """
    ### adjust x and y
    true_box_xy = y_true[..., 0:2]  # relative position to the containing cell

    ### adjust w and h
    true_box_wh = y_true[..., 2:4]  # number of cells accross, horizontally and vertically

    ### adjust confidence
    true_wh_half = true_box_wh / 2.
    true_mins = true_box_xy - true_wh_half
    true_maxes = true_box_xy + true_wh_half

    pred_wh_half = pred_box_wh / 2.
    pred_mins = pred_box_xy - pred_wh_half
    pred_maxes = pred_box_xy + pred_wh_half

    intersect_mins = tf.maximum(pred_mins, true_mins)
    intersect_maxes = tf.minimum(pred_maxes, true_maxes)
    intersect_wh = tf.maximum(intersect_maxes - intersect_mins, 0.)
    intersect_areas = intersect_wh[..., 0] * intersect_wh[..., 1]

    true_areas = true_box_wh[..., 0] * true_box_wh[..., 1]
    pred_areas = pred_box_wh[..., 0] * pred_box_wh[..., 1]

    union_areas = pred_areas + true_areas - intersect_areas
    iou_scores = tf.truediv(intersect_areas, union_areas)

    true_box_conf = iou_scores * y_true[..., 4]

    ### adjust class probabilities
    true_box_class = tf.argmax(y_true[..., 5:], -1)

    """
    Determine the masks
    """
    ### coordinate mask: simply the position of the ground truth boxes (the predictors)
    coord_mask = tf.expand_dims(y_true[..., 4], axis=-1) * config.COORD_SCALE

    ### confidence mask: penelize predictors + penalize boxes with low IOU
    # penalize the confidence of the boxes, which have IOU with some ground truth box < 0.6
    true_xy = true_boxes[..., 0:2]
    true_wh = true_boxes[..., 2:4]

    true_wh_half = true_wh / 2.
    true_mins = true_xy - true_wh_half
    true_maxes = true_xy + true_wh_half

    pred_xy = tf.expand_dims(pred_box_xy, 4)
    pred_wh = tf.expand_dims(pred_box_wh, 4)

    pred_wh_half = pred_wh / 2.
    pred_mins = pred_xy - pred_wh_half
    pred_maxes = pred_xy + pred_wh_half

    intersect_mins = tf.maximum(pred_mins, true_mins)
    intersect_maxes = tf.minimum(pred_maxes, true_maxes)
    intersect_wh = tf.maximum(intersect_maxes - intersect_mins, 0.)
    intersect_areas = intersect_wh[..., 0] * intersect_wh[..., 1]

    true_areas = true_wh[..., 0] * true_wh[..., 1]
    pred_areas = pred_wh[..., 0] * pred_wh[..., 1]

    union_areas = pred_areas + true_areas - intersect_areas
    iou_scores = tf.truediv(intersect_areas, union_areas)

    best_ious = tf.reduce_max(iou_scores, axis=4)
    conf_mask = conf_mask + tf.to_float(best_ious < 0.6) * (1 - y_true[..., 4]) * config.NO_OBJECT_SCALE

    # penalize the confidence of the boxes, which are reponsible for corresponding ground truth box
    conf_mask = conf_mask + y_true[..., 4] * config.OBJECT_SCALE

    ### class mask: simply the position of the ground truth boxes (the predictors)
    class_mask = y_true[..., 4] * tf.gather(config.CLASS_WEIGHTS, true_box_class) * config.CLASS_SCALE

    """
    Warm-up training
    """
    no_boxes_mask = tf.to_float(coord_mask < config.COORD_SCALE / 2.)
    seen = tf.assign_add(seen, 1.)

    true_box_xy, true_box_wh, coord_mask = tf.cond(tf.less(seen, config.WARM_UP_BATCHES),
                                                   lambda: [true_box_xy + (0.5 + cell_grid) * no_boxes_mask,
                                                            true_box_wh + tf.ones_like(true_box_wh) * np.reshape(
                                                                config.ANCHORS, [1, 1, 1, config.N_BOX, 2]) * no_boxes_mask,
                                                            tf.ones_like(coord_mask)],
                                                   lambda: [true_box_xy,
                                                            true_box_wh,
                                                            coord_mask])

    """
    Finalize the loss
    """
    nb_coord_box = tf.reduce_sum(tf.to_float(coord_mask > 0.0))
    nb_conf_box = tf.reduce_sum(tf.to_float(conf_mask > 0.0))
    nb_class_box = tf.reduce_sum(tf.to_float(class_mask > 0.0))

    loss_xy = tf.reduce_sum(tf.square(true_box_xy - pred_box_xy) * coord_mask) / (nb_coord_box + 1e-6) / 2.
    loss_wh = tf.reduce_sum(tf.square(true_box_wh - pred_box_wh) * coord_mask) / (nb_coord_box + 1e-6) / 2.
    loss_conf = tf.reduce_sum(tf.square(true_box_conf - pred_box_conf) * conf_mask) / (nb_conf_box + 1e-6) / 2.
    loss_class = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=true_box_class, logits=pred_box_class)
    loss_class = tf.reduce_sum(loss_class * class_mask) / (nb_class_box + 1e-6)

    loss = loss_xy + loss_wh + loss_conf + loss_class

    nb_true_box = tf.reduce_sum(y_true[..., 4])
    nb_pred_box = tf.reduce_sum(tf.to_float(true_box_conf > 0.5) * tf.to_float(pred_box_conf > 0.3))

    """
    Debugging code
    """
    current_recall = nb_pred_box / (nb_true_box + 1e-6)
    total_recall = tf.assign_add(total_recall, current_recall)

    loss = tf.Print(loss, [tf.zeros((1))], message='Dummy Line \t', summarize=1000)
    loss = tf.Print(loss, [loss_xy], message='Loss XY \t', summarize=1000)
    loss = tf.Print(loss, [loss_wh], message='Loss WH \t', summarize=1000)
    loss = tf.Print(loss, [loss_conf], message='Loss Conf \t', summarize=1000)
    loss = tf.Print(loss, [loss_class], message='Loss Class \t', summarize=1000)
    loss = tf.Print(loss, [loss], message='Total Loss \t', summarize=1000)
    loss = tf.Print(loss, [current_recall], message='Current Recall \t', summarize=1000)
    loss = tf.Print(loss, [total_recall / seen], message='Average Recall \t', summarize=1000)

    return loss


def yolo_branch_graph(x, true_boxes, config, alpha=1.0, depth_multiplier=1):
    """ YOLO branch following the feature map to generate bbox based on prior anchors
    :param x: input feature map
    :param true_boxes: input_true_boxes
    :return: output with shape [None, 7, 7, 5, NUM_CLASSES+5]
    """
    # 28x28x512
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, strides=(2, 2), block_id=7)

    # 14x14x512
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=8)
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=9)
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=10)
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=11)
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=12)

    x = _depthwise_conv_block(x, 1024, alpha, depth_multiplier, strides=(2, 2), block_id=13)

    # 7x7x1024
    x = _depthwise_conv_block(x, 1024, alpha, depth_multiplier, block_id=14)

    # yolo output
    x = KL.Conv2D(config.N_BOX * (4 + 1 + config.NUM_CLASSES), (1, 1), strides=(1, 1), padding='same', name='conv_23')(x)
    output = KL.Reshape((config.GRID_H, config.GRID_W, config.N_BOX, 4 + 1 + config.NUM_CLASSES))(x)

    # small hack to allow true_boxes to be registered when Keras build the model
    output = KL.Lambda(lambda args: args[0])([output, true_boxes])

    return output


def build_yolo_model(ture_boxes, config, depth):
    """ Build a keras model for the YOLO model
    :param depth: depth of input feature map, for now is 512
    :return: a keras model object, the last layer of the model is a standard YOLOv2 output
    with shape [None, GRID_H, GRID_W, N_BOX, 5 + NUM_CLASSES]
    """
    input_feature_map = KL.Input(shape=[None, None, depth], name="input_yolo_feature_map")
    # input_true_boxes = KL.Input(shape=(1, 1, 1, config.TRUE_BOX_BUFFER, 4))
    output = yolo_branch_graph(input_feature_map, ture_boxes, config)

    return KM.Model([input_feature_map, ture_boxes], output, name="yolo_model")


############################################################
#  ROIAlign Layer
############################################################

def log2_graph(x):
    """Implementation of Log2. TF doesn't have a native implementation."""
    return tf.log(x) / tf.log(2.0)


class PyramidROIAlign(KE.Layer):
    """Implements ROI Pooling on multiple levels of the feature pyramid.

    Params:
    - pool_shape: [pool_height, pool_width] of the output pooled regions. Usually [7, 7]

    Inputs:
    - boxes: [batch, num_boxes, (y1, x1, y2, x2)] in normalized
             coordinates. Possibly padded with zeros if not enough
             boxes to fill the array.
    - image_meta: [batch, (meta data)] Image details. See compose_image_meta()
    - feature_maps: List of feature maps from different levels of the pyramid.
                    Each is [batch, height, width, channels]

    Output:
    Pooled regions in the shape: [batch, num_boxes, pool_height, pool_width, channels].
    The width and height are those specific in the pool_shape in the layer
    constructor.
    """

    def __init__(self, pool_shape, **kwargs):
        super(PyramidROIAlign, self).__init__(**kwargs)
        self.pool_shape = tuple(pool_shape)

    def call(self, inputs):
        # Crop boxes [batch, num_boxes, (y1, x1, y2, x2)] in normalized coords
        boxes = inputs[0]

        # Image meta
        # Holds details about the image. See compose_image_meta()
        # image_meta = inputs[1]

        # Feature Maps. List of feature maps from different level of the
        # feature pyramid. Each is [batch, height, width, channels]
        feature_maps = inputs[1:]

        # Assign each ROI to a level in the pyramid based on the ROI area.
        y1, x1, y2, x2 = tf.split(boxes, 4, axis=2)
        h = y2 - y1
        w = x2 - x1
        # Use shape of first image. Images in a batch must have the same size.
        # image_shape = parse_image_meta_graph(image_meta)['image_shape'][0]
        image_shape = [224, 224, 3]
        # Equation 1 in the Feature Pyramid Networks paper. Account for
        # the fact that our coordinates are normalized here.
        # e.g. a 224x224 ROI (in pixels) maps to P4
        image_area = tf.cast(image_shape[0] * image_shape[1], tf.float32)
        roi_level = log2_graph(tf.sqrt(h * w) / (224.0 / tf.sqrt(image_area)))
        roi_level = tf.minimum(5, tf.maximum(
            2, 4 + tf.cast(tf.round(roi_level), tf.int32)))
        roi_level = tf.squeeze(roi_level, 2)

        # Loop through levels and apply ROI pooling to each. P2 to P5.
        pooled = []
        box_to_level = []
        for i, level in enumerate(range(2, 6)):
            ix = tf.where(tf.equal(roi_level, level))
            level_boxes = tf.gather_nd(boxes, ix)

            # Box indices for crop_and_resize.
            box_indices = tf.cast(ix[:, 0], tf.int32)

            # Keep track of which box is mapped to which level
            box_to_level.append(ix)

            # Stop gradient propogation to ROI proposals
            level_boxes = tf.stop_gradient(level_boxes)
            box_indices = tf.stop_gradient(box_indices)

            # Crop and Resize
            # From Mask R-CNN paper: "We sample four regular locations, so
            # that we can evaluate either max or average pooling. In fact,
            # interpolating only a single value at each bin center (without
            # pooling) is nearly as effective."
            #
            # Here we use the simplified approach of a single value per bin,
            # which is how it's done in tf.crop_and_resize()
            # Result: [batch * num_boxes, pool_height, pool_width, channels]
            pooled.append(tf.image.crop_and_resize(
                feature_maps[i], level_boxes, box_indices, self.pool_shape,
                method="bilinear"))

        # Pack pooled features into one tensor
        pooled = tf.concat(pooled, axis=0)

        # Pack box_to_level mapping into one array and add another
        # column representing the order of pooled boxes
        box_to_level = tf.concat(box_to_level, axis=0)
        box_range = tf.expand_dims(tf.range(tf.shape(box_to_level)[0]), 1)
        box_to_level = tf.concat([tf.cast(box_to_level, tf.int32), box_range],
                                 axis=1)

        # Rearrange pooled features to match the order of the original boxes
        # Sort box_to_level by batch then box index
        # TF doesn't have a way to sort by two columns, so merge them and sort.
        sorting_tensor = box_to_level[:, 0] * 100000 + box_to_level[:, 1]
        ix = tf.nn.top_k(sorting_tensor, k=tf.shape(
            box_to_level)[0]).indices[::-1]
        ix = tf.gather(box_to_level[:, 2], ix)
        pooled = tf.gather(pooled, ix)

        # Re-add the batch dimension
        shape = tf.concat([tf.shape(boxes)[:2], tf.shape(pooled)[1:]], axis=0)
        pooled = tf.reshape(pooled, shape)
        return pooled

    def compute_output_shape(self, input_shape):
        return input_shape[0][:2] + self.pool_shape + (input_shape[2][-1], )


############################################################
#  Mask Graph
############################################################


def build_fpn_mask_graph(rois, feature_maps,
                         pool_size, num_classes, train_bn=True):
    """Builds the computation graph of the mask head of Feature Pyramid Network.

    rois: [batch, num_rois, (y1, x1, y2, x2)] Proposal boxes in normalized
          coordinates.
    feature_maps: List of feature maps from different layers of the pyramid,
                  [P2, P3, P4, P5]. Each has a different resolution.
    image_meta: [batch, (meta data)] Image details. See compose_image_meta()
    pool_size: The width of the square feature map generated from ROI Pooling.
    num_classes: number of classes, which determines the depth of the results
    train_bn: Boolean. Train or freeze Batch Norm layers

    Returns: Masks [batch, num_rois, MASK_POOL_SIZE, MASK_POOL_SIZE, NUM_CLASSES]
    """
    # ROI Pooling
    # Shape: [batch, num_rois, MASK_POOL_SIZE, MASK_POOL_SIZE, channels]
    x = PyramidROIAlign([pool_size, pool_size],
                        name="roi_align_mask")([rois] + feature_maps)

    # Conv layers
    x = KL.TimeDistributed(KL.Conv2D(256, (3, 3), padding="same"),
                           name="mrcnn_mask_conv1")(x)
    x = KL.TimeDistributed(KL.BatchNormalization(), name='mrcnn_mask_bn1')(x)
    x = KL.Activation('relu')(x)

    x = KL.TimeDistributed(KL.Conv2D(256, (3, 3), padding="same"),
                           name="mrcnn_mask_conv2")(x)
    x = KL.TimeDistributed(KL.BatchNormalization(),
                           name='mrcnn_mask_bn2')(x, training=train_bn)
    x = KL.Activation('relu')(x)

    x = KL.TimeDistributed(KL.Conv2D(256, (3, 3), padding="same"),
                           name="mrcnn_mask_conv3")(x)
    x = KL.TimeDistributed(KL.BatchNormalization(),
                           name='mrcnn_mask_bn3')(x, training=train_bn)
    x = KL.Activation('relu')(x)

    x = KL.TimeDistributed(KL.Conv2D(256, (3, 3), padding="same"),
                           name="mrcnn_mask_conv4")(x)
    x = KL.TimeDistributed(KL.BatchNormalization(),
                           name='mrcnn_mask_bn4')(x, training=train_bn)
    x = KL.Activation('relu')(x)

    x = KL.TimeDistributed(KL.Conv2DTranspose(256, (2, 2), strides=2, activation="relu"),
                           name="mrcnn_mask_deconv")(x)
    x = KL.TimeDistributed(KL.Conv2D(num_classes, (1, 1), strides=1, activation="sigmoid"),
                           name="mrcnn_mask")(x)
    return x


def mrcnn_mask_loss_graph(target_masks, target_class_ids, pred_masks):
    """Mask binary cross-entropy loss for the masks head.

    target_masks: [batch, num_rois, height, width].
        A float32 tensor of values 0 or 1. Uses zero padding to fill array.
    target_class_ids: [batch, num_rois]. Integer class IDs. Zero padded.
    pred_masks: [batch, proposals, height, width, num_classes] float32 tensor
                with values from 0 to 1.
    """
    # Reshape for simplicity. Merge first two dimensions into one.
    target_class_ids = K.reshape(target_class_ids, (-1,))
    mask_shape = tf.shape(target_masks)
    target_masks = K.reshape(target_masks, (-1, mask_shape[2], mask_shape[3]))
    pred_shape = tf.shape(pred_masks)
    pred_masks = K.reshape(pred_masks,
                           (-1, pred_shape[2], pred_shape[3], pred_shape[4]))
    # Permute predicted masks to [N, num_classes, height, width]
    pred_masks = tf.transpose(pred_masks, [0, 3, 1, 2])

    # Only positive ROIs contribute to the loss. And only
    # the class specific mask of each ROI.
    positive_ix = tf.where(target_class_ids > 0)[:, 0]
    positive_class_ids = tf.cast(
        tf.gather(target_class_ids, positive_ix), tf.int64)
    indices = tf.stack([positive_ix, positive_class_ids], axis=1)

    # Gather the masks (predicted and true) that contribute to loss
    y_true = tf.gather(target_masks, positive_ix)
    y_pred = tf.gather_nd(pred_masks, indices)

    # Compute binary cross entropy. If no positive ROIs, then return 0.
    # shape: [batch, roi, num_classes]
    loss = K.switch(tf.size(y_true) > 0,
                    K.binary_crossentropy(target=y_true, output=y_pred),
                    tf.constant(0.0))
    loss = K.mean(loss)
    return loss

############################################################
# Mask YOLO class
############################################################


class MaskYOLO():
    """ Build the overall structure of MaskYOLO class
    which only generate bbox and mask, no classification involved
    """
    def __init__(self, mode, config, model_dir):
        """
        mode: Either "training" or "inference"
        config: A Sub-class of the Config class
        model_dir: Directory to save training logs and trained weights
        """
        assert mode in ['training', 'inference']
        self.mode = mode
        self.config = config
        self.model_dir = model_dir
        self.keras_model = self.build(mode=mode, config=config)

    def build(self, mode, config):
        assert mode in ['training', 'inference']

        # Image size must be divided by 2 multiple times
        h, w = config.IMAGE_SHAPE[:2]
        if h / 2**6 != int(h / 2**6) or w / 2**6 != int(w / 2**6):
            raise Exception("Image size must be dividable by 2 at least 6 times "
                            "to avoid fractions when downscaling and upscaling."
                            "For example, use 256, 320, 384, 448, 512, ... etc. ")

        # Inputs
        input_image = KL.Input(shape=[None, None, config.IMAGE_SHAPE[2]], name="input_image")

        if mode == "training":
            # input_yolo_anchors and true_boxes
            input_true_boxes = KL.Input(shape=(1, 1, 1, config.TRUE_BOX_BUFFER, 4))
            input_yolo_target = KL.Input(shape=[None, 5], name="input_yolo_target", dtype=tf.float32)

            # GT Masks (zero padded)  TODO
            if config.USE_MINI_MASK:
                input_gt_masks = KL.Input(shape=[config.MINI_MASK_SHAPE[0],
                                                 config.MINI_MASK_SHAPE[1], None],
                                          name="input_gt_masks", dtype=bool)
            else:
                input_gt_masks = KL.Input(shape=[config.IMAGE_SHAPE[0],
                                                 config.IMAGE_SHAPE[1], None],
                                          name="input_gt_masks", dtype=bool)
        elif mode == "inference":
            raise NotImplementedError

        myolo_feature_maps = C4 = mobilenet_graph(input_image, config.BACKBONE, stage5=False)

        # build YOLO branch graph
        yolo_model = build_yolo_model(input_true_boxes, config, config.TOP_FEATURE_MAP_DEPTH)
        yolo_output = yolo_model([myolo_feature_maps, input_true_boxes])

        # TODO: decode yolo network output to anchors with the size of feature map
        feature_map_shape = [(config.IMAGE_SHAPE[0] / config.BACKBONE_STRIDES)[0],
                             (config.IMAGE_SHAPE[1] / config.BACKBONE_STRIDES)[0]]
        rois = batch_decode_yolo(yolo_output, config.ANCHORS, config.NUM_CLASSES, feature_map_shape)

        # TODO: build_mask_graph(rois, myolo_feature_maps, config.MASK_POOL_SIZE)
        myolo_mask = build_fpn_mask_graph(rois, myolo_feature_maps,
                                          config.MASK_POOL_SIZE,
                                          config.NUM_CLASSES)
        output_rois = KL.Lambda(lambda x: x * 1, name="output_rois")(rois)

        # TODO: Losses
        # 1. YOLO custom loss (bbox loss and binary classification loss)
        yolo_sum_loss = KL.Lambda(lambda x: yolo_custom_loss(*x), name="yolo_sum_loss")(
            [input_yolo_target, yolo_output, input_true_boxes]
        )
        # 2. mask_loss
        mask_loss = KL.Lambda(lambda x: mrcnn_mask_loss_graph(*x), name="mrcnn_mask_loss")(
            [target_mask, target_class_ids, myolo_mask])

        # Model
        inputs = [input_image, input_true_boxes]

        outputs = [output_rois, myolo_mask, yolo_sum_loss, mask_loss]

        model = KM.Model(inputs, outputs, name="mask_yolo")


def compute_backbone_shapes(config, image_shape):
    """Computes the width and height of each stage of the backbone network.
    Return: [(height, width)].
    """
    # Currently supports mobilenet only
    assert config.BACKBONE in ["mobilenet"]
    return np.array(
        [int(math.ceil(image_shape[0] / config.BACKBONE_STRIDES)),
          int(math.ceil(image_shape[1] / config.BACKBONE_STRIDES))])


############################################################
# Decode YOLO output to final bbox
# (equivalent to ProposalLayer + DetectionTargetLayer)
############################################################


class BoundBox:
    def __init__(self, xmin, ymin, xmax, ymax, c=None, classes=None):
        self.xmin = xmin
        self.ymin = ymin
        self.xmax = xmax
        self.ymax = ymax

        self.c = c
        self.classes = classes

        self.label = -1
        self.score = -1

    def get_label(self):
        if self.label == -1:
            self.label = np.argmax(self.classes)

        return self.label

    def get_score(self):
        if self.score == -1:
            self.score = self.classes[self.get_label()]

        return self.score


def bbox_iou(box1, box2):
    intersect_w = _interval_overlap([box1.xmin, box1.xmax], [box2.xmin, box2.xmax])
    intersect_h = _interval_overlap([box1.ymin, box1.ymax], [box2.ymin, box2.ymax])

    intersect = intersect_w * intersect_h

    w1, h1 = box1.xmax - box1.xmin, box1.ymax - box1.ymin
    w2, h2 = box2.xmax - box2.xmin, box2.ymax - box2.ymin

    union = w1 * h1 + w2 * h2 - intersect

    return float(intersect) / union


def _interval_overlap(interval_a, interval_b):
    x1, x2 = interval_a
    x3, x4 = interval_b

    if x3 < x1:
        if x4 < x1:
            return 0
        else:
            return min(x2,x4) - x1
    else:
        if x2 < x3:
             return 0
        else:
            return min(x2,x4) - x3


def batch_decode_yolo(yolo_out, anchors, nb_class, feature_map_shape, obj_thre=0.3, nms_thre=0.3):
    """
    :param yolo_out: with shape [batch, 7, 7, 5, 7]
    :param anchors:
    :param nb_class:
    :param obj_thre:
    :param nms_thre:
    :return:
    """
    batch, grid_h, grid_w, nb_box = yolo_out.shape[:4]

    boxes = []
    fm_height = int(feature_map_shape[0])
    fm_width = int(feature_map_shape[1])

    # decode the output by the network
    yolo_out[..., 4] = _sigmoid(yolo_out[..., 4])  # sigmoid for bx, by, bw, bh and c
    yolo_out[..., 5:] = yolo_out[..., 4][..., np.newaxis] * _softmax(yolo_out[..., 5:])  # softmax for class prob
    yolo_out[..., 5:] *= yolo_out[..., 5:] > obj_thre  # select bbox with prob higher than threshold

    for row in range(grid_h):
        for col in range(grid_w):
            for b in range(nb_box):
                # from 4th element onwards are confidence and class classes
                classes = yolo_out[row, col, b, 5:]

                if np.sum(classes) > 0:
                    # first 4 elements are x, y, w, and h
                    x, y, w, h = yolo_out[row, col, b, :4]

                    x = (col + _sigmoid(x)) / grid_w  # center position, unit: image width
                    y = (row + _sigmoid(y)) / grid_h  # center position, unit: image height
                    w = anchors[2 * b + 0] * np.exp(w) / grid_w  # unit: image width
                    h = anchors[2 * b + 1] * np.exp(h) / grid_h  # unit: image height
                    confidence = yolo_out[row, col, b, 4]

                    # generate bbox on the 28x28 feature max
                    box = BoundBox(x - w / 2, y - h / 2, x + w / 2, y + h / 2, confidence, classes)
                    xmin = min(int(box.xmin * fm_width), fm_height)
                    ymin = min(int(box.ymin * fm_height), fm_height)
                    xmax = min(int(box.xmax * fm_width), fm_width)
                    ymax = min(int(box.ymax * fm_height), fm_height)

                    box = BoundBox(xmin, ymin, xmax, ymax, confidence, classes)
                    # box = BoundBox(x - w / 2, y - h / 2, x + w / 2, y + h / 2, confidence, classes)
                    boxes.append(box)  # xmin, ymin, xmax, ymax, confidence, classes

    # suppress non-maximal boxes
    for c in range(nb_class):
        sorted_indices = list(reversed(np.argsort([box.classes[c] for box in boxes])))

        for i in range(len(sorted_indices)):
            index_i = sorted_indices[i]

            if boxes[index_i].classes[c] == 0:
                continue
            else:
                for j in range(i + 1, len(sorted_indices)):
                    index_j = sorted_indices[j]

                    if bbox_iou(boxes[index_i], boxes[index_j]) >= nms_thre:
                        boxes[index_j].classes[c] = 0

    # remove the boxes which are less likely than a obj_threshold
    boxes = [box for box in boxes if box.get_score() > obj_thre]

    # convert the unit from network image input to feature map

    return boxes


def _sigmoid(x):
    return 1. / (1. + np.exp(-x))


def _softmax(x, axis=-1, t=-100.):
    x = x - np.max(x)

    if np.min(x) < t:
        x = x / np.min(x) * t

    e_x = np.exp(x)

    return e_x / e_x.sum(axis, keepdims=True)



