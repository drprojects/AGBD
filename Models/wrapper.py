""" 

Definition of the Model() Module, which is the wrapper module for all models.

"""

###################################################################################################
# Imports

import numpy as np
import torch
import torch.nn as nn
import pytorch_lightning as pl
from loss import TrainLoss, RMSE

############################################################################################################################
# Helper functions

class Model(pl.LightningModule):

    def __init__(self, model, lr, step_size, gamma, patch_size, downsample, loss_fn):
        """
        Args:
        - model (nn.Module): the model to train
        - lr (float): learning rate
        - step_size (int): the number of epochs before decreasing the learning rate
        - gamma (float): the factor by which the learning rate will be decreased
        - patch_size (list): the size of the patches to extract, in pixels
        - downsample (bool): whether to downsample the patches from 10m resolution to 50m resolution
        - loss_fn (str): the loss function to use for the training.  (Only 'MSE' is currently supported.)
        """ 

        super().__init__()
        self.model = model
        self.num_outputs = model.num_outputs
        self.lr = lr
        self.step_size = step_size
        self.gamma = gamma
        self.best_val_rmse = np.inf 
        
        # With downsampling, we go from 10m per pixel to 50m per pixel
        if downsample:
            self.center = int(patch_size[0] // 5) // 2
        else: 
            self.center = int(patch_size[0] // 2)
        
        self.loss_fn = loss_fn

        self.preds, self.val_preds, self.test_preds = [], [], []
        self.labels, self.val_labels, self.test_labels = [], [], []

        self.TrainLoss = TrainLoss(num_outputs = self.num_outputs, loss_fn = self.loss_fn)
                         
    def training_step(self, batch, batch_idx):

        # split batch
        images, labels = batch
        
        # get prediction
        predictions = self.model(images)
        predictions = predictions[:,:,self.center,self.center]

        # Store the predictions and labels
        if batch_idx % 50 == 0:
            rmse = torch.sqrt(torch.mean(torch.pow(predictions[:, 0] - labels, 2)))
            self.log('train/agbd_rmse', rmse)

        # Return the loss
        loss = self.TrainLoss(predictions, labels)

        return loss


    def validation_step(self, batch, batch_idx, dataloader_idx = None):

        # Ordinary validation 
        if dataloader_idx == None or dataloader_idx == 0:
            
            # split batch
            images, labels = batch

            # get predictions
            predictions = self.model(images).detach().cpu()
            predictions = predictions[:,:,self.center,self.center]

            # Store the predictions, labels for the on_validation_epoch_end method
            self.val_preds.append(predictions[:, 0])
            self.val_labels.append(labels.detach().cpu())
        
        # Validation on the test set
        elif dataloader_idx == 1 :
    
            # split batch
            images, labels = batch

            # get predictions
            predictions = self.model(images).detach().cpu()
            predictions = predictions[:,:,self.center,self.center]

            # Store the predictions, labels for the on_validation_epoch_end method
            self.test_preds.append(predictions[:, 0])
            self.test_labels.append(labels.detach().cpu())
        
        else: raise ValueError('dataloader_idx should be 0 or 1')
    

    def on_validation_epoch_end(self):
        """
        Calculate the overall validation RMSE and binned metrics.
        """
        # Log info about the training regime
        lr = self.trainer.lr_scheduler_configs[0].scheduler.optimizer.param_groups[0]["lr"]
        self.log_dict({'trainer/learning_rate': lr, "step": self.current_epoch})

        # Ordinary validation #####################################################################

        # Log the validation epoch's predictions and labels
        val_preds = torch.cat(self.val_preds).unsqueeze(1)
        val_labels = torch.cat(self.val_labels)
        val_agbd_rmse = RMSE()(val_preds, val_labels)
        self.log_dict({'val/agbd_rmse': val_agbd_rmse, "step": self.current_epoch})

        # Log the validation agbd rmse by bin
        bins = np.arange(0, 501, 50)
        for lb, ub in zip(bins[:-1], bins[1:]):
            mask = (lb <= val_labels) & (val_labels < ub)
            bin_preds = val_preds[mask]
            bin_labels = val_labels[mask]
            rmse = RMSE()(bin_preds, bin_labels)
            self.log_dict({f'val/binned/rmse_{lb}-{ub}': rmse, "step": self.current_epoch})
        
        # Set the predictions and labels back to empty lists
        self.val_preds = []
        self.val_labels = []

        # Validation on the test set ##############################################################

        # Log the test set agbd rmse
        test_preds = torch.cat(self.test_preds).unsqueeze(1)
        test_labels = torch.cat(self.test_labels)
        test_agbd_rmse = RMSE()(test_preds, test_labels)
        self.log_dict({'test/agbd_rmse': test_agbd_rmse, "step": self.current_epoch})

        # Log the test set agbd rmse by bin
        bins = np.arange(0, 501, 50)
        for lb, ub in zip(bins[:-1], bins[1:]):
            mask = (lb <= test_labels) & (test_labels < ub)
            bin_preds = test_preds[mask]
            bin_labels = test_labels[mask]
            rmse = RMSE()(bin_preds, bin_labels)
            self.log_dict({f'test/binned/rmse_{lb}-{ub}': rmse, "step": self.current_epoch})

        # Set the predictions and labels back to empty lists
        self.test_labels = []
        self.test_preds = []

        # Keep track of the performance of the best-so-far model, based
        # on validation metrics
        if val_agbd_rmse < self.best_val_rmse:
            self.best_val_rmse = val_agbd_rmse
            self.log_dict({'val/best_rmse': val_agbd_rmse, "step": self.current_epoch})
            self.log_dict({'test/best_rmse': test_agbd_rmse, "step": self.current_epoch})

            # Log the test set agbd rmse by bin
            bins = np.arange(0, 501, 50)
            for lb, ub in zip(bins[:-1], bins[1:]):
                mask = (lb <= test_labels) & (test_labels < ub)
                bin_preds = test_preds[mask]
                bin_labels = test_labels[mask]
                rmse = RMSE()(bin_preds, bin_labels)
                self.log_dict({f'test/binned/best_rmse_{lb}-{ub}': rmse, "step": self.current_epoch})

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.model.parameters(), lr = self.lr)
        return [optimizer], [torch.optim.lr_scheduler.StepLR(optimizer, step_size = self.step_size, gamma = self.gamma)]
