#!/usr/bin/env python
# coding=utf-8
import argparse
import logging
import os
import datasets
from accelerate import Accelerator
from accelerate.logging import get_logger

import transformers
import sys
from utils.helpers import (
    get_target_layers_llama,
)
from utils.model_utils import get_model
from utils.dataloader_utils import get_dataloader
from utils.tokenizer_utils import get_tokenizer
from intrinsic_dimension.compute_distances import compute_id
import torch
import os

current_path = os.getcwd()
print(current_path)
sys.stdout.flush()

from dataset_utils.utils import MMLU_Dataset

logger = get_logger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Finetune a transformers model on a causal language modeling task"
    )
    parser.add_argument(
        "--dataset_name",
        type=str,
        default=None,
        help="The name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--dataset_config_name",
        type=str,
        default=None,
        help="The configuration name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--train_file",
        type=str,
        default=None,
        help="A csv or a json file containing the training data.",
    )
    parser.add_argument(
        "--checkpoint_dir",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
        required=False,
    )
    parser.add_argument(
        "--config_name",
        type=str,
        default=None,
        help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--tokenizer_name",
        type=str,
        default=None,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--use_flash_attn",
        action="store_true",
        help="If passed, will use flash attention to train the model.",
    )
    parser.add_argument(
        "--low_cpu_mem_usage",
        action="store_true",
        help=(
            "It is an option to create the model as an empty shell, then only materialize its parameters when the pretrained weights are loaded."
            "If passed, LLM loading time and RAM consumption will be benefited."
        ),
    )
    parser.add_argument(
        "--use_slow_tokenizer",
        action="store_true",
        help="If passed, will use a slow tokenizer (not backed by the 🤗 Tokenizers library).",
    )
    parser.add_argument(
        "--max_seq_len",
        type=int,
        default=None,
        help="The maximum total sequence length (prompt+completion) of each training example.",
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=1,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--out_dir", type=str, default=None, help="Where to store the final model."
    )
    parser.add_argument(
        "--out_filename", type=str, default=None, help="Where to store the final model."
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="A seed for reproducible training."
    )
    parser.add_argument(
        "--preprocessing_num_workers",
        type=int,
        default=None,
        help="The number of processes to use for the preprocessing.",
    )
    parser.add_argument(
        "--logging_steps",
        type=int,
        default=None,
        help="Print every logging_steps samples processed.",
    )
    parser.add_argument(
        "--use_last_token",
        action="store_true",
        help="If passed, ID will be measured on the last token represenation.",
    )
    parser.add_argument(
        "--save_distances",
        action="store_true",
        help="If passed, the distance matrices will be saved",
    )
    parser.add_argument(
        "--remove_duplicates",
        action="store_true",
        help="If passed, duplicate datapoints will be removed",
    )
    parser.add_argument(
        "--target_layer",
        type=str,
        default="norm1",
        help="The name of the layer to analyze.",
    )
    parser.add_argument(
        "--maxk",
        type=int,
        default=50,
        help="max nn order of the stored distence matrices",
    )
    parser.add_argument(
        "--layer_interval",
        type=int,
        default=1,
        help="Extract 1 layer every 'layer interval'.",
    )
    parser.add_argument(
        "--model_name",
        type=str,
        help="model_name.",
    )
    parser.add_argument(
        "--use_text_encoder",
        action="store_true",
        help="If passed, ID will be measured on the last token represenation.",
    )
    args = parser.parse_args([])
    return args


args = parse_args()
# Initialize the accelerator. We will let the accelerator handle device placement for us in this example.
# If we're using tracking, we also need to initialize it here and it will by default pick up all supported trackers
# in the environment

accelerator = Accelerator()
# Make one log on every process with the configuration for debugging.
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    level=logging.INFO,
)
logger.info(accelerator.state, main_process_only=False)
if accelerator.is_local_main_process:
    datasets.utils.logging.set_verbosity_warning()
    transformers.utils.logging.set_verbosity_info()
else:
    datasets.utils.logging.set_verbosity_error()
    transformers.utils.logging.set_verbosity_error()

if accelerator.is_main_process and args.out_dir is not None:
    os.makedirs(args.out_dir, exist_ok=True)

accelerator.wait_for_everyone()
world_size = accelerator.num_processes

# **************************************************************************************
model = get_model(
    model_name_or_path=args.checkpoint_dir,
    precision=torch.bfloat16,
    low_cpu_mem_usage=args.low_cpu_mem_usage,
    use_flash_attn=args.use_flash_attn,
)

tokenizer = get_tokenizer(
    tokenizer_path=args.tokenizer_name, model_path=args.checkpoint_dir
)
max_seq_len = tokenizer.model_max_length
if args.max_seq_len is not None:
    max_seq_len = args.max_seq_len

# useless in this case:
pad_token_id = tokenizer.pad_token_id
n_layer = model.config.num_hidden_layers
print("model loaded. \n\n")
sys.stdout.flush()

dataset = MMLU_Dataset(
    tokenizer=tokenizer,
    max_seq_len=max_seq_len,
    num_few_shots=args.num_few_shots,
    subject=None,
    num_prcesses=args.preprocessing_num_workers,
).construct_dataset()

dataloader = get_dataloader(
    dataset,
    args.batch_size,
    pad_token_id,
    max_seq_len=max_seq_len,
    world_size=world_size,
    shuffle=False,
    num_processes=args.preprocessing_num_workers,
)
# ***********************************************************************

# Prepare everything with `accelerator`.
model = accelerator.prepare(model)

if args.model_name.startswith("llama"):
    target_layers = get_target_layers_llama(
        model=model,
        n_layer=n_layer,
        option=args.target_layer,
        every=args.layer_interval,
    )
elif args.model_name.startswith("mistral"):
    pass

elif args.model_name.startswith("pythia"):
    pass

nsamples = len(dataloader.dataset)
print("num_total_samples", nsamples)
print("terget_layers", target_layers)

compute_id(
    model,
    args.model_name,
    dataloader,
    target_layers,
    nsamples=nsamples,
    use_last_token=args.use_last_token,
    maxk=args.maxk,
    range_scaling=1050,
    dirpath=args.out_dir + f"/{args.model_name}",
    filename=args.out_filename,
    save_distances=args.save_distances,
    remove_duplicates=args.remove_duplicates,
    print_every=args.logging_steps,
)
