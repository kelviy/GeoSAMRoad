#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Author: Gaodian Zhou(zhougaodian@cug.edu.cn)
# Pytorch implementation of SGCN
# 
# Slight Modified for:
# 1. Multiple Bands input from first convolution
# 2. Vectorise normalisation
# 3. Device Agnostic Edits

import lightning.pytorch as pl
import segmentation_models_pytorch as smp
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchmetrics import MetricCollection
from torchmetrics.classification import (
    BinaryF1Score,
    BinaryJaccardIndex,
    BinaryPrecision,
    BinaryRecall,
)

BatchNorm2d = nn.BatchNorm2d
BatchNorm1d = nn.BatchNorm1d

class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, dilation=1, downsample=None, fist_dilation=1, multi_grid=1):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False)
        self.bn1 = BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride,
                               padding=dilation*multi_grid, dilation=dilation*multi_grid, bias=False)
        self.bn2 = BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, planes * 4, kernel_size=1, bias=False)
        self.bn3 = BatchNorm2d(planes * 4)
        self.relu = nn.ReLU(inplace=False)
        self.relu_inplace = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.dilation = dilation
        self.stride = stride

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

        out = out + residual
        out = self.relu_inplace(out)

        return out


class Sobel(nn.Module):
    def __init__(self,in_channel,out_channel):
        super(Sobel,self).__init__()
        kernel_x = [[-1.0,0.0,1.0],[-2.0,0.0,2.0],[-1.0,0.0,1.0]]
        kernel_y = [[-1.0,-2.0,-1.0],[0.0,0.0,0.0],[1.0,2.0,1.0]]
        kernel_x = torch.tensor(kernel_x).expand(out_channel, in_channel, 3, 3).contiguous()
        kernel_y = torch.tensor(kernel_y).expand(out_channel, in_channel, 3, 3).contiguous()
        self.register_buffer("weight_x", kernel_x)
        self.register_buffer("weight_y", kernel_y)
        self.softmax = nn.Softmax(dim=0)
    
    def forward(self,x):
        b,c,h,w = x.size()
        sobel_x = F.conv2d(x,self.weight_x,stride=1, padding=1)
        sobel_x = torch.abs(sobel_x)
        sobel_y = F.conv2d(x,self.weight_y,stride=1, padding=1)
        sobel_y = torch.abs(sobel_y)
        if c == 1:
            sobel_x = sobel_x.view(b, h, -1)
            sobel_y = sobel_y.view(b, h, -1).permute(0,2,1)
        else:
            sobel_x = sobel_x.view(b, c, -1)
            sobel_y = sobel_y.view(b, c, -1).permute(0,2,1)
        sobel_A = torch.bmm(sobel_x,sobel_y)
        sobel_A = self.softmax(sobel_A)
        return sobel_A
        
class GCNSpatial(nn.Module):
    def __init__(self , channels):
        super(GCNSpatial,self).__init__()
        self.sobel = Sobel(channels , channels)
        self.fc1 = nn.Conv1d(channels, channels, kernel_size=1, bias=False)
        self.fc2 = nn.Conv1d(channels, channels, kernel_size=1, bias=False)
        self.fc3 = nn.Conv1d(channels, channels, kernel_size=1, bias=False)
    
    def normalize(self, A):
        b, c, im = A.size()
        A1 = A + torch.eye(c, im, device=A.device, dtype=A.dtype)
        D = torch.diag_embed(torch.pow(A1.sum(dim=2), -0.5))
        return (D.bmm(A1).bmm(D)).detach()

    def forward(self,x):
        b,c,h,w = x.size()
        A = self.sobel(x)
        A = self.normalize(A)
        x = x.view(b, c, -1)
        x = F.relu(self.fc1(A.bmm(x)))
        x = F.relu(self.fc2(A.bmm(x)))
        x = self.fc3(A.bmm(x))
        out = x.view(b, c, h ,w)
        return out

