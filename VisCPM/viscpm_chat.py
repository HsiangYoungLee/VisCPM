import numpy as np
import torch
from PIL import Image
from cpm_live.tokenizers import CPMBeeTokenizer
from diffusers.pipelines.stable_diffusion import StableDiffusionSafetyChecker
from timm.models import create_model
from torchvision.transforms.functional import to_tensor, to_pil_image
from torchvision.utils import make_grid
from transformers import CLIPImageProcessor
from typing import List

from VisCPM.generation.vllm_bee import VLLMCPMBeeBeamSearch
from VisCPM.models import VLU_CPMBee
from VisCPM.models.cpmbee import CPMBeeConfig, CPMBeeTorch
from VisCPM.utils import utils

torch.set_num_threads(1)


def grid_image(images: List[Image.Image]) -> Image.Image:
    n = len(images)
    nrow = min(n, 8)
    images_tensor = [to_tensor(image) for image in images]
    images_tensor_grid = make_grid(images_tensor, nrow, padding=0)
    images_grid = to_pil_image(images_tensor_grid)
    return images_grid


class VisCPMChat(object):
    def __init__(self, model_path, config_path='./config/cpm-bee-10b.json', image_safety_checker=False) -> None:
        self.transform = utils.build_transform(is_train=False)
        self.tokenizer = CPMBeeTokenizer()

        self.beit3_wrapper = create_model("beit3_large_patch16_224")
        self.config = CPMBeeConfig.from_json_file(config_path)
        self.cpm_model = CPMBeeTorch(self.config)

        self.vlu_cpmbee = VLU_CPMBee(
            llm=self.cpm_model,
            vpm=self.beit3_wrapper,
            vision_dim=self.beit3_wrapper.args.encoder_embed_dim,
            query_num=64,
        )

        self.beam_search = VLLMCPMBeeBeamSearch(
            self.vlu_cpmbee, self.tokenizer, self.transform
        )

        vlu_state_dict = torch.load(model_path, map_location="cpu")
        self.vlu_cpmbee.load_state_dict(vlu_state_dict)
        self.vlu_cpmbee.half().cuda()
        self.vlu_cpmbee.eval()

        if image_safety_checker:
            self.image_safety_checker = StableDiffusionSafetyChecker.from_pretrained(
                "CompVis/stable-diffusion-safety-checker"
            )
            self.feature_extractor = CLIPImageProcessor.from_pretrained(
                "openai/clip-vit-base-patch32"
            )  # Download image_processing_config from huggingface.co and cache.
            self.image_safety_checker.to('cuda')
        else:
            self.image_safety_checker = None
            self.feature_extractor = None

    def chat(self, image, question, context='', vision_hidden_states=None):
        extra_inp_dict = {
            "context": context,
            "question": question,
        }

        images, has_nsfw_concept = self.run_image_safety_checker(
            np.asarray(image), "cuda", torch.float
        )
        if has_nsfw_concept and has_nsfw_concept[0]:
            print("Content is not safe for work.")
            images = grid_image(np.asarray(image))

        res, vision_hidden_states = self.beam_search.generate(
            [image],
            max_inp_length=3000,
            max_length=512,
            extra_inp_dict=extra_inp_dict,
            vision_hidden_states=vision_hidden_states,
            return_vision_hidden_states=True,
            beam_size=3,
            temperature=0.7,
            repetition_penalty=1.1,
            length_penalty=3,
        )

        answer = res[0]["<ans>"]

        context += "User: " + question + "\n"
        context += "AI: " + answer + "\n"
        return answer, context, vision_hidden_states

    def run_image_safety_checker(self, image, device, dtype):
        if self.image_safety_checker is not None:
            image_safety_checker_input = self.feature_extractor(
                image, return_tensors="pt"
            ).to(device)
            image, has_nsfw_concept = self.image_safety_checker(
                images=image,
                clip_input=image_safety_checker_input.pixel_values.to(dtype),
            )
            flagged_images = np.zeros((2, *image.shape[1:]))
            if any(has_nsfw_concept):
                print(
                    "Potential NSFW content was detected in one or more images. A black image will be returned"
                    " instead."
                    f"{'You may look at this images in the `unsafe_images` variable of the output at your own discretion.' if enable_safety_guidance else 'Try again with a different prompt and/or seed.'}"
                )
                for idx, has_nsfw_concept in enumerate(has_nsfw_concept):
                    if has_nsfw_concept:
                        flagged_images[idx] = images[idx]
                        image[idx] = np.zeros(image[idx].shape)  # black image
        else:
            has_nsfw_concept = None
        return image, has_nsfw_concept
