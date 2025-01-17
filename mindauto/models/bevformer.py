import copy

import numpy as np
import mindspore as ms
from mindspore import ops

from mindauto.core.bbox.transforms import bbox3d2result
from mindauto.models.utils.grid_mask import GridMask
from mindauto.core.bbox.structures import LiDARInstance3DBoxes
from .detectors import MVXTwoStageDetector


def split_array(array):
    split_list = np.split(array, array.shape[0])
    split_list = [np.squeeze(item) for item in split_list]
    return split_list


def restore_img_metas(kwargs, new_args):
    # only support batch_size = 1
    # type_conversion = {'prev_bev_exists': bool, 'can_bus': np.ndarray,
    #                     'lidar2img': list, 'scene_token': str, 'box_type_3d: type}
    type_mapping = {
        "<class 'mindauto.core.bbox.structures.lidar_box3d.LiDARInstance3DBoxes'>": LiDARInstance3DBoxes}
    key_list = kwargs[-1].asnumpy()[0]
    img_meta_dict = {}
    for key, value in zip(key_list, kwargs[:-1]):
        if key.startswith("img_metas"):
            key_list = key.split("/")
            middle_key = int(key_list[1])
            last_key = key_list[-1]
            if middle_key not in img_meta_dict:
                img_meta_dict[middle_key] = {}
            if last_key in ['prev_bev_exists', 'scene_token']:
                img_meta_dict[middle_key][last_key] = value.asnumpy().item()
            elif last_key == 'lidar2img':
                img_meta_dict[middle_key][last_key] = split_array(value.asnumpy()[0])
            elif last_key == 'box_type_3d':
                img_meta_dict[middle_key][last_key] = type_mapping[value.asnumpy().item()]
            elif last_key == 'img_shape':
                img_shape = value.asnumpy()[0]
                img_meta_dict[middle_key][last_key] = [tuple(each) for each in img_shape]
            else:  # can_bus
                img_meta_dict[middle_key][last_key] = value.asnumpy()[0]
        else:
            if key == 'gt_labels_3d':
                new_args[key] = [value[0].asnumpy()]
            if key == 'img':
                new_args[key] = value
    new_args['img_metas'] = [img_meta_dict]


def restore_3d_bbox(kwargs, new_args):
    key_list = kwargs[-1].asnumpy().tolist()[0]
    tensor = kwargs[key_list.index('tensor')][0]
    box_dim = kwargs[key_list.index('box_dim')].asnumpy().item()
    with_yaw = kwargs[key_list.index('with_yaw')].asnumpy().item()
    origin = tuple(kwargs[key_list.index('origin')].asnumpy()[0].tolist())
    new_args['gt_bboxes_3d'] = [LiDARInstance3DBoxes(tensor, box_dim, with_yaw, origin)]


