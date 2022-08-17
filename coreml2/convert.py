# Copyright 2022 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
from typing import TYPE_CHECKING, List, Optional, Tuple, Union, Mapping, Any

import coremltools as ct
from coremltools.converters.mil import register_torch_op
from coremltools.converters.mil.frontend.torch.torch_op_registry import _TORCH_OPS_REGISTRY

import numpy as np

#TODO: if integrating this into transformers, replace imports with ..
from transformers.utils import (
    is_torch_available,
    is_tf_available,
    logging,
)
from .config import CoreMLConfig


if is_torch_available():
    from transformers.modeling_utils import PreTrainedModel

if is_tf_available():
    from transformers.modeling_tf_utils import TFPreTrainedModel

if TYPE_CHECKING:
    from transformers.feature_extraction_utils import FeatureExtractionMixin
    from transformers.processing_utils import ProcessorMixin
    from transformers.tokenization_utils import PreTrainedTokenizer


logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


def get_output_names(spec):
    """Return a list of all output names in the Core ML model."""
    outputs = []
    for out in spec.description.output:
        outputs.append(out.name)
    return outputs


def get_output_named(spec, name):
    """Return the output node with the given name in the Core ML model."""
    for out in spec.description.output:
        if out.name == name:
            return out
    return None


def set_multiarray_shape(node, shape):
    """Change the shape of the specified input or output in the Core ML model."""
    del node.type.multiArrayType.shape[:]
    for x in shape:
        node.type.multiArrayType.shape.append(x)


def get_labels_as_list(model):
    """Return the labels of a classifier model as a sorted list."""
    labels = []
    for i in range(len(model.config.id2label)):
        if i in model.config.id2label.keys():
            labels.append(model.config.id2label[i])
    return labels


def _is_image_std_same(preprocessor: "FeatureExtractionMixin") -> bool:
    return preprocessor.image_std[0] == preprocessor.image_std[1] == preprocessor.image_std[2]


def _get_input_types(
    preprocessor: Union["PreTrainedTokenizer", "FeatureExtractionMixin", "ProcessorMixin"],
    config: CoreMLConfig,
    dummy_inputs: Mapping[str, np.ndarray],
) -> List[Union[ct.ImageType, ct.TensorType]]:
    """
    Create the ct.InputType objects that describe the inputs to the Core ML model

    Args:
        preprocessor ([`PreTrainedTokenizer`], [`FeatureExtractionMixin`] or [`ProcessorMixin`]):
            The preprocessor used for encoding the data.
        config ([`~coreml.config.CoreMLConfig`]):
            The Core ML configuration associated with the exported model.
        dummy_inputs (`Mapping[str, np.ndarray]`):
            The dummy input tensors that describe the expected shapes of the inputs.

    Returns:
        `List[Union[ct.ImageType, ct.TensorType]]`: ordered list of input types
    """
    input_descs = config.inputs
    input_types = []

    if config.modality == "text":
        # input_ids
        input_desc = input_descs.pop(0)
        input_types.append(
            ct.TensorType(name=input_desc.name, shape=dummy_inputs[input_desc.name].shape, dtype=np.int32)
        )

        # attention_mask
        if len(input_descs) > 0:
            input_desc = input_descs.pop(0)
            input_types.append(
                ct.TensorType(name=input_desc.name, shape=dummy_inputs[input_desc.name].shape, dtype=np.int32)
            )
        else:
            logger.info("Skipping attention_mask input")

        # token_type_ids
        if len(input_descs) > 0:
            input_desc = input_descs.pop(0)
            input_types.append(
                ct.TensorType(name=input_desc.name, shape=dummy_inputs[input_desc.name].shape, dtype=np.int32)
            )
        else:
            logger.info("Skipping token_type_ids input")

        # TODO: could do the above in a loop! (first fix input/output definitions structure)

    if config.modality == "vision":
        if hasattr(preprocessor, "image_mean"):
            bias = [
                -preprocessor.image_mean[0],
                -preprocessor.image_mean[1],
                -preprocessor.image_mean[2],
            ]
        else:
            bias = [ 0.0, 0.0, 0.0 ]

        # If the stddev values are all equal, they can be folded into bias and
        # scale. If not, Wrapper will insert an additional division operation.
        if hasattr(preprocessor, "image_std") and _is_image_std_same(preprocessor):
            bias[0] /= preprocessor.image_std[0]
            bias[1] /= preprocessor.image_std[1]
            bias[2] /= preprocessor.image_std[2]
            scale = 1.0 / (preprocessor.image_std[0] * 255.0)
        else:
            scale = 1.0 / 255

        # image
        input_desc = input_descs.pop(0)
        input_types.append(
            ct.ImageType(
                name=input_desc.name,
                shape=dummy_inputs[input_desc.name].shape,
                scale=scale,
                bias=bias,
                color_layout=input_desc.color_layout or "RGB",
                channel_first=True,
            )
        )

        if config.task == "masked-im":
            # bool_masked_pos
            input_desc = input_descs.pop(0)
            input_types.append(
                ct.TensorType(
                    name=input_desc.name,
                    shape=dummy_inputs[input_desc.name].shape,
                    dtype=np.int32
                )
            )

    return input_types


