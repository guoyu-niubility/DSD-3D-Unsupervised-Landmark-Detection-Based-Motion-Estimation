from tqdm import trange
import torch

from torch.utils.data import DataLoader
import torch.nn.functional as F
from logger import Logger
from modules.model_3d import GeneratorFullModel, DiscriminatorFullModel
from modules.util_3d import AntiAliasInterpolation3d, make_coordinate_grid
from modules.vnet_3d import Temporal_Encoder_Pre, Temporal_Encoder_Pre_New, Temporal_Encoder_Pre_Eval
from torch.optim.lr_scheduler import MultiStepLR

from sync_batchnorm import DataParallelWithCallback

from frames_dataset import DatasetRepeater
from niidata import *
import random
from itertools import combinations

from gradcam import *

device0 = torch.device("cuda:0")
device1 = torch.device("cuda:1")



class Transform:
    """
    Random tps transformation for equivariance constraints. See Sec 3.3
    """
    def __init__(self, bs, **kwargs):
        noise = torch.normal(mean=0, std=kwargs['sigma_affine'] * torch.ones([bs, 3, 4])).to(device0)#.cuda()
        #noise[:,:,2] = 0
        #noise[:,2,:2] = 0
        self.theta = noise + torch.eye(3, 4).view(1, 3, 4).to(device0)
        self.bs = bs

        if ('sigma_tps' in kwargs) and ('points_tps' in kwargs):
            self.tps = True
            self.control_points = make_coordinate_grid((kwargs['points_tps'], kwargs['points_tps'], kwargs['points_tps']), type=noise.type()).to(device0)#.cuda()
            self.control_points = self.control_points.unsqueeze(0)
            self.control_params = torch.normal(mean=0,
                                               std=kwargs['sigma_tps'] * torch.ones([bs, 1, kwargs['points_tps'] ** 3])).to(device0)#.cuda()
        else:
            self.tps = False

    def transform_frame(self, frame):
        grid = make_coordinate_grid(frame.shape[2:], type=frame.type()).unsqueeze(0).to(device0)#.cuda()
        grid = grid.view(1, frame.shape[2] * frame.shape[3] * frame.shape[4], 3)
        grid = self.warp_coordinates(grid).view(self.bs, frame.shape[2], frame.shape[3], frame.shape[4], 3)
        return F.grid_sample(frame, grid, padding_mode="reflection")

    def warp_coordinates(self, coordinates):
        theta = self.theta.type(coordinates.type())
        theta = theta.unsqueeze(1)
        transformed = torch.matmul(theta[:, :, :, :3], coordinates.unsqueeze(-1)) + theta[:, :, :, 3:]
        transformed = transformed.squeeze(-1)

        if self.tps:
            control_points = self.control_points.type(coordinates.type())
            control_params = self.control_params.type(coordinates.type())
            distances = coordinates.view(coordinates.shape[0], -1, 1, 3) - control_points.view(1, 1, -1, 3)
            distances = torch.abs(distances).sum(-1)

            result = distances ** 2
            result = result * torch.log(distances + 1e-6)
            result = result * control_params
            result = result.sum(dim=2).view(self.bs, coordinates.shape[1], 1)
            transformed = transformed + result

        return transformed
        






def pretext_vis(config, log_dir, dataset):
    log_dir_new = os.path.join(log_dir, 'pretext')
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    if not os.path.exists(log_dir_new):
        os.makedirs(log_dir_new)
    train_params = config['train_params']
    #s_pretext = Temporal_Encoder_Pre_Eval().to(device0)
    s_pretext = Temporal_Encoder_Pre_New().to(device0)

    pre_model = torch.load( \
        '')
    s_pretext.load_state_dict(pre_model)

    criterionCrossEn = torch.nn.CrossEntropyLoss().to(device0)

    #optimizer_pre = torch.optim.Adam(s_pretext.parameters(), lr=0.00005, betas=(0.5, 0.999))
    start_epoch = 0

    grad_cam = GradCam(model=s_pretext, feature_module=s_pretext.down_block3, \
                       target_layer_names=["convb"], use_cuda=True)
    

    #random.shuffle(dataset)
    #dataset_new = dataset[:-1]
    list_order = [0,1,2,3,4]
    list_order = list(combinations(list_order,2))
    img_data_list = []
    gap_num = 14

    train_loss = []

    

    img_dir = dataset

    img_data_list = []
    for k in range(5):
        img_data_list.append(img_dir+'/image_data/t'+str(k+1)+'.nii')

    num_lists = np.arange(0,5)
    num_shuffled = []
    for num in range(1):
        np.random.shuffle(num_lists)
        num_shuffled.append(num_lists.copy())
    trainning_list = Temporal_read_pre(img_data_list, \
        num_shuffled, job='seg')

    train_loader = DataLoader(
                        trainning_list, batch_size=1, \
                        shuffle=None, num_workers=6, pin_memory=False)

    for batch_idx, (img_list, index_l) in enumerate(train_loader):

        #optimizer_pre.zero_grad()

        transform = Transform(1, **config['train_params']['augmentation_params'])

        img_list = F.interpolate(img_list, size=(96,96,96), mode='trilinear')
        #img_list = transform.transform_frame(img_list.to(device0))

        index_l = index_l.to(device0)

        target_index = None
        for num_index in range(5):
            mask = grad_cam(img_list[:,num_index,:,:,:].unsqueeze(1), target_index)
            mask = sitk.GetImageFromArray(mask)
            sitk.WriteImage(mask, 'attention_'+str(num_index+1)+'.nii')

            img_tmp = img_list[0,num_index,:,:,:].cpu().data.numpy()
            img_tmp = sitk.GetImageFromArray(img_tmp)
            sitk.WriteImage(img_tmp, 'attention_img_'+str(num_index+1)+'.nii')


    