"""
Original code: https://github.com/pytorch/vision/blob/master/torchvision/models/resnet.py
"""


import torch
import torchvision
import torch.nn as nn
from torch import Tensor
from typing import Dict, Union
from torch.utils.model_zoo import load_url
from .methods.addgraph_cam import ADDGraphCAM
from .methods.graph_cam import GraphCAM
from .methods.grad_cam import GradCAM
from .methods import AcolBase, ADL, spg
from .methods.util import normalize_tensor
from .util import remove_layer, replace_layer, initialize_weights
from .gnn import models

__all__ = ['resnet50']

model_urls = {
    'resnet50': 'https://download.pytorch.org/models/resnet50-19c8e357.pth',
}

_ADL_POSITION = [[], [], [], [0], [0, 2]]


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None,
                 base_width=64):
        super(Bottleneck, self).__init__()
        width = int(planes * (base_width / 64.))
        self.conv1 = nn.Conv2d(inplanes, width, 1, bias=False)
        self.bn1 = nn.BatchNorm2d(width)
        self.conv2 = nn.Conv2d(width, width, 3,
                               stride=stride, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(width)
        self.conv3 = nn.Conv2d(width, planes * self.expansion, 1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * self.expansion)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        identity = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)

        return out


class ResNetCam(nn.Module):
    def __init__(self, block, layers, classif_type="multi_class", num_classes=1000,
                 large_feature_map=False, **kwargs):
        super(ResNetCam, self).__init__()

        stride_l3 = 1 if large_feature_map else 2
        self.classif_type = classif_type
        self.inplanes = 64

        self.conv1 = nn.Conv2d(3, self.inplanes, kernel_size=7, stride=2,
                               padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(self.inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

        self.layer1 = self._make_layer(block, 64, layers[0], stride=1)
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=stride_l3)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=1)
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(512 * block.expansion, num_classes)

        initialize_weights(self.modules(), init_mode='xavier')

    def forward(self, x, labels=None, return_cam=False):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        pre_logit = self.avgpool(x)
        pre_logit = pre_logit.reshape(pre_logit.size(0), -1)
        logits = self.fc(pre_logit)

        if return_cam:
            if self.classif_type == "multi_class":
                feature_map = x.detach().clone()
                cam_weights = self.fc.weight[labels]
                cams = (cam_weights.view(*feature_map.shape[:2], 1, 1) *
                        feature_map).mean(1, keepdim=False)
            else:
                b, c = labels.shape
                feature_map = x.detach().clone()
                # vectorize weights selection for multi-label
                label_masks = labels.repeat_interleave(c, dim=0)
                I = torch.eye(c).repeat((b, 1))
                label_masks = label_masks * I.cuda()
                cam_weights = label_masks.view(
                    b, -1, c).to(self.fc.weight.device) @ self.fc.weight
                cams = (cam_weights.view(c, b, -1, 1, 1) *
                        feature_map).mean(2).view(b, c, *feature_map.shape[2:])
            return cams
        return {'logits': logits}

    def _make_layer(self, block, planes, blocks, stride):
        layers = self._layer(block, planes, blocks, stride)
        return nn.Sequential(*layers)

    def _layer(self, block, planes, blocks, stride):
        downsample = get_downsampling_layer(self.inplanes, block, planes,
                                            stride)

        layers = [block(self.inplanes, planes, stride, downsample)]
        self.inplanes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return layers


class Resnet(nn.Module):
    def __init__(self, model, num_classes):
        super(Resnet, self).__init__()
        self.features = nn.Sequential(
            model.conv1,
            model.bn1,
            model.relu,
            model.maxpool,
            model.layer1,
            model.layer2,
            model.layer3,
            model.layer4,
        )
        self.num_classes = num_classes
        self.pooling = nn.MaxPool2d(14, 14)
        self.linear = nn.Linear(in_features=2048, out_features=num_classes)
        # image normalization
        self.image_normalization_mean = [0.485, 0.456, 0.406]
        self.image_normalization_std = [0.229, 0.224, 0.225]

    def forward(self, feature):
        feature = self.features(feature)
        feature = self.pooling(feature)
        feature = feature.view(feature.size(0), -1)
        x = self.linear(feature)
        return x