if is_torch_available():
    import torch

    class Wrapper(torch.nn.Module):
        def __init__(self, preprocessor, model, config):
            super().__init__()
            self.preprocessor = preprocessor
            self.model = model.eval()
            self.config = config

        def forward(self, inputs, extra_input1=None, extra_input2=None):
            output_defs = self.config.outputs

            # Core ML's image preprocessing does not allow a different scaling
            # factor for each color channel, so do this manually.
            if hasattr(self.preprocessor, "image_std") and not _is_image_std_same(self.preprocessor):
                image_std = torch.tensor(self.preprocessor.image_std).reshape(1, -1, 1, 1)
                inputs = inputs / image_std

            model_kwargs = {
                #"output_attentions": False,
                #"output_hidden_states": False,
                "return_dict": False,
            }

            if self.config.modality == "text":
                if extra_input2 is not None:
                    model_kwargs["token_type_ids"] = extra_input2
                if extra_input1 is not None:
                    model_kwargs["attention_mask"] = extra_input1
            elif self.config.modality == "vision":
                if self.config.task == "masked-im":
                    model_kwargs["bool_masked_pos"] = extra_input1

            outputs = self.model(inputs, **model_kwargs)

            if self.config.task == "image-classification":
                return torch.nn.functional.softmax(outputs[0], dim=1)  # logits

            if self.config.task == "masked-im":
                # Some models also return loss even if no labels provided (e.g. ViT)
                # so need to skip that if it's present.
                return outputs[1] if len(outputs) >= 2 else outputs[0]  # logits

            if self.config.task in [
                "masked-lm",
                "multiple-choice",
                "next-sentence-prediction",
                "sequence-classification",
                "token-classification"
            ]:
                return torch.nn.functional.softmax(outputs[0], dim=-1)  # logits

            if self.config.task == "object-detection":
                return outputs[0], outputs[1]  # logits, pred_boxes

            if self.config.task == "question-answering":
                start_scores = torch.nn.functional.softmax(outputs[0], dim=-1)  # start_logits
                end_scores   = torch.nn.functional.softmax(outputs[1], dim=-1)  # end_logits
                return start_scores, end_scores

            if self.config.task == "semantic-segmentation":
                x = outputs[0]  # logits

                _, output_config = output_defs.popitem(last=False)
                if output_config.get("do_upsample", False):
                    x = torch.nn.functional.interpolate(x, size=inputs.shape[-2:], mode="bilinear", align_corners=False)
                if output_config.get("do_argmax", False):
                    x = x.argmax(1)
                return x

            if self.config.task == "default":
                if len(output_defs) > 1 and len(outputs) > 1:
                    return outputs[0], outputs[1]  # last_hidden_state, pooler_output
                else:
                    return outputs[0]  # last_hidden_state

            raise AssertionError(f"Cannot compute outputs for unknown task '{self.config.task}'")


