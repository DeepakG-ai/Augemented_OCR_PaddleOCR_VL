FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
ENV PADDLE_PDX_CACHE_HOME=/opt/paddleocr/pdx_cache
ENV MODELSCOPE_CACHE=/opt/paddleocr/modelscope_cache
ENV PADDLE_HOME=/opt/paddleocr/paddle_home

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential libglib2.0-0 libsm6 libxrender1 libxext6 libgl1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

ARG PADDLEOCR_PREWARM=1
RUN if [ "$PADDLEOCR_PREWARM" = "1" ]; then \
      python -c "import numpy as np; from paddleocr import PaddleOCR; ocr = PaddleOCR(text_detection_model_name='PP-OCRv5_mobile_det', text_recognition_model_name='PP-OCRv5_mobile_rec', use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, device='cpu', enable_mkldnn=True, cpu_threads=4, return_word_box=True); img = np.full((96, 320, 3), 255, dtype=np.uint8); ocr.predict(img); print('PaddleOCR models prewarmed')" ; \
    fi

COPY . /app

EXPOSE 8000