class ResNetGradCam(nn.Module):
    def __init__(self, classif_type="multi_class", num_classes=80, **kwargs):
        super(ResNetGradCam, self).__init__()
        self.num_classes = num_classes
        self.classif_type = classif_type
        self.model = models.resnet(
            num_classes=num_classes, model_name="resnet50")
        self.grad_cam = GradCAM(
            self.model, target_module=self.model.model[7][2])

    def forward(self, imgs, labels=None, return_cam=False):
        return self.grad_cam(imgs, labels, return_cam)

    def load_state_dict(self, state_dict: Union[Dict[str, Tensor], Dict[str, Tensor]], strict: bool):
        return self.model.load_state_dict(state_dict, strict=strict)

    def state_dict(self, destination=None, prefix='', keep_vars=False):
        return self.model.state_dict(destination, prefix, keep_vars)


class ResNext50GradCam(nn.Module):
    def __init__(self, classif_type="multi_label", num_classes=80, **kwargs):
        super(ResNext50GradCam, self).__init__()
        self.num_classes = num_classes
        self.classif_type = classif_type

        self.model = models.build_net(
            arch='resnext50_32x4d_swsl', num_classes=num_classes)
        # self.model = Resnet(torchvision.models.resnext50_32x4d(), num_classes)
        self.grad_cam = GradCAM(
            self.model, target_module=self.model.model[7][2])

    def forward(self, imgs, labels=None, return_cam=False):
        return self.grad_cam(imgs, labels, return_cam)

    def load_state_dict(self, state_dict: Union[Dict[str, Tensor], Dict[str, Tensor]], strict: bool):
        return self.model.load_state_dict(state_dict, strict=strict)

    def state_dict(self, destination=None, prefix='', keep_vars=False):
        return self.model.state_dict(destination, prefix, keep_vars)


class ResNext50GCN(nn.Module):
    def __init__(self, classif_type="multi_label", num_classes=80, adj_dd_threshold=0.4,
                 adj_files=None, emb_features=300, gtn=False, embedding=None, **kwargs):
        super(ResNext50GCN, self).__init__()
        self.classif_type = classif_type
        self.model = models.build_net(arch='resnext50_32x4d_swsl', num_classes=num_classes,
                                      t=adj_dd_threshold, adj_files=adj_files, emb_features=emb_features, graph=True, gtn=gtn)

        self.graph_cam = GraphCAM(
            self.model, embedding, target_module=self.model.model[7][2])

    def forward(self, imgs, targets=None, return_cam=False):
        return self.graph_cam(imgs, targets, return_cam)

    def load_state_dict(self, state_dict: Union[Dict[str, Tensor], Dict[str, Tensor]], strict: bool):
        return self.model.load_state_dict(state_dict, strict=strict)

    def state_dict(self, destination=None, prefix='', keep_vars=False):
        return self.model.state_dict(destination, prefix, keep_vars)


class ResNet101GCN(nn.Module):
    def __init__(self, classif_type="multi_label", num_classes=80, adj_dd_threshold=0.4,
                 adj_files=None, emb_features=300, gtn=False, embedding=None, **kwargs):
        super(ResNet101GCN, self).__init__()
        self.classif_type = classif_type
        self.model = models.build_net(arch="resnet101", num_classes=num_classes,
                                      t=adj_dd_threshold, adj_files=adj_files, emb_features=emb_features, graph=True, gtn=gtn)

        self.graph_cam = GraphCAM(
            self.model, embedding, target_module=self.model.model[7][2])

    def forward(self, imgs, targets=None, return_cam=False):
        return self.graph_cam(imgs, targets, return_cam)

    def load_state_dict(self, state_dict: Union[Dict[str, Tensor], Dict[str, Tensor]], strict: bool):
        return self.model.load_state_dict(state_dict, strict=strict)

    def state_dict(self, destination=None, prefix='', keep_vars=False):
        return self.model.state_dict(destination, prefix, keep_vars)


class ResNet101GradCam(nn.Module):
    def __init__(self, classif_type="multi_label", num_classes=80, **kwargs):
        super(ResNet101GradCam, self).__init__()
        self.num_classes = num_classes
        self.classif_type = classif_type
        self.model = Resnet(torchvision.models.resnet101(), num_classes)
        self.grad_cam = GradCAM(
            self.model, target_module=self.model.features[7][2])

    def forward(self, imgs, labels=None, return_cam=False):
        return self.grad_cam(imgs, labels, return_cam)

    def load_state_dict(self, state_dict: Union[Dict[str, Tensor], Dict[str, Tensor]], strict: bool):
        return self.model.load_state_dict(state_dict, strict=strict)

    def state_dict(self, destination=None, prefix='', keep_vars=False):
        return self.model.state_dict(destination, prefix, keep_vars)


