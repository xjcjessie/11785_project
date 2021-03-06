import torch
from torch import nn
import torch.nn.init as init
from torch.utils.data import TensorDataset, DataLoader, Dataset
from wide_resnet import Wide_ResNet
from advertorch.utils import batch_per_image_standardization
from advertorch_examples.models import WideResNet
import numpy as np
import utils
import sys

class Flatten(nn.Module):
    def __init__(self):
        super(Flatten, self).__init__()
    
    def forward(self, x):
        return x.view(x.shape[0], -1)

class MINet(nn.Module):
    def __init__(self, x_size, y_size, hidden_size, outsize, device):
        super(MINet, self).__init__()
        self.fc_x = nn.Linear(x_size, hidden_size)
        self.fc_y = nn.Linear(y_size, hidden_size)
        self.mlp = nn.Sequential(
            nn.Linear(2*hidden_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, outsize),
        )
        self.device = device
        self.to(device)

    def forward(self, x, y):
        proj_x = self.fc_x(x)
        proj_y = self.fc_y(y)

        cat = torch.cat((proj_x, proj_y), dim=1)
        joint = self.mlp(cat)

        rand_idxs = np.arange(len(y))
        np.random.shuffle(rand_idxs)
        cat = torch.cat((proj_x, proj_y[rand_idxs]), dim=1)
        marginal = self.mlp(cat)        
        return joint, marginal
        
    def compute_nim(self, joint, marginal):
        return joint.mean(0) - (torch.logsumexp(marginal, dim=0) - np.log(len(marginal)))

    def fit(self, loader, epochs, optimizer, scheduler, verbose=True):
        patience = 10
        bad_iters = 0
        prev_error = sys.maxsize

        for e in range(epochs):
            avg_loss = 0            
            for bi,(x,y) in enumerate(loader):
                x = x.to(self.device)
                y = y.to(self.device)

                optimizer.zero_grad()
                joint, marginal = self.forward(x, y)
                loss = -self.compute_nim(joint, marginal).mean() 
                loss.backward()                
                optimizer.step()
                avg_loss = (bi*avg_loss + loss)/(bi+1)
            scheduler.step(avg_loss)            
            if verbose:
                print('%d/%d' % (e+1,epochs), float(avg_loss), bad_iters)
            # t.set_postfix(loss=avg_loss, bad_iters=bad_iters)
            if np.isclose(float(avg_loss), float(prev_error)) or avg_loss > prev_error:
                if bad_iters >= patience -1:
                    break
                else:
                    bad_iters += 1
            else:
                bad_iters = 0
                prev_error = avg_loss


def compute_A(S, N=1):    
    S_inv = torch.inverse(S)    

    I = torch.eye(S.shape[0], device=S_inv.device)
    A = I - S_inv / torch.diag(S_inv).view(-1,1)
    
    return A

def compute_S(Z):
    N, d = Z.shape

    Z = Z.unsqueeze(2)
    S = torch.bmm(Z, Z.transpose(1,2)).sum(0)
    # assert (S == S.transpose(0,1)).all()
    return S

def set_grad(var):
    def hook(grad):
        var.grad = grad
    return hook

class ActivationNormalization(nn.Module):
    def __init__(self):
        super(ActivationNormalization, self).__init__()
        self.alpha = nn.Parameter(torch.ones((1,)))
        self.beta = nn.Parameter(torch.zeros((1,)))
    
    def forward(self, z):
        z_shape = z.shape
        z = z.view(z_shape[0], -1)
        z_normed = z/torch.norm(z, p=2, dim=1, keepdim=True)
        z_normed = z_normed.view(*z_shape)        
        z_normed = self.alpha * z_normed        
        return z_normed


