import gc
import queue
import torch
import numpy as np
from pathlib import Path

from loguru import logger

from fish_speech.i18n import i18n
from tools.schema import ServeTTSRequest
from tools.webui import build_html_error_message
from fish_speech.utils import autocast_exclude_mps, set_seed
from fish_speech.text.chn_text_norm.text import Text as ChnNormedText
from tools.file import AUDIO_EXTENSIONS, audio_to_bytes, list_files, read_ref_text
from tools.api import decode_vq_tokens, encode_reference, list_files, read_ref_text

from tools.llama.generate import (
    GenerateRequest,
    GenerateResponse,
    WrappedGenerateResponse,
)


@torch.inference_mode()
def inference(req: ServeTTSRequest):

    idstr: str | None = req.reference_id
    prompt_tokens, prompt_texts = [], []
    if idstr is not None:
        ref_folder = Path("references") / idstr
        ref_folder.mkdir(parents=True, exist_ok=True)
        ref_audios = list_files(
            ref_folder, AUDIO_EXTENSIONS, recursive=True, sort=False
        )

        if req.use_memory_cache == "never" or (
            req.use_memory_cache == "on-demand" and len(prompt_tokens) == 0
        ):
            prompt_tokens = [
                encode_reference(
                    decoder_model=req.decoder_model,
                    reference_audio=audio_to_bytes(str(ref_audio)),
                    enable_reference_audio=True,
                )
                for ref_audio in ref_audios
            ]
            prompt_texts = [
                read_ref_text(str(ref_audio.with_suffix(".lab")))
                for ref_audio in ref_audios
            ]
        else:
            logger.info("Use same references")

    else:
        # Parse reference audio aka prompt
        refs = req.references

        if req.use_memory_cache == "never" or (
            req.use_memory_cache == "on-demand" and len(prompt_tokens) == 0
        ):
            prompt_tokens = [
                encode_reference(
                    decoder_model=req.decoder_model,
                    reference_audio=ref.audio,
                    enable_reference_audio=True,
                )
                for ref in refs
            ]
            prompt_texts = [ref.text for ref in refs]
        else:
            logger.info("Use same references")

    if req.seed is not None:
        set_seed(req.seed)
        logger.warning(f"set seed: {req.seed}")

    # LLAMA Inference
    request = dict(
        device=req.decoder_model.device,
        max_new_tokens=req.max_new_tokens,
        text=(
            req.text
            if not req.normalize
            else ChnNormedText(raw_text=req.text).normalize()
        ),
        top_p=req.top_p,
        repetition_penalty=req.repetition_penalty,
        temperature=req.temperature,
        compile=req.compile,
        iterative_prompt=req.chunk_length > 0,
        chunk_length=req.chunk_length,
        max_length=4096,
        prompt_tokens=prompt_tokens,
        prompt_text=prompt_texts,
    )

    response_queue = queue.Queue()
    req.llama_queue.put(
        GenerateRequest(
            request=request,
            response_queue=response_queue,
        )
    )

    segments = []

    while True:
        wrapped_result: WrappedGenerateResponse = response_queue.get()
        if wrapped_result.status == "error":
            yield None, None, build_html_error_message(wrapped_result.response)
            break

        if not isinstance(wrapped_result.response, GenerateResponse):
            raise TypeError("Expected GenerateResponse, got {type(wrapped_result.response).__name__}")
        result: GenerateResponse = wrapped_result.response
        if result.action == "next":
            break

        with autocast_exclude_mps(
            device_type=req.decoder_model.device.type, dtype=req.precision
        ):
            fake_audios = decode_vq_tokens(
                decoder_model=req.decoder_model,
                codes=result.codes,
            )

        fake_audios = fake_audios.float().cpu().numpy()
        segments.append(fake_audios)

    if len(segments) == 0:
        return (
            None,
            None,
            build_html_error_message(
                i18n("No audio generated, please check the input text.")
            ),
        )

    # No matter streaming or not, we need to return the final audio
    audio = np.concatenate(segments, axis=0)
    yield None, (req.decoder_model.spec_transform.sample_rate, audio), None

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        gc.collect()