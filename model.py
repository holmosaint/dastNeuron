import numpy as np
import torch
import torch.nn as nn
import sys

from arch.resnet import resnet18, resnet34, resnet50, resnet101, resnet152
from arch.vgg import vgg11_bn, vgg13_bn, vgg16_bn, vgg19_bn
from arch.googlenet import googlenet
from arch.inceptionv3 import inceptionv3
from arch.inceptionv4 import inceptionv4
from arch.densenet import densenet121, densenet161, densenet169, densenet201
from arch.alexnet import alexnet
from arch.reverselayer import ReverseLayerF

class FeatureExtractor(nn.Module):

    def __init__(
        self,
        time_len,
        sequence_num,
        output_dim,
        cnn_type,
        time_type,
        fc_layers,
        cnn_config,
        cuda=True,
        dropout=False,
    ):
        super(FeatureExtractor, self).__init__()
        self.time_len = time_len
        self.sequence_num = sequence_num
        self.output_dim = output_dim
        self.cnn_type = cnn_type
        self.time_type = time_type
        self.fc_layers = fc_layers
        self._cuda = cuda
        self.dropout = dropout
        self.cnn_config = cnn_config

        if self.cnn_type == 'resnet':
            if self.cnn_config == 18:
                self.conv = resnet18(self.sequence_num)
            elif self.cnn_config == 34:
                self.conv = resnet34(self.sequence_num)
            elif self.cnn_config == 50:
                self.conv = resnet50(self.sequence_num)
            elif self.cnn_config == 101:
                self.conv = resnet101(self.sequence_num)
            elif self.cnn_config == 152:
                self.conv = resnet152(self.sequence_num)
            else:
                print(
                    "Current ResNet arch also supports layer: [18, 34, 50, 101, 152], but got {}"
                    .format(self.cnn_config))
                sys.exit(-1)

            resdim = {18: 512, 34: 512, 50: 2048, 101: 2048, 152: 2048}
            conv_last_dim = resdim[cnn_config]
        else:
            raise NotImplementedError()

        self.time_hid_dim = conv_last_dim
        if self.time_type == 'lstm':
            self.time_net = nn.LSTM(self.time_hid_dim,
                                    self.time_hid_dim,
                                    bias=True)
        else:
            raise NotImplementedError()

        self.conv1x1 = nn.Sequential(*[
            nn.Conv1d(self.time_hid_dim, 512, kernel_size=1, bias=True),
            nn.LeakyReLU(inplace=False)
        ])

        with torch.no_grad():
            x = torch.zeros(1, self.sequence_num, self.time_len)
            x = self.get_feature(x, reset=False, batch=1)
            self.fc_dim = x.view(1, -1).shape[1]

    def init_hidden(self, hidden_dim, batch, layer):
        document_rnn_init_h = nn.Parameter(nn.init.xavier_uniform_(
            torch.Tensor(layer, batch, hidden_dim).type(torch.FloatTensor)),
                                           requires_grad=True)
        if self._cuda:
            document_rnn_init_h = document_rnn_init_h.cuda()
        if self.time_type == 'lstm':
            document_rnn_init_c = nn.Parameter(nn.init.xavier_uniform_(
                torch.Tensor(layer, batch, hidden_dim).type(torch.FloatTensor)),
                                               requires_grad=True)
            if self._cuda:
                document_rnn_init_c = document_rnn_init_h.cuda()
            return (document_rnn_init_h, document_rnn_init_c)

    def forward(self,
                dynamic,
                feature=None,
                batch=1,
                reset=True):
        if feature is None:
            feature = list()
            assert dynamic.shape[-1] % self.time_len == 0, "dynamic shape: {}".format(dynamic.shape)
            _idx = np.arange(int(dynamic.shape[-1] // self.time_len))
            np.random.shuffle(_idx)
            off = np.random.randint(1, _idx.shape[0] + 1)
            for i in _idx[:off]:
                fea = self.get_feature(dynamic[..., i*self.time_len:(i+1)*self.time_len],
                                       batch=batch,
                                       reset=reset)
                feature.append(fea.unsqueeze(1))
        feature = torch.flatten(torch.mean(torch.cat(feature, dim=1), dim=1), start_dim=1, end_dim=-1)
        return feature 

    def get_feature(self, dynamic, batch=1, reset=True):
        batch = dynamic.shape[0]
        if len(dynamic.shape) == 2:
            dynamic = dynamic.unsqueeze(1)
        assert dynamic.shape[
            1] == self.sequence_num, "Dynamic shape: {} seq num: {}".format(
                dynamic.shape, self.sequence_num)
        x = self.conv(dynamic)
        x = x.permute((2, 0, 1))
        if reset and self.time_type in ['lstm']:
            x = self.time_net(x, self.init_hidden(self.time_hid_dim, batch, 1))
        else:
            x = self.time_net(x)
        if type(x) is tuple:
            x = x[0]
        x = x.permute((1, 2, 0))
        x = self.conv1x1(x)

        return x


class Regressioner(nn.Module):

    def __init__(self, in_dim, output_dim, fc_layers, cuda=True, dropout=False):
        super(Regressioner, self).__init__()

        self.in_dim = in_dim
        self.output_dim = output_dim
        self.fc_layers = fc_layers
        self._cuda = cuda
        self.dropout = dropout

        fc_list = list()
        fc_dim_list = [in_dim, 512, 256, 256, 128, 128]

        assert self.fc_layers <= 5 and self.fc_layers > 0

        for i in range(self.fc_layers):
            fc = nn.Sequential(*[
                nn.Linear(fc_dim_list[i], fc_dim_list[i + 1]),
                nn.Dropout(0.1) if self.dropout else nn.Identity(),
                nn.LeakyReLU(inplace=False)
            ])
            fc_list.append(fc)
        fc_list.append(nn.Linear(fc_dim_list[self.fc_layers], self.output_dim))

        self.fc = nn.ModuleList(fc_list)

    def forward(self, x, _end=None):
        if _end is None:
            _end = len(self.fc)
        fc_feature = list()
        for i in range(_end):
            x = self.fc[i](x)
            fc_feature.append(x)
        return torch.sigmoid(x), fc_feature


class Classifier(nn.Module):

    def __init__(self, in_dim, fc_layers, cuda=True, dropout=False):
        super(Classifier, self).__init__()

        self.in_dim = in_dim
        self.fc_layers = fc_layers
        self._cuda = cuda
        self.dropout = dropout

        fc_list = list()
        fc_dim_list = [self.in_dim, 512, 256, 256, 128, 128]

        assert self.fc_layers <= 5 and self.fc_layers > 0

        for i in range(self.fc_layers):
            fc_list.append(nn.Linear(fc_dim_list[i], fc_dim_list[i + 1]))
            if self.dropout:
                fc_list.append(nn.Dropout(0.1))
            fc_list.append(nn.LeakyReLU(inplace=False))
        fc_list.append(nn.Linear(fc_dim_list[self.fc_layers], 2))
        fc_list.append(nn.LogSoftmax(dim=1)) 
        self.fc = nn.Sequential(*fc_list)

    def forward(self, x, alpha):
        x = ReverseLayerF.apply(x, alpha)
        x = self.fc(x)
        return x