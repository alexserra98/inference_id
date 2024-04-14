import sys
import torch


def get_target_layers_llama(model, n_layer, option="norm1", every=1, world_size=1):
    map_names = dict(
        norm1=".input_layernorm",
        norm2=".post_attention_layernorm",
        res2="",
    )
    suffix = map_names[option]
    names = [name for name, _ in model.named_modules()]

    prefix = "module."
    middle = ""
    if world_size > 1:
        prefix = "_fsdp_wrapped_module."
        if map_names[option] != "":
            middle = "._fsdp_wrapped_module"

    target_layers = {
        i: f"{prefix}model.layers.{i}{middle}{suffix}" for i in range(0, n_layer, every)
    }

    target_layers[n_layer] = f"{prefix}model.norm"
    target_layers[n_layer + 1] = f"{prefix}lm_head"

    for target_layer in target_layers.values():
        assert target_layer in names, (target_layer, names)

    return target_layers


def print_memory_consumed(rank=None):
    torch.cuda.empty_cache()
    allocated = torch.cuda.max_memory_allocated() / 2**30
    reserved = torch.cuda.max_memory_reserved() / 2**30
    if rank is not None and rank == 0:
        print(f"CUDA mem allocated: {allocated} GB")
        print(f"CUDA mem reserved: {reserved} GB")
    else:
        print(f"CUDA mem allocated: {allocated} GB")
        print(f"CUDA mem reserved: {reserved} GB")
    sys.stdout.flush()
