
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset, Subset
from torchvision import transforms
from tqdm import tqdm
import numpy as np
from sklearn.metrics import average_precision_score, precision_score, recall_score, accuracy_score
import argparse
import torchvision.models as vis_models
import re

import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data.distributed import DistributedSampler

from dataset import ForenSynths
# from extract_features import *
from augment import ImageAugmentor
from mask import *
from earlystop import EarlyStopping
from utils import *
from networks.resnet import resnet50
from networks.resnet_mod import resnet50 as _resnet50, ChannelLinear

from networks.clip_models import CLIPModel
import os
os.environ['NCCL_BLOCKING_WAIT'] = '1'
os.environ['NCCL_DEBUG'] = 'WARN'

os.environ['LOCAL_RANK']

def main(
    local_rank=0,
    nhead=8,
    num_layers=6,
    num_epochs=10000,
    ratio=50,
    batch_size=64,
    model_name='RN50',
    band='all',
    mask_type=None,
    pretrained=False,
    args=None,
    ):

    seed = args.seed
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    device = torch.device(f'cuda:{local_rank}')
    torch.cuda.set_device(device)
    dist.init_process_group(backend='nccl')


    # Set options for augmentation
    train_opt = {
        'rz_interp': ['bilinear'],
        'loadSize': 256,
        'blur_prob': 0.1,  # Set your value
        'blur_sig': [0.0, 3.0],
        'jpg_prob': 0.1,  # Set your value
        'jpg_method': ['cv2', 'pil'],
        'jpg_qual': [30, 100]
    }

    val_opt = {
        'rz_interp': ['bilinear'],
        'loadSize': 256,
        'blur_prob': 0.1,  # Set your value
        'blur_sig': [(0.0 + 3.0) / 2],
        'jpg_prob': 0.1,  # Set your value
        'jpg_method': ['pil'],
        'jpg_qual': [int((30 + 100) / 2)]
    }

    if ratio > 1.0 or ratio < 0.0:
        raise valueError(f"Invalid mask ratio {ratio}")
    else:
        # Create a MaskGenerator
        if mask_type == 'spectral':
            mask_generator = FrequencyMaskGenerator(ratio=ratio, band=band)
        elif mask_type == 'pixel':
            mask_generator = PixelMaskGenerator(ratio=ratio)
        elif mask_type == 'patch':
            mask_generator = PatchMaskGenerator(ratio=ratio)
        else:
            mask_generator = None

    train_transform = train_augment(ImageAugmentor(train_opt), mask_generator, args)
    val_transform = val_augment(ImageAugmentor(val_opt), mask_generator, args)

    # Creating training dataset from images
    train_data = ForenSynths('/home/users/chandler_doloriel/scratch/Datasets/Wang_CVPR2020/training', transform=train_transform)
    if args.smallset:
        subset_size = int(0.02 * len(train_data))
        subset_indices = random.sample(range(len(train_data)), subset_size)
        train_data = Subset(train_data, subset_indices)
    train_sampler = DistributedSampler(train_data, shuffle=True, seed=seed)
    train_loader = DataLoader(train_data, batch_size=batch_size, sampler=train_sampler, num_workers=4)

    # Creating validation dataset from images
    val_data = ForenSynths('/home/users/chandler_doloriel/scratch/Datasets/Wang_CVPR2020/validation', transform=val_transform)
    # val_sampler = DistributedSampler(val_data, shuffle=False, seed=seed)
    val_loader = DataLoader(val_data, batch_size=batch_size, shuffle=False, num_workers=4)

    # Creating and training the binary classifier
    if model_name == 'RN50':
        model = resnet50(pretrained=pretrained)
        model.fc = nn.Linear(model.fc.in_features, 1)
    elif model_name == 'RN50_mod':
        model = _resnet50(pretrained=pretrained, stride0=1)
        model.fc = ChannelLinear(model.fc.in_features, 1)
    elif model_name.startswith('clip'):
        clip_model_name = 'ViT-L/14'
        model = CLIPModel(clip_model_name, num_classes=1)
    else:
        raise ValueError(f"Model {model_name} not recognized!")

    model = model.to(device)
    model = DistributedDataParallel(model)

    criterion = nn.BCEWithLogitsLoss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, betas=(0.9, 0.999), weight_decay=1e-4) 
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[3], gamma=0.1, last_epoch=-1)

    if args.pretrained == False:
        checkpoint_path = args.checkpoint_path
        checkpoint = torch.load(checkpoint_path)

        if args.model_name == 'clip':
            model.module.fc.load_state_dict(checkpoint['model_state_dict'])
        else:
            model.load_state_dict(checkpoint['model_state_dict'])

    pruned_model = iterative_pruning_finetuning(
        model, 
        criterion, 
        optimizer, 
        scheduler,
        train_loader, 
        val_loader, 
        device,
        args.lr, 
        args=args
        )
        
