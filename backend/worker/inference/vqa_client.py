import base64
import json
import logging
from typing import Optional

import cv2
import httpx
import numpy as np

from core.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a document data extraction assistant.
Your only job is to extract specific field values from document images.
Rules:
- Return ONLY valid JSON. No explanation, no markdown fences, no preamble.
- If the requested field is not present in the document, return null for that field.
- Do not invent or guess values. Only extract what is explicitly written."""


def numpy_to_base64(image: np.ndarray) -> str:
    """Convert numpy image to base64-encoded JPEG."""
    _, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return base64.b64encode(buffer).decode("utf-8")


def build_vqa_prompt(anchor: dict, layout_summary: str) -> str:
    """Build the VQA prompt with layout context and spatial hints."""
    return f"""Document structural context:
{layout_summary}

Extraction task:
{anchor["prompt"]}

Spatial hint: The field is located near normalized coordinates 
X: {anchor["x_pct"]:.4f}, Y: {anchor["y_pct"]:.4f}
(0,0 = top-left corner, 1,1 = bottom-right corner of the page)

Return ONLY this JSON with no other text:
{{"{anchor["field"]}": "<extracted_value_or_null>"}}"""


async def run_vqa(
    image: np.ndarray,
    anchor: dict,
    layout_summary: str,
) -> Optional[str]:
    """
    Send a full page image + semantic prompt to vLLM and extract field value.
    Rule #2: temperature=0.0 always — deterministic extraction.
    Rule #8: No cropping — full page image sent.
    Rule #10: JSON parse failure = None, not crash.
    """
    b64 = numpy_to_base64(image)
    prompt = build_vqa_prompt(anchor, layout_summary)

    payload = {
        "model": settings.VLLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            },
        ],
        "temperature": 0.0,  # CRITICAL: zero creativity, deterministic extraction
        "max_tokens": 128,
        "top_p": 1.0,
    }

    vllm_url = f"{settings.VLLM_SERVER_URL}/v1/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=float(settings.VLLM_TIMEOUT)) as client:
            response = await client.post(vllm_url, json=payload)
            response.raise_for_status()

        raw = response.json()["choices"][0]["message"]["content"].strip()

        # Strip markdown fences if model ignores instructions
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        logger.info(f"Raw VLM response for '{anchor['field']}': {repr(raw)}")

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                value = parsed.get(anchor["field"])
            else:
                value = str(parsed)
            return value if value not in (None, "", "null", "None") else None
        except json.JSONDecodeError as e:
            logger.warning(f"VQA JSON parse failure for field '{anchor['field']}': {e}")
            
            # Fallback 1: Try to extract a JSON block using RegEx (in case of conversational filler)
            import re
            json_match = re.search(r'\{[^{}]*\}', raw)
            if json_match:
                try:
                    fallback_parsed = json.loads(json_match.group(0))
                    if isinstance(fallback_parsed, dict):
                        value = fallback_parsed.get(anchor["field"])
                        return value if value not in (None, "", "null", "None") else None
                except Exception as inner_e:
                    logger.warning(f"Fallback regex JSON parsing failed: {inner_e}")

            # Fallback 2: If it explicitly returned null/None as plain text
            if raw.lower().strip() in ("null", "none", "", "null."):
                return None
                
            # Fallback 3: Return the raw string directly
            return raw.strip()
    except httpx.HTTPError as e:
        logger.error(f"VQA HTTP error for field '{anchor['field']}': {e}")
        return None
    except Exception as e:
        logger.error(f"VQA unexpected error for field '{anchor['field']}': {e}")
        return None