class LayeredModel(nn.Module):
    def __init__(self, args):
        super(LayeredModel, self).__init__()
        self.layers = []
        self.args = args

    def get_layer_output_sizes(self):
        pass

    def forward(self, x, store_intermediate=False):
        Z = []
        if hasattr(self.args, 'normalize_input') and self.args.normalize_input:
            x = utils.normalize_image_tensor(x, **(utils.dataset_stats[self.args.dataset]))
        z = x
        for i,l in enumerate(self.layers):
            z = l(z)            
            if store_intermediate:
                if hasattr(self.args, 'layer_idxs') and (i in self.args.layer_idxs or len(self.args.layer_idxs)==0):                    
                    Z.append(z)
        if store_intermediate:
            return z, Z
        else:
            return z

class GaussianNoiseLayer(nn.Module):
    def __init__(self, mean=0., std=0.):
        super(GaussianNoiseLayer, self).__init__()
        self.mean = mean
        self.std = std

    def forward(self, x):
        zeros = torch.zeros(x.shape, device=x.device)
        noise = torch.normal(zeros+self.mean, zeros+self.std).to(x.device)        
        return x + noise

class ResidualRegularizedModel(LayeredModel):
    def __init__(self, args, *args_, **kwargs):    
        super(ResidualRegularizedModel, self).__init__()
        self.layers = []
        self.cov = []        
        self.cov_update_alpha = args.cov_update_alpha
        self.mask_layers = []

    def reset(self):
        self.cov = []

    def init_mask(self):
        pass

    def forward(self, x, compute_residuals=False, store_intermediate=False):
        residuals = []
        Z = []

        z = x
        for i,l in enumerate(self.layers):
            z = l(z)
            if i < 5:#len(self.layers)-1:
                if store_intermediate:
                    z.retain_grad()
                    Z.append(z)
                if compute_residuals:
                    z_shape = z.shape
                    z_dim_order = range(len(z.shape))
                    if len(z_shape) == 4:
                        z = z.permute(0,2,3,1)
                        z_shape = z.shape
                        z_dim_order = (0,3,1,2)
                        z = z.reshape(-1, z.shape[3])
                    
                    S = compute_S(z)
                    if self.training:                    
                        if i >= len(self.cov):
                            self.cov.append(S)
                        else:
                            self.cov[i] = (1-self.cov_update_alpha)*self.cov[i].detach() + self.cov_update_alpha*S
                        S = self.cov[i]
                    else:
                        S = (1-self.cov_update_alpha)*self.cov[i].detach() + self.cov_update_alpha*S
                    A = compute_A(S, z.shape[0])  
                    # assert (torch.diag(A) == 0).all()

                    r = torch.mm(z, A.transpose(0,1)) - z

                    if self.args.mask_low_residual:
                        diff = (r-self.mask_layers[i]).mean(0,keepdim=True)
                        p = diff / diff.sum()
                        mask = torch.bernoulli(p)
                        z = z*mask

                    r = r.reshape(*z_shape)
                    r = r.permute(*z_dim_order).contiguous()                
                    residuals.append(r) 

                    z = z.reshape(*z_shape)
                    z = z.permute(*z_dim_order).contiguous()                    
        if compute_residuals:
            if store_intermediate:                
                return z, residuals, Z
            else:
                return z, residuals
        else:
            return z