class ResNet101ADDGraphCam(nn.Module):
    def __init__(self, classif_type="multi_label", num_classes=80, **kwargs):
        super(ResNet101ADDGraphCam, self).__init__()
        self.num_classes = num_classes
        self.classif_type = classif_type
        self.model = models.build_net(
            arch="resnet101", num_classes=num_classes, dynamic=True)
        self.addgraph_cam = ADDGraphCAM(self.model)

    def forward(self, imgs, labels=None, return_cam=False):
        return self.addgraph_cam(imgs, labels, return_cam)

    def load_state_dict(self, state_dict: Union[Dict[str, Tensor], Dict[str, Tensor]], strict: bool):
        return self.model.load_state_dict(state_dict, strict=strict)

    def state_dict(self, destination=None, prefix='', keep_vars=False):
        return self.model.state_dict(destination, prefix, keep_vars)


class ResNetAcol(AcolBase):
    def __init__(self, block, layers, num_classes=1000,
                 large_feature_map=False, **kwargs):
        super(ResNetAcol, self).__init__()

        stride_l3 = 1 if large_feature_map else 2
        self.inplanes = 64

        self.label = None
        self.drop_threshold = kwargs['acol_drop_threshold']

        self.conv1 = nn.Conv2d(3, self.inplanes, kernel_size=7, stride=2,
                               padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(self.inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

        self.layer1 = self._make_layer(block, 64, layers[0], stride=1)
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=stride_l3)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=1)

        self.classifier_A = nn.Sequential(
            nn.Conv2d(512 * block.expansion, 1024, 3, 1, padding=1),
            nn.ReLU(inplace=False),
            nn.Conv2d(1024, 1024, 3, 1, padding=1),
            nn.ReLU(inplace=False),
            nn.Conv2d(1024, num_classes, 1, 1, padding=0),
        )
        self.classifier_B = nn.Sequential(
            nn.Conv2d(512 * block.expansion, 1024, 3, 1, padding=1),
            nn.ReLU(inplace=False),
            nn.Conv2d(1024, 1024, 3, 1, padding=1),
            nn.ReLU(inplace=False),
            nn.Conv2d(1024, num_classes, 1, 1, padding=0),
        )

        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        initialize_weights(self.modules(), init_mode='he')

    def forward(self, x, labels=None, return_cam=False):
        batch_size = x.shape[0]

        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        feature = self.layer4(x)

        logits_dict = self._acol_logits(feature=feature, labels=labels,
                                        drop_threshold=self.drop_threshold)

        if return_cam:
            normalized_a = normalize_tensor(
                logits_dict['feat_map_a'].detach().clone())
            normalized_b = normalize_tensor(
                logits_dict['feat_map_b'].detach().clone())
            feature_map = torch.max(normalized_a, normalized_b)
            cams = feature_map[range(batch_size), labels]
            return cams

        return logits_dict

    def _make_layer(self, block, planes, blocks, stride):
        layers = self._layer(block, planes, blocks, stride)
        return nn.Sequential(*layers)

    def _layer(self, block, planes, blocks, stride):
        downsample = get_downsampling_layer(self.inplanes, block, planes,
                                            stride)

        layers = [block(self.inplanes, planes, stride, downsample)]
        self.inplanes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return layers


