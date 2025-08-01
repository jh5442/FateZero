import os
from glob import glob
import copy
from typing import Optional,Dict
from tqdm.auto import tqdm
from omegaconf import OmegaConf
import click

import torch
import torch.utils.data
import torch.utils.checkpoint

from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from diffusers import (
    AutoencoderKL,
    DDIMScheduler,
)
from diffusers.utils.import_utils import is_xformers_available
from transformers import AutoTokenizer, CLIPTextModel
from einops import rearrange

from video_diffusion.models.unet_3d_condition import UNetPseudo3DConditionModel
from video_diffusion.data.dataset import ImageSequenceDataset
from video_diffusion.common.util import get_time_string, get_function_args
from video_diffusion.common.logger import get_logger_config_path
from video_diffusion.common.image_util import log_train_samples
from video_diffusion.common.instantiate_from_config import instantiate_from_config
from video_diffusion.pipelines.p2p_validation_loop import P2pSampleLogger

# logger = get_logger(__name__)


def collate_fn(examples):
    """Concat a batch of sampled image in dataloader
    """
    batch = {
        "prompt_ids": torch.cat([example["prompt_ids"] for example in examples], dim=0),
        "images": torch.stack([example["images"] for example in examples]),
    }
    return batch



def test(
    config: str,
    pretrained_model_path: str,
    dataset_config: Dict,
    logdir: str = None,
    editing_config: Optional[Dict] = None,
    test_pipeline_config: Optional[Dict] = None,
    gradient_accumulation_steps: int = 1,
    seed: Optional[int] = None,
    mixed_precision: Optional[str] = "fp16",
    batch_size: int = 1,
    model_config: dict={},
    verbose: bool=True,
    **kwargs

):
    args = get_function_args()

    time_string = get_time_string()
    if logdir is None:
        logdir = config.replace('config', 'result').replace('.yml', '').replace('.yaml', '')
    logdir += f"_{time_string}"

    accelerator = Accelerator(
        gradient_accumulation_steps=gradient_accumulation_steps,
        mixed_precision=mixed_precision,
    )
    if accelerator.is_main_process:
        os.makedirs(logdir, exist_ok=True)
        OmegaConf.save(args, os.path.join(logdir, "config.yml"))
    logger = get_logger_config_path(logdir)

    if seed is not None:
        set_seed(seed)

    # Load the tokenizer
    tokenizer = AutoTokenizer.from_pretrained(
        pretrained_model_path,
        subfolder="tokenizer",
        use_fast=False,
    )

    # Load models and create wrapper for stable diffusion
    text_encoder = CLIPTextModel.from_pretrained(
        pretrained_model_path,
        subfolder="text_encoder",
    )

    vae = AutoencoderKL.from_pretrained(
        pretrained_model_path,
        subfolder="vae",
    )

    unet = UNetPseudo3DConditionModel.from_2d_model(
        os.path.join(pretrained_model_path, "unet"), model_config=model_config
    )

    if 'target' not in test_pipeline_config:
        test_pipeline_config['target'] = 'video_diffusion.pipelines.stable_diffusion.SpatioTemporalStableDiffusionPipeline'
   
    pipeline = instantiate_from_config(
        test_pipeline_config,
        vae=vae,
        text_encoder=text_encoder,
        tokenizer=tokenizer,
        unet=unet,
        scheduler=DDIMScheduler.from_pretrained(
            pretrained_model_path,
            subfolder="scheduler",
        ),
        disk_store=kwargs.get('disk_store', False)
    )
    pipeline.scheduler.set_timesteps(editing_config['num_inference_steps'])
    pipeline.set_progress_bar_config(disable=True)
    pipeline.print_pipeline(logger)

    if is_xformers_available():
        try:
            pipeline.enable_xformers_memory_efficient_attention()
        except Exception as e:
            logger.warning(
                "Could not enable memory efficient attention. Make sure xformers is installed"
                f" correctly and a GPU is available: {e}"
            )

    vae.requires_grad_(False)
    unet.requires_grad_(False)
    text_encoder.requires_grad_(False)
    prompt_ids = tokenizer(
        dataset_config["prompt"],
        truncation=True,
        padding="max_length",
        max_length=tokenizer.model_max_length,
        return_tensors="pt",
    ).input_ids
    video_dataset = ImageSequenceDataset(**dataset_config, prompt_ids=prompt_ids)

    train_dataloader = torch.utils.data.DataLoader(
        video_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=4,
        collate_fn=collate_fn,
    )
    train_sample_save_path = os.path.join(logdir, "train_samples.gif")
    log_train_samples(save_path=train_sample_save_path, train_dataloader=train_dataloader)

    unet, train_dataloader  = accelerator.prepare(
        unet, train_dataloader
    )

    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
        print('use fp16')
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    # Move text_encode and vae to gpu.
    # For mixed precision training we cast the text_encoder and vae weights to half-precision
    # These models are only used for inference, keeping weights in full precision is not required.
    vae.to(accelerator.device, dtype=weight_dtype)
    text_encoder.to(accelerator.device, dtype=weight_dtype)


    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        accelerator.init_trackers("video")  # , config=vars(args))
    logger.info("***** wait to fix the logger path *****")

    if editing_config is not None and accelerator.is_main_process:
        validation_sample_logger = P2pSampleLogger(**editing_config, logdir=logdir, source_prompt=dataset_config['prompt'])
        # validation_sample_logger.log_sample_images(
        #     pipeline=pipeline,
        #     device=accelerator.device,
        #     step=0,
        # )
    def make_data_yielder(dataloader):
        while True:
            for batch in dataloader:
                yield batch
            accelerator.wait_for_everyone()

    train_data_yielder = make_data_yielder(train_dataloader)

    
    batch = next(train_data_yielder)
    if editing_config.get('use_invertion_latents', False):
        # Precompute the latents for this video to align the initial latents in training and test
        assert batch["images"].shape[0] == 1, "Only support, overfiting on a single video"
        # we only inference for latents, no training
        vae.eval()
        text_encoder.eval()
        unet.eval()

        text_embeddings = pipeline._encode_prompt(
                dataset_config.prompt,
                device = accelerator.device,
                num_images_per_prompt = 1,
                do_classifier_free_guidance = True,
                negative_prompt=None
        )
       
        use_inversion_attention =  editing_config.get('use_inversion_attention', False)
        batch['latents_all_step'] = pipeline.prepare_latents_ddim_inverted(
            rearrange(batch["images"].to(dtype=weight_dtype), "b c f h w -> (b f) c h w"),
            batch_size = 1,
            num_images_per_prompt = 1,  # not sure how to use it
            text_embeddings = text_embeddings,
            prompt = dataset_config.prompt,
            store_attention=use_inversion_attention,
            LOW_RESOURCE = True, # not classifier-free guidance
            save_path = logdir if verbose else None
            )

        batch['ddim_init_latents'] = batch['latents_all_step'][-1]

    else:
        batch['ddim_init_latents'] = None

    vae.eval()
    text_encoder.eval()
    unet.eval()

    # with accelerator.accumulate(unet):
    # Convert images to latent space
    images = batch["images"].to(dtype=weight_dtype)
    images = rearrange(images, "b c f h w -> (b f) c h w")

    if accelerator.is_main_process:

        if validation_sample_logger is not None:
            unet.eval()
            validation_sample_logger.log_sample_images(
                image=images, # torch.Size([8, 3, 512, 512])
                pipeline=pipeline,
                device=accelerator.device,
                step=0,
                latents = batch['ddim_init_latents'],
                save_dir = logdir if verbose else None
            )
        # accelerator.log(logs, step=step)

    accelerator.end_training()