class GCNChannel(nn.Module):
    def __init__(self , channels):
        super(GCNChannel,self).__init__()
        self.input = nn.Sequential(
            nn.Conv2d(channels, channels, kernel_size=3, stride=2, padding=1),
            BatchNorm2d(channels),
            nn.ReLU(inplace=True)
            )
        self.sobel = Sobel(1,1)
        self.fc1 = nn.Conv1d(channels, channels, kernel_size=1, bias=False)
        self.fc2 = nn.Conv1d(channels, channels, kernel_size=1, bias=False)
        self.fc3 = nn.Conv1d(channels, channels, kernel_size=1, bias=False)
    
    # def sobel_channel(self,x):
    #     b,c,h,w = x.size()
    #     sobel = Sobel(h*w,h*w)
    #     return sobel

    def pre(self,x):
        b,c,h,w = x.size()
        x = x.view(b, c, -1).permute(0, 2, 1)
        x = x.view(b, 1, h*w, c)
        return x

    def normalize(self, A):
        b, c, im = A.size()
        A1 = A + torch.eye(c, im, device=A.device, dtype=A.dtype)
        D = torch.diag_embed(torch.pow(A1.sum(dim=2), -0.5))
        return (D.bmm(A1).bmm(D)).detach()

    def forward(self,x):
        b,c,h,w = x.size()
        x = self.input(x)
        b,c,h1,w1 = x.size()
        x = self.pre(x)
        # A = self.sobel_channel(x)
        A = self.sobel(x)
        A = self.normalize(A)
        x = x.view(b, -1, c)
        x = F.relu(self.fc1(A.bmm(x).permute(0,2,1))).permute(0,2,1)
        x = F.relu(self.fc2(A.bmm(x).permute(0,2,1))).permute(0,2,1)
        x = self.fc3(A.bmm(x).permute(0,2,1))
        out = x.view(b, c, h1 ,w1)
        out = F.interpolate(out, size=(h,w), mode='bilinear', align_corners=True)
        return out