class ResNetSpg(nn.Module):
    def __init__(self, block, layers, num_classes=1000,
                 large_feature_map=False, **kwargs):
        super(ResNetSpg, self).__init__()

        stride_l3 = 1 if large_feature_map else 2
        self.inplanes = 64

        self.conv1 = nn.Conv2d(3, self.inplanes, kernel_size=7, stride=2,
                               padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(self.inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

        self.layer1 = self._make_layer(block=block, planes=64,
                                       blocks=layers[0],
                                       stride=1, split=False)
        self.layer2 = self._make_layer(block=block, planes=128,
                                       blocks=layers[1],
                                       stride=2, split=False)
        self.SPG_A1, self.SPG_A2 = self._make_layer(block=block, planes=256,
                                                    blocks=layers[2],
                                                    stride=stride_l3,
                                                    split=True)
        self.layer4 = self._make_layer(block=block, planes=512,
                                       blocks=layers[3],
                                       stride=1, split=False)
        self.SPG_A4 = nn.Conv2d(512 * block.expansion, num_classes,
                                kernel_size=1)
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))

        self.SPG_B_1a = nn.Sequential(
            nn.Conv2d(1024, 1024, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
        )
        self.SPG_B_2a = nn.Sequential(
            nn.Conv2d(1024, 1024, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
        )
        self.SPG_B_shared = nn.Sequential(
            nn.Conv2d(1024, 1024, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(1024, 1, kernel_size=1),
        )

        self.SPG_C = nn.Sequential(
            nn.Conv2d(2048, 512, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 1, kernel_size=1),
        )

        initialize_weights(self.modules(), init_mode='xavier')

    def _make_layer(self, block, planes, blocks, stride, split=None):
        downsample = get_downsampling_layer(self.inplanes, block, planes,
                                            stride)

        first_layers = [block(self.inplanes, planes, stride, downsample)]
        self.inplanes = planes * block.expansion
        other_layers = []
        for _ in range(1, blocks):
            other_layers.append(block(self.inplanes, planes))

        if split:
            return nn.Sequential(*first_layers), nn.Sequential(*other_layers)
        else:
            return nn.Sequential(*(first_layers + other_layers))

    def _layer(self, block, planes, blocks, stride):
        downsample = get_downsampling_layer(self.inplanes, block, planes,
                                            stride)

        layers = [block(self.inplanes, planes, stride, downsample)]
        self.inplanes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return layers

    def forward(self, x, labels=None, return_cam=False):
        batch_size = x.shape[0]

        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.SPG_A1(x)

        logits_b1 = self.SPG_B_1a(x)
        logits_b1 = self.SPG_B_shared(logits_b1)

        x = self.SPG_A2(x)
        logits_b2 = self.SPG_B_2a(x)
        logits_b2 = self.SPG_B_shared(logits_b2)

        x = self.layer4(x)
        feat_map = self.SPG_A4(x)

        logits_c = self.SPG_C(x)

        logits = self.avgpool(feat_map)
        logits = logits.view(logits.shape[0:2])

        labels = logits.argmax(dim=1).long() if labels is None else labels
        attention, fused_attention = spg.compute_attention(
            feat_map=feat_map, labels=labels,
            logits_b1=logits_b1, logits_b2=logits_b2)

        if return_cam:
            feature_map = feat_map.clone().detach()
            cams = feature_map[range(batch_size), labels]
            return cams
        return {'attention': attention, 'fused_attention': fused_attention,
                'logits': logits, 'logits_b1': logits_b1,
                'logits_b2': logits_b2, 'logits_c': logits_c}


class ResNetAdl(nn.Module):
    def __init__(self, block, layers, num_classes=1000,
                 large_feature_map=False, **kwargs):
        super(ResNetAdl, self).__init__()

        self.stride_l3 = 1 if large_feature_map else 2
        self.inplanes = 64

        self.adl_drop_rate = kwargs['adl_drop_rate']
        self.adl_threshold = kwargs['adl_drop_threshold']

        self.conv1 = nn.Conv2d(3, self.inplanes, kernel_size=7, stride=2,
                               padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(self.inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

        self.layer1 = self._make_layer(block, 64, layers[0],
                                       stride=1,
                                       split=_ADL_POSITION[1])
        self.layer2 = self._make_layer(block, 128, layers[1],
                                       stride=2,
                                       split=_ADL_POSITION[2])
        self.layer3 = self._make_layer(block, 256, layers[2],
                                       stride=self.stride_l3,
                                       split=_ADL_POSITION[3])
        self.layer4 = self._make_layer(block, 512, layers[3],
                                       stride=1,
                                       split=_ADL_POSITION[4])

        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(512 * block.expansion, num_classes)

        initialize_weights(self.modules(), init_mode='xavier')

    def forward(self, x, labels=None, return_cam=False):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        pre_logit = self.avgpool(x)
        pre_logit = pre_logit.reshape(pre_logit.size(0), -1)
        logits = self.fc(pre_logit)

        if return_cam:
            feature_map = x.detach().clone()
            cam_weights = self.fc.weight[labels]
            cams = (cam_weights.view(*feature_map.shape[:2], 1, 1) *
                    feature_map).mean(1, keepdim=False)
            return cams

        return {'logits': logits}

    def _make_layer(self, block, planes, blocks, stride, split=None):
        layers = self._layer(block, planes, blocks, stride)
        for pos in reversed(split):
            layers.insert(pos + 1, ADL(self.adl_drop_rate, self.adl_threshold))
        return nn.Sequential(*layers)

    def _layer(self, block, planes, blocks, stride):
        downsample = get_downsampling_layer(self.inplanes, block, planes,
                                            stride)

        layers = [block(self.inplanes, planes, stride, downsample)]
        self.inplanes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(block(self.inplanes, planes))
        return layers


def get_downsampling_layer(inplanes, block, planes, stride):
    outplanes = planes * block.expansion
    if stride == 1 and inplanes == outplanes:
        return
    else:
        return nn.Sequential(
            nn.Conv2d(inplanes, outplanes, 1, stride, bias=False),
            nn.BatchNorm2d(outplanes),
        )


def align_layer(state_dict):
    keys = [key for key in sorted(state_dict.keys())]
    for key in reversed(keys):
        move = 0
        if 'layer' not in key:
            continue
        key_sp = key.split('.')
        layer_idx = int(key_sp[0][-1])
        block_idx = key_sp[1]
        if not _ADL_POSITION[layer_idx]:
            continue

        for pos in reversed(_ADL_POSITION[layer_idx]):
            if pos < int(block_idx):
                move += 1

        key_sp[1] = str(int(block_idx) + move)
        new_key = '.'.join(key_sp)
        state_dict[new_key] = state_dict.pop(key)
    return state_dict


def batch_replace_layer(state_dict):
    state_dict = replace_layer(state_dict, 'layer3.0.', 'SPG_A1.0.')
    state_dict = replace_layer(state_dict, 'layer3.1.', 'SPG_A2.0.')
    state_dict = replace_layer(state_dict, 'layer3.2.', 'SPG_A2.1.')
    state_dict = replace_layer(state_dict, 'layer3.3.', 'SPG_A2.2.')
    state_dict = replace_layer(state_dict, 'layer3.4.', 'SPG_A2.3.')
    state_dict = replace_layer(state_dict, 'layer3.5.', 'SPG_A2.4.')
    return state_dict


def load_pretrained(model, wsol_method, architecture, path=None, **kwargs):
    strict_rule = True

    if path:
        print(f"Loaded model from {path}")
        state_dict = torch.load(path)['state_dict']
    else:
        state_dict = load_url(model_urls[architecture], progress=True)

    if wsol_method == 'adl':
        state_dict = align_layer(state_dict)
    elif wsol_method == 'spg':
        state_dict = batch_replace_layer(state_dict)

    if path is None and (kwargs['dataset_name'] != 'ILSVRC' or wsol_method in ('acol', 'spg')):
        state_dict = remove_layer(state_dict, 'fc')
        strict_rule = False

    model.load_state_dict(state_dict, strict=strict_rule)
    return model


def resnet50(wsol_method, pretrained=False, pretrained_path=None,
             **kwargs):
    model = {'cam': ResNetCam,
             'grad_cam': ResNetGradCam,
             'acol': ResNetAcol,
             'spg': ResNetSpg,
             'adl': ResNetAdl}[wsol_method](block=Bottleneck, layers=[3, 4, 6, 3], **kwargs)
    if pretrained:
        model = load_pretrained(model, wsol_method, "resnet50",
                                path=pretrained_path, **kwargs)
    return model


def resnet101(wsol_method, pretrained=False, pretrained_path=None,
              **kwargs):
    model = {'grad_cam': ResNet101GradCam,
             'graph_cam': ResNet101GCN,
             'addgraph_cam': ResNet101ADDGraphCam}[wsol_method](**kwargs)

    if pretrained:
        model = load_pretrained(model, wsol_method, "resnet101",
                                path=pretrained_path, **kwargs)
    return model


def resnext50(wsol_method, pretrained=False, pretrained_path=None,
              **kwargs):
    model = {'grad_cam': ResNext50GradCam,
             'graph_cam': ResNext50GCN
             }[wsol_method](**kwargs)

    if pretrained:
        model = load_pretrained(
            model, wsol_method, "resnext50", path=pretrained_path, **kwargs)
    return model
