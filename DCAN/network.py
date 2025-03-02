"""
ResNet code gently borrowed from
https://github.com/pytorch/vision/blob/master/torchvision/models/resnet.py
"""
from __future__ import print_function, division, absolute_import
from collections import OrderedDict
import math
import torch
import torch.nn as nn

__all__ = ['DCCANet', 'dcca_resnet50', 'dcca_resnet101']

pretrained_settings = {
    'attention_resnet50': {
        'imagenet': {
            'RESTORE_FROM': 'pretrained_models/attention_resnet50_pretrained_imagenet.pth',
            'INPUT_SPACE': 'RGB',
            'INPUT_SIZE': [3, 224, 224],
            'INPUT_RANGE': [0, 1],
            'MEAN': [0.485, 0.456, 0.406],
            'STD': [0.229, 0.224, 0.225],
            'NUM_CLASSES': 1000
        }
    },
    'attention_resnet101': {
        'imagenet': {
            'RESTORE_FROM': 'pretrained_models/attention_resnet101_pretrained_imagenet.pth',
            'INPUT_SPACE': 'RGB',
            'INPUT_SIZE': [3, 224, 224],
            'INPUT_RANGE': [0, 1],
            'MEAN': [0.485, 0.456, 0.406],
            'STD': [0.229, 0.224, 0.225],
            'NUM_CLASSES': 1000
        }
    }
}


class DCCAModule(nn.Module):
    """
    Domain Conditioned Channel Attention module.
    Capture domain-specific information in low-level stage and model the independencies
    between the convolutional channels for source and target respectively.
    """

    def __init__(self, channels, reduction):
        super(DCCAModule, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc0 = nn.Conv2d(channels, channels // reduction, kernel_size=1,
                             padding=0)
        self.fc1 = nn.Conv2d(channels, channels // reduction, kernel_size=1,
                             padding=0)
        self.relu = nn.ReLU(inplace=True)
        self.fc2 = nn.Conv2d(channels // reduction, channels, kernel_size=1,
                             padding=0)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        module_input = x
        x = self.avg_pool(x)
        if self.training:
            src = self.fc0(x[:int(x.size(0) / 2), ])
            # src = self.fc1(x[:int(x.size(0) / 2), ])
            trg = self.fc1(x[int(x.size(0) / 2):, ])
            x = torch.cat((src, trg), 0)
        else:
            x = self.fc1(x)

        x = self.relu(x)
        x = self.fc2(x)
        x = self.sigmoid(x)
        return module_input * x


class Bottleneck(nn.Module):
    """
    Base class for bottlenecks that implements `forward()` method.
    """

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out = self.se_module(out) + residual
        out = self.relu(out)

        return out


class DCCAResNetBottleneck(Bottleneck):
    """
    ResNet bottleneck with a Domain Conditioned Channel Attention module.
    It follows Caffe implementation and uses `stride=stride` in `conv1` and not in `conv2`
    (the latter is used in the torchvision implementation of ResNet).
    """

    expansion = 4

    def __init__(self, inplanes, planes, groups, reduction, stride=1,
                 downsample=None):
        super(DCCAResNetBottleneck, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False,
                               stride=stride)
        self.bn1 = nn.BatchNorm2d(planes, track_running_stats=True)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, padding=1,
                               groups=groups, bias=False)
        self.bn2 = nn.BatchNorm2d(planes, track_running_stats=True)
        self.conv3 = nn.Conv2d(planes, planes * 4, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * 4, track_running_stats=True)
        self.relu = nn.ReLU(inplace=True)
        self.se_module = DCCAModule(planes * 4, reduction=reduction)
        self.downsample = downsample
        self.stride = stride