class decoder(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(decoder, self).__init__()
        # ConvTranspose
        self.up = nn.ConvTranspose2d(in_channels, out_channels, kernel_size=2, stride=2)
        self.up_conv = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True)
        )

    def forward(self, x_copy, x, interpolate=True):
        out = self.up(x)
        if interpolate:
            # Iterative filling, in order to obtain better results
            out = F.interpolate(out, size=(x_copy.size(2), x_copy.size(3)),
                                mode="bilinear", align_corners=True
                                )
        else:
            # Different filling volume
            diffY = x_copy.size()[2] - x.size()[2]
            diffX = x_copy.size()[3] - x.size()[3]
            out = F.pad(out, (diffX // 2, diffX - diffX // 2, diffY, diffY - diffY // 2))
        # Splicing
        out = torch.cat([x_copy, out], dim=1)
        out_conv = self.up_conv(out)
        return out_conv

class TwofoldGCN(nn.Module):
    def __init__(self, in_channels, out_channels, num_classes):
        super(TwofoldGCN, self).__init__()
        # Depthwise convolution # for spatial feature extraction
        self.depth_conv=nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding=1, groups=out_channels),
            BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1, groups=out_channels),
            BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1, groups=out_channels),
            BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1, groups=out_channels),
            BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1, groups=out_channels),
            BatchNorm2d(out_channels)
            )
        # GCN Spatial #
        self.spatial_in = nn.Sequential(
            nn.Conv2d(out_channels, out_channels // 2, kernel_size=3, stride=1, padding=1),
            BatchNorm2d(out_channels // 2),
            nn.ReLU(inplace=True)
            )
        self.gcn_s = GCNSpatial(out_channels // 2)
        self.conv_s = nn.Sequential(
            nn.Conv2d(out_channels // 2, out_channels // 2, kernel_size=3, stride=1, padding=1),
            BatchNorm2d(out_channels // 2)
            )

        # Pointwise convolution # for channel feature extraction
        self.channel_conv=nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=1),
            BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=1, stride=1),
            BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=1, stride=1),
            BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=1, stride=1),
            BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=1, stride=1),
            BatchNorm2d(out_channels)
            )
        # GCN Channel #
        self.channel_in = nn.Sequential(
            nn.Conv2d(out_channels, out_channels // 2, kernel_size=1, stride=1),
            BatchNorm2d(out_channels // 2),
            nn.ReLU(inplace=True)
            )
        self.gcn_c = GCNChannel(out_channels // 2)
        self.conv_c = nn.Sequential(
            nn.Conv2d(out_channels // 2, out_channels // 2, kernel_size=3, stride=1, padding=1),
            BatchNorm2d(out_channels // 2)
            )
        # output
        self.combine = nn.Sequential(
            nn.Conv2d(out_channels, out_channels, kernel_size=1, bias=False),
            BatchNorm2d(out_channels)
            )
        self.output = nn.Sequential(nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1, bias=False),
                                   BatchNorm2d(out_channels),
                                   nn.ReLU(out_channels),
                                   nn.Conv2d(out_channels, num_classes, kernel_size=1, stride=1, bias=True)
                                   )

    def forward(self, x):
        # GCN_Spatial
        x_spatial_in = self.depth_conv(x)
        x_spatial_in = self.spatial_in(x_spatial_in)
        x_spatial = self.gcn_s(x_spatial_in)
        x_spatial = x_spatial_in + x_spatial
        # GCN_Channel
        x_channel_in = self.channel_conv(x)
        x_channel_in = self.channel_in(x_channel_in)
        x_channel = self.gcn_c(x_channel_in)
        x_channel = x_channel_in + x_channel
        # out
        out = torch.cat((x_spatial, x_channel), 1) + x
        out = self.combine(out)
        out = self.output(out)
        return out


class ResNet(nn.Module):
    def __init__(self, block, layers, num_classes, in_channels=3):
        self.inplanes = 128
        super(ResNet, self).__init__()
        #  ResNet # for feature extraction
        self.conv0 = nn.Sequential(
            nn.Conv2d(in_channels, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.Conv2d(64,64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.Conv2d(64,64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True)
        )

        self.conv1 = nn.Sequential(
            nn.Conv2d(64, 64, kernel_size=3, stride=2, padding=1, bias=False),
            BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.Conv2d(64, 64, kernel_size=3, stride=1,padding=1, bias=False),
            BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.Conv2d(64, 128, kernel_size=3, stride=1,padding=1, bias=False)
            )
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.bn1 = BatchNorm2d(self.inplanes)
        self.relu = nn.ReLU(inplace=False)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1, ceil_mode=True)
        self.layer1 = self._make_layer(block, 64, layers[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=1, dilation=2)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=1, dilation=4, multi_grid=(1, 2, 4))
        self.conv2 = nn.Sequential(nn.Conv2d(2048,512, kernel_size=3, stride=1, padding=1, bias=False),
                                   BatchNorm2d(512),
                                   nn.ReLU(512))
        
        #  TwofoldGCN # for channel and spatial feature 
        self.gcn_out =TwofoldGCN(512, 512, 512)

        self.up1 = decoder(512, 256)
        self.up2 = decoder(256, 128)
        self.up3 = decoder(128, 64)
        #Full connection
        self.final_conv = nn.Conv2d(64, num_classes, kernel_size=1)
        self._initalize_weights()
    
    def _initalize_weights(self):
        for module in self.modules():
            if isinstance(module, nn.Conv2d) or isinstance(module, nn.Linear):
                nn.init.kaiming_normal_(module.weight)
                if module.bias is not None:
                    module.bias.data.zero_()
            elif isinstance(module, nn.BatchNorm2d):
                module.weight.data.fill_(1)
                module.bias.data.zero_()

    def _make_layer(self, block, planes, blocks, stride=1, dilation=1, multi_grid=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                BatchNorm2d(planes * block.expansion))

        layers = []
        generate_multi_grid = lambda index, grids: grids[index % len(grids)] if isinstance(grids, tuple) else 1
        layers.append(block(self.inplanes, planes, stride, dilation=dilation, downsample=downsample,
                            multi_grid=generate_multi_grid(0, multi_grid)))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(
                block(self.inplanes, planes, dilation=dilation, multi_grid=generate_multi_grid(i, multi_grid)))

        return nn.Sequential(*layers)

    def forward(self, x):
        x0 = self.conv0(x)
        x1 = self.conv1(x0)
        x1 = self.bn1(x1)
        x1 = self.relu(x1)
        x2 = self.maxpool(x1)
        x2 = self.layer1(x2)
        x3 = self.layer2(x2)
        x3 = self.layer3(x3)

        x3 = self.layer4(x3)
        x3 = self.conv2(x3)
        x3 = self.gcn_out(x3)

        x3 = self.up1(x2,x3)
        x3 = self.up2(x1,x3)
        x3 = self.up3(x0,x3)
        final = self.final_conv(x3)

        return final



def SGCN_res50(num_classes=2, in_channels=12):
    model = ResNet(Bottleneck, [3, 4, 6, 3], num_classes, in_channels=in_channels)
    return model