# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

"""Implements a Hugging Causal LM wrapped inside a :class:`.ComposerModel`."""

import os
from typing import Mapping

# required for loading a python model into composer
from composer.metrics.nlp import (InContextLearningCodeEvalAccuracy,
                                  InContextLearningLMAccuracy,
                                  InContextLearningLMExpectedCalibrationError,
                                  InContextLearningMCExpectedCalibrationError,
                                  InContextLearningMultipleChoiceAccuracy,
                                  InContextLearningQAAccuracy,
                                  LanguageCrossEntropy, LanguagePerplexity)
from composer.utils import dist
from omegaconf import DictConfig
from transformers import (AutoConfig, AutoModel, AutoModelForCausalLM,
                          PreTrainedTokenizerBase)

from llmfoundry.models.hf.hf_fsdp import hf_get_init_device
from llmfoundry.models.hf.model_wrapper import HuggingFaceModelWithZLoss
from llmfoundry.models.layers.llama_attention_monkeypatch import \
    get_llama_attention_patch_fn
from llmfoundry.models.utils import init_empty_weights

try:
    from peft import LoraConfig, PeftModel, get_peft_model
    _peft_installed = True
    _model_type = PeftModel

except ImportError:
    # raising warnings below only if users try to use PEFT
    _peft_installed = False
    _model_type = None

__all__ = ['ComposerHFCausalLM']


def print_trainable_parameters(model: AutoModel) -> None:
    # Prints the number of trainable parameters in the model.
    if _model_type is None:
        raise ImportError(
            "PEFT not installed. Run pip install -e \".[gpu,peft]\"")
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    print(
        f'trainable params: {trainable_params} || all params: {all_param} || trainable%: {100 * trainable_params / all_param}'
    )