class VGG16(LayeredModel):
    def __init__(self, args, num_classes):
        super(VGG16, self).__init__(args)
        self.name = 'VGG16'

        if hasattr(self.args, 'use_preactivation') and self.args.use_preactivation:
            self.layers = nn.ModuleList([
                nn.Conv2d(3, 64, 3, padding=1),
                self.conv_bn_act(64, 64, 3, 1),
                self.conv_bn_act(64, 64, 3, 1),
                self.conv_bn_act_pooling(64, 128, 3, 1 ,2, 2),
                self.conv_bn_act(128, 128, 3, 1),
                self.conv_bn_act_pooling(128, 256, 3, 1 ,2, 2),
                self.conv_bn_act(256, 256, 3, 1),
                self.conv_bn_act_pooling(256, 512, 3, 1 ,2, 2),
                self.conv_bn_act(512, 512, 3, 1),
                self.conv_bn_act(512, 512, 3, 1),
                self.conv_bn_act_pooling(512, 512, 3, 1 ,2, 2),
                self.conv_bn_act(512, 512, 3, 1),
                self.conv_bn_act(512, 512, 3, 1),
                nn.Sequential(
                    self.bn_act(512),
                    nn.MaxPool2d(kernel_size=2, stride=2),
                    nn.AdaptiveAvgPool2d((1,1)),
                    Flatten(),
                    nn.Linear(512, num_classes),
                ),                
            ])
        else:
            self.layers = nn.ModuleList([
                self.conv_bn_act(3, 64, 3, 1),
                self.conv_bn_act(64, 64, 3, 1),
                self.conv_bn_act_pooling(64, 64, 3, 1, 2, 2),
                self.conv_bn_act(64, 128, 3, 1),
                self.conv_bn_act_pooling(128, 128, 3, 1, 2, 2),
                self.conv_bn_act(128, 256, 3, 1),            
                self.conv_bn_act_pooling(256, 256, 3, 1, 2, 2),
                self.conv_bn_act(256, 512, 3, 1),
                self.conv_bn_act(512, 512, 3, 1),
                self.conv_bn_act_pooling(512, 512, 1, 0, 2, 2),
                self.conv_bn_act(512, 512, 3, 1),
                self.conv_bn_act(512, 512, 3, 1),
                nn.Sequential(
                    self.conv_bn_act_pooling(512, 512, 1, 0, 2, 2),
                    nn.AdaptiveAvgPool2d((1,1)),
                    Flatten(),
                ),
                nn.Linear(512, num_classes),
            ])
    
    def init_mask(self):
        output_sizes = [64,64,64,128,128,256,256,512,512,512,512,512]        
        mask_layers = [nn.Parameter(torch.rand(s)) for s in output_sizes]
        return mask_layers

    def bn_act(self, in_channels):
        return nn.Sequential(
            nn.BatchNorm2d(in_channels),
            nn.ELU(inplace=True)
        )
    def conv_bn_act(self, in_channels, out_channels, kernel_size, padding):
        if hasattr(self.args, 'use_preactivation') and self.args.use_preactivation:
            module = [
                self.bn_act(in_channels),
                nn.Conv2d(in_channels,out_channels, kernel_size=kernel_size, padding=padding),                
            ]
        else:
            module = [
                nn.Conv2d(in_channels,out_channels, kernel_size=kernel_size, padding=padding),
                self.bn_act(out_channels)
            ]
        if self.args.normalize_activations:
            module.append(ActivationNormalization())
        return nn.Sequential(*module)
    
    def conv_bn_act_pooling(self, in_channels, out_channels, kernel_size, padding, pooling_kernel_size, pooling_stride):
        if hasattr(self.args, 'use_preactivation') and self.args.use_preactivation:
            module = [
                self.bn_act(in_channels),
                nn.MaxPool2d(kernel_size=pooling_kernel_size, stride=pooling_stride),
                nn.Conv2d(in_channels,out_channels, kernel_size=kernel_size, padding=padding),                
            ]
        else:
            module = [
                nn.Conv2d(in_channels,out_channels, kernel_size=kernel_size, padding=padding),
                self.bn_act(out_channels),
                nn.MaxPool2d(kernel_size=pooling_kernel_size, stride=pooling_stride),
            ]
            
        if self.args.normalize_activations:
            module.append(ActivationNormalization())
        return nn.Sequential(*module)

def conv3x3(in_planes, out_planes, stride=1):
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=1, padding=1, bias=True)

def conv_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        init.xavier_uniform_(m.weight, gain=np.sqrt(2))
        init.constant_(m.bias, 0)
    elif classname.find('BatchNorm') != -1:
        init.constant_(m.weight, 1)
        init.constant_(m.bias, 0)

def bn_act(in_channels):
    return nn.Sequential(
        nn.BatchNorm2d(in_channels),
        nn.ReLU()
    )

