import argparse
import torch
from logger import utils
from .utils import get_data_loaders
import accelerate
from tools.infer_tools import DiffusionSVC
from tools.tools import StepLRWithWarmUp

def parse_args(args=None, namespace=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", type=str, default="configs/config.yaml")
    parser.add_argument("-m", "--model", type=str, default="exp/diffusion/model_540000.pt")
    return parser.parse_args(args=args, namespace=namespace)

if __name__ == '__main__':
    cmd = parse_args()
    
    args = utils.load_config(cmd.config)
    accelerator = accelerate.Accelerator(gradient_accumulation_steps=args.model.text2semantic.train.gradient_accumulation_steps)
    device = accelerator.device
    
    if args.model.text2semantic.type == "roformer":
        from text2semantic.roformer.train import train
        from text2semantic.roformer.roformer import get_model
    else:
        raise ValueError(f"[x] Unknown Model: {args.model.text2semantic.type}")
    
    model = get_model(**args.model.text2semantic)
    
    if args.model.text2semantic.train.generate_audio and accelerator.is_main_process:
        diffusion_model = DiffusionSVC(device=device)
        diffusion_model.load_model(model_path=cmd.model, f0_model="fcpe", f0_max=800, f0_min=65)
    else:
        diffusion_model = None
    
    optimizer = torch.optim.AdamW(model.parameters())
    initial_global_step, model, optimizer = utils.load_model(args.model.text2semantic.train.expdir, model, optimizer, device=device)
    for param_group in optimizer.param_groups:
        param_group['initial_lr'] = args.model.text2semantic.train.lr
        param_group['lr'] = args.model.text2semantic.train.lr * args.model.text2semantic.train.gamma ** max((initial_global_step - 2) // args.model.text2semantic.train.decay_step, 0)
        param_group['weight_decay'] = args.train.weight_decay
    scheduler = StepLRWithWarmUp(optimizer, step_size=args.model.text2semantic.train.decay_step, gamma=args.model.text2semantic.train.gamma, last_epoch=initial_global_step-2, warm_up_steps=args.model.text2semantic.train.warm_up_steps, start_lr=float(args.model.text2semantic.train.start_lr))
    
    model = model.to(device)
    
    for state in optimizer.state.values():
        for k, v in state.items():
            if torch.is_tensor(v):
                state[k] = v.to(device)
                    
    loader_train, loader_valid = get_data_loaders(args,model = model, accelerate=accelerator)
    _, model, optimizer, scheduler = accelerator.prepare(loader_train, model, optimizer, scheduler)

    train(args, initial_global_step, model, optimizer, scheduler, diffusion_model, loader_train, loader_valid, accelerator)
    
