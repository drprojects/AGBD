"""

This script defines the training loop for the different models.

Launch the training with any of the /scripts/<architecture>_<patch_size>/<features>.sh files.
There, you can specify all arguments for the training. Hint: should you want to modify any argument,
modify /scripts/template.sh and run /scripts/gen_scripts.sh to apply the changes to all possible
training combinations.

The expected file structure is as follows:
- the Models/Data/ directory contains the .h5 files with the data, the .pkl file containing the
  statistics, and the biomes_splits_to_name.pkl file.
- the Models/cat2vec/ directory contains the cat2vec embeddings (by default, not used).

Basically, all of the necessary data to run this script should be in the Data/ and cat2vec/
directories, which should be subdirectories of Models/. Should you wish to change the paths, you can
do so in lines .....


"""

###################################################################################################
# Imports

import os
from rf import RandomForest
from table import RF_GEDIDataset
from models import Net
from wrapper import Model
from parser import setup_parser
from dataset import GEDIDataset
from torch.utils.data import DataLoader
from torch import set_float32_matmul_precision
from pytorch_lightning.loggers import WandbLogger
from pytorch_lightning import Trainer, seed_everything
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint, TQDMProgressBar
import wandb

try: 
    seed_everything(3 + int(os.environ.get('SLURM_ARRAY_TASK_ID')), workers = True)
except: 
    seed_everything(3, workers = True)
os.environ["WANDB__SERVICE_WAIT"] = "300"

#####################################################################################################################################################
# Helper functions

def get_model_checkpoint_callback(dir, fname):
    return ModelCheckpoint(monitor = 'val/agbd_rmse', dirpath = dir, filename = f'{fname}_best', save_top_k = 1, mode = 'min', save_last = True)

def get_early_stopping_callback(patience, min_delta):
    return EarlyStopping(monitor = 'val/agbd_rmse', patience = patience, min_delta = min_delta, verbose = True)

def get_progress_bar():
    return TQDMProgressBar(refresh_rate = 1000)

#####################################################################################################################################################
# Code execution

current_dir = os.getcwd()
data_dir = os.path.join(current_dir, 'Data') # Path to the Data/ directory
log_dir = os.path.join(current_dir, 'Logs') # Path to the Logs/ directory
local_dataset_paths = {'h5': data_dir,
                    'norm': data_dir, 
                    'map': data_dir,
                    'embeddings': os.path.join(current_dir, 'cat2vec')}

def main():
    
    # Parse the arguments
    args, _ = setup_parser().parse_known_args()
    if args.dataset_path == 'local' : 
        dataset_path = local_dataset_paths
        debug = False
    else: 
        dataset_path = {k:args.dataset_path for k in local_dataset_paths.keys()}
        debug = False
        
    # Settings
    set_float32_matmul_precision('high')
    if (args.dataset_path == 'local') :
        accelerator = 'auto'
        cpus_per_task = 8
    else:
        accelerator = 'gpu'
        cpus_per_task = int(os.environ.get('SLURM_CPUS_PER_TASK'))
    if cpus_per_task is None: cpus_per_task = 8

    # In the case of RF, the dataset is tabular
    if args.arch == 'rf': 
        train_dataset = RF_GEDIDataset(data_path = dataset_path['h5'], mode = "train", args = args, years = args.years)
        val_dataset = RF_GEDIDataset(data_path = dataset_path['h5'], mode = "val", args = args, years = args.years)
        test_dataset = RF_GEDIDataset(data_path = dataset_path['h5'], mode = "test", args = args, years = args.years)

    # Load the dataset
    else:
        train_dataset = GEDIDataset(paths = dataset_path, years = args.years, chunk_size = args.chunk_size, mode = "train", args = args, debug = debug)
        val_dataset = GEDIDataset(paths = dataset_path, years = args.years, chunk_size = args.chunk_size, mode = "val", args = args, debug = debug)
        test_dataset = GEDIDataset(paths = dataset_path, years = args.years, chunk_size = args.chunk_size, mode = "test", args = args, debug = debug)
        train_loader = DataLoader(train_dataset, batch_size = args.batch_size, shuffle = True, num_workers = cpus_per_task, persistent_workers = True, pin_memory = True)
        val_loader = DataLoader(val_dataset, batch_size = args.batch_size, shuffle = False, num_workers = cpus_per_task, pin_memory = True)
        test_loader = DataLoader(test_dataset, batch_size = args.batch_size, shuffle = False, num_workers = cpus_per_task, pin_memory = True)

    # Set up Weights & Biases logger
    model_name = args.model_name.split('/')[-1]
    if model_name == 'local' :
        # if training locally, give a random wandb name
        wandb_logger = WandbLogger(entity = args.entity, project = args.arch, log_model = False, save_dir=log_dir)
        model_name = wandb_logger.experiment.name
    else:
        # if on the cluster, model_name is the JOB ID and MODEL ID in the ensemble
        wandb_logger = WandbLogger(entity = args.entity, project = args.arch, name = model_name, log_model = False, save_dir=log_dir)

    # Define the trainer
    trainer = Trainer(max_epochs = args.n_epochs, devices = 1, accelerator = accelerator, logger = wandb_logger, num_sanity_val_steps = 1, val_check_interval = 0.5,
                        callbacks = [get_early_stopping_callback(patience = args.patience, min_delta = args.min_delta), 
                                     get_model_checkpoint_callback(dir = args.model_path, fname = model_name),
                                     get_progress_bar()])
    wandb_logger.experiment.config.update(args)

    # Build the network based on the architecture requested
    if args.arch in ['fcn', 'unet', 'nico']:
        model = Net(model_name = args.arch, in_features = args.in_features, num_outputs = args.num_outputs, 
                    channel_dims = args.channel_dims, max_pool = args.max_pool, downsample = None,
                    leaky_relu = args.leaky_relu, patch_size = args.patch_size, num_sepconv_blocks = args.num_sepconv_blocks, 
                    num_sepconv_filters = args.num_sepconv_filters, long_skip = args.long_skip)

        # Define the model
        model = Model(model, lr = args.lr, step_size = args.step_size, gamma = args.gamma, 
                        patch_size = args.patch_size, downsample = args.downsample, 
                        loss_fn = args.loss_fn)

        # Train the model
        trainer.fit(model, train_dataloaders = train_loader, val_dataloaders = [val_loader, test_loader])

    elif args.arch == 'rf':
        model = RandomForest(model_name = model_name)
        model.fit(train_dataset, val_dataset, test_dataset)
        wandb.log({'ens_val_rmse': model.ens_val_rmse, 'ens_val_std': model.ens_val_std, 'ens_test_rmse': model.ens_test_rmse, 'ens_test_std': model.ens_test_std})

if __name__ == '__main__':
    main()