def conv_bn_act(args, in_channels, out_channels, kernel_size, padding, stride):
    if hasattr(args, 'use_preactivation') and args.use_preactivation:
        module = [
            bn_act(in_channels),
            nn.Conv2d(in_channels,out_channels, kernel_size=kernel_size, padding=padding, stride=stride),                
        ]
    else:
        module = [
            nn.Conv2d(in_channels,out_channels, kernel_size=kernel_size, padding=padding, stride=stride),
            bn_act(out_channels)
        ]
    if args.normalize_activations:
        module.append(ActivationNormalization())
    return nn.Sequential(*module)

class wide_basic(nn.Module):
    def __init__(self, args, in_planes, planes, dropout_rate, stride=1):
        super(wide_basic, self).__init__()
        self.conv1 = conv_bn_act(args, in_planes, planes, 3, 1, 1)
        self.dropout = nn.Dropout(p=dropout_rate)        
        self.conv2 = conv_bn_act(args, planes, planes, 3, 1, stride)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != planes:
            self.shortcut = nn.Sequential(
                conv_bn_act(args, in_planes, planes, 1, 0, stride),
            )
        self.normalize = ActivationNormalization() if args.normalize_activations else nn.Sequential()
    def forward(self, x):
        out = self.conv2(self.dropout(self.conv1(x)))        
        return self.normalize(out + self.shortcut(x))

class WideResnet(LayeredModel):
    def __init__(self, args, depth, widen_factor, dropout_rate, num_classes):
        super(WideResnet, self).__init__(args)
        self.name = 'WideResNet_%d_%d' % (depth, widen_factor)
        self.in_planes = 16

        assert ((depth-4)%6 ==0), 'Wide-resnet depth should be 6n+4'
        n = (depth-4)/6
        k = widen_factor

        print('| Wide-Resnet %dx%d' %(depth, k))
        nStages = [16, 16*k, 32*k, 64*k]
        
        if args.normalize_activations and not args.use_preactivation:
            conv1 = conv_bn_act(args, 3, nStages[0], 3, 1, 1)            
        else:
            conv1 = nn.Conv2d(3, nStages[0], kernel_size=3, stride=1, padding=1, bias=True)
        layer1 = self._wide_layer(wide_basic, nStages[1], n, dropout_rate, stride=1)
        layer2 = self._wide_layer(wide_basic, nStages[2], n, dropout_rate, stride=2)
        layer3 = self._wide_layer(wide_basic, nStages[3], n, dropout_rate, stride=2)
        bn1 = nn.BatchNorm2d(nStages[3], momentum=0.9)
        linear = nn.Linear(nStages[3], num_classes)

        self.layers = nn.Sequential(
            conv1,
            *layer1,
            *layer2,
            *layer3,
            nn.Sequential(                
                bn_act(nStages[3]) if not args.use_preactivation else nn.Sequential(),
                nn.AdaptiveAvgPool2d((1,1)),
                Flatten(),
                ActivationNormalization() if args.normalize_activations and not args.use_preactivation else nn.Sequential()
            ),
            linear            
        )

        self.layer_output_sizes = nStages

    def _wide_layer(self, block, planes, num_blocks, dropout_rate, stride):
        strides = [stride] + [1]*(int(num_blocks)-1)
        layers = []

        for stride in strides:
            layers.append(block(self.args, self.in_planes, planes, dropout_rate, stride))
            self.in_planes = planes

        return layers

    def forward(self, x, store_intermediate=False):
        return super().forward(x, store_intermediate=store_intermediate)

class Args:
    def __init__(self, use_preactivation, normalize_activations, dataset):
        self.use_preactivation = use_preactivation
        self.normalize_activations = normalize_activations
        self.dataset = dataset

if __name__ == '__main__':
    net = VGG16(Args(True, True, "cifar10"), 10)
    print(net)
    torch.save(net, "./models/test_vgg16.pth")
    # net=WideResnet(28, 10, 0.3, 10).cuda()
    # print(net)
    # y, z = net(torch.randn(1,3,32,32).cuda(), store_intermediate=True)

    # print(y.size())
    # print([zz.shape for zz in z])