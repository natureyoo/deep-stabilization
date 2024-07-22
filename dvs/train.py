import os
import sys
import torch
import torchvision
import torch.nn as nn
from torch.autograd import Variable

import time
import yaml
import argparse
import numpy as np
from printer import Printer
from dataset import get_data_loader
from model import Model
import datetime
import copy
from util import make_dir, get_optimizer, AverageMeter, save_train_info, norm_flow
from gyro import torch_QuaternionProduct, torch_QuaternionReciprocal, torch_norm_quat

os.environ["CUDA_VISIBLE_DEVICES"] = "0"

def run_epoch(model, loader, cf, epoch, lr, optimizer=None, is_training=True, USE_CUDA=True, clip_norm=0):
    no_flo = False
    number_virtual, number_real = cf['data']["number_virtual"], cf['data']["number_real"]
    avg_loss = AverageMeter()
    if is_training:
        model.net.train()
        model.unet.train()
    else:
        model.net.eval()
        model.unet.eval()
    for i, data in enumerate(loader, 0):
        # get the inputs; data is a list of [inputs, labels]
        real_inputs, times, flo, flo_back, real_projections, real_postion, ois, real_queue_idx = data
        print("Fininsh Load data")

        real_inputs = real_inputs.type(torch.float) #[b,60,84=21*4]
        real_projections = real_projections.type(torch.float) 
        flo = flo.type(torch.float) 
        flo_back = flo_back.type(torch.float) 
        ois = ois.type(torch.float) 

        batch_size, step, dim = real_inputs.size()
        times = times.numpy()
        real_queue_idx = real_queue_idx.numpy()
        virtual_queue = loader.dataset.random_init_virtual_queue(batch_size, real_postion[:,0,:].numpy(), times[:,1]) # TODO
        # virtual_queue = [None] * batch_size
        loss = 0
        model.net.init_hidden(batch_size)
        for j in range(step):
            virtual_inputs, vt_1 = loader.dataset.get_virtual_data(
                virtual_queue, real_queue_idx, times[:, j], times[:, j+1], times[:, 0], batch_size, number_virtual, real_postion[:,j]) 
            
            real_inputs_step = real_inputs[:,j,:]
            inputs = torch.cat((real_inputs_step,virtual_inputs), dim = 1) 

            # inputs = Variable(real_inputs_step)
            if USE_CUDA:
                real_inputs_step = real_inputs_step.cuda()
                virtual_inputs = virtual_inputs.cuda()
                inputs = inputs.cuda()
                if no_flo is False:
                    flo_step = flo[:,j].cuda()
                    flo_back_step = flo_back[:,j].cuda()
                else:
                    flo_step = None
                    flo_back_step = None
                vt_1 = vt_1.cuda()
                real_projections_t = real_projections[:,j+1].cuda()
                real_projections_t_1 = real_projections[:,j].cuda()
                real_postion_anchor = real_postion[:,j].cuda()
                ois_step = ois[:,j].cuda()

            if no_flo is False:
                b, h, w, _ = flo_step.size()
                flo_step = norm_flow(flo_step, h, w)
                flo_back_step = norm_flow(flo_back_step, h, w)

            if is_training:
                if no_flo is False:
                    flo_out = model.unet(flo_step, flo_back_step)
                else:
                    flo_out = None

                if j < 1:
                    for i in range(2):
                        out = model.net(inputs, flo_out, ois_step)
                else:
                    out = model.net(inputs, flo_out, ois_step)
            else:
                with torch.no_grad():
                    if no_flo is False:
                        flo_out = model.unet(flo_step, flo_back_step)
                    else:
                        flo_out = None

                    if j < 1:
                        for i in range(2):
                            out = model.net(inputs, flo_out, ois_step)
                    else:
                        out = model.net(inputs, flo_out, ois_step)

            if epoch <= 30:
                follow = True
            else:
                follow = False

            if epoch > 30:
                undefine = True
            else:
                undefine = False

            if epoch > 40:
                optical = True
            else:
                optical = False
            
            loss_step = model.loss(out, vt_1, virtual_inputs, real_inputs_step, \
                flo_step, flo_back_step, real_projections_t, real_projections_t_1, real_postion_anchor, \
                follow = follow, undefine = undefine, optical = optical, stay = optical)
            
            virtual_position = virtual_inputs[:, -4:]
            pos = torch_QuaternionProduct(virtual_position, real_postion_anchor)
            out = torch_QuaternionProduct(out, pos)

            if USE_CUDA:
                out = out.cpu().detach().numpy() 

            virtual_queue = loader.dataset.update_virtual_queue(batch_size, virtual_queue, out, times[:,j+1])

            if (j+1) % 10 == 0:
                print("Step: "+str(j+1)+"/"+str(step))
                print(loss_step)
            loss = sum(loss_step.values())
            if is_training:
                optimizer.zero_grad()
                loss.backward(retain_graph=True)
                if clip_norm:
                    nn.utils.clip_grad_norm_(model.net.parameters(), max_norm=clip_norm)
                    nn.utils.clip_grad_norm_(model.unet.parameters(), max_norm=clip_norm)
                optimizer.step()

            avg_loss.update(loss.item(), batch_size) 
    
    return avg_loss.avg