class BEVFormer(MVXTwoStageDetector):
    """BEVFormer.
    Args:
        video_test_mode (bool): Decide whether to use temporal information during inference.
    """

    def __init__(self,
                 use_grid_mask=False,
                 pts_voxel_layer=None,
                 pts_voxel_encoder=None,
                 pts_middle_encoder=None,
                 pts_fusion_layer=None,
                 img_backbone=None,
                 pts_backbone=None,
                 img_neck=None,
                 pts_neck=None,
                 pts_bbox_head=None,
                 img_roi_head=None,
                 img_rpn_head=None,
                 train_cfg=None,
                 test_cfg=None,
                 pretrained=None,
                 video_test_mode=False
                 ):

        super(BEVFormer,
              self).__init__(pts_voxel_layer, pts_voxel_encoder,
                             pts_middle_encoder, pts_fusion_layer,
                             img_backbone, pts_backbone, img_neck, pts_neck,
                             pts_bbox_head, img_roi_head, img_rpn_head,
                             train_cfg, test_cfg, pretrained)
        self.grid_mask = GridMask(
            True, True, rotate=1, offset=False, ratio=0.5, mode=1, prob=0.7)
        self.use_grid_mask = use_grid_mask
        self.fp16_enabled = False

        # temporal
        self.video_test_mode = video_test_mode
        self.prev_bev = ms.Parameter(ops.zeros(()), name='prev_bev')  # ops.zeros() replace None
        self.scene_token = ms.Parameter(ops.zeros(()), name='scene_token')
        self.prev_pos = ms.Parameter(0, name='prev_pos')
        self.prev_angle = ms.Parameter(0, name='prev_angle')

    def init_weights(self):
        self.pts_bbox_head.init_weights()

    def extract_img_feat(self, img, img_metas, len_queue=None):
        """Extract features of images."""
        B = img.shape[0]
        if img is not None:

            # input_shape = img.shape[-2:]
            # # update real input shape of each single img
            # for img_meta in img_metas:
            #     img_meta.update(input_shape=input_shape)

            if img.ndim == 5 and img.shape[0] == 1:
                img = ops.squeeze(img)
            elif img.ndim == 5 and img.shape[0] > 1:
                B, N, C, H, W = img.shape
                img = img.reshape(B * N, C, H, W)
            if self.use_grid_mask:
                img = self.grid_mask(img)
            img_feats = self.img_backbone(img)
            if isinstance(img_feats, dict):
                img_feats = list(img_feats.values())
        else:
            return None
        if self.with_img_neck:
            img_feats = self.img_neck(img_feats)

        img_feats_reshaped = []
        for img_feat in img_feats:
            BN, C, H, W = img_feat.shape
            if len_queue is not None:
                img_feats_reshaped.append(img_feat.view(int(B / len_queue), len_queue, int(BN / B), C, H, W))
            else:
                img_feats_reshaped.append(img_feat.view(B, int(BN / B), C, H, W))
        return img_feats_reshaped

    def extract_feat(self, img, img_metas=None, len_queue=None):
        """Extract features from images and points."""

        img_feats = self.extract_img_feat(img, img_metas, len_queue=len_queue)

        return img_feats

    def forward_pts_train(self,
                          pts_feats,
                          gt_bboxes_3d,
                          gt_labels_3d,
                          img_metas,
                          gt_bboxes_ignore=None,
                          prev_bev=None):
        """Forward function'
        Args:
            pts_feats (list[mindspore.Tensor]): Features of point cloud branch
            gt_bboxes_3d (list[:obj:`BaseInstance3DBoxes`]): Ground truth
                boxes for each sample.
            gt_labels_3d (list[mindspore.Tensor]): Ground truth labels for
                boxes of each sampole
            img_metas (list[dict]): Meta information of samples.
            gt_bboxes_ignore (list[mindspore.Tensor], optional): Ground truth
                boxes to be ignored. Defaults to None.
            prev_bev (mindspore.Tensor, optional): BEV features of previous frame.
        Returns:
            dict: Losses of each branch.
        """
        outs = self.pts_bbox_head(
            pts_feats, img_metas, prev_bev)
        loss_inputs = [gt_bboxes_3d, gt_labels_3d, outs]
        losses = self.pts_bbox_head.loss(*loss_inputs, img_metas=img_metas)
        return losses

    def forward_dummy(self, img):
        dummy_metas = None
        return self.forward_test(img=img, img_metas=[[dummy_metas]])

    def construct(self,
                  *args,
                  **kwargs):
        """Calls either forward_train or forward_test depending on whether
        return_loss=True.
        Note this setting will change the expected inputs. When
        `return_loss=True`, img and img_metas are single-nested (i.e.
        mindspore.Tensor and list[dict]), and when `resturn_loss=False`, img and
        img_metas should be double nested (i.e.  list[mindspore.Tensor],
        list[list[dict]]), with the outer list indicating test time
        augmentations.
        """
        new_args = {}
        restore_img_metas(args, new_args)
        restore_3d_bbox(args, new_args)

        if self.training:
            return self.forward_train(**new_args)
        else:
            return self.forward_test(**new_args)

    def obtain_history_bev(self, imgs_queue, img_metas_list):
        """Obtain history BEV features iteratively. To save GPU memory, gradients are not calculated.
        """
        self.set_train(False)

        prev_bev = None
        bs, len_queue, num_cams, C, H, W = imgs_queue.shape
        imgs_queue = imgs_queue.reshape(bs * len_queue, num_cams, C, H, W)
        img_feats_list = self.extract_feat(img=imgs_queue, len_queue=len_queue)
        for i in range(len_queue):
            img_metas = [each[i] for each in img_metas_list]
            if not img_metas[0]['prev_bev_exists']:
                prev_bev = None
            # img_feats = self.extract_feat(img=img, img_metas=img_metas)
            img_feats = [each_scale[:, i] for each_scale in img_feats_list]
            prev_bev = self.pts_bbox_head(
                img_feats, img_metas, prev_bev, only_bev=True)

        self.set_train(True)
        return prev_bev

    def forward_train(self,
                      points=None,
                      img_metas=None,
                      gt_bboxes_3d=None,
                      gt_labels_3d=None,
                      gt_labels=None,
                      gt_bboxes=None,
                      img=None,
                      proposals=None,
                      gt_bboxes_ignore=None,
                      img_depth=None,
                      img_mask=None,
                      ):
        """Forward training function.
        Args:
            points (list[mindspore.Tensor], optional): Points of each sample.
                Defaults to None.
            img_metas (list[dict], optional): Meta information of each sample.
                Defaults to None.
            gt_bboxes_3d (list[:obj:`BaseInstance3DBoxes`], optional):
                Ground truth 3D boxes. Defaults to None.
            gt_labels_3d (list[mindspore.Tensor], optional): Ground truth labels
                of 3D boxes. Defaults to None.
            gt_labels (list[mindspore.Tensor], optional): Ground truth labels
                of 2D boxes in images. Defaults to None.
            gt_bboxes (list[mindspore.Tensor], optional): Ground truth 2D boxes in
                images. Defaults to None.
            img (mindspore.Tensor optional): Images of each sample with shape
                (N, C, H, W). Defaults to None.
            proposals ([list[mindspore.Tensor], optional): Predicted proposals
                used for training Fast RCNN. Defaults to None.
            gt_bboxes_ignore (list[mindspore.Tensor], optional): Ground truth
                2D boxes in images to be ignored. Defaults to None.
        Returns:
            dict: Losses of different branches.
        """

        len_queue = img.shape[1]
        prev_img = img[:, :-1, ...]
        img = img[:, -1, ...]

        prev_img_metas = copy.deepcopy(img_metas)
        prev_bev = self.obtain_history_bev(prev_img, prev_img_metas)

        img_metas = [each[len_queue - 1] for each in img_metas]
        if not img_metas[0]['prev_bev_exists']:
            prev_bev = None
        img_feats = self.extract_feat(img=img, img_metas=img_metas)
        losses = dict()
        losses_pts = self.forward_pts_train(img_feats, gt_bboxes_3d,
                                            gt_labels_3d, img_metas,
                                            gt_bboxes_ignore, prev_bev)

        losses.update(losses_pts)
        total_loss = ms.Tensor(0.0)
        for _, each_loss in losses.items():
            total_loss += each_loss
        return total_loss

    def forward_test(self, img_metas, img=None, **kwargs):
        for var, name in [(img_metas, 'img_metas')]:
            if not isinstance(var, list):
                raise TypeError('{} must be a list, but got {}'.format(
                    name, type(var)))
        img = [img] if img is None else img

        if img_metas[0][0]['scene_token'] != self.scene_token:
            # the first sample of each scene is truncated
            self.prev_bev.set_data(ops.zeros(()))
        # update idx
        self.scene_token.set_data(img_metas[0][0]['scene_token'])

        # do not use temporal information
        if not self.video_test_mode:
            self.prev_bev.set_data(ops.zeros(()))

        # Get the delta of ego position and angle between two timestamps.
        tmp_pos = copy.deepcopy(img_metas[0][0]['can_bus'][:3])
        tmp_angle = copy.deepcopy(img_metas[0][0]['can_bus'][-1])
        if self.prev_bev.shape != ():  # self.prev_bev is not None
            img_metas[0][0]['can_bus'][:3] -= self.prev_pos
            img_metas[0][0]['can_bus'][-1] -= self.prev_angle
        else:
            img_metas[0][0]['can_bus'][-1] = 0
            img_metas[0][0]['can_bus'][:3] = 0

        new_prev_bev, bbox_results = self.simple_test(
            img_metas[0], img[0], prev_bev=self.prev_bev, **kwargs)
        # During inference, we save the BEV features and ego motion of each timestamp.
        self.prev_pos.set_data(tmp_pos)
        self.prev_angle.set_data(tmp_angle)
        self.prev_bev.set_data(new_prev_bev)
        return bbox_results

    def simple_test_pts(self, x, img_metas, prev_bev=None, rescale=False):
        """Test function"""
        outs = self.pts_bbox_head(x, img_metas, prev_bev=prev_bev)

        bbox_list = self.pts_bbox_head.get_bboxes(
            outs, img_metas, rescale=rescale)
        bbox_results = [
            bbox3d2result(bboxes, scores, labels)
            for bboxes, scores, labels in bbox_list
        ]
        return outs['bev_embed'], bbox_results

    def simple_test(self, img_metas, img=None, prev_bev=None, rescale=False):
        """Test function without augmentaiton."""
        img_feats = self.extract_feat(img=img, img_metas=img_metas)

        bbox_list = [dict() for i in range(len(img_metas))]
        new_prev_bev, bbox_pts = self.simple_test_pts(
            img_feats, img_metas, prev_bev, rescale=rescale)
        for result_dict, pts_bbox in zip(bbox_list, bbox_pts):
            result_dict['pts_bbox'] = pts_bbox
        return new_prev_bev, bbox_list