class DCCANet(nn.Module):

    def __init__(self, block, layers, groups, reduction, dropout_p=0.2,
                 inplanes=128, input_3x3=True, downsample_kernel_size=3,
                 downsample_padding=1):
        super(DCCANet, self).__init__()
        self.inplanes = inplanes
        if input_3x3:
            layer0_modules = [
                ('conv1', nn.Conv2d(3, 64, 3, stride=2, padding=1,
                                    bias=False)),
                ('bn1', nn.BatchNorm2d(64, track_running_stats=True)),
                ('relu1', nn.ReLU(inplace=True)),
                ('conv2', nn.Conv2d(64, 64, 3, stride=1, padding=1,
                                    bias=False)),
                ('bn2', nn.BatchNorm2d(64, track_running_stats=True)),
                ('relu2', nn.ReLU(inplace=True)),
                ('conv3', nn.Conv2d(64, inplanes, 3, stride=1, padding=1,
                                    bias=False)),
                ('bn3', nn.BatchNorm2d(inplanes, track_running_stats=True)),
                ('relu3', nn.ReLU(inplace=True)),
            ]
        else:
            layer0_modules = [
                ('conv1', nn.Conv2d(3, inplanes, kernel_size=7, stride=2,
                                    padding=3, bias=False)),
                ('bn1', nn.BatchNorm2d(inplanes, track_running_stats=True)),
                ('relu1', nn.ReLU(inplace=True)),
            ]
        # To preserve compatibility with Caffe weights `ceil_mode=True` is used instead of `padding=1`.
        layer0_modules.append(('pool', nn.MaxPool2d(3, stride=2,
                                                    ceil_mode=True)))
        self.layer0 = nn.Sequential(OrderedDict(layer0_modules))
        self.layer1 = self._make_layer(
            block,
            planes=64,
            blocks=layers[0],
            groups=groups,
            reduction=reduction,
            downsample_kernel_size=1,
            downsample_padding=0
        )
        self.layer2 = self._make_layer(
            block,
            planes=128,
            blocks=layers[1],
            stride=2,
            groups=groups,
            reduction=reduction,
            downsample_kernel_size=downsample_kernel_size,
            downsample_padding=downsample_padding
        )
        self.layer3 = self._make_layer(
            block,
            planes=256,
            blocks=layers[2],
            stride=2,
            groups=groups,
            reduction=reduction,
            downsample_kernel_size=downsample_kernel_size,
            downsample_padding=downsample_padding
        )
        self.layer4 = self._make_layer(
            block,
            planes=512,
            blocks=layers[3],
            stride=2,
            groups=groups,
            reduction=reduction,
            downsample_kernel_size=downsample_kernel_size,
            downsample_padding=downsample_padding
        )
        self.avg_pool = nn.AvgPool2d(7, stride=1)
        self.dropout = nn.Dropout(dropout_p) if dropout_p is not None else None

        self.feature = nn.Sequential(self.layer0, self.layer1, self.layer2, self.layer3, self.layer4)

    def _make_layer(self, block, planes, blocks, groups, reduction, stride=1,
                    downsample_kernel_size=1, downsample_padding=0):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=downsample_kernel_size, stride=stride,
                          padding=downsample_padding, bias=False),
                nn.BatchNorm2d(planes * block.expansion, track_running_stats=True),
            )

        layers = []
        layers.append(block(self.inplanes, planes, groups, reduction, stride,
                            downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes, groups, reduction))

        return nn.Sequential(*layers)

    def features(self, x):
        x = self.layer0(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        return x

    def logits(self, x):
        x = self.avg_pool(x)
        if self.dropout is not None:
            x = self.dropout(x)
        x = x.view(x.size(0), -1)

        return x

    def forward(self, x):
        # x = self.features(x)
        x = self.feature(x)
        x = self.logits(x)
        return x


def dcca_resnet50(pretrained='imagenet'):
    model = DCCANet(DCCAResNetBottleneck, [3, 4, 6, 3], groups=1, reduction=16,
                    dropout_p=None, inplanes=64, input_3x3=False,
                    downsample_kernel_size=1, downsample_padding=0)

    pretrained_dict = attention_resnet50(num_classes=1000, pretrained=pretrained).state_dict()

    model_dict = model.state_dict()
    pretrained_dict = {k: v for k, v in pretrained_dict.items()
                       if k in model_dict}
    layer = [3, 4, 6, 3]
    model_dict.update(pretrained_dict)
    for i in range(1, 5):
        for j in range(layer[i - 1]):
            a1 = 'layer{}.{}.se_module.fc0.weight'.format(i, j)
            a2 = 'layer{}.{}.se_module.fc0.bias'.format(i, j)
            b1 = 'layer{}.{}.se_module.fc1.weight'.format(i, j)
            b2 = 'layer{}.{}.se_module.fc1.bias'.format(i, j)
            model_dict[a1] = pretrained_dict[b1]
            model_dict[a2] = pretrained_dict[b2]

    # load the new state dict
    model.load_state_dict(model_dict)

    return model


def dcca_resnet101(pretrained='imagenet'):
    model = DCCANet(DCCAResNetBottleneck, [3, 4, 23, 3], groups=1, reduction=16,
                    dropout_p=None, inplanes=64, input_3x3=False,
                    downsample_kernel_size=1, downsample_padding=0)

    pretrained_dict = attention_resnet101(num_classes=1000, pretrained=pretrained).state_dict()

    model_dict = model.state_dict()
    pretrained_dict = {k: v for k, v in pretrained_dict.items()
                       if k in model_dict}
    layer = [3, 4, 23, 3]
    model_dict.update(pretrained_dict)
    for i in range(1, 5):
        for j in range(layer[i - 1]):
            a1 = 'layer{}.{}.se_module.fc0.weight'.format(i, j)
            a2 = 'layer{}.{}.se_module.fc0.bias'.format(i, j)
            b1 = 'layer{}.{}.se_module.fc1.weight'.format(i, j)
            b2 = 'layer{}.{}.se_module.fc1.bias'.format(i, j)
            model_dict[a1] = pretrained_dict[b1]
            model_dict[a2] = pretrained_dict[b2]
    # load the new state dict
    model.load_state_dict(model_dict)
    return model


class AttentionModule(nn.Module):

    def __init__(self, channels, reduction):
        super(AttentionModule, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc1 = nn.Conv2d(channels, channels // reduction, kernel_size=1,
                             padding=0)
        self.relu = nn.ReLU(inplace=True)
        self.fc2 = nn.Conv2d(channels // reduction, channels, kernel_size=1,
                             padding=0)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        module_input = x
        x = self.avg_pool(x)
        x = self.fc1(x)
        x = self.relu(x)
        x = self.fc2(x)
        x = self.sigmoid(x)
        return module_input * x


class AttentionResNetBottleneck(Bottleneck):
    """
    ResNet bottleneck with an Attention module. It follows Caffe
    implementation and uses `stride=stride` in `conv1` and not in `conv2`
    (the latter is used in the torchvision implementation of ResNet).
    """
    
    expansion = 4

    def __init__(self, inplanes, planes, groups, reduction, stride=1,
                 downsample=None):
        super(AttentionResNetBottleneck, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False,
                               stride=stride)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, padding=1,
                               groups=groups, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, planes * 4, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * 4)
        self.relu = nn.ReLU(inplace=True)
        self.se_module = AttentionModule(planes * 4, reduction=reduction)
        self.downsample = downsample
        self.stride = stride


class AttentionResNet(nn.Module):

    def __init__(self, block, layers, groups, reduction, dropout_p=0.2,
                 inplanes=128, input_3x3=True, downsample_kernel_size=3,
                 downsample_padding=1, num_classes=1000):
        super(AttentionResNet, self).__init__()
        self.inplanes = inplanes
        if input_3x3:
            layer0_modules = [
                ('conv1', nn.Conv2d(3, 64, 3, stride=2, padding=1,
                                    bias=False)),
                ('bn1', nn.BatchNorm2d(64)),
                ('relu1', nn.ReLU(inplace=True)),
                ('conv2', nn.Conv2d(64, 64, 3, stride=1, padding=1,
                                    bias=False)),
                ('bn2', nn.BatchNorm2d(64)),
                ('relu2', nn.ReLU(inplace=True)),
                ('conv3', nn.Conv2d(64, inplanes, 3, stride=1, padding=1,
                                    bias=False)),
                ('bn3', nn.BatchNorm2d(inplanes)),
                ('relu3', nn.ReLU(inplace=True)),
            ]
        else:
            layer0_modules = [
                ('conv1', nn.Conv2d(3, inplanes, kernel_size=7, stride=2,
                                    padding=3, bias=False)),
                ('bn1', nn.BatchNorm2d(inplanes)),
                ('relu1', nn.ReLU(inplace=True)),
            ]
        # To preserve compatibility with Caffe weights `ceil_mode=True`
        # is used instead of `padding=1`.
        layer0_modules.append(('pool', nn.MaxPool2d(3, stride=2,
                                                    ceil_mode=True)))
        self.layer0 = nn.Sequential(OrderedDict(layer0_modules))
        self.layer1 = self._make_layer(
            block,
            planes=64,
            blocks=layers[0],
            groups=groups,
            reduction=reduction,
            downsample_kernel_size=1,
            downsample_padding=0
        )
        self.layer2 = self._make_layer(
            block,
            planes=128,
            blocks=layers[1],
            stride=2,
            groups=groups,
            reduction=reduction,
            downsample_kernel_size=downsample_kernel_size,
            downsample_padding=downsample_padding
        )
        self.layer3 = self._make_layer(
            block,
            planes=256,
            blocks=layers[2],
            stride=2,
            groups=groups,
            reduction=reduction,
            downsample_kernel_size=downsample_kernel_size,
            downsample_padding=downsample_padding
        )
        self.layer4 = self._make_layer(
            block,
            planes=512,
            blocks=layers[3],
            stride=2,
            groups=groups,
            reduction=reduction,
            downsample_kernel_size=downsample_kernel_size,
            downsample_padding=downsample_padding
        )
        self.avg_pool = nn.AvgPool2d(7, stride=1)
        self.dropout = nn.Dropout(dropout_p) if dropout_p is not None else None
        self.last_linear = nn.Linear(512 * block.expansion, num_classes)

    def _make_layer(self, block, planes, blocks, groups, reduction, stride=1,
                    downsample_kernel_size=1, downsample_padding=0):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=downsample_kernel_size, stride=stride,
                          padding=downsample_padding, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, groups, reduction, stride,
                            downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes, groups, reduction))

        return nn.Sequential(*layers)

    def features(self, x):
        x = self.layer0(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        return x

    def logits(self, x):
        x = self.avg_pool(x)
        if self.dropout is not None:
            x = self.dropout(x)
        x = x.view(x.size(0), -1)
        x = self.last_linear(x)
        return x

    def forward(self, x):
        x = self.features(x)
        x = self.logits(x)
        return x


def initialize_pretrained_model(model, num_classes, settings):
    assert num_classes == settings['NUM_CLASSES'], \
        'num_classes should be {}, but is {}'.format(
            settings['NUM_CLASSES'], num_classes)

    model.load_state_dict(torch.load(settings['RESTORE_FROM']))
    model.input_space = settings['INPUT_SPACE']
    model.input_size = settings['INPUT_SIZE']
    model.input_range = settings['INPUT_RANGE']
    model.mean = settings['MEAN']
    model.std = settings['STD']


def attention_resnet50(num_classes=1000, pretrained='imagenet'):
    model = AttentionResNet(AttentionResNetBottleneck, [3, 4, 6, 3], groups=1, reduction=16,
                            dropout_p=None, inplanes=64, input_3x3=False,
                            downsample_kernel_size=1, downsample_padding=0,
                            num_classes=num_classes)
    if pretrained is not None:
        settings = pretrained_settings['attention_resnet50'][pretrained]
        initialize_pretrained_model(model, num_classes, settings)
    return model


def attention_resnet101(num_classes=1000, pretrained='imagenet'):
    model = AttentionResNet(AttentionResNetBottleneck, [3, 4, 23, 3], groups=1, reduction=16,
                            dropout_p=None, inplanes=64, input_3x3=False,
                            downsample_kernel_size=1, downsample_padding=0,
                            num_classes=num_classes)
    if pretrained is not None:
        settings = pretrained_settings['attention_resnet101'][pretrained]
        initialize_pretrained_model(model, num_classes, settings)
    return model
