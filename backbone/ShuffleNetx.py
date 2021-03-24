import torch
import torch.nn as nn
import torch.utils.model_zoo as model_zoo


def channel_shuffle(x, groups):
    # type: (torch.Tensor, int) -> torch.Tensor
    batchsize, num_channels, height, width = x.data.size()
    channels_per_group = num_channels // groups

    # reshape
    x = x.view(batchsize, groups,
               channels_per_group, height, width)

    x = torch.transpose(x, 1, 2).contiguous()

    # flatten
    x = x.view(batchsize, -1, height, width)

    return x


class ShuffleV2Block(nn.Module):
    def __init__(self, inp, oup, stride):
        super(ShuffleV2Block, self).__init__()

        if not (1 <= stride <= 3):
            raise ValueError('illegal stride value')
        self.stride = stride

        branch_features = oup // 2
        assert (self.stride != 1) or (inp == branch_features << 1)

        if self.stride > 1:
            self.branch1 = nn.Sequential(
                self.depthwise_conv(inp, inp, kernel_size=3, stride=self.stride, padding=1),
                nn.BatchNorm2d(inp),
                nn.Conv2d(inp, branch_features, kernel_size=1, stride=1, padding=0, bias=False),
                nn.BatchNorm2d(branch_features),
                nn.ReLU(inplace=True),
            )
        else:
            self.branch1 = nn.Sequential()

        self.branch2 = nn.Sequential(
            nn.Conv2d(inp if (self.stride > 1) else branch_features,
                      branch_features, kernel_size=1, stride=1, padding=0, bias=False),
            nn.BatchNorm2d(branch_features),
            nn.ReLU(inplace=True),
            self.depthwise_conv(branch_features, branch_features, kernel_size=3, stride=self.stride, padding=1),
            nn.BatchNorm2d(branch_features),
            nn.Conv2d(branch_features, branch_features, kernel_size=1, stride=1, padding=0, bias=False),
            nn.BatchNorm2d(branch_features),
            nn.ReLU(inplace=True),
        )

    @staticmethod
    def depthwise_conv(i, o, kernel_size, stride=1, padding=0, bias=False):
        return nn.Conv2d(i, o, kernel_size, stride, padding, bias=bias, groups=i)

    def forward(self, x):
        if self.stride == 1:
            x1, x2 = x.chunk(2, dim=1)
            out = torch.cat((x1, self.branch2(x2)), dim=1)
        else:
            out = torch.cat((self.branch1(x), self.branch2(x)), dim=1)

        out = channel_shuffle(out, 2)

        return out


class SNet(nn.Module):
    def __init__(self,
                 model_size='0.5x',
                 out_stages=(2, 3, 4),
                 with_last_conv=False,
                 kernal_size=3):
        super(SNet, self).__init__()

        self.stage_repeats = [4, 8, 4]
        self.model_size = model_size
        self.out_stages = out_stages
        self.with_last_conv = with_last_conv
        self.kernal_size = kernal_size
        
        if model_size == 'snet49':
            print('model : SNet-49')
            self._stage_out_channels = [24, 60, 120, 240, 1024]
        elif model_size == 'snet146':
            print('model : snet146')
            self._stage_out_channels = [24, 132, 264, 528, 1024]
        elif model_size == 'snet535':
            print('model : snet535')
            self._stage_out_channels = [24, 248, 496, 992, 1024]
        else:
            raise NotImplementedError

        # building first layer
        input_channels = 3
        output_channels = self._stage_out_channels[0]
        self.conv1 = nn.Sequential(
            nn.Conv2d(input_channels, output_channels, 3, 2, 1, bias=False),
            nn.BatchNorm2d(output_channels),
            nn.ReLU(inplace=True),
        )
        input_channels = output_channels

        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

        stage_names = ['stage{}'.format(i) for i in [2, 3, 4]]
        for name, repeats, output_channels in zip(
                stage_names, self.stage_repeats, self._stage_out_channels[1:]):
            seq = [ShuffleV2Block(input_channels, output_channels, 2)]
            for i in range(repeats - 1):
                seq.append(ShuffleV2Block(output_channels, output_channels, 1))
            setattr(self, name, nn.Sequential(*seq))
            input_channels = output_channels
        output_channels = self._stage_out_channels[-1]

        self.conv5 = nn.Sequential(
            nn.Conv2d(input_channels, output_channels, 1, bias=False),
            nn.BatchNorm2d(output_channels),
            nn.ReLU(inplace=True),
        )
        self.gap = nn.AdaptiveAvgPool2d((1, 1))
        self.pred = nn.Conv2d(output_channels, 1000, 1)
        
        self._initialize_weights()


    def _initialize_weights(self, pretrain=True):
        print('init weights...')
        for name, m in self.named_modules():
            if isinstance(m, nn.Conv2d):
                if 'first' in name:
                    nn.init.normal_(m.weight, 0, 0.01)
                else:
                    nn.init.normal_(m.weight, 0, 1.0 / m.weight.shape[1])
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0.0001)
                nn.init.constant_(m.running_mean, 0)
            elif isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.weight, 1)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0.0001)
                nn.init.constant_(m.running_mean, 0)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)


    def forward(self, x):
        x = self.conv1(x)
        x = self.maxpool(x)
        # stage2 -> stage4
        for i in range(2, 5):
            stage = getattr(self, 'stage{}'.format(i))
            x = stage(x)
        # conv5
        x = self.conv5(x)
        # global avgpool
        x = self.gap(x)
        # class
        x = self.pred(x)

        return x.view(x.size(0), -1)


def snet49(pretrained=False, **kwargs):
    """Constructs a SNet49 model.

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = SNet(model_size='snet49')

    return model


def snet146(pretrained=False, **kwargs):
    """Constructs a SNet146 model.

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = SNet(model_size='snet146')

    return model


def snet535(pretrained=False, **kwargs):
    """Constructs a SNet535 model.

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = SNet(model_size='snet535')

    return model


if __name__ == "__main__":
    model = SNet(model_size='2.0x')
    print(model)
    x = torch.rand(5, 3, 224, 224)
    y = model(x)