def export_pytorch(
    preprocessor: Union["PreTrainedTokenizer", "FeatureExtractionMixin", "ProcessorMixin"],
    model: "PreTrainedModel",
    config: CoreMLConfig,
    quantize: str = "float32",
    legacy: bool = False,
    compute_units: ct.ComputeUnit = ct.ComputeUnit.ALL,
) -> ct.models.MLModel:
    """
    Export a PyTorch model to Core ML format

    Args:
        preprocessor ([`PreTrainedTokenizer`], [`FeatureExtractionMixin`] or [`ProcessorMixin`]):
            The preprocessor used for encoding the data.
        model ([`PreTrainedModel`]):
            The model to export.
        config ([`~coreml.config.CoreMLConfig`]):
            The Core ML configuration associated with the exported model.
        quantize (`str`, *optional*, defaults to `"float32"`):
            Quantization options. Possible values: `"float32"`, `"float16"`.
        legacy (`bool`, *optional*, defaults to `False`):
            If `True`, the converter will produce a model in the older NeuralNetwork format.
            By default, the ML Program format will be used.
        compute_units (`ct.ComputeUnit`, *optional*, defaults to `ct.ComputeUnit.ALL`):
            Whether to optimize the model for CPU, GPU, and/or Neural Engine.

    Returns:
        `ct.models.MLModel`: the Core ML model object
    """
    if not issubclass(type(model), PreTrainedModel):
        raise ValueError(f"Cannot convert unknown model type: {type(model)}")

    logger.info(f"Using framework PyTorch: {torch.__version__}")

    # Check if we need to override certain configuration items
    if config.values_override is not None:
        logger.info(f"Overriding {len(config.values_override)} configuration item(s)")
        for override_config_key, override_config_value in config.values_override.items():
            logger.info(f"\t- {override_config_key} -> {override_config_value}")
            setattr(model.config, override_config_key, override_config_value)

    # Create dummy input data for doing the JIT trace.
    dummy_inputs = config.generate_dummy_inputs(preprocessor)

    # Convert to Torch tensors, using inputs in order from the config.
    input_names = [input_desc.name for input_desc in config.inputs]
    example_input = [torch.tensor(dummy_inputs[name]) for name in input_names]

    wrapper = Wrapper(preprocessor, model, config).eval()

    # Running the model once with gradients disabled prevents an error during JIT tracing
    # that happens with certain models such as LeViT. The error message is: "Cannot insert
    # a Tensor that requires grad as a constant."
    with torch.no_grad():
        dummy_output = wrapper(*example_input)

    traced_model = torch.jit.trace(wrapper, example_input, strict=True)

    # Run the traced PyTorch model to get the shapes of the output tensors.
    with torch.no_grad():
        example_output = traced_model(*example_input)

    if isinstance(example_output, (tuple, list)):
        example_output = [x.numpy() for x in example_output]
    else:
        example_output = [example_output.numpy()]

    convert_kwargs = { }
    if not legacy:
        convert_kwargs["compute_precision"] = ct.precision.FLOAT16 if quantize == "float16" else ct.precision.FLOAT32

    # For classification models, add the labels into the Core ML model and
    # designate it as the special `classifier` model type.
    if config.task in ["image-classification", "multiple-choice", "sequence-classification"]:
        class_labels = [model.config.id2label[x] for x in range(model.config.num_labels)]
    elif config.task == "next-sentence-prediction":
        class_labels = ["true", "false"]
    else:
        class_labels = None

    if class_labels is not None:
        classifier_config = ct.ClassifierConfig(class_labels)
        convert_kwargs['classifier_config'] = classifier_config

    input_tensors = _get_input_types(preprocessor, config, dummy_inputs)

    patched_ops = config.patch_pytorch_ops()
    restore_ops = {}
    if patched_ops is not None:
        for name, func in patched_ops.items():
            logger.info(f"Patching PyTorch conversion '{name}' with {func}")
            if name in _TORCH_OPS_REGISTRY:
                restore_ops[name] = _TORCH_OPS_REGISTRY[name]
                del _TORCH_OPS_REGISTRY[name]
            _TORCH_OPS_REGISTRY[name] = func

    mlmodel = ct.convert(
        traced_model,
        inputs=input_tensors,
        convert_to="neuralnetwork" if legacy else "mlprogram",
        compute_units=compute_units,
        **convert_kwargs,
    )

    if restore_ops is not None:
        for name, func in restore_ops.items():
            if func is not None:
                logger.info(f"Restoring PyTorch conversion op '{name}' to {func}")
                _TORCH_OPS_REGISTRY[name] = func

    spec = mlmodel._spec

    for input_desc in config.inputs:
        mlmodel.input_description[input_desc.name] = input_desc.description

    user_defined_metadata = {}
    if model.config.transformers_version:
        user_defined_metadata["transformers_version"] = model.config.transformers_version

    output_defs = config.outputs

    if config.task in [
        "image-classification",
        "multiple-choice",
        "next-sentence-prediction",
        "sequence-classification"
    ]:
        output_name, output_config = output_defs.popitem(last=False)
        ct.utils.rename_feature(spec, spec.description.predictedProbabilitiesName, output_name)
        spec.description.predictedProbabilitiesName = output_name
        mlmodel.output_description[output_name] = output_config["description"]

        output_name, output_config = output_defs.popitem(last=False)
        ct.utils.rename_feature(spec, spec.description.predictedFeatureName, output_name)
        spec.description.predictedFeatureName = output_name
        mlmodel.output_description[output_name] = output_config["description"]
    else:
        for i, (output_name, output_config) in enumerate(output_defs.items()):
            if i < len(example_output):
                output = spec.description.output[i]
                ct.utils.rename_feature(spec, output.name, output_name)
                mlmodel.output_description[output_name] = output_config["description"]
                set_multiarray_shape(output, example_output[i].shape)

        if config.task in ["object-detection", "semantic-segmentation", "token-classification"]:
            labels = get_labels_as_list(model)
            user_defined_metadata["classes"] = ",".join(labels)

        if config.task == "semantic-segmentation":
            # Make the model available in Xcode's previewer.
            mlmodel.user_defined_metadata["com.apple.coreml.model.preview.type"] = "imageSegmenter"
            mlmodel.user_defined_metadata["com.apple.coreml.model.preview.params"] = json.dumps({"labels": labels})

    if len(user_defined_metadata) > 0:
        spec.description.metadata.userDefined.update(user_defined_metadata)

    # Reload the model in case any input / output names were changed.
    mlmodel = ct.models.MLModel(mlmodel._spec, weights_dir=mlmodel.weights_dir)

    if legacy and quantize == "float16":
        mlmodel = ct.models.neural_network.quantization_utils.quantize_weights(mlmodel, nbits=16)

    return mlmodel