class ComposerHFCausalLM(HuggingFaceModelWithZLoss):
    """Configures a :class:`.HuggingFaceModel` around a Causal LM.

    Args:
        om_model_config (DictConfig): an omegaconf dictionary used to configure the model.
        the following keys are required:
            cfg.pretrained_model_name_or_path (str): The name of or local path to
                the HF Causal LM (e.g., `gpt2` to instantiate a GPT2LMHeadModel).
            cfg.config_overrides (dict, optional): An optional dictionary of keyword
                arguments that override the default configuration associated with
                cfg.pretrained_model_name_or_path.
            cfg.pretrained (bool): Whether to instantiate the model with pre-trained
                weights coming from cfg.pretrained_model_name_or_path. If ``True``,
                cfg.config_overrides must be compatible with the pre-trained weights.
            cfg.init_device ('cpu' | 'meta'): Which device, 'cpu' or 'meta', to
                initialize the model on. Currently, `meta` is only supported when
                cfg.pretrained is ``False``. Default: ``'cpu'``.
        tokenizer (PreTrainedTokenizer): The tokenizer that the model will use.
    """

    def __init__(self, om_model_config: DictConfig,
                 tokenizer: PreTrainedTokenizerBase):

        # set up training and eval metrics
        train_metrics = [
            LanguageCrossEntropy(),
            LanguagePerplexity(),
        ]
        eval_metrics = [
            LanguageCrossEntropy(),
            LanguagePerplexity(),
            InContextLearningLMAccuracy(),
            InContextLearningMultipleChoiceAccuracy(),
            InContextLearningCodeEvalAccuracy(),
            InContextLearningQAAccuracy(),
            InContextLearningLMExpectedCalibrationError(),
            InContextLearningMCExpectedCalibrationError()
        ]

        # load the model config
        trust_remote_code = om_model_config.get('trust_remote_code', True)
        use_auth_token = om_model_config.get('use_auth_token', False)
        config = AutoConfig.from_pretrained(
            om_model_config.pretrained_model_name_or_path,
            trust_remote_code=trust_remote_code,
            use_auth_token=use_auth_token,
        )

        # set config overrides
        for k, v in om_model_config.get('config_overrides', {}).items():
            if not hasattr(config, k):
                raise ValueError(
                    f'config does not have attribute "{k}" to override ({k}: {v}).'
                )

            attr = getattr(config, k)
            if isinstance(attr, Mapping):
                extra_keys = [_k for _k in v.keys() if _k not in attr.keys()]
                if extra_keys:
                    raise ValueError(
                        f'Config dict override got unknown keys. ' +
                        f'Extra keys: {extra_keys}. ' +
                        f'Expected (a subset of) keys: {list(attr.keys())}.')
                getattr(config, k).update(v)
            else:
                setattr(config, k, v)

        # below we set up the device to initialize the model on
        init_device = om_model_config.get('init_device', 'cpu')

        # Get the device we want to initialize, and use the
        # reolved version to initialize the HF model
        resolved_init_device = hf_get_init_device(init_device)

        # We need to have all non-zero local ranks be not-pretrained
        # Rank 0 will still be pretrained, and distribute the weights appropriately
        if dist.get_local_rank() != 0 and init_device == 'mixed':
            om_model_config.pretrained = False

        # initialize the model on the correct device
        if resolved_init_device == 'cpu':
            if om_model_config.pretrained:
                model = AutoModelForCausalLM.from_pretrained(
                    om_model_config.pretrained_model_name_or_path,
                    trust_remote_code=trust_remote_code,
                    use_auth_token=use_auth_token,
                    config=config)
            else:
                model = AutoModelForCausalLM.from_config(
                    config,
                    trust_remote_code=trust_remote_code,
                )
        elif resolved_init_device == 'meta':
            if om_model_config.pretrained:
                raise ValueError(
                    'Setting cfg.pretrained=True is not supported when init_device="meta".'
                )
            with init_empty_weights(include_buffers=False):
                model = AutoModelForCausalLM.from_config(
                    config,
                    trust_remote_code=trust_remote_code,
                )
        else:
            raise ValueError(
                f'init_device="{init_device}" must be either "cpu" or "meta".')

        signal_file_path = '.local_rank0_completed_autoresume'
        if dist.get_local_rank() == 0:
            with open(signal_file_path, 'wb') as f:
                f.write(b'local_rank0_completed_download')

        # Avoid the collective call until the local rank zero has finished trying to download the checkpoint
        # so that we don't timeout for large downloads. This syncs all processes on the node
        with dist.local_rank_zero_download_and_wait(signal_file_path):
            # Then, wait to ensure every node has finished downloading the checkpoint
            dist.barrier()

        if dist.get_local_rank() == 0:
            os.remove(signal_file_path)

        z_loss = om_model_config.get('z_loss', 0.0)

        # if om_model_config includes lora and peft is installed, add lora modules
        lora_cfg = om_model_config.get('lora', None)
        if lora_cfg is not None:
            if _peft_installed == True:
                print('Building Lora config...')
                lora_cfg = LoraConfig(**lora_cfg.args)
                print('Lora config built.')
                print('Adding Lora modules...')
                model = get_peft_model(model, lora_cfg)
                print('Lora modules added.')
                print_trainable_parameters(model)
            else:
                raise ImportError(
                    "cfg.model.lora is given but PEFT not installed. Run pip install -e \".[gpu,peft]\""
                )

        attention_patch_type = om_model_config.get('attention_patch_type', None)
        if attention_patch_type is not None:
            if model.config.model_type != 'llama':
                raise ValueError(
                    f'attention_patch_type is only supported for llama models, but got {model.config.model_type}'
                )

            print(
                f'Patching llama attention with {attention_patch_type} attention'
            )
            from transformers.models.llama.modeling_llama import LlamaAttention
            LlamaAttention.forward = get_llama_attention_patch_fn(
                attention_patch_type)
            model.config.use_cache = False

        composer_model = super().__init__(model=model,
                                          shift_labels=True,
                                          tokenizer=tokenizer,
                                          metrics=train_metrics,
                                          eval_metrics=eval_metrics,
                                          z_loss=z_loss,
                                          init_device=init_device)

        return composer_model