if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Your model description here")

    parser.add_argument('--local_rank', type=int, default=0, help='Local rank for distributed training')
    parser.add_argument('--num_epochs', type=int, default=2, help='Number of epochs training')
    parser.add_argument(
        '--model_name',
        default='RN50',
        type=str,
        choices=[
            'RN18', 'RN34', 'RN50', 'RN50_mod', 'clip',
            # 'ViT_base_patch16_224', 'ViT_base_patch32_224',
            # 'ViT_large_patch16_224', 'ViT_large_patch32_224'
        ],
        help='Type of model to use; includes ResNet'
        )
    parser.add_argument(
        '--band', 
        default='all',
        type=str,
        choices=[
            'all', 'low', 'mid', 'high',
        ]
        )
    parser.add_argument(
        '--pretrained', 
        action='store_true', 
        help='if use ImageNet weights'
        )
    parser.add_argument(
        '--smallset', 
        action='store_true', 
        help='For using small subset of training set'
        )
    parser.add_argument(
        '--mask_type', 
        default='spectral', 
        choices=[
            'pixel', 
            'spectral', 
            'patch',
            'nomask'], 
        help='Type of mask generator'
        )
    parser.add_argument(
        '--batch_size', 
        type=int, 
        default=64, 
        help='Batch Size'
        )
    parser.add_argument(
        '--ratio', 
        type=int, 
        default=50, 
        help='Masking ratio'
        )
    parser.add_argument(
        '--lr', 
        type=float, 
        default=0.0001, 
        help='learning rate'
        )
    parser.add_argument(
        '--pruning_ft', 
        action='store_true', 
        help='For finetuning after pruning'
        )
    parser.add_argument(
        '--pruning_test', 
        action='store_true', 
        help='For test after pruning'
        )
    parser.add_argument(
        '--global_prune', 
        action='store_true', 
        help='to apply global unstructured pruning or not'
        )
    parser.add_argument(
        '--conv2d_prune_amount', 
        type=float, 
        default=0.2, 
        help='amount to prune'
        )
    parser.add_argument(
        '--linear_prune_amount', 
        type=float, 
        default=0.1, 
        help='amount to prune'
        )
    parser.add_argument(
        '--pruning_rounds', 
        type=int, 
        default=1, 
        help='pruning iteration'
        )
    parser.add_argument(
        '--seed', 
        type=int, 
        default=44, 
        help='seed number'
        )
    parser.add_argument(
        '--checkpoint_path', 
        default='./checkpoints/mask_0/rn50ft.pth',
        type=str,
        )

    args = parser.parse_args()
    model_name = args.model_name.lower().replace('/', '').replace('-', '')
    finetune = 'ft' if args.pretrained else ''
    band = '' if args.band == 'all' else args.band

    if args.mask_type != 'nomask':
        ratio = args.ratio
        ckpt_folder = f'./checkpoints/mask_{ratio}'
    else:
        ratio = 0
        args.band = 'None'
        ckpt_folder = f'./checkpoints/mask_{ratio}'


    # Pretty print the arguments
    print("\nSelected Configuration:")
    print("-" * 30)
    print(f"Seed: {args.seed}")
    print(f"Mask Type: {args.mask_type}")
    print(f"Mask Ratio: {ratio}")
    print(f"Mask Band: {args.band}")
    print(f"Model Arch: {args.model_name}")
    print(f"ImageNet Weights: {args.pretrained}")
    if args.pretrained == False:
        print(f"Checkpoint: {args.checkpoint_path}")
    print(f"\n")
    print(f"Global Pruning: {args.global_prune}")
    print(f"Pruning-Test: {args.pruning_test}")
    print(f"Pruning-Finetune: {args.pruning_ft}")
    print(f"Pruning Ratio: {args.conv2d_prune_amount}")
    print("-" * 30, "\n")

    main(
        local_rank=args.local_rank,
        num_epochs=5,
        ratio=ratio/100,
        batch_size=args.batch_size,
        model_name=args.model_name,
        band=args.band,
        mask_type=args.mask_type,
        pretrained=args.pretrained,
        args=args
    )


# How to run?
# python -m torch.distributed.launch --nproc_per_node=2 train.py -- --args.parse