def export_tensorflow(
    preprocessor: Union["PreTrainedTokenizer", "FeatureExtractionMixin"],
    model: "TFPreTrainedModel",
    config: CoreMLConfig,
    quantize: str = "float32",
    legacy: bool = False,
    compute_units: ct.ComputeUnit = ct.ComputeUnit.ALL,
) -> ct.models.MLModel:
    """
    Export a TensorFlow model to Core ML format

    Args:
        preprocessor ([`PreTrainedTokenizer`] or [`FeatureExtractionMixin`]):
            The preprocessor used for encoding the data.
        model ([`TFPreTrainedModel`]):
            The model to export.
        config ([`~coreml.config.CoreMLConfig`]):
            The Core ML configuration associated with the exported model.
        quantize (`str`, *optional*, defaults to `"float32"`):
            Quantization options. Possible values: `"float32"`, `"float16"`.
        legacy (`bool`, *optional*, defaults to `False`):
            If `True`, the converter will produce a model in the older NeuralNetwork format.
            By default, the ML Program format will be used.
        compute_units (`ct.ComputeUnit`, *optional*, defaults to `ct.ComputeUnit.ALL`):
            Whether to optimize the model for CPU, GPU, and/or Neural Engine.

    Returns:
        `ct.models.MLModel`: the Core ML model object
    """
    raise AssertionError(f"Core ML export does not currently support TensorFlow models")


def export(
    preprocessor: Union["PreTrainedTokenizer", "FeatureExtractionMixin", "ProcessorMixin"],
    model: Union["PreTrainedModel", "TFPreTrainedModel"],
    config: CoreMLConfig,
    quantize: str = "float32",
    legacy: bool = False,
    compute_units: ct.ComputeUnit = ct.ComputeUnit.ALL,
) -> ct.models.MLModel:
    """
    Export a Pytorch or TensorFlow model to Core ML format

    Args:
        preprocessor ([`PreTrainedTokenizer`], [`FeatureExtractionMixin`] or [`ProcessorMixin`]):
            The preprocessor used for encoding the data.
        model ([`PreTrainedModel`] or [`TFPreTrainedModel`]):
            The model to export.
        config ([`~coreml.config.CoreMLConfig`]):
            The Core ML configuration associated with the exported model.
        quantize (`str`, *optional*, defaults to `"float32"`):
            Quantization options. Possible values: `"float32"`, `"float16"`.
        legacy (`bool`, *optional*, defaults to `False`):
            If `True`, the converter will produce a model in the older NeuralNetwork format.
            By default, the ML Program format will be used.
        compute_units (`ct.ComputeUnit`, *optional*, defaults to `ct.ComputeUnit.ALL`):
            Whether to optimize the model for CPU, GPU, and/or Neural Engine.

    Returns:
        `ct.models.MLModel`: the Core ML model object
    """
    if not (is_torch_available() or is_tf_available()):
        raise ImportError(
            "Cannot convert because neither PyTorch nor TensorFlow are not installed. "
            "Please install torch or tensorflow first."
        )

    if is_torch_available() and issubclass(type(model), PreTrainedModel):
        return export_pytorch(preprocessor, model, config, quantize, legacy, compute_units)
    elif is_tf_available() and issubclass(type(model), TFPreTrainedModel):
        return export_tensorflow(preprocessor, model, config, quantize, legacy, compute_units)
    else:
        raise ValueError(f"Cannot convert unknown model type: {type(model)}")