def train(args = None):
    torch.autograd.set_detect_anomaly(True)
    config_file = args.config
    cf = yaml.load(open(config_file, 'r'))
    
    USE_CUDA = cf['data']["use_cuda"]
    seed = cf['train']["seed"]
    
    torch.manual_seed(seed)
    if USE_CUDA:
        torch.cuda.manual_seed(seed)

    checkpoints_dir = cf['data']['checkpoints_dir']
    epochs = cf["train"]["epoch"]
    snapshot = cf["train"]["snapshot"]
    decay_epoch = cf['train']['decay_epoch']
    init_lr = cf["train"]["init_lr"]
    lr_decay = cf["train"]["lr_decay"]
    lr_step = cf["train"]["lr_step"]
    clip_norm = cf["train"]["clip_norm"]
    load_model = cf["model"]["load_model"]

    checkpoints_dir = make_dir(checkpoints_dir, cf)

    if load_model is None:
        log_file = open(os.path.join(cf["data"]["log"], cf['data']['exp']+'.log'), 'w+')
    else:
        log_file = open(os.path.join(cf["data"]["log"], cf['data']['exp']+'.log'), 'a')
    printer = Printer(sys.stdout, log_file).open()
    
    print('----Print Arguments Setting------') 
    for key in cf:
        print('{}:'.format(key))
        for para in cf[key]:
            print('{:50}:{}'.format(para,cf[key][para]))
        print('\n')

    # Define the model
    model = Model(cf) 
    optimizer = get_optimizer(cf["train"]["optimizer"], model, init_lr, cf)

    for idx, m in enumerate(model.net.children()):
        print('{}:{}'.format(idx,m))
    for idx, m in enumerate(model.unet.children()):
        print('{}:{}'.format(idx,m))

    if load_model is not None:
        print("------Load Pretrined Model--------")
        checkpoint = torch.load(load_model)
        model.net.load_state_dict(checkpoint['state_dict'])
        model.unet.load_state_dict(checkpoint['unet'])
        print("------Resume Training Process-----")
        optimizer.load_state_dict(checkpoint['optim_dict'])
        epoch_load = checkpoint['epoch']
        print("Epoch load: ", epoch_load)
    else:
        epoch_load = 0
                
    if USE_CUDA:
        model.net.cuda()
        model.unet.cuda()
        if load_model is not None:
            for state in optimizer.state.values():
                for k, v in state.items():
                    if isinstance(v, torch.Tensor):
                        state[k] = v.cuda()
            for param in optimizer.param_groups:
                init_lr = param['lr']

    print("-----------Load Dataset----------")
    train_loader, test_loader = get_data_loader(cf, no_flo = False, flo_model=args.flo_model)

    print("----------Start Training----------")
    currentDT = datetime.datetime.now()
    print(currentDT.strftime(" %Y-%m-%d %H:%M:%S"))
    
    start_time = time.time()

    if lr_step:
        decay_epoch = list(range(1+lr_step, epochs+1, lr_step))
    
    lr = init_lr

    for count in range(epoch_load+1, epochs+1):
        if decay_epoch != None and count in decay_epoch:
            lr *= lr_decay
            for param in optimizer.param_groups:
                param['lr'] *= lr_decay
        
        print("Epoch: %d, learning_rate: %.5f" % (count, lr))

        train_loss = run_epoch(model, train_loader, cf, count, lr, optimizer=optimizer, clip_norm=clip_norm, is_training=True, USE_CUDA=USE_CUDA)

        test_loss = run_epoch(model, test_loader, cf, count, lr, is_training=False, USE_CUDA=USE_CUDA)

        time_used = (time.time() - start_time) / 60
        print("Epoch %d done | TrLoss: %.4f | TestLoss: %.4f | Time_used: %.4f minutes" % (
            count, train_loss,  test_loss, time_used))

        if count % snapshot == 0:
            save_train_info("epoch", checkpoints_dir, cf, model, count, optimizer)
            save_train_info("last", checkpoints_dir, cf, model, count, optimizer)
            print("Model stored at epoch %d"%count)

    currentDT = datetime.datetime.now()
    print(currentDT.strftime(" %Y-%m-%d %H:%M:%S"))
    print("------------End Training----------")
    return 