@click.command()
@click.option("--config", type=str, default="config/sample.yml")
def run(config):
    Omegadict = OmegaConf.load(config)
    if 'unet' in os.listdir(Omegadict['pretrained_model_path']):
        test(config=config, **Omegadict)
    else:
        # Go through all ckpt if possible
        checkpoint_list = sorted(glob(os.path.join(Omegadict['pretrained_model_path'], 'checkpoint_*')))
        print('checkpoint to evaluate:')
        for checkpoint in checkpoint_list:
            epoch = checkpoint.split('_')[-1]

        for checkpoint in tqdm(checkpoint_list):
            epoch = checkpoint.split('_')[-1]
            if 'pretrained_epoch_list' not in Omegadict or int(epoch) in Omegadict['pretrained_epoch_list']:
                print(f'Evaluate {checkpoint}')
                # Update saving dir and ckpt
                Omegadict_checkpoint = copy.deepcopy(Omegadict)
                Omegadict_checkpoint['pretrained_model_path'] = checkpoint

                if 'logdir' not in Omegadict_checkpoint:
                    logdir = config.replace('config', 'result').replace('.yml', '').replace('.yaml', '')
                    logdir +=  f"/{os.path.basename(checkpoint)}"

                Omegadict_checkpoint['logdir'] = logdir
                print(f'Saving at {logdir}')

                test(config=config, **Omegadict_checkpoint)


if __name__ == "__main__":
    run()