def tmp_lstm_cell():
    import torch
    import torch.nn as nn
    import torch.optim as optim

    # Define the LSTM Cell
    input_size = 10
    hidden_size = 20
    lstm_cell = nn.LSTMCell(input_size, hidden_size)

    # Define a simple optimizer
    optimizer = optim.Adam(lstm_cell.parameters(), lr=0.01)

    # Training loop for 10 steps
    num_steps = 10
    batch_size = 5
    x = torch.randn(batch_size, input_size)  # Input tensor

    for step in range(num_steps):
        # Initialize hidden and cell states
        hx = torch.randn(batch_size, hidden_size)  # Hidden state
        cx = torch.randn(batch_size, hidden_size)  # Cell state

        optimizer.zero_grad()

        # Forward pass through LSTM Cell for a single time step
        hx, cx = lstm_cell(x, (hx, cx))

        # Dummy loss for backward pass
        loss = hx.sum() + cx.sum()
        print(f'Step {step + 1}, Loss: {loss.item()}')

        # Backward pass
        loss.backward()

        # Update weights
        optimizer.step()

    print("Backward pass and weight update for LSTMCell completed successfully.")


def tmp_lstm():
    import torch
    import torch.nn as nn
    import torch.optim as optim

    # Define the LSTM
    input_size = 10
    hidden_size = 20
    num_layers = 2
    lstm = nn.LSTM(input_size, hidden_size, num_layers)

    # Define a simple optimizer
    optimizer = optim.Adam(lstm.parameters(), lr=0.01)

    # Training loop for 10 steps
    num_steps = 10
    batch_size = 5
    seq_len = 6
    x = torch.randn(seq_len, batch_size, input_size)  # Input tensor

    for step in range(num_steps):
        # Initialize hidden and cell states
        h0 = torch.randn(num_layers, batch_size, hidden_size)  # Hidden state
        c0 = torch.randn(num_layers, batch_size, hidden_size)  # Cell state

        optimizer.zero_grad()

        # Forward pass through LSTM
        output, (hn, cn) = lstm(x, (h0, c0))

        # Dummy loss for backward pass
        loss = output.sum()
        print(f'Step {step + 1}, Loss: {loss.item()}')

        # Backward pass
        loss.backward()

        # Update weights
        optimizer.step()

    print("Backward pass and weight update for LSTM completed successfully.")



if __name__ == '__main__':
    parser = argparse.ArgumentParser("Training model")
    parser.add_argument("--config", default="./conf/stabilzation_train.yaml", help="Config file.")
    parser.add_argument("--flo_model", default="raft_8x2_100k_mixed_368x768")
    args = parser.parse_args()
    train(args = args)
    # tmp_lstm_cell()
    # tmp_lstm()
